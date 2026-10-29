# -*- coding: utf-8 -*-
"""
clean.py — 交付前清理与打包闸门

技能市场上传的是**整个包**，而市场侧解析 SKILL.md 时只读前一小段：
一旦包体超限被截断，后果不是"报个错"，而是正文读不全——市场会直接判定
「SKILL.md 正文缺失或无法解析」。所以这里把配额当硬闸而不是提示。

用法：
  python clean.py --dry-run    # 只列出将删除的内容与配额占用（推荐先跑）
  python clean.py --check      # 只跑交付体检（文件数/体积/SKILL.md 位置/违禁文件），不改文件
  python clean.py --full       # 删除 __pycache__/pytest_cache + 运行时目录/文件 + 违规嵌套目录
                               # + 紧凑化 data/*.json，然后自动跑一次 --check
  python clean.py --binary     # 只删已知二进制文件（pdf/png/jpg/zip/docx 等）
  python clean.py --zip        # 清理后按交付白名单打包，zip 落在**技能目录之外**并回验配额

注意：
  - 清理之后不要再执行任何 import 项目代码的命令（会重新生成 .pyc）；
    必要验证请加 PYTHONDONTWRITEBYTECODE=1
  - 测试请走 runtests.py（自带前后清理），不要直跑 python -m pytest
  - tests/ 与 runtests.py/pytest.ini 属开发副本，不进技能上传包（见 DEV_ONLY）
"""
import argparse
import json
import shutil
import sys
import zipfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent

# ── 交付配额 ─────────────────────────────────────────────
# 下面三个数字是本包**自设的安全边际**，不是平台公示的接口限制——平台不会在
# 上传前告诉你阈值，超限只会表现为"内容被截断 / 正文判缺失"。依据：
#   · v11.0.0 曾以 211 个文件发布成功，说明文件数并非 200 就硬拒；
#     取 200 是为了留余量，别让边界值成为下一次的截断点。
#   · 3 MB（3145728 字节）是另一个技能上传通道实测拒绝过的体积（见交付经验），
#     超过它就打印差额提醒；10 MB 按"解压后"口径兜底。
FILE_LIMIT = 200                          # 自设上限：留出余量，避免踩边界
SIZE_LIMIT = 10 * 1024 * 1024             # 硬闸：解压后总体积兜底
SIZE_WARN = 3 * 1024 * 1024               # 严线：更小的上传通道会按此拒绝
MAX_DEPTH = 2                             # 允许 根/二级目录/文件

# 运行时产物目录（整目录删除；macros/ 是用户宏资产，不删）
RUNTIME_DIRS = [
    "data/exports", "data/reports", "data/generated_reports", "data/charts",
    "data/packages", "data/archives", "data/scheduler_logs",
    "data/sentiment_exports", "data/sentiment_snapshots",
    "data/backtest_conflicts", "data/overseas_cache", "data/scrape_cache",
    "data/browser_cache", "data/browser_state", "data/screenshots",
    "data/downloads", "data/selector_cache", "data/temp_packages",
    "data/fusion", "data/conflicts", "data/realtime", "data/crawl_state",
    # 登录档案含真实会话 Cookie，任何情况下都不许进交付包
    "data/login_profiles",
]

# 运行时大文件（体积超限主因；update_stock_list 工具可随时重新生成，
# 包内无任何读取方——测试 test_v700_enhancements 缺文件会自动 skip）
RUNTIME_FILES = [
    "data/listed_companies.json",
    # 扩充引擎每次 run() 生成的审计报告，包内无读取方，随时可重跑生成
    "data/expansion_report.json",
]

# 开发副本专供文件：跑测试/生成文档用，不进技能上传包
DEV_ONLY = {"tests", "runtests.py", "pytest.ini"}

# 已知二进制扩展名
BINARY_EXTS = {
    ".pdf", ".png", ".jpg", ".jpeg", ".gif", ".zip", ".docx", ".xlsx",
    ".pptx", ".exe", ".bin", ".onnx", ".pyc", ".pyo", ".pyd", ".7z", ".rar",
}

JUNK_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".DS_Store"}


def _scan_binary() -> list:
    hits = []
    for f in SKILL_DIR.rglob("*"):
        if f.is_file() and f.suffix.lower() in BINARY_EXTS:
            hits.append(f)
    return hits


def _scan_pycache() -> list:
    return [d for d in SKILL_DIR.rglob("__pycache__") if d.is_dir()]


def _scan_pytest_cache() -> list:
    return [d for d in SKILL_DIR.rglob(".pytest_cache") if d.is_dir()]


def _scan_deep_dirs() -> list:
    """超过 根/二级目录/文件 深度的目录（相对 parts >= 2），上传校验只允许两级结构。

    白名单 RUNTIME_DIRS 之外的运行时目录（如新增脚本的缓存目录）也会被兜底抓到。
    """
    hits = []
    for d in SKILL_DIR.rglob("*"):
        if d.is_dir() and len(d.relative_to(SKILL_DIR).parts) >= MAX_DEPTH:
            hits.append(d)
    # 先删最深的，避免输出里出现"已随父目录删除"的重复项
    hits.sort(key=lambda p: len(p.relative_to(SKILL_DIR).parts), reverse=True)
    top = []
    seen = []
    for d in hits:
        if not any(d.is_relative_to(s) for s in seen):
            top.append(d)
            seen.append(d)
    return top


def _package_size() -> int:
    return sum(f.stat().st_size for f in SKILL_DIR.rglob("*") if f.is_file())


def _compact_data_json() -> int:
    """紧凑化 data/*.json（去掉缩进空格），语义不变，仅省交付体积。

    返回节省的字节数；解析失败的文件原样保留（宁大勿坏）。
    """
    saved = 0
    for p in sorted((SKILL_DIR / "data").glob("*.json")):
        try:
            raw = p.read_bytes()
            obj = json.loads(raw)
            compact = json.dumps(obj, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8")
        except (OSError, ValueError):
            continue
        if len(compact) < len(raw):
            p.write_bytes(compact)
            saved += len(raw) - len(compact)
    return saved


# ── 交付白名单与体检 ──────────────────────────────────────

def _runtime_paths() -> list:
    out = [SKILL_DIR / rel for rel in RUNTIME_DIRS]
    out += [SKILL_DIR / rel for rel in RUNTIME_FILES]
    return out


def delivery_files() -> list:
    """返回应进入技能上传包的相对路径（排序稳定，供 --check 与 --zip 共用）。

    判定顺序：跳过开发副本文件 → 跳过缓存名 → 跳过二进制 → 跳过运行时产物
    → 跳过超过 根/二级目录/文件 的深层文件。
    """
    runtime = _runtime_paths()
    picked = []
    for f in sorted(SKILL_DIR.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(SKILL_DIR)
        parts = rel.parts
        if parts[0] in DEV_ONLY:
            continue
        if any(part in JUNK_NAMES for part in parts):
            continue
        if f.suffix.lower() in BINARY_EXTS:
            continue
        if any(f == r or f.is_relative_to(r) for r in runtime):
            continue
        if len(parts) > MAX_DEPTH:
            continue
        picked.append(rel)
    return picked


def _meta() -> dict:
    try:
        return json.loads((SKILL_DIR / "_meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check(verbose: bool = True) -> dict:
    """交付体检：文件数/体积/SKILL.md 位置/登录档案残留，返回可判定的结论字典。"""
    files = delivery_files()
    total = sum((SKILL_DIR / f).stat().st_size for f in files)
    problems = []
    if not any(str(f).replace("\\", "/") == "SKILL.md" for f in files):
        problems.append("E_SKILL_MD_MISSING：交付白名单里没有根级 SKILL.md，市场会判『正文缺失』")
    if len(files) > FILE_LIMIT:
        problems.append(f"E_FILE_LIMIT：{len(files)} 个文件 > {FILE_LIMIT}，超出即被截断")
    if total > SIZE_LIMIT:
        problems.append(f"E_SIZE：解压后 {total} 字节 > {SIZE_LIMIT}")
    leaked = [str(f) for f in files if "login_profiles" in str(f)]
    if leaked:
        problems.append(f"E_LOGIN_LEAK：登录档案（含真实 Cookie）不得进包：{leaked[:3]}")
    binary = [str(f) for f in files if f.suffix.lower() in BINARY_EXTS]
    if binary:
        problems.append(f"E_BINARY：包内含二进制文件 {binary[:3]}")
    report = {
        "ok": not problems,
        "files": len(files),
        "file_limit": FILE_LIMIT,
        "bytes": total,
        "size_limit": SIZE_LIMIT,
        "size_warn": SIZE_WARN,
        "skill_md": any(str(f).replace("\\", "/") == "SKILL.md" for f in files),
        "problems": problems,
        "skipped_dev": sorted(str(x) for x in DEV_ONLY),
    }
    if verbose:
        print(f"交付白名单：{report['files']} 个文件 / 限额 {FILE_LIMIT}"
              f"（{'✅' if report['files'] <= FILE_LIMIT else '⚠️ 超限'}）")
        warn = "" if total <= SIZE_WARN else (
            f"（超出严线 {SIZE_WARN}，差 {total - SIZE_WARN} 字节；"
            f"按解压后口径的上传通道会拒，本包仍按 10 MB 判定为可交付）"
        )
        print(f"交付体积：{total} 字节 / 限额 {SIZE_LIMIT}{warn}")
        print(f"SKILL.md 在根目录：{'✅' if report['skill_md'] else '❌'}")
        print(f"开发副本不入包：{', '.join(report['skipped_dev'])}")
        for p in problems:
            print(f"  ❌ {p}")
        if not problems:
            print("  ✅ 交付体检通过")
    return report


def make_zip(zip_name: str = "") -> dict:
    """按白名单打包，zip 落在技能目录**之外**（避免把包塞进自己），打完再回验一次配额。"""
    meta = _meta()
    slug = meta.get("slug") or SKILL_DIR.name.replace("_", "-")
    version = meta.get("version") or "0.0.0"
    name = zip_name or f"{slug}-v{version}.zip"
    if not name.endswith(".zip"):
        name += ".zip"
    out = (SKILL_DIR.parent / name).resolve()
    report = check(verbose=False)
    if not report["ok"]:
        print("❌ 交付体检未通过，先修复再打包：")
        for p in report["problems"]:
            print(f"  - {p}")
        return {"ok": False, "problems": report["problems"]}
    files = delivery_files()
    with zipfile.ZipFile(str(out), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            zf.write(str(SKILL_DIR / rel), str(rel).replace("\\", "/"))
    # 回验：以 zip 内容为准，而不是以源目录为准
    with zipfile.ZipFile(str(out)) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        unzipped = sum(info.file_size for info in zf.infolist())
    verified = {
        "ok": ("SKILL.md" in names and len(names) <= FILE_LIMIT and unzipped <= SIZE_LIMIT),
        "zip": str(out),
        "entries": len(names),
        "uncompressed_bytes": unzipped,
        "zip_bytes": out.stat().st_size,
        "skill_md_at_root": "SKILL.md" in names,
        "problems": report["problems"],
    }
    print(f"📦 {verified['zip']}")
    print(f"   {verified['entries']} 个条目 / 解压 {verified['uncompressed_bytes']} 字节"
          f" / 压缩包 {verified['zip_bytes']} 字节")
    print(f"   SKILL.md 在根目录：{'✅' if verified['skill_md_at_root'] else '❌'}")
    print(f"   结论：{'✅ 可上传' if verified['ok'] else '❌ 仍不合规'}")
    return verified


def dry_run():
    print("== 二进制文件 ==")
    for f in _scan_binary():
        print(f"  {f.relative_to(SKILL_DIR)} ({f.stat().st_size} B)")
    print("== __pycache__ 目录 ==")
    for d in _scan_pycache():
        print(f"  {d.relative_to(SKILL_DIR)}/")
    print("== .pytest_cache 目录 ==")
    for d in _scan_pytest_cache():
        print(f"  {d.relative_to(SKILL_DIR)}/")
    print("== 违规嵌套目录（超过 根/二级目录/文件，上传会被拒）==")
    for d in _scan_deep_dirs():
        print(f"  {d.relative_to(SKILL_DIR)}/")
    size = _package_size()
    flag = "⚠️ 超限！" if size > SIZE_LIMIT else "✅"
    print(f"== 源目录总大小 == {size} 字节 / 限制 {SIZE_LIMIT} {flag}")
    print("== 交付体检 ==")
    check(verbose=True)


def full():
    n = 0
    for f in _scan_binary():
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    for d in _scan_pycache() + _scan_pytest_cache():
        try:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
        except OSError:
            pass
    for rel in RUNTIME_DIRS:
        d = SKILL_DIR / rel
        if d.exists() and d.is_dir():
            try:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
            except OSError:
                pass
    for rel in RUNTIME_FILES:
        f = SKILL_DIR / rel
        if f.exists() and f.is_file():
            try:
                f.unlink()
                n += 1
            except OSError:
                pass
    for d in _scan_deep_dirs():
        shutil.rmtree(d, ignore_errors=True)
        n += 1
    saved = _compact_data_json()
    print(f"清理完成：{n} 项；data/*.json 紧凑化节省 {saved} 字节")
    dry_run()


def binary_only():
    n = 0
    for f in _scan_binary():
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    print(f"删除二进制文件 {n} 个")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="交付前清理、体检与打包")
    ap.add_argument("--dry-run", action="store_true", help="只列出不删除")
    ap.add_argument("--full", action="store_true", help="全量清理（推荐）")
    ap.add_argument("--binary", action="store_true", help="只删二进制文件")
    ap.add_argument("--check", action="store_true", help="只跑交付体检（退出码 0/1）")
    ap.add_argument("--zip", nargs="?", const="", default=None, metavar="NAME",
                    help="按白名单打包到技能目录之外，可指定文件名")
    args = ap.parse_args()
    if args.check:
        sys.exit(0 if check()["ok"] else 1)
    elif args.zip is not None:
        sys.exit(0 if make_zip(args.zip)["ok"] else 1)
    elif args.dry_run:
        dry_run()
    elif args.binary:
        binary_only()
    else:
        full()
