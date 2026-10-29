# -*- coding: utf-8 -*-
"""
login_session.py — v12.0.0 登录态中枢（信息获取第四条线：登录）

定位：把「需要登录才能看到的页面」变成可爬。本模块**不托管账号密码、不破解验证码**，
它管理的是**用户自己已完成登录所得到的会话材料**（Cookie / storage_state），
并把这些材料注入 HTTP 与浏览器两条抓取链路。

支持的导入形式：
  1. 浏览器插件导出的 Cookie JSON（EditThisCookie / Cookie-Editor 数组格式）
  2. Playwright ``storage_state`` JSON（``{"cookies": [...], "origins": [...]}``）
  3. Netscape ``cookies.txt``（curl / ``--cookie-jar`` 产物）
  4. 裸 Cookie 头字符串（``k1=v1; k2=v2``）

安全边界（硬约束，勿在功能之外放宽）：
  - 档案文件只落在 ``data/login_profiles/``，该目录属运行时目录，交付前由 clean.py 删除，
    包内永不含 Cookie 明文。
  - ``list_profiles()`` / ``login_status_report()`` / 所有 MCP 工具输出**只给名字、域、
    条数、过期时间**，绝不返回 Cookie 值。
  - 不读写系统密钥库；如需用到账号密码，请只走环境变量并自行传给 Playwright 流程。

典型用法：
    from scripts.login_session import (
        add_profile, list_profiles, cookie_header, apply_to_session,
        check_login, wait_for_manual_login,
    )

    # 1) 从浏览器插件导出的 JSON 导入
    add_profile("eastmoney", "quote.eastmoney.com", cookie_file="~/cookies.json")

    # 2) 注入 HTTP 会话后抓取登录后可见页
    from scripts.http_utils import get_session, http_get
    s = get_session(reuse=False)
    apply_to_session(s, "quote.eastmoney.com")
    resp = http_get("https://quote.eastmoney.com/...", session=s)

    # 3) 不知道登录态还在不在 → 先探测
    check_login("eastmoney")

    # 4) 扫码登录：起一个可见浏览器，等用户扫码后自动保存会话
    wait_for_manual_login("xiaohongshu", "https://www.xiaohongshu.com/explore")
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from http.cookiejar import Cookie as _JarCookie
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

log = logging.getLogger("cnfs.login")

SKILL_DIR = Path(__file__).resolve().parent.parent

# 档案目录可用 CNFS_LOGIN_DIR 改道（测试与多实例隔离用）；默认落在运行时目录，
# 该目录在交付前由 clean.py 删除，保证包内不含会话材料。
PROFILE_DIR = Path(os.environ.get("CNFS_LOGIN_DIR")
                   or (SKILL_DIR / "data" / "login_profiles")).expanduser()


def set_profile_dir(path: str) -> Path:
    """改档案根目录（测试隔离 / 换机器时指到包外）。"""
    global PROFILE_DIR
    PROFILE_DIR = Path(path).expanduser()
    return PROFILE_DIR

# 常见"登录成功/失效"判据词表（探测页面无稳定接口时用作正文指纹）
_LOGGED_IN_MARKERS = [
    "退出登录", "注销", "我的关注", "个人中心", "账号安全", "已认证", "会员",
]
_LOGGED_OUT_MARKERS = [
    "请登录", "立即登录", "扫码登录", "手机号/邮箱", "统一的登录", "登录发现更懂你",
    "请先登录", "登录后查看", "unauthorized",
]
# 视为"身份凭据"的 Cookie 名片段：命中则档案可信度更高
_AUTH_COOKIE_HINTS = (
    "token", "sess", "session", "auth", "jwt", "uid", "userid", "user_id",
    "login", "passport", "st-", "remember", "ticket",
)

_IMPORT_KINDS = ("cookie_json", "storage_state", "cookies_txt", "cookie_header")


class LoginError(Exception):
    """登录态相关的可解释错误。

    ``code`` / ``next_step`` 与 pipeline_runner 的错误码口径一致，便于把失败原样
    转成"用户下一步该做什么"，而不是把调试成本丢回给用户。
    """

    def __init__(self, code: str, reason: str, next_step: str):
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason
        self.next_step = next_step

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "reason": self.reason, "next_step": self.next_step}


# ============================================================================
# 数据结构
# ============================================================================


@dataclass
class LoginProfile:
    """一个站点的登录档案。

    ``expires_at`` 取所有具名 Cookie 中**最早**的过期时间：只要有一个关键 Cookie
    到期，登录态就不可靠，因此按最短的那条判断健康度。
    """
    name: str
    domain: str
    kind: str = "cookie_json"
    probe_url: str = ""
    success_markers: List[str] = field(default_factory=list)
    failure_markers: List[str] = field(default_factory=list)
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    expires_at: Optional[str] = None
    cookies: List[Dict[str, Any]] = field(default_factory=list)
    origins: List[Dict[str, Any]] = field(default_factory=list)

    # ── 落盘 ────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        return PROFILE_DIR / f"{safe_profile_name(self.name)}.json"

    def save(self) -> Path:
        _ensure_profile_dir()
        data = asdict(self)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        _harden_file(self.path)
        return self.path

    @classmethod
    def load(cls, name: str) -> "LoginProfile":
        payload = _read_profile_file(name)
        fields = cls.__dataclass_fields__
        clean = {k: v for k, v in payload.items() if k in fields}
        prof = cls(**clean)
        prof.cookies = _normalize_cookies(prof.cookies)
        return prof

    # ── 派生信息 ────────────────────────────────────────────

    @property
    def host(self) -> str:
        return _strip_scheme(self.domain)

    def auth_cookie_names(self) -> List[str]:
        return [c["name"] for c in self.cookies
                if any(h in c["name"].lower() for h in _AUTH_COOKIE_HINTS)]

    def summary(self) -> Dict[str, Any]:
        """对外可见的档案摘要——**不含任何 Cookie 值**。"""
        health = self.health()
        return {
            "name": self.name,
            "domain": self.domain,
            "kind": self.kind,
            "cookie_count": len(self.cookies),
            "auth_cookies": self.auth_cookie_names(),
            "expires_at": self.expires_at,
            "updated_at": self.updated_at,
            "probe_url": self.probe_url or f"https://{self.host}/",
            "notes": self.notes,
            **health,
        }

    def health(self) -> Dict[str, Any]:
        """登录态健康度：fresh / expiring / expired / session_only / unknown。"""
        stamps = [c.get("expires") for c in self.cookies if c.get("expires")]
        if not stamps:
            return {"status": "session_only",
                    "days_left": None,
                    "message": "只有会话级 Cookie（关浏览器即失效），"
                               "建议用 wait_for_manual_login() 重新获取"}
        earliest = min(stamps, key=_to_epoch)
        if earliest is None:
            return {"status": "unknown", "days_left": None,
                    "message": "过期时间无法解析"}
        epoch = _to_epoch(earliest)
        days_left = round((epoch - time.time()) / 86400.0, 2)
        if days_left <= 0:
            return {"status": "expired", "days_left": days_left,
                    "message": "登录态已过期，需要重新登录"}
        if days_left <= 2:
            return {"status": "expiring", "days_left": days_left,
                    "message": f"不足 2 天过期（{days_left} 天），建议尽快刷新"}
        return {"status": "fresh", "days_left": days_left, "message": "登录态可用"}


def safe_profile_name(name: str) -> str:
    """档案名安全化：只留字母数字与 . _ -，防止路径穿越。"""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._") or "default"
    return cleaned[:64]


# ============================================================================
# 归一化：四种导入形式 → 统一 cookie dict 列表
# ============================================================================

_SAMESITE_MAP = {
    "no_restriction": "None", "none": "None", "n": "None",
    "lax": "Lax", "unspecified": "Lax", "t": "Lax",
    "strict": "Strict", "s": "Strict",
}


def _strip_scheme(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" in value:
        return urlparse(value).netloc or value
    return value.split("/")[0]


def _to_epoch(value: Any) -> Optional[float]:
    """把 epoch 秒/毫秒、ISO 字符串统一成 epoch 秒；解析不了返回 None。"""
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return value / 1000.0 if value > 1e11 else float(value)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        num = float(text)
        return num / 1000.0 if num > 1e11 else num
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    for fmt in ("%a, %d %b %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:25], fmt).replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _iso(epoch: Optional[float]) -> Optional[str]:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_cookie(item: Dict[str, Any], default_domain: str = "") -> Optional[Dict[str, Any]]:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    domain = _strip_scheme(str(item.get("domain") or default_domain)).lower()
    if not domain:
        return None
    raw_exp = item.get("expirationDate", item.get("expires"))
    epoch = _to_epoch(raw_exp)
    same = str(item.get("sameSite") or "").strip().lower()
    return {
        "name": name,
        "value": str(item.get("value", "")),
        "domain": domain,
        "path": str(item.get("path") or "/"),
        "expires": epoch,
        "httpOnly": bool(item.get("httpOnly", False)),
        "secure": bool(item.get("secure", False)),
        "sameSite": _SAMESITE_MAP.get(same, "Lax"),
    }


def _normalize_cookies(raw: Any, default_domain: str = "") -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not raw:
        return out
    if isinstance(raw, dict):
        raw = raw.get("cookies") or []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        cc = _clean_cookie(item, default_domain)
        if cc:
            out.append(cc)
    return out


def parse_cookie_header(text: str, domain: str) -> List[Dict[str, Any]]:
    """解析裸 Cookie 头（``k1=v1; k2=v2``，允许前缀 ``Cookie:``）。"""
    body = (text or "").strip()
    if body.lower().startswith("cookie:"):
        body = body[len("cookie:"):].strip()
    pairs = [p for p in re.split(r"[;\n]+", body) if p.strip()]
    cookies = []
    for pair in pairs:
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        cookies.append({
            "name": k, "value": v, "domain": domain.lower(),
            "path": "/", "expires": None, "httpOnly": False,
            "secure": True, "sameSite": "Lax",
        })
    return cookies


def parse_netscape(text: str) -> List[Dict[str, Any]]:
    """解析 Netscape/curl ``cookies.txt``（7 列 Tab 分隔，# 开头为注释）。"""
    cookies: List[Dict[str, Any]] = []
    for line in (text or "").splitlines():
        row = line.strip()
        if not row or row.startswith("#"):
            continue
        parts = row.split("\t")
        if len(parts) < 7:
            # 允许空格分隔的退化写法
            parts = row.split()
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        cookies.append({
            "name": name,
            "value": value,
            "domain": domain.lstrip(".").lower(),
            "path": path or "/",
            "expires": _to_epoch(expires),
            "httpOnly": False,
            "secure": secure.strip().upper() == "TRUE",
            "sameSite": "Lax",
        })
    return cookies


def _sniff_payload(text: str, domain: str) -> Tuple[str, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按内容猜导入形式，返回 (kind, cookies, origins)。"""
    body = (text or "").strip()
    if not body:
        return "cookie_json", [], []
    if body[0] in "{[":
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LoginError(
                "E_COOKIE_BAD_JSON",
                f"Cookie 文件不是合法 JSON：{exc}",
                "确认导出时选的是 JSON 格式（不是 HTTP Header / curl 格式）",
            ) from exc
        if isinstance(data, dict) and "cookies" in data:
            return ("storage_state",
                    _normalize_cookies(data.get("cookies"), domain),
                    list(data.get("origins") or []))
        return "cookie_json", _normalize_cookies(data, domain), []
    if "\t" in body or body.startswith("# Netscape") or "#HTTP Cookie File" in body:
        return "cookies_txt", parse_netscape(body), []
    if "=" in body and ";" in body:
        return "cookie_header", parse_cookie_header(body, domain), []
    if re.fullmatch(r"[^=;,\s]+=[^\s;]+", body):
        # 单条 Cookie（如 "sessid=1"）也是合法的 header 写法
        return "cookie_header", parse_cookie_header(body, domain), []
    if "=" in body and "\n" in body:
        return "cookie_header", parse_cookie_header(body, domain), []
    raise LoginError(
        "E_COOKIE_UNKNOWN_FORMAT",
        "无法识别 Cookie 文件格式",
        "支持：浏览器插件导出的 JSON 数组、Playwright storage_state JSON、"
        "Netscape cookies.txt、裸 Cookie 头（k=v; k=v）",
    )


# ============================================================================
# 档案目录与文件权限
# ============================================================================


def _ensure_profile_dir() -> Path:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return PROFILE_DIR


def _harden_file(path: Path) -> None:
    """尽力把档案权限收紧到当前用户（Windows 上通常无效，静默跳过）。"""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        os.chmod(path, mode & ~(stat.S_IRWXG | stat.S_IRWXO))
    except (OSError, AttributeError):
        pass


def _read_profile_file(name: str) -> Dict[str, Any]:
    key = safe_profile_name(name)
    path = PROFILE_DIR / f"{key}.json"
    if not path.exists():
        raise LoginError(
            "E_PROFILE_NOT_FOUND",
            f"找不到登录档案 '{name}'（{path.name} 不存在）",
            "先 add_profile(...) 导入 Cookie，或 list_profiles() 看现有档案名",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoginError(
            "E_PROFILE_CORRUPT",
            f"登录档案 '{name}' 读取失败：{exc}",
            "remove_profile(...) 删掉后重新导入",
        ) from exc
    if not isinstance(data, dict):
        raise LoginError("E_PROFILE_CORRUPT",
                         f"登录档案 '{name}' 结构异常",
                         "remove_profile(...) 删掉后重新导入")
    return data


def _profile_path_by_name(name: str) -> Path:
    return PROFILE_DIR / f"{safe_profile_name(name)}.json"


# ============================================================================
# 档案 CRUD
# ============================================================================


def add_profile(name: str,
                domain: str,
                cookies: Any = None,
                cookie_file: str = "",
                cookie_text: str = "",
                probe_url: str = "",
                success_markers: Sequence[str] = (),
                failure_markers: Sequence[str] = (),
                notes: str = "") -> Dict[str, Any]:
    """新建/覆盖一个登录档案，返回**摘要**（不含 Cookie 值）。

    Args:
        name: 档案名（字母数字._-，如 eastmoney / xueqiu-trial）
        domain: 站点主域，如 ``quote.eastmoney.com``（不带 scheme 也能用）
        cookies: 已解析的 Cookie（列表或 storage_state dict）
        cookie_file: Cookie 文件路径（四种格式自动识别；支持 ``~``）
        cookie_text: Cookie 文本（storage_state JSON / JSON 数组 / cookies.txt / 头串）
        probe_url: ``check_login`` 探测用的页面，默认 ``https://<domain>/``
        success_markers: 判定"仍登录"的正文关键词，默认用内置词表
        failure_markers: 判定"已掉登录"的正文关键词，默认用内置词表
        notes: 备注（比如"扫码登录，7 天有效"）

    四个 Cookie 来源至少给一个；都不给会报错，而不是悄悄存一个空档案。
    """
    host = _strip_scheme(domain)
    if not host:
        raise LoginError("E_PROFILE_NO_DOMAIN",
                         f"档案 '{name}' 缺少 domain",
                         "传 domain，例如 domain='quote.eastmoney.com'")

    kind = "cookie_json"
    origins: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []

    if cookies:
        if isinstance(cookies, dict) and "cookies" in cookies:
            kind = "storage_state"
            origins = list(cookies.get("origins") or [])
        normalized = _normalize_cookies(cookies, host)
    elif cookie_file or cookie_text:
        if cookie_file:
            path = Path(cookie_file).expanduser()
            if not path.exists():
                raise LoginError("E_COOKIE_FILE_MISSING",
                                 f"Cookie 文件不存在：{path}",
                                 "确认导出后文件是否被移动/改名，或传绝对路径")
            body = path.read_text(encoding="utf-8", errors="replace")
            head = path.name.lower()
            text = body
        else:
            head = ""
            text = cookie_text
        kind, normalized, origins = _sniff_payload(text, host)
        if head.endswith((".txt",)) and not normalized:
            kind, normalized, origins = "cookies_txt", parse_netscape(text), []
    else:
        raise LoginError(
            "E_PROFILE_NO_COOKIES",
            f"档案 '{name}' 没有提供任何 Cookie 来源",
            "在 cookies / cookie_file / cookie_text 里至少填一个",
        )

    if not normalized:
        raise LoginError(
            "E_PROFILE_EMPTY_COOKIES",
            f"档案 '{name}' 解析后得到 0 条可用 Cookie",
            "检查导出内容是否为空、是否包含 name/domain/value 三列",
        )

    now = _iso(time.time())
    prof = LoginProfile(
        name=safe_profile_name(name),
        domain=host,
        kind=kind,
        probe_url=probe_url or f"https://{host}/",
        success_markers=list(success_markers) or list(_LOGGED_IN_MARKERS),
        failure_markers=list(failure_markers) or list(_LOGGED_OUT_MARKERS),
        notes=notes,
        created_at=now,
        updated_at=now,
        cookies=normalized,
        origins=origins,
    )
    prof.expires_at = _earliest_expiry(normalized)
    prof.save()
    summary = prof.summary()
    summary["saved_to"] = _display_path(prof.path)
    return summary


def _display_path(path: Path) -> str:
    """档案在源码树内时给相对路径，改道到包外时给绝对路径。"""
    try:
        return str(path.relative_to(SKILL_DIR)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _earliest_expiry(cookies: Iterable[Dict[str, Any]]) -> Optional[str]:
    stamps = [c.get("expires") for c in cookies if c.get("expires")]
    if not stamps:
        return None
    return _iso(min(stamps, key=lambda x: float(x)))


def get_profile(name: str) -> LoginProfile:
    return LoginProfile.load(name)


def list_profiles(domain: str = "") -> List[Dict[str, Any]]:
    """列出全部档案摘要（不含 Cookie 值）。``domain`` 非空时按后缀过滤。"""
    out: List[Dict[str, Any]] = []
    if not PROFILE_DIR.exists():
        return out
    needle = _strip_scheme(domain).lower()
    for path in sorted(PROFILE_DIR.glob("*.json")):
        try:
            prof = LoginProfile.load(path.stem)
        except LoginError:
            continue
        if needle and not _domain_matches(prof.domain, needle):
            continue
        out.append(prof.summary())
    return out


def update_profile(name: str, **fields: Any) -> Dict[str, Any]:
    """改档案元信息（probe_url / notes / markers）。不接受直接改 cookie 值。"""
    allowed = {"probe_url", "notes", "success_markers", "failure_markers", "domain"}
    unknown = set(fields) - allowed
    if unknown:
        raise LoginError("E_PROFILE_FIELD_READONLY",
                         f"这些字段不能改：{sorted(unknown)}",
                         f"可改字段：{sorted(allowed)}；换 Cookie 请重新 add_profile")
    prof = LoginProfile.load(name)
    for k, v in fields.items():
        if k == "domain":
            v = _strip_scheme(str(v)).lower()
        setattr(prof, k, v)
    prof.updated_at = _iso(time.time())
    prof.save()
    return prof.summary()


def remove_profile(name: str) -> Dict[str, Any]:
    path = _profile_path_by_name(name)
    if not path.exists():
        raise LoginError("E_PROFILE_NOT_FOUND",
                         f"找不到登录档案 '{name}'",
                         "list_profiles() 确认名字")
    path.unlink()
    return {"removed": safe_profile_name(name), "ok": True}


def rename_profile(old_name: str, new_name: str) -> Dict[str, Any]:
    prof = LoginProfile.load(old_name)
    target = safe_profile_name(new_name)
    old_path = prof.path
    prof.name = target
    prof.updated_at = _iso(time.time())
    new_path = prof.save()
    if old_path.exists() and old_path != new_path:
        old_path.unlink()
    return prof.summary()


# ============================================================================
# Cookie 取出与注入
# ============================================================================


def _domain_matches(cookie_domain: str, host: str) -> bool:
    """``.eastmoney.com`` 型 Cookie 对所有子域生效。"""
    cd = _strip_scheme(cookie_domain).lower().lstrip(".")
    h = _strip_scheme(host).lower()
    if not cd or not h:
        return False
    return h == cd or h.endswith("." + cd)


def cookies_for(domain: str, name: str = "") -> List[Dict[str, Any]]:
    """取适用于 ``domain`` 的 Cookie（含值，供内部注入使用）。"""
    host = _strip_scheme(domain).lower()
    picked: Dict[Tuple[str, str], Dict[str, Any]] = {}
    profiles = ([LoginProfile.load(name)] if name
                else [LoginProfile.load(p.stem) for p in sorted(PROFILE_DIR.glob("*.json"))]
                if PROFILE_DIR.exists() else [])
    for prof in profiles:
        for c in prof.cookies:
            if _domain_matches(c["domain"], host):
                picked[(c["name"], c["domain"])] = c
    return list(picked.values())


def cookie_header(domain: str, name: str = "") -> str:
    """拼出 ``Cookie`` 请求头值（``k1=v1; k2=v2``）。无可用 Cookie 时返回空串。"""
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies_for(domain, name))


def apply_to_session(session: Any, domain: str, name: str = "") -> int:
    """把登录 Cookie 注入会话，返回注入条数。

    兼容两条链路：
      - ``http_utils.StdlibSession`` / ``requests.Session``（都有 ``.cookies``）
      - 裸 dict headers（同时写 ``Cookie`` 头，保证任何 http client 都生效）
    """
    host = _strip_scheme(domain).lower()
    jar_cookies = cookies_for(host, name)
    if not jar_cookies:
        return 0
    jar = getattr(session, "cookies", None)
    injected = 0
    if jar is not None and hasattr(jar, "set_cookie"):
        for c in jar_cookies:
            try:
                jar.set_cookie(_to_jar_cookie(c))
                injected += 1
            except Exception as exc:  # 域/安全位组合不合法时退回 header 通道
                log.debug("cookie %s 入 jar 失败: %s", c["name"], exc)
    header_value = "; ".join(f"{c['name']}={c['value']}" for c in jar_cookies)
    headers = getattr(session, "headers", None)
    if isinstance(headers, dict):
        headers["Cookie"] = header_value
    if not injected:
        injected = len(jar_cookies)
    return injected


def _to_jar_cookie(c: Dict[str, Any]) -> _JarCookie:
    domain = c["domain"].lstrip(".")
    secure = bool(c.get("secure"))
    expires = c.get("expires")
    return _JarCookie(
        version=0,
        name=c["name"], value=c["value"],
        port=None, port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=not _is_ipish(domain),
        path=c.get("path") or "/",
        path_specified=True,
        secure=secure,
        expires=int(expires) if expires else None,
        discard=not expires,
        comment=None, comment_url=None,
        rest={"HttpOnly": ""} if c.get("httpOnly") else {},
        rfc2109=False,
    )


def _is_ipish(host: str) -> bool:
    return bool(re.fullmatch(r"[\d.]+", host or ""))


def storage_state(name: str) -> Dict[str, Any]:
    """导出 Playwright 版 ``storage_state``（可直接喂给 new_context）。"""
    prof = LoginProfile.load(name)
    cookies = []
    for c in prof.cookies:
        entry: Dict[str, Any] = {
            "name": c["name"], "value": c["value"],
            "domain": c["domain"],
            "path": c.get("path") or "/",
            "httpOnly": bool(c.get("httpOnly")),
            "secure": bool(c.get("secure")),
            "sameSite": c.get("sameSite") or "Lax",
        }
        if c.get("expires"):
            entry["expires"] = float(c["expires"])
        else:
            entry["expires"] = -1
        cookies.append(entry)
    origins = list(prof.origins or [])
    if not origins:
        origins = [{"origin": f"https://{prof.host}", "localStorage": []}]
    return {"cookies": cookies, "origins": origins}


def write_storage_state(name: str, out_path: str = "") -> str:
    """把 storage_state 写到文件，返回绝对路径。

    默认落在档案目录内（运行时目录，交付前由 clean.py 删除）；要落到包外请显式给
    ``out_path``，避免把会话材料留在源码树里。
    """
    if out_path:
        target = Path(out_path).expanduser()
    else:
        target = _ensure_profile_dir() / f"{safe_profile_name(name)}.storage_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(storage_state(name), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    _harden_file(target)
    return str(target.resolve())


# ============================================================================
# 登录态探测
# ============================================================================

_LOGIN_WALL_RE = re.compile(
    r"(请登录|立即登录|扫码登录|登录后查看|请先登录|sign\s*in|log\s*in)", re.I)


def check_login(name: str = "", url: str = "", timeout: int = 15,
                session: Any = None) -> Dict[str, Any]:
    """判断"现在还登着吗"，输出原因与下一步，**不猜**。

    判定顺序：
      1. 档案健康度（Cookie 是否已过期）→ expired 直接给结论，不发请求
      2. 抓 ``url``（默认档案 probe_url），带注入的 Cookie
      3. 命中 success_markers → 已登录；只命中 failure/登录墙 → 未登录
      4. 网络失败或两类词都没命中 → ``unknown``，附 HTTP 诊断
    """
    prof = LoginProfile.load(name) if name else None
    health = prof.health() if prof else {"status": "no_profile", "days_left": None,
                                         "message": "未指定档案，仅按匿名请求探测"}
    if prof and health["status"] == "expired":
        return {"profile": prof.name, "logged_in": False, "verdict": "expired",
                "health": health, "checked_url": prof.probe_url,
                "evidence": "关键 Cookie 已过期",
                "next_step": "wait_for_manual_login() 重新登录，或重新导出 Cookie 后 add_profile()"}

    host = prof.host if prof else _strip_scheme(url)
    target = url or (prof.probe_url if prof else "")
    if not target:
        raise LoginError("E_CHECK_NO_TARGET", "既没给档案也没给 url，无从探测",
                        "check_login(name='eastmoney') 或 check_login(url='https://...')")

    try:
        from http_utils import get_session, http_get, get_diagnostic
    except ImportError:  # 以 scripts 包方式加载时退回包内路径
        from scripts.http_utils import get_session, http_get, get_diagnostic

    own_session = session if session is not None else get_session(reuse=False)
    injected = 0
    if prof:
        injected = apply_to_session(own_session, host)
    resp = http_get(target, session=own_session, timeout=timeout, use_cache=False,
                    retries=1)
    if resp is None:
        diag = get_diagnostic(0, target)
        return {"profile": prof.name if prof else "", "logged_in": None,
                "verdict": "unreachable", "health": health,
                "checked_url": target, "cookies_injected": injected,
                "evidence": f"请求未成功：{diag['title']}",
                "next_step": f"{diag['suggestion']}；或换 probe_url 为确定存在的页面"}

    body = getattr(resp, "text", "") or ""
    if not body:
        content = getattr(resp, "content", b"") or b""
        body = content.decode("utf-8", "ignore") if isinstance(content, bytes) else str(content)
    hit_ok = [m for m in (prof.success_markers if prof else _LOGGED_IN_MARKERS)
              if m and m in body]
    hit_bad = [m for m in (prof.failure_markers if prof else _LOGGED_OUT_MARKERS)
               if m and m in body]
    wall = bool(_LOGIN_WALL_RE.search(body))

    status = int(getattr(resp, "status_code", 200) or 200)
    if hit_ok and not wall:
        verdict, logged = "logged_in", True
    elif wall or hit_bad:
        verdict, logged = "logged_out", False
    elif status in (401, 403):
        verdict, logged = f"http_{status}", False
    else:
        verdict, logged = "unknown", None

    suggestions = {
        "logged_in": "可直接抓取；如页面仍空，用 scrape_webpage(mode='realtime') 走浏览器",
        "logged_out": "登录态已失效：wait_for_manual_login() 重新登录，或重新导入 Cookie",
        "http_401": "该页需要鉴权：确认 probe_url 是登录后页面，或档案域是否覆盖此子域",
        "http_403": "疑似反爬拦截：降并发 + 走反爬管道（AntiBlockPipeline）后再判",
        "unknown": ("无法从正文判定：给档案配 success_markers（登录后才出现的文案，"
                    "如用户名/退出登录），再 check_login 一次"),
    }
    return {
        "profile": prof.name if prof else "",
        "logged_in": logged,
        "verdict": verdict,
        "health": health,
        "checked_url": target,
        "http_status": status,
        "cookies_injected": injected,
        "evidence": {"matched_logged_in": hit_ok,
                     "matched_logged_out": hit_bad,
                     "login_wall_seen": wall},
        "next_step": suggestions.get(verdict, suggestions["unknown"]),
    }


def login_status_report() -> Dict[str, Any]:
    """全部档案的健康度清单（脱敏），用于"我的登录态还能用吗"。"""
    profiles = list_profiles()
    buckets = {"fresh": [], "expiring": [], "expired": [], "session_only": [],
               "unknown": []}
    for p in profiles:
        buckets.setdefault(p["status"], []).append(p["name"])
    return {
        "total": len(profiles),
        "by_status": {k: v for k, v in buckets.items() if v},
        "profiles": profiles,
        "summary": (f"{len(profiles)} 个档案；"
                    f"可用 {len(buckets['fresh'])}、即将过期 {len(buckets['expiring'])}、"
                    f"已过期 {len(buckets['expired'])}、仅会话级 {len(buckets['session_only'])}"),
        "next_step": ("过期/即将过期的档案请重新登录："
                      "wait_for_manual_login(name, login_url)" if any(
                          buckets[k] for k in ("expired", "expiring")) else
                      "全部档案健康"),
    }


# ============================================================================
# 交互式登录（扫码 / 人工过验证码后自动接管会话）
# ============================================================================


def wait_for_manual_login(name: str,
                          login_url: str,
                          target_domain: str = "",
                          success_markers: Sequence[str] = (),
                          timeout: int = 180,
                          poll_seconds: float = 2.0,
                          headless: bool = False,
                          proxy: Any = None) -> Dict[str, Any]:
    """打开可见浏览器让用户自己登录（扫码/验证码），检测到成功后保存档案。

    这是"登录"这条线唯一的人工通道：脚本不参与输密码、不破解验证码，
    只负责等待并把生成的会话收下来存成档案。

    Returns:
        {"ok", "profile", "cookies_captured", "waited_seconds", "reason", "next_step"}
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "reason": "playwright_not_installed",
                "next_step": "pip install playwright && playwright install chromium",
                "profile": name}

    prof_domain = _strip_scheme(target_domain) or _strip_scheme(login_url)
    markers = list(success_markers) or list(_LOGGED_IN_MARKERS)
    started = time.time()
    captured: Optional[List[Dict[str, Any]]] = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=headless,
                                         proxy=proxy if isinstance(proxy, dict) else None)
            context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
            existing = None
            try:
                existing = LoginProfile.load(name)
                context.add_cookies([
                    {"name": c["name"], "value": c["value"], "domain": c["domain"],
                     "path": c.get("path") or "/"} for c in existing.cookies
                ])
            except LoginError:
                existing = None
            page = context.new_page()
            page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
            deadline = started + max(10, int(timeout))
            while time.time() < deadline:
                try:
                    raw = context.cookies()
                    html = page.content() or ""
                except Exception:
                    raw, html = [], ""
                usable = _normalize_cookies(raw, prof_domain)
                authed = [c for c in usable
                          if any(h in c["name"].lower() for h in _AUTH_COOKIE_HINTS)]
                if authed and any(m in html for m in markers):
                    captured = usable
                    break
                if authed and not _LOGIN_WALL_RE.search(html):
                    captured = usable
                    break
                time.sleep(max(0.5, float(poll_seconds)))
            if captured is None:
                try:
                    raw = context.cookies()
                except Exception:
                    raw = []
                fallback = _normalize_cookies(raw, prof_domain)
                authed = [c for c in fallback
                          if any(h in c["name"].lower() for h in _AUTH_COOKIE_HINTS)]
                captured = fallback if authed else None
            state = {"cookies": context.cookies(), "origins": []}
            context.close()
            browser.close()
    except Exception as exc:
        return {"ok": False, "reason": f"browser_error: {exc}",
                "next_step": "确认已装 chromium（playwright install chromium）、"
                             "浏览器窗口允许前台运行；或改用 cookie_file 直接导入 Cookie",
                "profile": name}

    waited = round(time.time() - started, 1)
    if not captured:
        return {"ok": False, "reason": "timeout_without_session",
                "waited_seconds": waited, "login_url": login_url,
                "next_step": (f"在 {timeout}s 内没检测到登录凭据 Cookie。重跑并加大 timeout，"
                              "或手动完成登录后用浏览器插件导出 Cookie 再 add_profile()")}

    summary = add_profile(
        name=name,
        domain=prof_domain,
        cookies=state,
        probe_url=login_url,
        success_markers=markers,
        notes=f"wait_for_manual_login 采集（{datetime.now():%Y-%m-%d %H:%M}）",
    )
    return {"ok": True, "profile": summary["name"],
            "cookies_captured": summary["cookie_count"],
            "auth_cookies": summary["auth_cookies"],
            "expires_at": summary["expires_at"],
            "waited_seconds": waited,
            "next_step": "check_login() 验证后可直接抓取登录后可见页"}


# ============================================================================
# 浏览器链路：把档案交给 BrowserScraper / Playwright
# ============================================================================


def apply_to_browser_context(context: Any, name: str) -> int:
    """把档案 Cookie 加进 Playwright BrowserContext，返回条数。"""
    prof = LoginProfile.load(name)
    payload = []
    for c in prof.cookies:
        item: Dict[str, Any] = {"name": c["name"], "value": c["value"],
                                "domain": c["domain"], "path": c.get("path") or "/"}
        if c.get("expires"):
            item["expires"] = float(c["expires"])
        if c.get("httpOnly"):
            item["httpOnly"] = True
        if c.get("secure"):
            item["secure"] = True
        payload.append(item)
    try:
        context.add_cookies(payload)
        return len(payload)
    except Exception as exc:
        raise LoginError("E_BROWSER_COOKIE_REJECT",
                        f"Playwright 拒绝了这批 Cookie：{exc}",
                        "常见是 secure/httpOnly 与域不匹配；重新用 "
                        "wait_for_manual_login() 采集，别手改 Cookie 文件")


def browser_context_kwargs(name: str) -> Dict[str, Any]:
    """给 ``browser.new_context(**kwargs)`` 用的 storage_state 参数。"""
    return {"storage_state": storage_state(name)}


# ============================================================================
# 自检
# ============================================================================


def self_check() -> Dict[str, Any]:
    """不联网的自检：档案目录可读、解析器正确、输出不泄值。"""
    checks: Dict[str, Any] = {"module": "login_session", "ok": True}
    try:
        parsed = parse_cookie_header("a=1; b=2", "example.com")
        checks["header_parser"] = len(parsed) == 2
        nets = parse_netscape("# Netscape HTTP Cookie File\n"
                             ".example.com\tTRUE\t/\tTRUE\t1900000000\tuid\tabc\n")
        checks["netscape_parser"] = len(nets) == 1 and nets[0]["name"] == "uid"
        ss = _sniff_payload(json.dumps({"cookies": [
            {"name": "t", "value": "1", "domain": ".example.com", "path": "/"}],
            "origins": []}), "example.com")
        checks["storage_state_sniff"] = ss[0] == "storage_state" and len(ss[1]) == 1
        jar = _to_jar_cookie({"name": "t", "value": "1", "domain": "example.com",
                             "path": "/", "expires": 1900000000, "httpOnly": True,
                             "secure": True})
        checks["jar_bridge"] = jar.name == "t" and jar.domain_specified
        checks["domain_match"] = (_domain_matches(".example.com", "a.example.com")
                                 and not _domain_matches("other.com", "a.example.com"))
        leaked = "abc" in json.dumps([p for p in list_profiles()], ensure_ascii=False)
        checks["no_secret_in_listing"] = not leaked
        for key, val in checks.items():
            if key != "module" and val is False:
                checks["ok"] = False
        checks["profiles"] = len(list_profiles())
        checks["profile_dir_present"] = PROFILE_DIR.exists()
    except Exception as exc:
        checks["ok"] = False
        checks["error"] = f"{type(exc).__name__}: {exc}"
    return checks


__all__ = [
    "LoginError", "LoginProfile", "add_profile", "apply_to_browser_context",
    "apply_to_session", "browser_context_kwargs", "check_login", "cookie_header",
    "cookies_for", "get_profile", "list_profiles", "login_status_report",
    "parse_cookie_header", "parse_netscape", "remove_profile", "rename_profile",
    "safe_profile_name", "set_profile_dir", "storage_state", "update_profile",
    "wait_for_manual_login", "write_storage_state", "self_check",
]
