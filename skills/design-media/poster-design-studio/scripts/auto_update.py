"""Automatic updates for a domestic storefront package.

Imported by mcp_client only when this file sits beside it. Package identity,
hosts, and discovery URLs are read from mcp_client when a function runs, so
importing this module does not cycle and does not copy those constants.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import time
import urllib.parse
import zipfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

GetBytes = Callable[[str, float, int], bytes]

UPDATE_CHECK_MAX_AGE_SECONDS = 24 * 60 * 60
UPDATE_LOCK_MAX_AGE_SECONDS = 10 * 60
UPDATE_DOWNLOAD_TIMEOUT_SECONDS = 60.0
MAX_UPDATE_MANIFEST_BYTES = 1024 * 1024
MAX_UPDATE_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_UPDATE_FILES = 512
MAX_UPDATE_FILE_BYTES = 16 * 1024 * 1024
MAX_UPDATE_TOTAL_BYTES = 64 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _cdn_url(url: str, route: str) -> str:
    import mcp_client as _client
    return _client._with_host(url, _client.ROUTE_HOSTS[route][3])


def _release_urls(discovery: dict[str, Any]) -> tuple[str, str]:
    import mcp_client as _client
    version = discovery.get("version")
    archive = discovery.get("archive")
    manifest = discovery.get("manifest")
    base_url = discovery.get("base_url")
    expected_base = _client.PACKAGE_CDN_BASE_TEMPLATE.format(version=version)
    expected_archive = f"{_client.PACKAGE_SLUG}-skill-{version}.zip"
    if (
        discovery.get("schema_version") != 1
        or discovery.get("package") != _client.PACKAGE_SLUG
        or discovery.get("channel") != _client.PACKAGE_CHANNEL
        or discovery.get("locale") != _client.PACKAGE_LOCALE
        or not isinstance(version, str)
        or archive != expected_archive
        or manifest != "skill-manifest.json"
        or base_url != expected_base
        or not isinstance(discovery.get("archive_sha256"), str)
        or _SHA256.fullmatch(discovery["archive_sha256"]) is None
        or not isinstance(discovery.get("manifest_sha256"), str)
        or _SHA256.fullmatch(discovery["manifest_sha256"]) is None
    ):
        raise RuntimeError("Beatra update discovery is invalid")
    parsed = urllib.parse.urlsplit(expected_base)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {hosts[3] for hosts in _client.ROUTE_HOSTS.values()}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Beatra update discovery is invalid")
    return f"{expected_base}/{archive}", f"{expected_base}/{manifest}"


def check_update(
    *,
    get_bytes: GetBytes | None = None,
    route: str | None = None,
) -> dict[str, Any]:
    import mcp_client as _client
    get_bytes = get_bytes or _client._default_get_bytes
    if not _client._has_version_list():
        version = _client.PACKAGE_VERSION
        return {
            "current_version": version,
            "available_version": version,
            "update_available": False,
            "discovery": {},
        }
    discovery = _client._json_object(
        _client._update_get(
            _client._discovery_url(),
            _client._site_url,
            route=route,
            timeout=_client.UPDATE_DISCOVERY_TIMEOUT_SECONDS,
            limit=_client.MAX_UPDATE_DISCOVERY_BYTES,
            get_bytes=get_bytes,
        ),
        "Beatra update discovery",
    )
    current = _client._semver(_client.PACKAGE_VERSION)
    available = _client._semver(discovery.get("version"))
    _release_urls(discovery)
    if available < current:
        raise RuntimeError("Beatra update discovery attempted a version downgrade")
    return {
        "current_version": _client.PACKAGE_VERSION,
        "available_version": discovery["version"],
        "update_available": available > current,
        "discovery": discovery,
    }


def _manifest_files(
    manifest: dict[str, Any],
    *,
    discovery: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    import mcp_client as _client
    if (
        manifest.get("schema_version") != 1
        or manifest.get("package") != _client.PACKAGE_SLUG
        or manifest.get("channel") != _client.PACKAGE_CHANNEL
        or manifest.get("locale") != _client.PACKAGE_LOCALE
        or manifest.get("version") != discovery["version"]
        or manifest.get("archive") != discovery["archive"]
        or manifest.get("archive_sha256") != discovery["archive_sha256"]
    ):
        raise RuntimeError("Beatra update manifest is invalid")
    listed = manifest.get("files")
    if not isinstance(listed, list) or not listed or len(listed) > MAX_UPDATE_FILES:
        raise RuntimeError("Beatra update manifest is invalid")
    files: dict[str, dict[str, Any]] = {}
    folded: set[str] = set()
    total = 0
    prefix = f"{_client.PACKAGE_SLUG}/"
    for item in listed:
        if not isinstance(item, dict):
            raise RuntimeError("Beatra update manifest is invalid")
        path = item.get("path")
        size = item.get("bytes")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not path.startswith(prefix)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_UPDATE_FILE_BYTES
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise RuntimeError("Beatra update manifest is invalid")
        relative = path[len(prefix) :]
        parts = relative.split("/")
        if (
            not relative
            or "\\" in relative
            or "\x00" in relative
            or any(part in {"", ".", ".."} for part in parts)
            or Path(relative).is_absolute()
        ):
            raise RuntimeError("Beatra update manifest contains an unsafe path")
        collision = relative.casefold()
        if relative in files or collision in folded:
            raise RuntimeError("Beatra update manifest contains duplicate paths")
        total += size
        if total > MAX_UPDATE_TOTAL_BYTES:
            raise RuntimeError("Beatra update manifest exceeded the extraction limit")
        folded.add(collision)
        files[relative] = {"bytes": size, "sha256": digest}
    return files


def _validated_archive(
    archive: bytes,
    *,
    manifest_files: dict[str, dict[str, Any]],
) -> dict[str, bytes]:
    import mcp_client as _client
    extracted: dict[str, bytes] = {}
    folded: set[str] = set()
    prefix = f"{_client.PACKAGE_SLUG}/"
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            members = bundle.infolist()
            if not members or len(members) > MAX_UPDATE_FILES:
                raise RuntimeError("Beatra update archive has an invalid file count")
            for member in members:
                name = member.filename
                mode = member.external_attr >> 16
                if (
                    member.is_dir()
                    or member.flag_bits & 0x1
                    or stat.S_ISLNK(mode)
                    or not name.startswith(prefix)
                    or "\\" in name
                    or "\x00" in name
                ):
                    raise RuntimeError("Beatra update archive contains an unsafe member")
                relative = name[len(prefix) :]
                parts = relative.split("/")
                if (
                    not relative
                    or any(part in {"", ".", ".."} for part in parts)
                    or Path(relative).is_absolute()
                    or relative not in manifest_files
                    or relative.casefold() in folded
                ):
                    raise RuntimeError("Beatra update archive contains an unsafe member")
                expected = manifest_files[relative]
                if member.file_size != expected["bytes"] or member.file_size > MAX_UPDATE_FILE_BYTES:
                    raise RuntimeError("Beatra update archive does not match its manifest")
                content = bundle.read(member)
                if len(content) != expected["bytes"] or _sha256(content) != expected["sha256"]:
                    raise RuntimeError("Beatra update archive does not match its manifest")
                folded.add(relative.casefold())
                extracted[relative] = content
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Beatra update archive is invalid") from exc
    if set(extracted) != set(manifest_files):
        raise RuntimeError("Beatra update archive does not match its manifest")
    return extracted


def download_update(
    discovery: dict[str, Any],
    *,
    get_bytes: GetBytes | None = None,
    route: str | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    import mcp_client as _client
    get_bytes = get_bytes or _client._default_get_bytes
    archive_url, manifest_url = _release_urls(discovery)
    manifest_content = _client._update_get(
        manifest_url,
        _cdn_url,
        route=route,
        timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
        limit=MAX_UPDATE_MANIFEST_BYTES,
        get_bytes=get_bytes,
    )
    if _sha256(manifest_content) != discovery["manifest_sha256"]:
        raise RuntimeError("Beatra update manifest checksum does not match discovery")
    manifest = _client._json_object(manifest_content, "Beatra update manifest")
    manifest_files = _manifest_files(manifest, discovery=discovery)
    archive = _client._update_get(
        archive_url,
        _cdn_url,
        route=route,
        timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
        limit=MAX_UPDATE_ARCHIVE_BYTES,
        get_bytes=get_bytes,
    )
    if _sha256(archive) != discovery["archive_sha256"]:
        raise RuntimeError("Beatra update archive checksum does not match discovery")
    return manifest, _validated_archive(archive, manifest_files=manifest_files)


def set_auto_update(
    enabled: bool,
    *,
    state_dir: Path | None = None,
    install_root: Path | None = None,
) -> None:
    import mcp_client as _client
    resolved_state = state_dir or Path.home() / ".beatra"
    resolved_root = (install_root or _client._current_install_root()).resolve()
    update_home = _client._update_home(resolved_state, resolved_root)
    state = _client._read_update_state(update_home)
    state["auto_update"] = bool(enabled)
    _client._write_private_json(update_home / "state.json", state)


def _safe_destination(install_root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if (
        not relative
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in parts)
        or Path(relative).is_absolute()
    ):
        raise RuntimeError("Beatra update contains an unsafe destination")
    current = install_root
    for part in parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            try:
                mode = os.lstat(current).st_mode
            except OSError as exc:
                raise RuntimeError("Beatra update cannot inspect its destination") from exc
            if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
                raise RuntimeError("Beatra update refused a linked destination")
    destination = install_root.joinpath(*parts)
    if destination.exists() or destination.is_symlink():
        try:
            mode = os.lstat(destination).st_mode
        except OSError as exc:
            raise RuntimeError("Beatra update cannot inspect its destination") from exc
        if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
            raise RuntimeError("Beatra update refused a non-file destination")
    return destination


def _nearest_existing_parent(path: Path, install_root: Path) -> Path:
    candidate = path.parent
    while candidate != install_root and not candidate.exists():
        candidate = candidate.parent
    if not candidate.exists() or not candidate.is_dir():
        raise RuntimeError("Beatra update destination is unavailable")
    return candidate


def _write_probe(directory: Path) -> None:
    probe = directory / f".beatra-update-probe-{os.getpid()}-{os.urandom(4).hex()}"
    descriptor = -1
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise RuntimeError("Beatra package installation is not writable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        probe.unlink(missing_ok=True)


def _transaction_temporary(destination: Path, nonce: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{16}", nonce) is None:
        raise RuntimeError("Beatra update recovery journal is invalid")
    return destination.with_name(f".{destination.name}.beatra-update-{nonce}.tmp")


def _copy_to_destination(
    content: bytes,
    destination: Path,
    *,
    mode: int = 0o644,
    temporary: Path | None = None,
) -> None:
    temporary = temporary or destination.with_name(
        f".{destination.name}.beatra-update-{os.getpid()}-{os.urandom(4).hex()}.tmp"
    )
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        if os.name == "posix":
            destination.chmod(mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _rollback_transaction(update_home: Path, install_root: Path) -> None:
    transaction = update_home / "transaction"
    journal_path = transaction / "journal.json"
    if not journal_path.exists():
        _remove_tree(transaction)
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Beatra update recovery journal is invalid") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("install_root") != str(install_root)
        or not isinstance(journal.get("nonce"), str)
        or re.fullmatch(r"[0-9a-f]{16}", journal["nonce"]) is None
        or not isinstance(journal.get("entries"), list)
    ):
        raise RuntimeError("Beatra update recovery journal is invalid")
    for entry in reversed(journal["entries"]):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise RuntimeError("Beatra update recovery journal is invalid")
        destination = _safe_destination(install_root, entry["path"])
        temporary = _transaction_temporary(destination, journal["nonce"])
        temporary.unlink(missing_ok=True)
        if entry.get("existed") is True:
            backup = transaction / "backup" / entry["path"]
            if not backup.is_file():
                raise RuntimeError("Beatra update recovery backup is missing")
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_to_destination(
                backup.read_bytes(),
                destination,
                mode=int(entry.get("mode", 0o644)),
                temporary=temporary,
            )
        elif destination.exists():
            destination.unlink()
    created = journal.get("created_dirs")
    if isinstance(created, list):
        for relative in reversed(created):
            if not isinstance(relative, str):
                continue
            directory = install_root / relative
            with suppress(OSError):
                directory.rmdir()
    _remove_tree(transaction)


def recover_update(
    *,
    state_dir: Path | None = None,
    install_root: Path | None = None,
) -> None:
    import mcp_client as _client
    resolved_state = state_dir or Path.home() / ".beatra"
    resolved_root = (install_root or _client._current_install_root()).resolve()
    update_home = _client._update_home(resolved_state, resolved_root)
    if (update_home / "transaction").exists():
        _rollback_transaction(update_home, resolved_root)


def _lock_update(update_home: Path, *, now: float) -> str | None:
    path = update_home / "update.lock"
    nonce = os.urandom(16).hex()
    for attempt in range(2):
        descriptor = -1
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(
                    {"schema_version": 1, "nonce": nonce, "created_at": now},
                    handle,
                    separators=(",", ":"),
                )
                handle.write("\n")
            return nonce
        except FileExistsError:
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                created = float(current["created_at"])
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                try:
                    created = path.stat().st_mtime
                except OSError:
                    created = now
            if attempt == 0 and now - created > UPDATE_LOCK_MAX_AGE_SECONDS:
                path.unlink(missing_ok=True)
                continue
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return None


def _unlock_update(update_home: Path, nonce: str) -> None:
    path = update_home / "update.lock"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("nonce") == nonce:
            path.unlink(missing_ok=True)
    except (OSError, AttributeError, json.JSONDecodeError):
        return


def _ensure_owned_baseline(
    *,
    install_root: Path,
    update_home: Path,
    get_bytes: GetBytes,
    route: str | None = None,
) -> None:
    import mcp_client as _client
    state = _client._read_update_state(update_home)
    if isinstance(state.get("owned_files"), dict):
        return
    version = _client.PACKAGE_VERSION
    archive = f"{_client.PACKAGE_SLUG}-skill-{version}.zip"
    base_url = _client.PACKAGE_CDN_BASE_TEMPLATE.format(version=version)
    manifest_content = _client._update_get(
        f"{base_url}/skill-manifest.json",
        _cdn_url,
        route=route,
        timeout=UPDATE_DOWNLOAD_TIMEOUT_SECONDS,
        limit=MAX_UPDATE_MANIFEST_BYTES,
        get_bytes=get_bytes,
    )
    manifest = _client._json_object(manifest_content, "Beatra installed package manifest")
    archive_sha256 = manifest.get("archive_sha256")
    if not isinstance(archive_sha256, str) or _SHA256.fullmatch(archive_sha256) is None:
        raise RuntimeError("Beatra installed package manifest is invalid")
    manifest_files = _manifest_files(
        manifest,
        discovery={
            "version": version,
            "archive": archive,
            "archive_sha256": archive_sha256,
            "channel": _client.PACKAGE_CHANNEL,
            "locale": _client.PACKAGE_LOCALE,
        },
    )
    owned: dict[str, dict[str, Any]] = {}
    for relative, metadata in manifest_files.items():
        destination = _safe_destination(install_root, relative)
        if not destination.is_file():
            continue
        try:
            if _sha256(destination.read_bytes()) == metadata["sha256"]:
                owned[relative] = metadata
        except OSError:
            continue
    state["installed_version"] = version
    state["owned_files"] = owned
    _client._write_private_json(update_home / "state.json", state)


def _apply_update(
    *,
    install_root: Path,
    update_home: Path,
    discovery: dict[str, Any],
    manifest: dict[str, Any],
    new_files: dict[str, bytes],
) -> None:
    import mcp_client as _client
    state = _client._read_update_state(update_home)
    previous_owned = state.get("owned_files")
    previous = previous_owned if isinstance(previous_owned, dict) else {}
    previous_paths = {
        relative
        for relative, metadata in previous.items()
        if isinstance(relative, str)
        and isinstance(metadata, dict)
        and isinstance(metadata.get("sha256"), str)
        and _SHA256.fullmatch(metadata["sha256"]) is not None
    }
    obsolete: list[str] = []
    for relative, metadata in previous.items():
        if relative in new_files or not isinstance(relative, str) or not isinstance(metadata, dict):
            continue
        destination = _safe_destination(install_root, relative)
        digest = metadata.get("sha256")
        if destination.is_file() and isinstance(digest, str):
            try:
                if _sha256(destination.read_bytes()) == digest:
                    obsolete.append(relative)
            except OSError:
                continue

    ordered_new = sorted(
        new_files,
        key=lambda path: (path == "scripts/mcp_client.py", path),
    )
    touched = [*ordered_new, *sorted(obsolete)]
    destinations = {relative: _safe_destination(install_root, relative) for relative in touched}
    collisions = [
        relative
        for relative in ordered_new
        if destinations[relative].exists() and relative not in previous_paths
    ]
    if collisions:
        raise RuntimeError(
            "Beatra update refused to replace an unowned installation file: "
            f"{collisions[0]}"
        )
    for directory in {_nearest_existing_parent(destination, install_root) for destination in destinations.values()}:
        _write_probe(directory)

    existing_bytes = sum(destination.stat().st_size for destination in destinations.values() if destination.exists())
    required_bytes = sum(len(content) for content in new_files.values()) + existing_bytes + 1024 * 1024
    try:
        if shutil.disk_usage(install_root).free < required_bytes:
            raise RuntimeError("Beatra package installation has insufficient free space")
    except OSError as exc:
        raise RuntimeError("Beatra package free space could not be verified") from exc

    transaction = update_home / "transaction"
    if transaction.exists():
        raise RuntimeError("Beatra update recovery is still pending")
    backup_root = transaction / "backup"
    _client._ensure_private_directory(backup_root)
    transaction_nonce = os.urandom(8).hex()
    entries: list[dict[str, Any]] = []
    created_dirs: set[str] = set()
    try:
        for relative in touched:
            destination = destinations[relative]
            existed = destination.exists()
            mode = stat.S_IMODE(os.lstat(destination).st_mode) if existed else 0o644
            entries.append({"path": relative, "existed": existed, "mode": mode})
            if existed:
                backup = backup_root / relative
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(destination, backup)
            parent = destination.parent
            while parent != install_root and not parent.exists():
                created_dirs.add(parent.relative_to(install_root).as_posix())
                parent = parent.parent
        journal = {
            "schema_version": 1,
            "install_root": str(install_root),
            "nonce": transaction_nonce,
            "from_version": _client.PACKAGE_VERSION,
            "to_version": discovery["version"],
            "entries": entries,
            "created_dirs": sorted(created_dirs, key=lambda value: (value.count("/"), value)),
        }
        _client._write_private_json(transaction / "journal.json", journal)
        for relative in journal["created_dirs"]:
            (install_root / relative).mkdir(exist_ok=True)
        for relative in ordered_new:
            destination = destinations[relative]
            _copy_to_destination(
                new_files[relative],
                destination,
                temporary=_transaction_temporary(destination, transaction_nonce),
            )
        for relative in obsolete:
            destinations[relative].unlink(missing_ok=True)
    except Exception as exc:
        try:
            _rollback_transaction(update_home, install_root)
        except Exception as recovery_exc:
            raise RuntimeError("Beatra update failed and requires recovery on the next start") from recovery_exc
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError("Beatra update could not replace the package safely") from exc

    _remove_tree(transaction)
    files = manifest.get("files")
    owned = {
        str(item["path"])[len(_client.PACKAGE_SLUG) + 1 :]: {
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }
        for item in files
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    }
    state["installed_version"] = discovery["version"]
    state["owned_files"] = owned
    with suppress(OSError):
        _client._write_private_json(update_home / "state.json", state)


def update_package(
    *,
    state_dir: Path | None = None,
    install_root: Path | None = None,
    get_bytes: GetBytes | None = None,
) -> dict[str, Any]:
    import mcp_client as _client
    get_bytes = get_bytes or _client._default_get_bytes
    if not _client._has_version_list():
        version = _client.PACKAGE_VERSION
        return {
            "current_version": version,
            "available_version": version,
            "update_available": False,
            "discovery": {},
        }
    resolved_state = state_dir or Path.home() / ".beatra"
    resolved_root = (install_root or _client._current_install_root()).resolve()
    if not resolved_root.is_dir():
        raise RuntimeError("Beatra package installation path is invalid")
    update_home = _client._update_home(resolved_state, resolved_root)
    route = _client.current_route(resolved_state)
    nonce = _lock_update(update_home, now=time.time())
    if nonce is None:
        raise RuntimeError("Another Beatra package update is already running")
    try:
        recover_update(state_dir=resolved_state, install_root=resolved_root)
        checked = check_update(get_bytes=get_bytes, route=route)
        if not checked["update_available"]:
            return checked
        _ensure_owned_baseline(
            install_root=resolved_root,
            update_home=update_home,
            get_bytes=get_bytes,
            route=route,
        )
        discovery = checked["discovery"]
        manifest, new_files = download_update(discovery, get_bytes=get_bytes, route=route)
        _apply_update(
            install_root=resolved_root,
            update_home=update_home,
            discovery=discovery,
            manifest=manifest,
            new_files=new_files,
        )
        return checked
    finally:
        _unlock_update(update_home, nonce)


def maybe_auto_update(
    *,
    state_dir: Path | None = None,
    install_root: Path | None = None,
    get_bytes: GetBytes | None = None,
    now: float | None = None,
) -> bool:
    """Best-effort silent update. Never block the requested MCP command."""
    import mcp_client as _client
    get_bytes = get_bytes or _client._default_get_bytes
    if not _client._has_version_list():
        return False

    resolved_state = state_dir or Path.home() / ".beatra"
    try:
        resolved_root = (install_root or _client._current_install_root()).resolve()
        update_home = _client._update_home(resolved_state, resolved_root)
        route = _client.current_route(resolved_state)
        observed_at = time.time() if now is None else now
        nonce = _lock_update(update_home, now=observed_at)
        if nonce is None:
            return False
        try:
            recover_update(state_dir=resolved_state, install_root=resolved_root)
            state = _client._read_update_state(update_home)
            if state.get("auto_update", True) is False:
                return False
            last_checked = state.get("last_checked_at")
            if (
                isinstance(last_checked, (int, float))
                and observed_at - float(last_checked) < UPDATE_CHECK_MAX_AGE_SECONDS
            ):
                return False
            state["last_checked_at"] = observed_at
            _client._write_private_json(update_home / "state.json", state)
            checked = check_update(get_bytes=get_bytes, route=route)
            if not checked["update_available"]:
                return False
            _ensure_owned_baseline(
                install_root=resolved_root,
                update_home=update_home,
                get_bytes=get_bytes,
                route=route,
            )
            discovery = checked["discovery"]
            manifest, new_files = download_update(discovery, get_bytes=get_bytes, route=route)
            _apply_update(
                install_root=resolved_root,
                update_home=update_home,
                discovery=discovery,
                manifest=manifest,
                new_files=new_files,
            )
            return True
        finally:
            _unlock_update(update_home, nonce)
    except Exception:
        return False


def auto_update_enabled(
    *,
    state_dir: Path | None = None,
    install_root: Path | None = None,
) -> bool:
    """Read ``auto_update`` from state.json. Missing or unreadable means on."""

    try:
        import mcp_client as _client

        resolved_state = state_dir or Path.home() / ".beatra"
        resolved_root = (install_root or _client._current_install_root()).resolve()
        state = _client._read_update_state(_client._update_home(resolved_state, resolved_root))
    except Exception:
        return True
    return state.get("auto_update", True) is not False
