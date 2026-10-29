# -*- coding: utf-8 -*-
"""
数据分析汇总引擎 v7.3.0 (data_aggregator.py)

增强数据分析与汇总能力：把多次爬取/舆情快照的结果做跨源聚合、去重、
统计与汇总报告输出。纯函数 + 标准库实现，零 pip 依赖。

能力：
  - dedupe_articles        : 跨快照模糊去重（标题字符二元组相似度）
  - sentiment_statistics   : 情感/严重度/时间/来源分布 + 热词统计
  - cross_section_summary  : 按目标/来源/类型截面汇总
  - aggregate_snapshots    : 聚合 data/sentiment_snapshots/ 全部快照
  - build_institution_brief: 机构简报（名录信息 + 相关舆情统计）
  - export_markdown        : 汇总结果导出 Markdown / JSON

用法：
    from scripts.data_aggregator import aggregate_snapshots, export_markdown
    agg = aggregate_snapshots()                # 默认 data/sentiment_snapshots
    md = export_markdown(agg)
    print(md)
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SKILL_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "sentiment_snapshots"
REGISTRY_FILE = DATA_DIR / "institution_registry.json"

_PUNCT_RE = re.compile(r"[\s\u3000|｜\-—_,，.。!！?？:：;；\"'“”‘’()（）【】\[\]《》<>()]+")


def normalize_title(title: str) -> str:
    """标题规范化：去标点/空白、小写化，用于跨源去重。"""
    return _PUNCT_RE.sub("", (title or "")).lower()


def _bigrams(text: str) -> set:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def title_similarity(a: str, b: str) -> float:
    """标题相似度（Jaccard 与包含度混合，字符二元组），0-1。

    包含度分量让转发/转载类标题（仅追加“（转发）”等后缀）也能判重；
    乘 0.9 衰减避免短标题被长标题无条件吞并。
    """
    sa, sb = _bigrams(normalize_title(a)), _bigrams(normalize_title(b))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    jaccard = inter / len(sa | sb)
    containment = inter / min(len(sa), len(sb))
    return max(jaccard, 0.9 * containment)


def dedupe_articles(articles: List[Dict[str, Any]],
                    threshold: float = 0.85) -> List[Dict[str, Any]]:
    """模糊去重：标题相似 > threshold 视为重复，保留先出现的。

    复杂度 O(n·k)（k 为已保留数），对几百条快照足够快。
    """
    kept: List[Dict[str, Any]] = []
    kept_titles: List[str] = []
    exact: set = set()
    for art in articles:
        title = art.get("title") or ""
        key = normalize_title(title)
        if not key or key in exact:
            continue
        dup = False
        for prev in kept_titles:
            if title_similarity(title, prev) > threshold:
                dup = True
                break
        if dup:
            continue
        exact.add(key)
        kept.append(art)
        kept_titles.append(title)
    return kept


def sentiment_statistics(articles: List[Dict[str, Any]]) -> Dict[str, Any]:
    """舆情统计：情感/严重度/时间/来源分布 + 高频关键词。"""
    senti = Counter(a.get("sentiment", "neutral") for a in articles)
    severity = Counter(a.get("severity", "中性") for a in articles)
    source = Counter((a.get("source") or "未知") for a in articles)

    by_day: Dict[str, int] = defaultdict(int)
    for a in articles:
        pt = (a.get("publish_time") or a.get("fetch_time") or "").strip()
        m = re.match(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})", pt)
        if m:
            by_day[m.group(1).replace("/", "-")] += 1

    keywords: Counter = Counter()
    for a in articles:
        for k in (a.get("keywords_matched") or []):
            keywords[k] += 1

    scores = [float(a.get("sentiment_score") or 0) for a in articles]
    neg = [s for s in scores if s < 0]
    pos = [s for s in scores if s > 0]
    return {
        "total": len(articles),
        "sentiment": dict(senti),
        "severity": dict(severity),
        "source_top": source.most_common(10),
        "by_day": dict(sorted(by_day.items())),
        "hot_keywords": keywords.most_common(20),
        "avg_score": round(sum(scores) / len(scores), 3) if scores else 0.0,
        "neg_ratio": round(len(neg) / len(articles), 3) if articles else 0.0,
        "pos_ratio": round(len(pos) / len(articles), 3) if articles else 0.0,
    }


def cross_section_summary(articles: List[Dict[str, Any]],
                          group_by: str = "target_name") -> List[Dict[str, Any]]:
    """截面汇总：按字段分组统计（默认按目标机构）。"""
    groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in articles:
        groups[(a.get(group_by) or "未知")].append(a)
    out = []
    for key, items in groups.items():
        stats = sentiment_statistics(items)
        out.append({
            group_by: key,
            "count": len(items),
            "negative": stats["sentiment"].get("negative", 0),
            "positive": stats["sentiment"].get("positive", 0),
            "neg_ratio": stats["neg_ratio"],
            "severity_top": max(stats["severity"], key=stats["severity"].get,
                                default="中性"),
        })
    out.sort(key=lambda x: (-x["neg_ratio"], -x["count"]))
    return out


def load_snapshots(snapshot_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """加载快照目录内全部 *.json（损坏文件跳过）。"""
    d = Path(snapshot_dir) if snapshot_dir else SNAPSHOT_DIR
    out = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.json")):
        if f.name.startswith("_"):
            continue
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("articles"), list):
                out.append(doc)
        except Exception:
            continue
    return out


def aggregate_snapshots(snapshot_dir: Optional[Path] = None,
                        days: int = 0) -> Dict[str, Any]:
    """聚合全部快照：跨快照去重 + 统计 + 截面汇总。

    Args:
        snapshot_dir: 快照目录（默认 data/sentiment_snapshots）
        days: 仅统计最近 N 天（0=全部）
    """
    snapshots = load_snapshots(snapshot_dir)
    articles: List[Dict[str, Any]] = []
    for snap in snapshots:
        articles.extend(snap.get("articles") or [])

    if days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
        kept = []
        for a in articles:
            pt = (a.get("publish_time") or a.get("fetch_time") or "").strip()
            try:
                ts = datetime.strptime(pt[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                kept.append(a)          # 无时间的保留
                continue
            if ts >= cutoff:
                kept.append(a)
        articles = kept

    deduped = dedupe_articles(articles)
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "snapshot_count": len(snapshots),
        "raw_count": len(articles),
        "dedup_count": len(deduped),
        "dedup_removed": len(articles) - len(deduped),
        "statistics": sentiment_statistics(deduped),
        "by_target": cross_section_summary(deduped, "target_name"),
        "by_source": cross_section_summary(deduped, "source"),
        "articles": deduped,
    }


def build_institution_brief(name: str,
                            snapshot_dir: Optional[Path] = None) -> Dict[str, Any]:
    """机构简报：名录信息 + 该机构相关舆情统计。"""
    brief: Dict[str, Any] = {"name": name, "registry": None,
                             "sentiment": None}
    # 1) 名录信息（直接读紧凑 registry，避免重依赖）
    try:
        doc = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
        cols, rows = doc["c"], doc["d"]
        i_name = cols.index("name")
        for row in rows:
            if name in (row[i_name] or ""):
                brief["registry"] = dict(zip(cols, row))
                break
    except Exception:
        pass
    # 2) 相关舆情
    agg = aggregate_snapshots(snapshot_dir)
    related = [a for a in agg["articles"]
               if name in (a.get("target_name") or "") or name in (a.get("title") or "")]
    if related:
        brief["sentiment"] = sentiment_statistics(related)
        brief["recent_titles"] = [a.get("title", "") for a in related[:5]]
    return brief


def export_markdown(agg: Dict[str, Any], path: Optional[Path] = None,
                    top_articles: int = 10) -> str:
    """把聚合结果导出为 Markdown 汇总报告。返回文本；给 path 则落盘。"""
    st = agg.get("statistics") or {}
    lines = [
        "# 金融数据爬取汇总报告",
        "",
        f"- 生成时间：{agg.get('generated_at', '')}",
        f"- 快照数：{agg.get('snapshot_count', 0)}，"
        f"原始条目 {agg.get('raw_count', 0)}，去重后 {agg.get('dedup_count', 0)}"
        f"（剔除重复 {agg.get('dedup_removed', 0)}）",
        "",
        "## 情感分布",
        "",
        "| 情感 | 数量 |", "|---|---|",
    ]
    for k, v in (st.get("sentiment") or {}).items():
        lines.append(f"| {k} | {v} |")
    lines += ["", f"负面占比 {st.get('neg_ratio', 0):.1%}，"
                  f"正面占比 {st.get('pos_ratio', 0):.1%}，"
                  f"平均分 {st.get('avg_score', 0)}", ""]

    hot = st.get("hot_keywords") or []
    if hot:
        lines += ["## 高频关键词", "",
                  "、".join(f"{k}({v})" for k, v in hot[:15]), ""]

    by_day = st.get("by_day") or {}
    if by_day:
        lines += ["## 时间分布", "", "| 日期 | 数量 |", "|---|---|"]
        for d, n in by_day.items():
            lines.append(f"| {d} | {n} |")
        lines.append("")

    by_target = agg.get("by_target") or []
    if by_target:
        lines += ["## 目标机构截面", "",
                  "| 机构 | 条数 | 负面 | 负面占比 | 最高等级 |",
                  "|---|---|---|---|---|"]
        for row in by_target[:20]:
            lines.append(f"| {row.get('target_name', '')} | {row['count']} | "
                         f"{row['negative']} | {row['neg_ratio']:.1%} | "
                         f"{row['severity_top']} |")
        lines.append("")

    articles = agg.get("articles") or []
    if articles:
        lines += ["## 重点条目（前 %d）" % top_articles, ""]
        for a in articles[:top_articles]:
            sev = a.get("severity", "")
            lines.append(f"- **[{a.get('sentiment', '')}/{sev}]** "
                         f"{a.get('title', '')}  \n"
                         f"  来源：{a.get('source', '')}  "
                         f"时间：{a.get('publish_time', '')}")
        lines.append("")

    md = "\n".join(lines)
    if path:
        Path(path).write_text(md, encoding="utf-8")
    return md


def export_json(agg: Dict[str, Any], path: Path) -> Path:
    """聚合结果落盘 JSON（含全部条目）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(agg, ensure_ascii=False, indent=1),
                 encoding="utf-8")
    return p


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="v7.3.0 数据分析汇总引擎")
    ap.add_argument("--dir", default="", help="快照目录（默认 data/sentiment_snapshots）")
    ap.add_argument("--days", type=int, default=0, help="仅统计最近 N 天")
    ap.add_argument("--output", default="", help="Markdown 输出路径（默认仅打印）")
    ap.add_argument("--brief", default="", help="生成某机构简报")
    args = ap.parse_args()

    if args.brief:
        print(json.dumps(build_institution_brief(args.brief),
                         ensure_ascii=False, indent=1))
    else:
        agg = aggregate_snapshots(Path(args.dir) if args.dir else None,
                                  days=args.days)
        md = export_markdown(agg, Path(args.output) if args.output else None)
        print(md)
