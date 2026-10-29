#!/usr/bin/env python3
"""校验技能目录结构，并按需生成 catalog.json。

用法：
    python scripts/check.py          # 只校验
    python scripts/check.py --write  # 校验并刷新 catalog.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = ROOT / "skills"
CATALOG = ROOT / "catalog.json"

CATEGORIES = [
    ("office-efficiency", "办公效率"),
    ("content-creation", "内容创作"),
    ("development", "开发编程"),
    ("data-analysis", "数据分析"),
    ("design-media", "设计多媒体"),
    ("business-operations", "商业运营"),
    ("education", "教育学习"),
    ("industry-experts", "行业专家"),
    ("life-services", "生活服务"),
]
CATEGORY_NAMES = dict(CATEGORIES)

KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):\s*(.*)$")
BLOCK_MARKS = {">", "|", ">-", "|-", ">+", "|+"}


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return re.sub(r"\s+", " ", value).strip()


def parse_frontmatter(text: str) -> dict[str, str]:
    """解析 SKILL.md 顶部的 YAML frontmatter（仅取需要的标量字段）。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}

    data: dict[str, str] = {}
    i = 0
    body = lines[1:end]
    while i < len(body):
        match = KEY_RE.match(body[i])
        i += 1
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if value in BLOCK_MARKS or value == "":
            block: list[str] = []
            while i < len(body) and (not body[i].strip() or body[i][:1] in " \t"):
                block.append(body[i].strip())
                i += 1
            value = " ".join(part for part in block if part)
        data[key] = unquote(value)
    return data


def describe(path: Path) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    skill_md = path / "SKILL.md"
    rel = skill_md.relative_to(ROOT).as_posix()
    if not skill_md.is_file():
        errors.append(f"{rel}: 缺少 SKILL.md")
        return {}, errors

    meta = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
    if not meta.get("name"):
        errors.append(f"{rel}: frontmatter 缺少 name")
    if not meta.get("description"):
        errors.append(f"{rel}: frontmatter 缺少 description")
    return meta, errors


def collect() -> tuple[list[dict], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    catalog: list[dict] = []

    if not SKILLS_DIR.is_dir():
        return [], [f"找不到 {SKILLS_DIR}"], []

    known = {slug for slug, _ in CATEGORIES}
    for child in sorted(SKILLS_DIR.iterdir()):
        if child.is_dir() and child.name not in known:
            warnings.append(f"未登记的分类目录：skills/{child.name}")

    for slug, zh in CATEGORIES:
        category_dir = SKILLS_DIR / slug
        entry = {"id": slug, "name": zh, "path": f"skills/{slug}", "count": 0, "skills": []}
        if not category_dir.is_dir():
            errors.append(f"缺少分类目录：skills/{slug}")
            catalog.append(entry)
            continue

        for skill_dir in sorted(p for p in category_dir.iterdir() if p.is_dir()):
            rel = skill_dir.relative_to(ROOT).as_posix()
            meta, problems = describe(skill_dir)
            errors.extend(problems)

            name = meta.get("name", "")
            if name:
                if name in seen:
                    warnings.append(f"{rel}: 技能名 {name} 与 {seen[name]} 重复，安装时以目录名为准")
                else:
                    seen[name] = rel

            entry["skills"].append(
                {
                    "id": skill_dir.name,
                    "name": name or skill_dir.name,
                    "version": meta.get("version", ""),
                    "description": meta.get("description", "")[:200],
                    "path": rel,
                }
            )
        entry["count"] = len(entry["skills"])
        catalog.append(entry)

    return catalog, errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 TeamOS Skills 仓库结构")
    parser.add_argument("--write", action="store_true", help="刷新 catalog.json")
    args = parser.parse_args()

    catalog, errors, warnings = collect()

    for entry in catalog:
        print(f"{entry['name']:<8} {entry['count']:>3} 个   skills/{entry['id']}")
    total = sum(entry["count"] for entry in catalog)
    print(f"{'合计':<8} {total:>3} 个")

    for warning in warnings:
        print(f"[警告] {warning}")
    for error in errors:
        print(f"[错误] {error}", file=sys.stderr)

    if args.write:
        CATALOG.write_text(
            json.dumps(
                {
                    "name": "TeamOS-Skills",
                    "updated": date.today().isoformat(),
                    "total": total,
                    "categories": catalog,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"已写入 {CATALOG.relative_to(ROOT)}")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
