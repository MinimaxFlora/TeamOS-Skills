# -*- coding: utf-8 -*-
"""跨源证据融合与汇总层（v11.0.0 新增）。

解决 v10 之前"汇总"的真实短板：`data_aggregator.aggregate_snapshots` 只是把各
快照的 articles 平铺 extend 再做模糊标题去重，**同一条新闻被 10 家媒体报道时，
10 条记录会同时留在结果里，谁说了什么、数字打架在哪、哪个来源更可信，全部丢失**。
v8 的 `analysis_enhanced.verify_cross_source` 有跨源置信度，但没有任何 MCP 工具引用它，
而且只标情感冲突、不核算数值冲突。

本模块补的是"数值与出处"这一层：
1. normalize_record  — 把各家 scraper 的异构字段收敛成一条规范记录
2. cluster_records   — 按实体分桶 + 标题相似度 + 时间窗聚成"事件簇"
3. reconcile_cluster — 数值字段算加权共识（权威度 × 时效），并量化分歧
4. ConflictLedger    — 分歧落盘成台账（v10 里 CONFLICTS_DIR 建了目录却从未写入）
5. HistoryIndex      — 内容哈希跨批次去重（v10 里 content_hash 算了却从不比对）
6. EvidenceBundle    — 可直接引用的证据包：结论 / 出处 / 分歧 / 缺口 / 时效

刻意**不做**的事：不在本地生成结论文案。输出全是"事实 + 可核验数字 + 缺口清单"，
成文交给上层模型——这样每一句结论都能回溯到具体来源，也不会把启发式包装成分析。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

try:  # 复用 v8 已有的权威度表与相似度实现，避免两处口径漂移
    from analysis_enhanced import (  # type: ignore
        DEFAULT_SOURCE_AUTHORITY, _normalize_event_title, _title_similarity, _parse_ts,
        _tokenize_zh,
    )
except ImportError:  # pragma: no cover
    from scripts.analysis_enhanced import (
        DEFAULT_SOURCE_AUTHORITY, _normalize_event_title, _title_similarity, _parse_ts,
        _tokenize_zh,
    )

__all__ = [
    "FIELD_ALIASES", "normalize_record", "content_hash", "extract_facts",
    "entity_of", "event_kind_of",
    "EventCluster", "cluster_records", "ReconciledFact", "reconcile_cluster",
    "Conflict", "ConflictLedger", "HistoryIndex",
    "EvidenceBundle", "build_evidence", "fusion_aggregate",
    "render_evidence_markdown", "render_digest", "DEFAULT_AUTHORITY",
]

SKILL_ROOT = Path(__file__).resolve().parent.parent
FUSION_DIR = SKILL_ROOT / "data" / "fusion"
CONFLICT_DIR = SKILL_ROOT / "data" / "conflicts"

DEFAULT_AUTHORITY = dict(DEFAULT_SOURCE_AUTHORITY)
UNKNOWN_AUTHORITY = 0.5

# 半衰期：72 小时后权威权重减半（新闻类信息衰减快）
FRESHNESS_HALFLIFE_HOURS = 72.0
# 相对分歧超过该比例即视为"数值打架"，需要人工/模型复核
DIVERGENCE_THRESHOLD = 0.15
# 同簇判定的标题相似度下限
CLUSTER_SIMILARITY = 0.62
CLUSTER_WINDOW_HOURS = 48
# 同主体时，正文 n-gram 重合度达到此值即视为同一事件
CONTENT_JACCARD = 0.18
# 同主体且事件类别相同时，改写稿的用词重合度可以低到多少仍算同一事件
KIND_MIN_JACCARD = 0.05
# 单一来源证据包的置信度上限
SINGLE_SOURCE_CAP = 0.45

_RE_INT_LIKE = re.compile(r"-?\d+(?:\.\d+)?")
_RE_AMOUNT = re.compile(r"(-?\d+(?:\.\d+)?)\s*(亿|万|千|百)?\s*(元|人民币|RMB|USD|美元)?")
_RE_PCT = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_RE_CODE = re.compile(r"\b(\d{6})\b")
# 机构名：{1,16} 而非 v8 的 {2,12}，否则"某银行"这类单字前缀整体漏检。
_RE_ORG = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9（）()]{1,16}?"
                     r"(?:银行|证券|保险|基金|信托|期货|租赁|集团|股份公司|股份|公司|券商|资管|金控)")
# "国家金融监管总局对某银行" 这类把监管方拼进来的匹配，切连接词取末段才是当事主体
_RE_CONNECTOR = re.compile(r"[对与和及被向给将让从由在中的]")
# 标题开头的媒体署名。必须先从标题里剥掉，否则 "上海证券报：…" 会被抽成 "上海证券"，
# 于是同一事件的报道因为一家媒体带了署名而聚不进同一簇。
_RE_MEDIA_PREFIX = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9]{2,14}?"
    r"(?:日报|周报|周刊|客户端|APP|App|报|社|网|台|新闻|资讯|财经|频道|一线|观察)"
    r"\s*[:：]\s*"
)
# 段首带这些动词说明它不是署名而是正文主语（"某银行发布三季报："），剥了就丢信息
_RE_TITLE_VERB = re.compile(r"发布|披露|公告|收到|领涨|上涨|下跌|增长|下滑|作出|通报|拟|计划")
# 没有企业后缀的简称（"宁德时代发布三季报"）抓不到主体，但动词前的短主语就是当事方。
# 限长 + 禁标点，免得把整句标题当成主体。
_RE_TITLE_SUBJECT = re.compile(
    r"^([\u4e00-\u9fa5A-Za-z0-9]{2,12}?)(?:股份|公司|集团)?"
    r"(?:发布|披露|公告|回应|表示|称|拟|宣布|推出|完成|收到|被|因)")

# 事件类别：同一主体同日可能有多件不同性质的新闻，只靠"主体相同"会误并。
# 类别相同 + 主体相同 才是"各家媒体报道同一件事"的可靠信号。
_EVENT_KINDS = (
    ("penalty", r"罚款|罚单|罚没|处罚|处分|警示函|监管措施|纪律处分|责令改正"),
    ("risk", r"退市|立案|被查|调查|违约|爆雷|风险警示|暂停|吊销"),
    ("lawsuit", r"诉讼|起诉|仲裁|被判|赔偿|强制执行"),
    ("dividend", r"分红|派息|送转|派发"),
    ("earnings", r"净利润|营收|营业收入|三季报|中报|年报|半年报|季报|业绩|利润|亏损"
                 r"|年度报告|半年度报告|季度报告|一季度|二季度|三季度|四季度"),
    ("financing", r"定增|配股|可转债|上市|募资|首发|IPO|新股"),
    ("shareholding", r"增持|减持|回购|举牌|股权转让|解禁"),
    ("approval", r"获批|核准|注册生效|过会|中标|资质"),
    ("personnel", r"董事长|总经理|任免|辞职|接任|高管|选举"),
    ("market", r"涨停|跌停|涨幅|跌幅|龙虎榜|封板|主力净流入"),
)

# 正文数值口径表：越具体的写法排在越前面，同一位置只认最长的那个词。
# 键名与 _NUMERIC_FIELD_ALIASES 的规范名对齐，这样"结构化字段"和"正文抽取"
# 能并进同一个 ReconciledFact 里比。
_TEXT_METRICS: Tuple[Tuple[str, str], ...] = (
    ("net_profit", r"归属母公司净利润|扣非净利润|归属净利润|净利润|净利|利润"),
    ("revenue", r"营业总收入|营业收入|营收|总收入"),
    ("total_assets", r"资产总额|总资产"),
    ("aum", r"管理资产规模|管理规模|资管规模|资产规模|托管规模"),
    ("penalty", r"罚款金额|罚没款|罚没|罚款|罚单"),
    ("dividend", r"派息|分红|股息"),
    ("shareholder_count", r"股东户数|户数"),
    ("nav", r"单位净值|累计净值|净值"),
    ("amount", r"成交额|成交金额"),
    ("volume", r"成交量"),
)
_TEXT_PCT_METRICS: Tuple[Tuple[str, str], ...] = (
    ("change_pct", r"涨跌幅|涨幅|跌幅|同比|环比|上涨|下跌|增长|下滑|下降"),
    ("margin", r"毛利率|净利率|利润率"),
)
# 口径词一般在数字前面，12 个字够覆盖"实现营业收入约 120 亿元"这类插入
_LABEL_WINDOW = 12
# "负债率 43.2%"、"不良率 1.8%" 这类比率各自成口径，不能并成一个 pct 去比大小
_RE_RATE_LABEL = re.compile(r"[\u4e00-\u9fa5]{2,6}率\s*$")

# 各家 scraper / MCP 工具的字段别名 → 规范名。缺这张表是"汇总"做不动的第一号原因。
FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "title": ("title", "article_title", "name", "heading", "subject", "标题", "news_title"),
    "url": ("url", "link", "href", "source_url", "page_url", "网址", "链接"),
    "source": ("source", "media", "site", "site_name", "source_name", "origin",
               "媒体", "来源", "站点"),
    "published_at": ("published_at", "published", "publishDate", "published_date",
                     "publish_time", "release_time", "pubtime", "time", "date",
                     "datetime", "created_at", "updated_at", "first_release_time",
                     "发布时间", "发布日期", "日期", "时间"),
    "content": ("content", "text", "body", "summary", "abstract", "description",
                "digest", "正文", "摘要", "内容"),
    "sentiment": ("sentiment", "sentiment_label", "polarity", "情感", "倾向"),
    "score": ("score", "sentiment_score", "weight", "热度", "评分"),
    "entity": ("entity", "institution", "org", "organization", "target", "company",
               "机构", "对象", "公司"),
    "code": ("code", "stock_code", "symbol", "ts_code", "证券代码"),
}

_NUMERIC_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "net_profit": ("net_profit", "net_income", "净利润", "归属净利润"),
    "revenue": ("revenue", "operating_revenue", "营业收入", "营收", "总收入"),
    "total_assets": ("total_assets", "资产总额", "总资产"),
    "aum": ("aum", "scale", "fund_scale", "管理规模", "规模", "净资产规模"),
    "nav": ("nav", "unit_nav", "dwjz", "单位净值", "净值"),
    "change_pct": ("change_pct", "pct_change", "涨跌幅", "涨幅", "日涨幅"),
    "amount": ("amount", "turnover_value", "成交额", "成交金额"),
    "volume": ("volume", "vol", "成交量"),
    "penalty": ("penalty", "fine", "罚款", "罚没金额"),
    "shareholder_count": ("shareholder_count", "股东户数"),
}


# ─── 记录归一 ────────────────────────────────────────────────────────────────

def _pick(raw: Dict[str, Any], key: str) -> Any:
    for alias in FIELD_ALIASES.get(key, (key,)):
        val = raw.get(alias)
        if val not in (None, "", [], {}):
            return val
    return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    m = _RE_INT_LIKE.search(text)
    if not m:
        return None
    num = float(m.group())
    if "%" in text:
        return num
    unit = _unit_scale(text)
    return num * unit if unit else num


def _unit_scale(text: str) -> float:
    """把 12.3亿 / 4500万 归一到同一量纲（元）。"""
    if "亿" in text:
        return 1e8
    if "万" in text:
        return 1e4
    if "千" in text:
        return 1e3
    return 1.0


def _hash64(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def content_hash(title: str, entity: str = "", published: str = "") -> str:
    """内容指纹：标题规范化 + 实体 + 到日粒度。跨批次去重靠它。

    日期必须先解析再格式化。`2026/10/01 10:00` 和 `2026-10-01T10:00:00` 是同一天，
    直接截前 10 个字符会得到两个指纹，同一篇稿子被当成两条独立记录。
    """
    canon = _normalize_event_title(title or "")
    if not canon:
        canon = re.sub(r"\s+", "", str(title or ""))[:60]
    ts = _parse_ts(published)
    day = ts.strftime("%Y-%m-%d") if ts else str(published or "")[:10]
    key = f"{canon}|{entity}|{day}"
    return _hash64(key)


def _metric_of_prefix(text: str, start: int,
                      table: Tuple[Tuple[str, str], ...]) -> str:
    """数字前一小段窗口里最近的口径词；没有就返回空串。"""
    window = text[max(0, start - _LABEL_WINDOW):start]
    best_metric, best_end = "", -1
    for metric, pat in table:
        for m in re.finditer(pat, window):
            if m.end() > best_end:
                best_metric, best_end = metric, m.end()
    return best_metric


def extract_facts(text: str) -> Dict[str, List[float]]:
    """从正文抽数值线索，按**口径**归类。

    认得出口径（净利润 / 营收 / 罚款……）的挂到口径名下，认不出的进
    `amount_unlabeled` / `pct_unlabeled`。这决定了两个数字能不能放在一起比：
    把一条报道里的 80 亿净利润和 120 亿营收并成同一个字段，等于凭空造出一场
    "分歧"。裸数字（年份、届次、排名）没有量纲也没有口径，直接不收。
    """
    out: Dict[str, List[float]] = {}
    if not text:
        return out
    for m in _RE_AMOUNT.finditer(text):
        unit, currency = m.group(2) or "", m.group(3) or ""
        metric = _metric_of_prefix(text, m.start(), _TEXT_METRICS)
        if not unit and not currency and not metric:
            continue
        scale = {"亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2}.get(unit, 1.0)
        out.setdefault(metric or "amount_unlabeled", []).append(float(m.group(1)) * scale)
    for m in _RE_PCT.finditer(text):
        metric = _metric_of_prefix(text, m.start(), _TEXT_PCT_METRICS)
        if not metric:
            head = text[max(0, m.start() - 8):m.start()]
            rm = _RE_RATE_LABEL.search(head)
            metric = rm.group().strip() if rm else "pct_unlabeled"
        out.setdefault(metric, []).append(float(m.group(1)))
    return out


def _guess_sentiment(text: str) -> str:
    """轻量倾向判定，仅供缺字段时兜底；正式倾向应由上层模型给。"""
    neg = len(re.findall(r"处罚|罚款|违规|亏损|下调|退市|诉讼|被查|警示|贬|下跌|风险", text or ""))
    pos = len(re.findall(r"增长|创新高|获批|中标|增持|回购|上调|盈利|突破|利好|分红", text or ""))
    if neg and neg > pos:
        return "negative"
    if pos and pos > neg:
        return "positive"
    return "neutral"


# "年报/季报/财报"里的"报"不是媒体署名。不排掉它，"某公司年报："会被整段当署名
# 剥掉，当事主体随之抽不出来，同一件事的报道也就聚不进一簇。
_RE_REPORT_TAIL = re.compile(
    r"(?:年报|中报|半年报|季报|财报|月报|快报|研报|通报|预告|报告)$")


def _title_for_entity(title: str, source: str = "") -> str:
    """抽主体/比对标题专用的清洗：先剥来源前缀，再剥媒体署名。"""
    canon = _normalize_event_title(title or "")
    if source and canon.startswith(source):
        tail = re.sub(r"^[:：\-—|｜\s]+", "", canon[len(source):])
        if tail:
            canon = tail
    m = _RE_MEDIA_PREFIX.match(canon)
    if m and not _RE_TITLE_VERB.search(m.group(0)):
        head = m.group(0).rstrip("：:  \t")
        if not _RE_REPORT_TAIL.search(head):
            canon = canon[m.end():]
    return canon or (title or "")


def event_kind_of(title: str = "", content: str = "") -> str:
    """事件类别（penalty/earnings/…），抽不到返回空串。用于区分同主体的不同事件。"""
    text = f"{title or ''}{content or ''}"
    for kind, pat in _EVENT_KINDS:
        if re.search(pat, text):
            return kind
    return ""


def entity_of(title: str = "", content: str = "", explicit: str = "",
              code: str = "", source: str = "") -> str:
    """当事主体。标题优先于正文，显式字段优先于一切，最后才退到代码。"""
    if explicit:
        cand = str(explicit).strip()
    elif code:
        return f"code:{code}"
    else:
        cand = ""
        for text in (_title_for_entity(title, source), (content or "")[:300]):
            m = _RE_ORG.search(text or "")
            if m:
                cand = m.group()
                break
        if not cand:
            m = _RE_TITLE_SUBJECT.match(_title_for_entity(title, source))
            if m:
                cand = m.group(1)
    if not cand:
        return ""
    parts = [p for p in _RE_CONNECTOR.split(cand) if p]
    cand = parts[-1] if parts else cand
    cand = cand.strip("的了等")
    return cand if len(cand) >= 3 else (cand if _RE_ORG.fullmatch(cand) else "")


def normalize_record(raw: Dict[str, Any], source_hint: str = "",
                     batch: str = "") -> Dict[str, Any]:
    """任意来源的一条记录 → 规范记录。缺字段不报错，只留空并标 missing。"""
    if not isinstance(raw, dict):
        raw = {"title": str(raw)}
    title = str(_pick(raw, "title") or "").strip()
    url = str(_pick(raw, "url") or "").strip()
    source = str(_pick(raw, "source") or source_hint or "").strip()
    published = str(_pick(raw, "published_at") or "").strip()
    content = str(_pick(raw, "content") or "").strip()
    code = str(_pick(raw, "code") or "").strip()
    if not code:
        cm = _RE_CODE.search(title + " " + content[:200])
        code = cm.group(1) if cm else ""
    entity = entity_of(title=title, content=content,
                       explicit=str(_pick(raw, "entity") or ""), code=code,
                       source=source)
    ts = _parse_ts(published) if published else None
    story = content_hash(title, entity, published)

    sentiment = str(_pick(raw, "sentiment") or "").strip().lower()
    if sentiment not in ("positive", "negative", "neutral"):
        sentiment = _guess_sentiment(title + " " + content)

    facts: Dict[str, float] = {}
    for canon, aliases in _NUMERIC_FIELD_ALIASES.items():
        for alias in aliases:
            if alias in raw:
                val = _as_float(raw[alias])
                if val is not None:
                    facts[canon] = val
                    break

    missing = [k for k, v in (("title", title), ("source", source),
                              ("published_at", published), ("url", url)) if not v]

    return {
        # story = 同一件事的指纹（标题+实体+到日），record_id 还要带上网址：
        # 三家媒体用同一个标题各发一篇时，story 相同、record 不同。少了网址，
        # 共识环节会把"1200 亿 / 1210 亿"当成同一篇稿子只计一次，分歧就此消失。
        "record_id": story if not url else _hash64(f"{story}|{url}"),
        "story_hash": story,
        "content_hash": story,
        "title": title,
        "canonical_title": _normalize_event_title(_title_for_entity(title, source)),
        "url": url, "source": source, "published_at": published,
        "ts": ts.timestamp() if ts else None,
        "ts_iso": ts.isoformat(timespec="seconds") if ts else "",
        "content": content[:1200],
        "sentiment": sentiment,
        "score": _as_float(_pick(raw, "score")),
        "entity": entity, "code": code,
        "event_kind": event_kind_of(title, content),
        "facts": facts,
        # 标题里常挂着头条数字（"净利润 95 亿"），只看正文会漏掉最强的一条线索
        "text_facts": extract_facts(f"{title} {content}".strip()),
        "authority": DEFAULT_AUTHORITY.get(source, UNKNOWN_AUTHORITY),
        "batch": batch,
        "missing": missing,
        "raw": raw,
    }


# ─── 事件簇 ──────────────────────────────────────────────────────────────────

@dataclass
class EventCluster:
    cluster_id: str
    representative: Dict[str, Any]
    records: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def sources(self) -> List[str]:
        seen: List[str] = []
        for r in self.records:
            if r["source"] and r["source"] not in seen:
                seen.append(r["source"])
        return seen

    @property
    def time_span(self) -> Tuple[str, str]:
        stamps = sorted(r["ts_iso"] for r in self.records if r["ts_iso"])
        return (stamps[0], stamps[-1]) if stamps else ("", "")

    def to_dict(self) -> Dict[str, Any]:
        first, last = self.time_span
        return {
            "cluster_id": self.cluster_id,
            "title": self.representative["title"],
            "entity": self.representative.get("entity", ""),
            "n_records": len(self.records),
            "n_sources": len(self.sources),
            "sources": self.sources,
            "first_seen": first, "last_seen": last,
            "sentiments": sorted({r["sentiment"] for r in self.records}),
        }


def _blocking_keys(rec: Dict[str, Any]) -> List[str]:
    """候选分桶键。同一记录可以同时进"实体桶"和"日期桶"——
    实体抽取有噪声，只按实体分桶会让抽取失败的记录彻底孤立。"""
    keys: List[str] = []
    ent = rec.get("entity") or ""
    if ent:
        keys.append("E:" + ent)
    # 日期桶用归一化后的 ts_iso：各家时间格式不统一，`2026/10/01` 和
    # `2026-10-01T…` 截前 10 位是两个桶，同一天的报道连比对机会都没有。
    day = (rec.get("ts_iso") or rec.get("published_at") or "")[:10]
    if day:
        keys.append("D:" + day)
    if rec.get("url"):
        keys.append("U:" + rec["url"])
    return keys or ["@"]


def _tokens(rec: Dict[str, Any]) -> set:
    """正文 + 规范化标题的中文 n-gram 集合；正文比标题更能识别同一事件。

    纯数字 token 被剔除：各家媒体写的金额本来就常常打架，
    把它们算进相似度只会让"同一事件的不同报道"看起来更不像同一件事。
    数值分歧交给 reconcile_cluster 单独量化，不掺进聚类判定。
    同理只保留 2/3-gram：整段中文拼出来的长 token 跨媒体几乎不会重合，
    只会撑大并集、把 Jaccard 压低。
    """
    cached = rec.get("_tok")
    if cached is not None:
        return cached
    text = f"{rec.get('content') or ''} {rec.get('canonical_title') or ''}"
    toks = {t for t in _tokenize_zh(text) if not t.isdigit() and len(t) <= 3}
    rec["_tok"] = toks
    return toks


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _comparable_fields(rec: Dict[str, Any]) -> set:
    """这条记录报了哪些**口径**的数。

    聚类要比的是口径集合而不是数值本身：两家写的净利润不一样，正说明它们在
    说同一件事——数值打架是分歧，不是两个事件。认不出口径的裸数字不算口径。
    """
    out = set(rec.get("facts") or {})
    out |= {k for k in (rec.get("text_facts") or {}) if not k.endswith("_unlabeled")}
    return out


def _same_event(a: Dict[str, Any], b: Dict[str, Any], sim: float,
                window_hours: int) -> Tuple[bool, str]:
    """事件同一性判定。

    纯标题编辑距离**不足以**判定同一事件：不同媒体对同一事件的标题改写，
    实测相似度只有 0.2~0.3。所以这里用复合证据——同主体 + 同类数值字段，
    或同主体 + 正文高度重合——并把命中的理由带回去，供结果自解释。

    注意"同主体 + 同类数值"会故意把**数值打架**的记录并进同一簇：
    分歧本身就是要暴露的东西，不该因为两家写的不一样就判成两件事。
    """
    if a.get("url") and a["url"] == b.get("url"):
        return True, "same_url"
    if a.get("record_id") and a["record_id"] == b.get("record_id"):
        return True, "same_hash"
    ta, tb = a.get("ts"), b.get("ts")
    if ta and tb and abs(ta - tb) > window_hours * 3600:
        return False, "out_of_window"
    ca, cb = a.get("canonical_title") or "", b.get("canonical_title") or ""
    tsim = _title_similarity(ca, cb) if ca and cb else 0.0
    if tsim >= sim:
        return True, f"title:{tsim:.2f}"
    ent_match = bool(a.get("entity")) and a["entity"] == b.get("entity")
    shared_facts = sorted(_comparable_fields(a) & _comparable_fields(b))
    jac = _jaccard(_tokens(a), _tokens(b))
    ka, kb = a.get("event_kind") or "", b.get("event_kind") or ""
    # 同一主体同一天完全可能有两件性质不同的新闻；类别打架时不走"主体+内容"这类弱通道，
    # 只保留同 URL / 同哈希 / 标题高度相似这三条强通道。
    if ka and kb and ka != kb:
        return False, f"kind_clash:{ka}/{kb}"
    if ent_match and shared_facts:
        return True, "entity+fact:" + ",".join(shared_facts)
    if ent_match and jac >= CONTENT_JACCARD:
        return True, f"entity+content:{jac:.2f}"
    if ent_match and ka and ka == kb and jac >= KIND_MIN_JACCARD:
        return True, f"entity+kind:{ka}({jac:.2f})"
    if jac >= 0.45 and (tsim >= 0.40 or ent_match):
        return True, f"content:{jac:.2f}"
    return False, ""


def cluster_records(records: Sequence[Dict[str, Any]], *,
                    already_normalized: bool = False,
                    similarity: float = CLUSTER_SIMILARITY,
                    window_hours: int = CLUSTER_WINDOW_HOURS,
                    max_bucket: int = 120) -> List[EventCluster]:
    """把（可能是异构的）记录聚成事件簇。

    先分桶生成候选对（避免 O(n²) 全比对），再用并查集合并——
    并查集保证"A 与 B 同事件、B 与 C 同事件"能正确得到 {A,B,C}，
    而贪心逐个挂靠代表会漏掉这种传递性。
    """
    recs = [r if already_normalized and "record_id" in r else normalize_record(r)
            for r in records]
    n = len(recs)
    if n == 0:
        return []
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    buckets: Dict[str, List[int]] = {}
    for idx, rec in enumerate(recs):
        for key in _blocking_keys(rec):
            buckets.setdefault(key, []).append(idx)

    for key, idxs in buckets.items():
        if key.startswith("@"):
            continue
        if len(idxs) > max_bucket:
            # 大桶按权威度截断，避免单桶退化成平方级比对
            idxs = sorted(idxs, key=lambda i: -recs[i]["authority"])[:max_bucket]
        for i_pos in range(len(idxs)):
            for j_pos in range(i_pos + 1, len(idxs)):
                a, b = idxs[i_pos], idxs[j_pos]
                if find(a) == find(b):
                    continue
                ok, _ = _same_event(recs[a], recs[b], similarity, window_hours)
                if ok:
                    union(a, b)

    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for idx, rec in enumerate(recs):
        grouped.setdefault(find(idx), []).append(rec)

    clusters: List[EventCluster] = []
    for root, grp in grouped.items():
        rep = _pick_representative(grp)
        cid = f"{grp[0].get('entity') or 'anon'}|{root}|{rep['record_id']}"
        clusters.append(EventCluster(cluster_id=cid, representative=rep, records=grp))
    clusters.sort(key=lambda c: (-c.representative.get("authority", 0), -len(c.records)))
    return clusters


def _pick_representative(grp: List[Dict[str, Any]]) -> Dict[str, Any]:
    """代表记录：权威度优先，其次标题最长（信息量），其次最新。"""
    return max(grp, key=lambda r: (round(r["authority"], 3), len(r["title"]),
                                   r["ts"] or 0))


# ─── 数值共识与分歧 ─────────────────────────────────────────────────────────

@dataclass
class ReconciledFact:
    field: str
    consensus: Optional[float]
    n_observed: int
    min_value: Optional[float]
    max_value: Optional[float]
    spread_pct: float
    divergent: bool
    observations: List[Dict[str, Any]] = field(default_factory=list)
    method: str = "weighted_median"
    n_sources: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _weight(rec: Dict[str, Any], now_ts: Optional[float] = None) -> float:
    """权重 = 权威度 × 时效衰减。"""
    auth = rec.get("authority", UNKNOWN_AUTHORITY) or UNKNOWN_AUTHORITY
    ts = rec.get("ts")
    if not ts:
        decay = 0.6  # 无时间戳：不假装新鲜
    else:
        ref = now_ts if now_ts is not None else datetime.now().timestamp()
        hours = max(0.0, (ref - ts) / 3600.0)
        decay = 0.5 ** (hours / FRESHNESS_HALFLIFE_HOURS)
    return max(0.01, auth * (0.35 + 0.65 * decay))


def _weighted_median(pairs: Sequence[Tuple[float, float]]) -> float:
    ordered = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in ordered)
    if total <= 0:
        return ordered[len(ordered) // 2][0]
    acc = 0.0
    for v, w in ordered:
        acc += w
        if acc >= total / 2.0:
            return v
    return ordered[-1][0]


def reconcile_cluster(cluster: EventCluster, *,
                      now_ts: Optional[float] = None) -> Dict[str, ReconciledFact]:
    """对簇内每个**口径**求加权共识，并量化分歧。

    两条纪律：
    1. 一条记录在同一口径上只贡献一个观测（多次提到取中位数）。同一篇报道里
       先写预告值后写正式值，不是"两家媒体打架"。
    2. divergent 只在观测来自 ≥2 个独立来源时才为真。单源内部的高低差要在
       gaps 里说清，但不能冒充跨源分歧。

    单来源观测标 divergent=False 但 n_observed=1 —— "只有一个来源说过"
    本身就是需要告知的缺口，不能伪装成共识。
    """
    pool: Dict[str, List[Tuple[float, float, Dict[str, Any]]]] = {}
    taken: set = set()
    unlabeled: Dict[str, int] = {}

    def add(field: str, value: float, weight: float, rec: Dict[str, Any]) -> None:
        # 同一条记录在同一个口径上只贡献一次观测：结构化字段和正文抽取经常指向
        # 同一个数，重复抓取的同 URL 记录更是同一篇稿子。重复计数会把
        # "两源一致"伪装成"三源一致"。结构化字段先加，正文值让位。
        ident = (field, rec.get("record_id") or rec.get("url") or rec.get("title"))
        if ident in taken:
            return
        taken.add(ident)
        pool.setdefault(field, []).append((value, weight, rec))

    for rec in cluster.records:
        w = _weight(rec, now_ts)
        for k, v in (rec.get("facts") or {}).items():
            add(k, v, w, rec)
        for k, values in (rec.get("text_facts") or {}).items():
            if not values:
                continue
            if k.endswith("_unlabeled"):
                unlabeled[k] = unlabeled.get(k, 0) + len(values)
                continue
            # 正文抽出来的数没有结构化字段可靠，权重折半
            add(k, statistics.median(values), w * 0.5, rec)

    out: Dict[str, ReconciledFact] = {}
    for key, obs in pool.items():
        values = [v for v, _, _ in obs]
        pairs = [(v, w) for v, w, _ in obs]
        cons = _weighted_median(pairs)
        lo, hi = min(values), max(values)
        base = abs(cons) if cons else (hi - lo)
        spread = 0.0 if base == 0 else (hi - lo) / base
        sources = {r.get("source") or r.get("record_id") or "?" for _, _, r in obs}
        out[key] = ReconciledFact(
            field=key,
            consensus=round(cons, 6),
            n_observed=len(values),
            min_value=round(lo, 6), max_value=round(hi, 6),
            spread_pct=round(spread * 100.0, 2),
            divergent=spread > DIVERGENCE_THRESHOLD and len(sources) > 1,
            observations=[{"source": r.get("source", ""), "value": round(v, 6),
                           "weight": round(w, 4), "published_at": r.get("published_at", ""),
                           "record_id": r.get("record_id", "")}
                          for v, w, r in obs],
            n_sources=len(sources),
        )
    if unlabeled:
        total = sum(unlabeled.values())
        out["unlabeled_numbers"] = ReconciledFact(
            field="unlabeled_numbers", consensus=None, n_observed=total,
            min_value=None, max_value=None, spread_pct=0.0, divergent=False,
            observations=[], method="count_only", n_sources=0,
        )
    return out


# ─── 冲突台账（补 v10 从未写入的 CONFLICTS_DIR）──────────────────────────────

@dataclass
class Conflict:
    key: str
    field: str
    cluster_title: str
    values: List[Dict[str, Any]]
    spread_pct: float
    detected_at: str
    status: str = "open"
    resolution: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ConflictLedger:
    """把"谁和谁数字打架"持久化成可复盘的台账，而不是只在内存里过一遍。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else CONFLICT_DIR / "ledger.json"
        self._lock = threading.Lock()
        self._entries: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        self._entries = raw if isinstance(raw, list) else []
        return len(self._entries)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._entries, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.path)

    def record(self, cluster: EventCluster,
               facts: Dict[str, ReconciledFact]) -> List[Conflict]:
        added: List[Conflict] = []
        with self._lock:
            known = {(e.get("key"), e.get("field")) for e in self._entries}
            for fname, fact in facts.items():
                if not fact.divergent:
                    continue
                key = f"{cluster.cluster_id}#{fname}"
                if (key, fname) in known:
                    continue
                c = Conflict(
                    key=key, field=fname, cluster_title=cluster.representative["title"],
                    values=fact.observations, spread_pct=fact.spread_pct,
                    detected_at=datetime.now().isoformat(timespec="seconds"),
                )
                self._entries.append(c.to_dict())
                added.append(c)
            if added:
                self._save()
        return added

    def open_conflicts(self, field: str = "") -> List[Dict[str, Any]]:
        return [e for e in self._entries
                if e.get("status") == "open" and (not field or e.get("field") == field)]

    def resolve(self, key: str, resolution: str = "") -> bool:
        """结案，只对 open 生效。

        已经结过案的条目再 resolve 一次返回 False：台账是"这条分歧最终按什么定"
        的唯一答案，允许第二次写入就等于让后一次口径覆盖前一次，审计线断了。
        """
        with self._lock:
            for e in self._entries:
                if e.get("key") == key and e.get("status") == "open":
                    e["status"] = "resolved"
                    e["resolution"] = resolution
                    e["resolved_at"] = datetime.now().isoformat(timespec="seconds")
                    self._save()
                    return True
        return False

    def stats(self) -> Dict[str, Any]:
        by_field: Dict[str, int] = {}
        for e in self._entries:
            by_field[e.get("field", "?")] = by_field.get(e.get("field", "?"), 0) + 1
        return {"total": len(self._entries),
                "open": sum(1 for e in self._entries if e.get("status") == "open"),
                "resolved": sum(1 for e in self._entries if e.get("status") == "resolved"),
                "by_field": by_field, "path": str(self.path)}

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save()


class HistoryIndex:
    """内容哈希跨批次账本：v10 里 content_hash 算了却从不与历史比对。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else FUSION_DIR / "history.json"
        self._index: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> int:
        if not self.path.exists():
            return 0
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        self._index = raw if isinstance(raw, dict) else {}
        return len(self._index)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._index, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _seen_key(rec: Dict[str, Any]) -> str:
        # 旧闻账本按"同一件事"记账，不按 URL：同一篇稿子被三家转载就是一件事，
        # 换 URL 再抓一遍也算见过。record_id 带网址，用它会把转载当成新消息。
        return (rec.get("story_hash") or rec.get("content_hash")
                or rec.get("record_id") or content_hash(rec.get("title", "")))

    def seen_before(self, record_id: str) -> bool:
        return record_id in self._index

    def observe(self, records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        """登记一批记录，返回新增/重复计数。"""
        fresh = dup = 0
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            for rec in records:
                rid = self._seen_key(rec)
                hit = self._index.get(rid)
                if hit:
                    hit["count"] = int(hit.get("count", 1)) + 1
                    hit["last_seen"] = now
                    dup += 1
                else:
                    self._index[rid] = {"first_seen": now, "last_seen": now, "count": 1,
                                        "title": (rec.get("title") or "")[:80],
                                        "source": rec.get("source", "")}
                    fresh += 1
            self._save()
        return {"new": fresh, "duplicate": dup, "total_indexed": len(self._index)}

    def split_new(self, records: Sequence[Dict[str, Any]]
                  ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        with self._lock:
            new = [r for r in records if self._seen_key(r) not in self._index]
            old = [r for r in records if self._seen_key(r) in self._index]
        return new, old

    def stats(self) -> Dict[str, Any]:
        return {"indexed": len(self._index), "path": str(self.path)}

    def clear(self) -> None:
        with self._lock:
            self._index = {}
            self._save()


# ─── 证据包 ──────────────────────────────────────────────────────────────────

@dataclass
class EvidenceBundle:
    """给上层模型"照着写就不会编"的结构：每条结论都挂着出处、分歧和缺口。"""

    cluster_id: str
    statement: str
    entity: str
    confidence: float
    sentiment: str                        # dominant：positive / negative / neutral
    sentiment_detail: Dict[str, Any]      # counts / contradictory_ratio / weighted
    facts: Dict[str, Any]
    disagreements: List[Dict[str, Any]]
    citations: List[Dict[str, Any]]
    freshness: Dict[str, Any]
    gaps: List[str]
    supporting_sources: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _sentiment_view(cluster: EventCluster) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for r in cluster.records:
        counts[r["sentiment"]] = counts.get(r["sentiment"], 0) + 1
    total = sum(counts.values()) or 1
    dominant = max(counts, key=lambda k: counts[k])
    opposed = total - counts[dominant]
    return {"counts": counts, "dominant": dominant,
            "contradictory_ratio": round(opposed / total, 3),
            "weighted": _weighted_sentiment(cluster)}


def _weighted_sentiment(cluster: EventCluster) -> float:
    """-1..1 的加权倾向：只用于排序，不用于下结论。"""
    sign = {"positive": 1.0, "negative": -1.0, "neutral": 0.0}
    num = den = 0.0
    for r in cluster.records:
        w = _weight(r)
        num += sign.get(r["sentiment"], 0.0) * w
        den += w
    return round(num / den, 4) if den else 0.0


def _confidence(cluster: EventCluster,
                now_ts: Optional[float] = None) -> Tuple[float, List[str]]:
    """置信度 = 独立来源数 × 权威度 × 时效 × 一致性。不假装是概率。"""
    gaps: List[str] = []
    srcs = cluster.sources
    n_src = len(srcs)
    if n_src == 0:
        gaps.append("无任何具名来源")
    src_score = min(1.0, 0.45 + 0.18 * n_src) if n_src else 0.2
    auth = statistics.mean([r["authority"] for r in cluster.records]) if cluster.records else UNKNOWN_AUTHORITY
    recency = max((r["ts"] or 0) for r in cluster.records) if cluster.records else 0
    if not recency:
        recency_score = 0.5
        gaps.append("缺少发布时间，无法判断时效")
    else:
        days = ((now_ts or datetime.now().timestamp()) - recency) / 86400.0
        recency_score = max(0.2, 0.5 ** (days / 14.0))
        if days > 30:
            gaps.append(f"最新记录已是 {round(days)} 天前")
    senti = _sentiment_view(cluster)
    agree_score = 1.0 - 0.5 * senti["contradictory_ratio"]
    if senti["contradictory_ratio"] > 0:
        gaps.append(f"{round(senti['contradictory_ratio'] * 100)}% 的记录倾向与主流相反")
    conf = src_score * (0.5 + 0.5 * auth) * recency_score * agree_score
    if n_src == 1:
        # 单源未经交叉验证，无论那一家多权威都不该读起来像"基本属实"，
        # 否则下游模型会把一条自媒体转发当成已确认事实。
        conf = min(conf, SINGLE_SOURCE_CAP)
        gaps.append("仅单一来源，未获交叉验证")
    urls = {r["url"] for r in cluster.records if r["url"]}
    if len(urls) < len(cluster.records) and cluster.records:
        gaps.append("存在同 URL 重复抓取，记录数不等于独立报道数")
    return round(max(0.0, min(1.0, conf)), 3), gaps


def build_evidence(cluster: EventCluster, *, ledger: Optional[ConflictLedger] = None,
                   now_ts: Optional[float] = None) -> EvidenceBundle:
    facts = reconcile_cluster(cluster, now_ts=now_ts)
    if ledger is not None:
        ledger.record(cluster, facts)
    conf, gaps = _confidence(cluster, now_ts)
    senti = _sentiment_view(cluster)
    first, last = cluster.time_span
    cited: List[Dict[str, Any]] = []
    seen: set = set()
    ordered = sorted(cluster.records, key=lambda r: (-r["authority"], r["published_at"]))
    for rec in ordered:
        fp = rec["url"] or rec["title"]
        if fp in seen:
            continue
        seen.add(fp)
        cited.append({
            "ref": f"S{len(cited) + 1}", "source": rec["source"] or "未知来源",
            "title": rec["title"], "url": rec["url"],
            "published_at": rec["published_at"], "authority": round(rec["authority"], 3),
            "sentiment": rec["sentiment"], "record_id": rec["record_id"],
            "missing_fields": rec["missing"],
        })
    disagree = [{"field": f.field, "spread_pct": f.spread_pct,
                 "consensus": f.consensus, "range": [f.min_value, f.max_value],
                 "n_observed": f.n_observed, "n_sources": f.n_sources,
                 "by_source": {o["source"]: o["value"] for o in f.observations},
                 "record_ids": sorted({o["record_id"] for o in f.observations})}
                for f in facts.values() if f.divergent]
    thin = [f for f in facts.values()
            if not f.divergent and f.n_observed == 1 and f.method != "count_only"]
    for f in thin:
        gaps.append(f"数值 {f.field} 只有 1 个来源给出，未经复核")
    for f in facts.values():
        if f.divergent:
            gaps.append(
                f"数值 {f.field} 各源相差 {f.spread_pct}%（{f.min_value} ~ {f.max_value}），"
                f"共识值仅按权威度加权，需回原始出处核对"
            )
    unlabeled = facts.get("unlabeled_numbers")
    if unlabeled and unlabeled.n_observed:
        gaps.append(f"正文另有 {unlabeled.n_observed} 个认不出口径的数字，未参与共识")
    return EvidenceBundle(
        cluster_id=cluster.cluster_id,
        statement=cluster.representative["title"],
        entity=cluster.representative.get("entity", ""),
        confidence=conf,
        sentiment=senti["dominant"],
        sentiment_detail=senti,
        facts={k: v.to_dict() for k, v in facts.items()},
        disagreements=disagree,
        citations=cited,
        freshness={"first_seen": first, "last_seen": last,
                   "n_records": len(cluster.records),
                   "stale": bool(last) and (_hours_ago(last, now_ts) > 168)},
        gaps=sorted(set(gaps)),
        supporting_sources=cluster.sources,
    )


def _hours_ago(iso: str, now_ts: Optional[float] = None) -> float:
    ts = _parse_ts(iso)
    if not ts:
        return 9e9
    ref = datetime.fromtimestamp(now_ts) if now_ts else datetime.now()
    return (ref - ts).total_seconds() / 3600.0


# ─── 一站式汇总 ──────────────────────────────────────────────────────────────

def fusion_aggregate(records: Sequence[Dict[str, Any]], *,
                     use_ledger: bool = False,
                     use_history: bool = False,
                     top: int = 20,
                     ledger: Optional[ConflictLedger] = None,
                     history: Optional[HistoryIndex] = None,
                     **kw: Any) -> Dict[str, Any]:
    """把一批异构记录变成"有结构、有出处、有分歧、有缺口"的汇总结果。

    use_ledger / use_history 默认 False：MCP 只读调用不应悄悄写盘。
    """
    recs = [r if "record_id" in r else normalize_record(r) for r in records]
    clusters = cluster_records(recs, already_normalized=True, **kw)
    led = ledger if use_ledger else None
    if use_ledger and led is None:
        led = ConflictLedger()
    bundles = [build_evidence(c, ledger=led).to_dict() for c in clusters]
    bundles.sort(key=lambda b: (-b["confidence"], -len(b["citations"])))
    counted = history.observe(recs) if (use_history and history) else None
    senti_total: Dict[str, int] = {}
    for r in recs:
        senti_total[r["sentiment"]] = senti_total.get(r["sentiment"], 0) + 1
    return {
        "input_records": len(recs),
        "clusters": len(clusters),
        "unique_sources": len({r["source"] for r in recs if r["source"]}),
        "multi_source_clusters": sum(1 for c in clusters if len(c.sources) > 1),
        "sentiment_distribution": senti_total,
        "conflicts": sum(len(b["disagreements"]) for b in bundles),
        "ledger": led.stats() if led else None,
        "history": counted if counted is not None else (history.stats() if history else None),
        "top_evidence": bundles[:max(1, top)],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _fmt_counts(counts: Dict[str, int]) -> str:
    """{'neutral': 3, 'positive': 1} → 'neutral×3、positive×1'。

    摘要里直接 print 一个 dict 的 repr，读起来像调试输出而不是结论。"""
    if not counts:
        return "无"
    return "、".join(f"{k}×{v}" for k, v in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def render_evidence_markdown(bundle: Dict[str, Any]) -> str:
    """单个证据包 → Markdown（引用编号与 citations 一一对应）。"""
    lines = [f"## {bundle.get('statement', '')}", ""]
    lines.append(f"- 置信度：{bundle.get('confidence')}（{len(bundle.get('citations', []))} 条独立引用）")
    if bundle.get("entity"):
        lines.append(f"- 主体：{bundle['entity']}")
    senti = bundle.get("sentiment_detail") or {}
    lines.append(f"- 倾向：{bundle.get('sentiment')}（{_fmt_counts(senti.get('counts'))}，"
                 f"相反倾向 {round((senti.get('contradictory_ratio') or 0) * 100)}%，"
                 f"加权 {senti.get('weighted')}）")
    fresh = bundle.get("freshness", {})
    lines.append(f"- 时间跨度：{fresh.get('first_seen', '未知')} → {fresh.get('last_seen', '未知')}"
                 + ("（已陈旧）" if fresh.get("stale") else ""))
    facts = bundle.get("facts", {})
    numeric = {k: v for k, v in facts.items() if v.get("method") != "count_only"}
    if numeric:
        lines += ["", "### 数值"]
        for name, f in sorted(numeric.items()):
            if f.get("divergent"):
                flag = "⚠ 跨源分歧"
            elif f.get("n_observed") == 1:
                flag = "（单源）"
            elif f.get("n_sources", 1) == 1:
                flag = "（同源多次）"
            else:
                # 观测数 > 1 但来自多家媒体，是相互印证，不是"同一篇稿子被数了一遍"
                flag = f"（{f.get('n_sources')} 源一致）"
            lines.append(f"- {name}：{f.get('consensus')} 极差 {f.get('spread_pct')}% "
                         f"[{f.get('min_value')}, {f.get('max_value')}] "
                         f"观测 {f.get('n_observed')} 次 / {f.get('n_sources')} 源 {flag}")
    if facts.get("unlabeled_numbers", {}).get("n_observed"):
        lines.append(f"- 另有 {facts['unlabeled_numbers']['n_observed']} 个认不出口径的数字未参与共识")
    if bundle.get("disagreements"):
        lines += ["", "### 分歧"]
        for d in bundle["disagreements"]:
            who = "，".join(f"{k}={v}" for k, v in list(d["by_source"].items())[:6])
            lines.append(f"- {d['field']}：极差 {d['spread_pct']}%（{who}）")
    if bundle.get("gaps"):
        lines += ["", "### 缺口 / 需复核"]
        lines += [f"- {g}" for g in bundle["gaps"]]
    lines += ["", "### 出处"]
    for c in bundle.get("citations", []):
        url = c.get("url") or "（无链接）"
        lines.append(f"- [{c['ref']}] {c.get('source')}：{c.get('title')} "
                     f"{c.get('published_at') or ''} | {url}")
    return "\n".join(lines)


def render_digest(result: Dict[str, Any], *, max_items: int = 5,
                  style: str = "brief") -> str:
    """分层摘要：结论级 → 证据级 → 异常级。style: brief | full。"""
    head = [
        f"# 汇总（输入 {result.get('input_records')} 条 → "
        f"{result.get('clusters')} 个事件簇 / "
        f"{result.get('unique_sources')} 个独立来源）", ""
    ]
    multi = result.get("multi_source_clusters", 0)
    head.append(f"- 经多源交叉验证的事件：{multi} 个；"
                f"单源未验证事件：{result.get('clusters', 0) - multi} 个")
    if result.get("conflicts"):
        head.append(f"- 检出数值分歧：{result['conflicts']} 处")
    head.append(f"- 倾向分布：{_fmt_counts(result.get('sentiment_distribution'))}")
    if style == "brief":
        head.append("")
        for b in (result.get("top_evidence") or [])[:max_items]:
            mark = "✅" if b["confidence"] >= 0.6 else ("△" if b["confidence"] >= 0.35 else "❓")
            head.append(f"{mark} **{b['statement']}** "
                        f"(置信 {b['confidence']}，来源 {len(b['supporting_sources'])})")
            if b["gaps"]:
                head.append(f"   待补：{'; '.join(b['gaps'][:2])}")
        return "\n".join(head)
    body = [render_evidence_markdown(b) for b in (result.get("top_evidence") or [])[:max_items]]
    return "\n".join(head + ["", "---", ""] + ["\n\n".join(body)])
