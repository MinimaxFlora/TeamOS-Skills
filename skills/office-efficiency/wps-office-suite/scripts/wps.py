#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WPS Office 全家桶 统一 CLI 入口 v5.3.0
=======================================
薄路由层：argparse 仅解析全局开关，命令分发查 registry.json 转发到现有脚本，
**不改任何脚本内部逻辑**。每条命令以独立子进程运行（隔离 + 复用 ENGINE_CACHE）。

命令形态：
  python scripts/wps --list
  python scripts/wps word create --title "测试" --filepath ./a.docx
  python scripts/wps office --app word --action create --title "测试"
  python scripts/wps excel engine-info --json
  python scripts/wps convert --input 报告.docx --output-format pdf
  python scripts/wps --json <任意命令> [参数]

设计要点（死规则 9/10/13/14）：
  - 纯本地逻辑，无外部 API、无网络（仅透传已有脚本行为）
  - 子进程独立运行，不残留常驻进程（规则 16）
  - 仅新增 .py + registry.json，合规（规则 13）
"""
import argparse
import json
import sys
import subprocess
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = SCRIPT_DIR.parent / "registry.json"

# app 名兜底（registry 优先）
APP_SCRIPTS = {"word": "wps_word.py", "excel": "wps_excel.py", "ppt": "wps_ppt.py"}


def load_registry():
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"apps": {}, "commands": {}}


REG = load_registry()
APPS = REG.get("apps", {}) or {}
COMMANDS = REG.get("commands", {}) or {}
SCRIPTS = {p.stem: p.name for p in SCRIPT_DIR.glob("*.py")}


def resolve_script(name):
    """name 可为：友好命令名 / app 名 / 脚本名（带或不带 .py）"""
    if name in COMMANDS:
        return COMMANDS[name].get("script")
    if name in APPS:
        return APPS[name]
    cand = name if name.endswith(".py") else name + ".py"
    if cand in SCRIPTS.values():
        return cand
    if name in SCRIPTS:
        return SCRIPTS[name]
    return None


def get_opt(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def detect_error_id(text):
    import re
    m = re.search(r"【(E\d{3})】", text)
    return m.group(1) if m else "E000"


def _emit(ok, error_id, elapsed, data):
    from wps_error import emit_json
    emit_json(ok, error_id, elapsed, data)


def run_script(script, script_args, use_json):
    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / script), *script_args],
            capture_output=True, text=True, cwd=str(SCRIPT_DIR.parent), timeout=600,
        )
    except FileNotFoundError:
        _emit(False, "E000", None, f"脚本不存在: {script}")
        return 1
    except subprocess.TimeoutExpired:
        _emit(False, "E006", round(time.time() - t0, 3), "子进程超时（600s）")
        return 1
    elapsed = round(time.time() - t0, 3)
    if use_json:
        ok = proc.returncode == 0
        error_id = None if ok else detect_error_id(proc.stderr + proc.stdout)
        data = proc.stdout.strip()
        try:
            data = json.loads(data)
        except Exception:
            pass
        _emit(ok, error_id, elapsed, data)
        return 0 if ok else 1
    if proc.stdout:
        sys.stdout.write(proc.stdout)
        if not proc.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")
    return proc.returncode


def route_office(rest, use_json):
    app = get_opt(rest, "--app")
    action = get_opt(rest, "--action")
    if not app or not action:
        print("office 需要 --app word|excel|ppt 与 --action <子命令>", file=sys.stderr)
        return 2
    script = APPS.get(app) or resolve_script(app)
    if not script:
        print(f"未知 app: {app}", file=sys.stderr)
        return 2
    trimmed = []
    skip = False
    for a in rest:
        if skip:
            skip = False
            continue
        if a in ("--app", "--action"):
            skip = True
            continue
        trimmed.append(a)
    return run_script(script, [action] + trimmed, use_json)


def route_convert(rest, use_json):
    inp = get_opt(rest, "--input")
    out_fmt = get_opt(rest, "--output-format", "pdf")
    out = get_opt(rest, "--output", "")
    if not inp:
        print("convert 需要 --input 文件路径", file=sys.stderr)
        return 2
    p = Path(inp)
    ext = p.suffix.lower()
    if ext == ".md":
        if "pptx" in (out_fmt + out):
            target = "md_converter.py"
            targs = ["to-pptx", "--file", inp, "--output", out or str(p.with_suffix(".pptx"))]
        else:
            target = "md_converter.py"
            targs = ["to-docx", "--file", inp, "--output", out or str(p.with_suffix(".docx"))]
    elif ext == ".docx" and "pptx" in (out_fmt + out):
        target = "wps_docx_to_ppt.py"
        targs = ["file", "--input", inp, "--output", out or str(p.with_suffix(".pptx"))]
    else:
        target = "format_converter.py"
        targs = ["convert", "--input", inp, "--output-format", out_fmt]
    return run_script(target, targs, use_json)


def list_commands():
    print("WPS Office 全家桶 · 统一命令入口（v5.3.0）")
    print("=" * 64)
    print("全局: python scripts/wps [--json] <命令> [参数]")
    print("      python scripts/wps --list")
    print("      python scripts/wps office --app word|excel|ppt --action <子命令> [参数]")
    print("      python scripts/wps convert --input <文件> --output-format <格式>")
    print("-" * 64)
    print("命令清单（registry.json 自动导出；全部脚本仍可 'wps <脚本名>' 直接调用）:")
    rows = []
    for name, meta in COMMANDS.items():
        rows.append((name, meta.get("desc", ""), meta.get("script", "")))
    reg_names = set(COMMANDS.keys())
    for stem, fname in sorted(SCRIPTS.items()):
        if stem in reg_names or fname in ("wps.py", "cli_entry.py", "wps_worker.py"):
            continue
        rows.append((stem, "(见脚本 --help)", fname))
    for name, desc, script in rows:
        print(f"  {name:<18} {desc}")
    print("-" * 64)
    print("示例:")
    print("  python scripts/wps word create --title 测试 --filepath ./a.docx")
    print("  python scripts/wps excel engine-info --json")
    print("  python scripts/wps convert --input 报告.docx --output-format pdf")


def main():
    parser = argparse.ArgumentParser(
        prog="wps",
        description="WPS Office 全家桶 统一命令入口 v5.3.0",
    )
    parser.add_argument("--json", action="store_true",
                        help="机器可读 JSON 输出 {ok,error_id,elapsed,data}")
    parser.add_argument("--list", action="store_true", help="列出全部命令与一句话用途")
    parser.add_argument("rest", nargs=argparse.REMAINDER, help="命令与参数")
    args = parser.parse_args()

    if args.list:
        list_commands()
        return 0

    rest = list(args.rest)
    use_json = args.json
    if "--json" in rest:
        rest.remove("--json")
        use_json = True

    if not rest:
        parser.print_help()
        return 0

    head = rest[0]
    if head == "office":
        return route_office(rest[1:], use_json)
    if head == "convert":
        return route_convert(rest[1:], use_json)
    script = resolve_script(head)
    if not script:
        print(f"未知命令: {head}（运行 python scripts/wps --list 查看可用命令）", file=sys.stderr)
        return 2
    return run_script(script, rest[1:], use_json)


if __name__ == "__main__":
    sys.exit(main())
