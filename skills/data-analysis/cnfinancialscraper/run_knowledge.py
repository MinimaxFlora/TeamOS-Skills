# -*- coding: utf-8 -*-
"""
cn-financial-scraper 知识库总结同步入口 v7.7.0

把爬取/汇总的数据按需求总结进知识库（IMA / Dify / Obsidian / 本地），
并支持从知识库反向校对爬取数据。

用法：
  python run_knowledge.py --target local --mode overall
  python run_knowledge.py --target obsidian --mode target --target-name 贵州茅台 \
      --requirement "重点关注处罚与业绩" --output <临时目录>   # 测试/预览用
  python run_knowledge.py --target ima --mode negative
  python run_knowledge.py --verify data/xxx.json --target dify
  python run_knowledge.py --search "招商银行 处罚" --target local
"""
import json
import sys
import os
import argparse
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.dont_write_bytecode = True
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")


def main(argv=None):
    from knowledge_sync import (get_adapter, sync_to_knowledge_base,
                                verify_against_knowledge_base,
                                summarize_to_notes, KnowledgeBaseError)

    ap = argparse.ArgumentParser(description="v7.7.0 知识库总结同步")
    ap.add_argument("--target", default="",
                    help="知识库适配器：local/obsidian/ima/dify（默认读配置）")
    ap.add_argument("--mode", default="overall",
                    choices=["overall", "target", "negative"],
                    help="总结需求模式")
    ap.add_argument("--target-name", default="", help="mode=target 的机构名")
    ap.add_argument("--requirement", default="", help="自定义总结需求")
    ap.add_argument("--verify", default="", help="校对：爬取结果 JSON 文件")
    ap.add_argument("--search", default="", help="检索知识库")
    ap.add_argument("--output", default="",
                    help="总结预览落盘路径（.md），不推送仅预览")
    args = ap.parse_args(argv)

    try:
        if args.search:
            ad = get_adapter(args.target or None)
            hits = ad.search(args.search, limit=5)
            print(json.dumps(hits, ensure_ascii=False, indent=1))
            return 0

        if args.verify:
            data = json.loads(Path(args.verify).read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("articles", [])
            report = verify_against_knowledge_base(
                items, adapter=get_adapter(args.target or None))
            print(json.dumps(report, ensure_ascii=False, indent=1))
            return 0

        if args.output:      # 仅预览：总结为 Markdown 落盘，不推送
            notes = summarize_to_notes(mode=args.mode, target=args.target_name,
                                       requirement=args.requirement)
            if not notes:
                print("无可总结的数据（快照为空）")
                return 1
            out = Path(args.output)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n\n---\n\n".join(
                n.to_frontmatter_markdown() for n in notes), encoding="utf-8")
            print(f"已生成总结预览：{out}（{len(notes)} 篇）")
            return 0

        res = sync_to_knowledge_base(target=args.target or None,
                                     mode=args.mode,
                                     requirement=args.requirement)
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0 if not res.get("failed") else 2
    except KnowledgeBaseError as exc:
        print(f"❌ 知识库操作失败：{exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"❌ 文件不存在：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
