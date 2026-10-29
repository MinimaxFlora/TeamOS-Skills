#!/usr/bin/env python3
"""Validate the competition-facing structure of official-document-skill.

Uses only the Python standard library so reviewers can run it directly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


EXPECTED_NAME = "official-document-skill"
REQUIRED_PATHS = (
    "SKILL.md",
    "README.md",
    "LICENSE",
    "agents/openai.yaml",
    "examples/examples.md",
    "templates/input-brief.md",
    "tests/manual-regression.md",
)


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def extract_frontmatter(text: str) -> str | None:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    return match.group(1) if match else None


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    errors: list[str] = []

    if root.name != EXPECTED_NAME:
        fail(f"顶层目录应命名为 {EXPECTED_NAME!r}，当前为 {root.name!r}", errors)

    for relative in REQUIRED_PATHS:
        if not (root / relative).is_file():
            fail(f"缺少必要文件：{relative}", errors)

    skill_path = root / "SKILL.md"
    if skill_path.is_file():
        try:
            skill_text = skill_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            fail("SKILL.md 不是有效的 UTF-8 文本", errors)
        else:
            frontmatter = extract_frontmatter(skill_text)
            if frontmatter is None:
                fail("SKILL.md 缺少有效的 YAML frontmatter 边界", errors)
            else:
                checks = {
                    "name": rf"(?m)^name:\s*{re.escape(EXPECTED_NAME)}\s*$",
                    "description": r"(?m)^description:\s*\S.+$",
                    "metadata": r"(?m)^metadata:\s*$",
                    "version": r"(?m)^\s+version:\s*[\"']?\d+\.\d+\.\d+[\"']?\s*$",
                    "triggers": r"(?m)^\s+triggers:\s*$",
                }
                for label, pattern in checks.items():
                    if not re.search(pattern, frontmatter):
                        fail(f"YAML 元数据缺少或无法识别：{label}", errors)

                trigger_items = re.findall(
                    r"(?m)^\s{4}-\s+[\"']?(.+?)[\"']?\s*$", frontmatter
                )
                if len(trigger_items) < 3:
                    fail("触发条件应至少包含 3 条具体场景", errors)

            for reference in (
                "examples/examples.md",
                "templates/input-brief.md",
                "tests/manual-regression.md",
                "scripts/validate_package.py",
            ):
                if reference not in skill_text:
                    fail(f"SKILL.md 未导航到资源：{reference}", errors)

    if errors:
        print("验证失败：")
        for item in errors:
            print(f"- {item}")
        return 1

    print("验证通过：目录结构、UTF-8 编码、核心元数据、触发条件和资源导航均符合预期。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
