#!/usr/bin/env bash
# 把仓库里的技能平铺安装到 Agent 的技能目录。
# 用法：bash scripts/install.sh [目标目录]
# 默认目标：~/.hermes/skills（TeamOS 容器内为 /opt/data/skills）

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-$HOME/.hermes/skills}"

if [ ! -d "$ROOT/skills" ]; then
  echo "找不到 $ROOT/skills" >&2
  exit 1
fi

mkdir -p "$TARGET"

count=0
while IFS= read -r -d '' src; do
  name="$(basename "$src")"
  rm -rf "${TARGET:?}/$name"
  cp -R "$src" "$TARGET/$name"
  count=$((count + 1))
done < <(find "$ROOT/skills" -mindepth 3 -maxdepth 3 -type d -print0)

echo "已安装 $count 个技能到 $TARGET"
