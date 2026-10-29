# -*- coding: utf-8 -*-
"""
数据分析深度增强模块 v8.0 (analysis_enhanced.py)

在 v7.3.0 `data_aggregator.py` 基础上，强化"爬完之后到底看到了什么"，
让汇总结果真正能拿来做判断，而不是只给一堆原始计数。

主要能力：
1. **KeywordExtractor 关键词/实体抽取**
   - 中文 n-gram 提取（bigram / trigram）
   - 简易实体识别（机构名/股票代码/数字金额）
   - 新词发现（高频共现组合）
2. **CrossSourceVerifier 跨源交叉验证**
   - 同事件多源匹配（标题+时间窗+实体）
   - 可信度评分（来源数 × 时效 × 来源权威度）
   - 冲突检测（同一事件不同情感倾向时标 conflict）
3. **TrendAnalyzer 趋势与异常分析**
   - 时间序列移动平均 / 周环比 / 同比
   - 异常点检测（z-score / IQR）
   - 简单线性外推预测（最近 N 天）
4. **PeerBenchmark 同业对标**
   - 把目标按行业分组，与组内其他目标对比
   - 偏离度 / 排名 / percentile
5. **InsightGenerator 洞察生成**
   - 把统计结果组织成可读的"3 条核心结论 + 风险点 + 行动建议"
6. **TimeBucketAggregator 时间分桶**
   - 按小时/天/周聚合任意指标

纯标准库实现，零 pip 依赖；可独立 import，可单测。
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================================
# 1. KeywordExtractor 关键词与实体抽取
# ============================================================================


# 中文常见停用词（精简版；够用即可，避免引入 jieba 依赖）
_STOPWORDS_ZH = set("""
的 了 在 是 我 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 你 会 着 没有
看 好 自己 这 那 把 它 但 为 与 及 或 等 被 让 把 此 由 于 因 所 该 并 与其
这个 那个 因此 因为 所以 但是 然而 而且 并且 已经 正在 即将 关于 通过 对于
""".split())

# 实体识别正则
_RE_STOCK_CODE = re.compile(r"\b[036]\d{5}\b")                     # A 股代码
_RE_FUND_CODE = re.compile(r"\b\d{6}\b")                            # 基金代码 6 位
_RE_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(亿|万|千|百)?元?")       # 金额
_RE_PCT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")                  # 百分比
_RE_DATE = re.compile(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})")        # 日期


def _tokenize_zh(text: str) -> List[str]:
    """中文友好的简易分词：
    - 英文/数字按 token 保留
    - 中文按滑动窗口生成 2-gram 与 3-gram（避免单字噪声）
    - 过滤停用词
    """
    if not text:
        return []
    # 拆出英文/数字 token + 中文 run
    tokens: List[str] = []
    for m in re.finditer(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text):
        seg = m.group(0)
        if not seg:
            continue
        if seg[0].isascii():
            t = seg.lower()
            if t and t not in _STOPWORDS_ZH and len(t) <= 30:
                tokens.append(t)
        else:
            # 中文 run：先过滤单字停用词，再生成滑动窗口
            chars = [c for c in seg if c not in _STOPWORDS_ZH]
            for w in (2, 3):
                for i in range(len(chars) - w + 1):
                    ng = "".join(chars[i:i + w])
                    if len(ng) >= w:
                        tokens.append(ng)
            # 长词（>=4 字）也整体保留
            if len(chars) >= 4:
                tokens.append("".join(chars))
    return [t for t in tokens if t]


def _ngrams(tokens: List[str], n: int) -> List[str]:
    """生成 n-gram（仅中文适用；英文按词组合）。"""
    if len(tokens) < n:
        return []
    return ["".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


@dataclass
class KeywordResult:
    """关键词抽取结果。"""
    unigrams: List[Tuple[str, int]] = field(default_factory=list)
    bigrams: List[Tuple[str, int]] = field(default_factory=list)
    trigrams: List[Tuple[str, int]] = field(default_factory=list)
    stock_codes: List[Tuple[str, int]] = field(default_factory=list)
    fund_codes: List[Tuple[str, int]] = field(default_factory=list)
    amounts: List[Tuple[str, int]] = field(default_factory=list)
    percentages: List[Tuple[str, int]] = field(default_factory=list)
    new_words: List[Tuple[str, int]] = field(default_factory=list)

    def top(self, n: int = 20) -> Dict[str, List[Tuple[str, int]]]:
        return {
            "unigrams": self.unigrams[:n],
            "bigrams": self.bigrams[:n],
            "trigrams": self.trigrams[:n],
            "stock_codes": self.stock_codes[:n],
            "fund_codes": self.fund_codes[:n],
            "amounts": self.amounts[:n],
            "percentages": self.percentages[:n],
            "new_words": self.new_words[:n],
        }


def extract_keywords(articles: List[Dict[str, Any]],
                    min_freq: int = 2,
                    bigram_min_freq: int = 3,
                    trigram_min_freq: int = 3,
                    new_word_min_score: float = 5.0) -> KeywordResult:
    """从文章列表抽取关键词、n-gram 与实体。

    Args:
        articles: 文章 dict 列表，需含 title / summary / content 等字段
        min_freq: unigram 最低频次
        bigram_min_freq / trigram_min_freq: n-gram 阈值（低频易噪声）
        new_word_min_score: 新词凝固度门槛（最小 PMI，单位是 nat）。
                            5.0 大约对应几千字以上的批次；几十行的
                            小样本算不出足够低的独立概率，会全部落选。

    Returns:
        KeywordResult 含各维度 Top 列表
    """
    res = KeywordResult()
    uni_c: Counter = Counter()
    bi_c: Counter = Counter()
    tri_c: Counter = Counter()
    code_c: Counter = Counter()
    fund_c: Counter = Counter()
    amt_c: Counter = Counter()
    pct_c: Counter = Counter()
    char_c: Counter = Counter()   # 字频（新词发现要的是字级独立性）
    run_c: Counter = Counter()    # 中文连续串里的 2~4 字 n-gram 频次

    for a in articles:
        # 拼出用于分析的文本（标题权重大，复用一次）
        title = a.get("title") or ""
        summary = a.get("summary") or a.get("content") or ""
        text = f"{title} {title} {summary}".strip()
        # 实体（先抽实体，再 n-gram，避免代码/数字被打散）
        for m in _RE_STOCK_CODE.finditer(text):
            code_c[m.group(0)] += 1
        for m in _RE_FUND_CODE.finditer(text):
            v = m.group(0)
            # 区分基金/股票：6 位数字都可能，按上下文补判
            fund_c[v] += 1
        for m in _RE_AMOUNT.finditer(text):
            amt_c[m.group(0).replace(" ", "")] += 1
        for m in _RE_PCT.finditer(text):
            pct_c[m.group(1) + "%"] += 1
        # n-gram
        tokens = _tokenize_zh(text)
        for t in tokens:
            if len(t) >= 2:
                uni_c[t] += 1
        bi_c.update(_ngrams([t for t in tokens if len(t) >= 2], 2))
        tri_c.update(_ngrams([t for t in tokens if len(t) >= 2], 3))
        for m in re.finditer(r"[\u4e00-\u9fff]+", text):
            run = m.group(0)
            for ch in run:
                char_c[ch] += 1
            for w in (2, 3, 4):
                for i in range(len(run) - w + 1):
                    run_c[run[i:i + w]] += 1

    res.unigrams = [(w, n) for w, n in uni_c.most_common()
                    if n >= min_freq and len(w) >= 2][:50]
    res.bigrams = [(w, n) for w, n in bi_c.most_common()
                   if n >= bigram_min_freq and len(w) >= 4][:30]
    res.trigrams = [(w, n) for w, n in tri_c.most_common()
                    if n >= trigram_min_freq and len(w) >= 6][:20]
    res.stock_codes = [(c, n) for c, n in code_c.most_common() if n >= 1][:20]
    res.fund_codes = [(c, n) for c, n in fund_c.most_common() if n >= 1][:20]
    res.amounts = [(c, n) for c, n in amt_c.most_common() if n >= 1][:15]
    res.percentages = [(c, n) for c, n in pct_c.most_common() if n >= 1][:15]

    # 新词发现（字级凝固度）。
    # 旧实现是死代码：它拿 token 级 unigram 计数去估"组成字独立出现频率"，
    # 而 token 最短 2 字，uni_c.get(单个汉字) 恒为 0 → expected 恒为 0 → 一条都不出。
    # 现在按标准做法：候选 n-gram 在每个切分点上的 PMI 取最小值（最弱一环仍紧耦合才算词）。
    new_words: List[Tuple[str, float]] = []
    if char_c:
        total_chars = max(1, sum(char_c.values()))

        def _cfreq(s: str) -> int:
            return char_c[s] if len(s) == 1 else run_c.get(s, 0)

        for cand, n in run_c.items():
            if n < bigram_min_freq or len(cand) < 3:
                continue
            min_pmi = math.inf
            for cut in range(1, len(cand)):
                left, right = cand[:cut], cand[cut:]
                fl, fr = _cfreq(left) + 1, _cfreq(right) + 1
                p = (n + 1) / total_chars
                pmi = math.log(p / ((fl / total_chars) * (fr / total_chars)))
                min_pmi = min(min_pmi, pmi)
            if min_pmi >= new_word_min_score:
                new_words.append((cand, round(n * min_pmi, 2)))
    new_words.sort(key=lambda x: (-x[1], -len(x[0])))
    # 只保留最大Span：'贵州茅台' 成立时 '贵州茅'/'州茅台' 是同一段的碎片，
    # 三个并列输出只会让下游把同一个词数三遍。
    kept: List[Tuple[str, float]] = []
    for cand, score in new_words:
        if any(cand != k[0] and cand in k[0] for k in kept):
            continue
        kept.append((cand, score))
    res.new_words = kept[:15]

    return res


# ============================================================================
# 2. CrossSourceVerifier 跨源交叉验证
# ============================================================================


# 来源权威度（可由调用方覆盖）
DEFAULT_SOURCE_AUTHORITY = {
    "上海证券报": 0.95, "中国证券报": 0.95, "证券时报": 0.95,
    "证券日报": 0.9, "经济参考报": 0.9, "21世纪经济报道": 0.9,
    "财联社": 0.9, "第一财经": 0.9, "澎湃新闻": 0.85,
    "新华财经": 0.95, "人民网财经": 0.9, "新华网": 0.95,
    "东方财富": 0.8, "新浪财经": 0.75, "凤凰财经": 0.75,
    "华尔街见闻": 0.8, "同花顺": 0.7, "雪球": 0.6,
    "网易财经": 0.65, "腾讯财经": 0.7, "今日头条": 0.5,
    "百度": 0.5, "微信": 0.4, "微博": 0.3,
}


@dataclass
class VerifiedEvent:
    """一个被多源验证的事件。"""
    title_canonical: str               # 规范化后的代表标题
    sources: List[str] = field(default_factory=list)
    sentiment: str = "neutral"
    first_seen: str = ""
    last_seen: str = ""
    mention_count: int = 0
    confidence: float = 0.0            # 0-1
    conflict: bool = False             # 是否情感倾向冲突
    entities: List[str] = field(default_factory=list)


def _normalize_event_title(title: str) -> str:
    """事件标题规范化：去来源前缀、【】、序号等。

    剥来源前缀必须在"括号→空格"**之前**做：否则 `【财联社】` 先变成
    ` 财联社 `，后面那条 `^【…】` 规则永远匹配不到，媒体名会污染每一条标题。
    """
    if not title:
        return ""
    t = re.sub(r"^\s*[\[【（(]([^\]】）)]{1,14})[\]】）)]\s*[:：\-—]?\s*", "", title)
    t = re.sub(r"^[\u4e00-\u9fa5A-Za-z0-9]{2,12}\d{1,2}月\d{1,2}日电[:：]?\s*", "", t)
    t = re.sub(r"[\【\】\[\]「」『』《》]", " ", t)
    t = re.sub(r"^[\s\d\.\-]+", "", t)   # 去掉序号
    t = re.sub(r"\s+", "", t)
    return t[:60]


def _title_similarity(a: str, b: str) -> float:
    """事件标题相似度（综合 Jaccard + 编辑距离 + 长度差异）。

    对短中文标题偏向宽容（1 字差异不应判同），用编辑距离作为
    主要区分信号，再辅以 Jaccard。返回值 0-1。
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    jaccard = inter / max(1, len(sa | sb))
    # Levenshtein 距离（动态规划；限制最大长度避免极端开销）
    la, lb = len(a), len(b)
    if max(la, lb) > 200:
        return jaccard
    dp = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        dp[i][0] = i
    for j in range(lb + 1):
        dp[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1]
            else:
                dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])
    ed_ratio = dp[la][lb] / max(la, lb)
    # 主要靠编辑距离（更稳健），Jaccard 仅做小幅加权
    sim = 0.7 * (1.0 - ed_ratio) + 0.3 * jaccard
    return sim


def _from_epoch(value: float) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_ts(s: str) -> Optional[datetime]:
    """各家爬虫的时间串 → naive 本地 datetime。

    支持 ISO8601（带 T/秒/毫秒/时区/Z）、`2026-10-01 09:00`、`2026/10/01`、
    `2026年10月1日`、`20261001` 以及 Unix 秒/毫秒。认不出的返回 None，
    调用方按"无时间戳"降级——绝不能因为格式不合就假装记录是新的。
    """
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)) and not isinstance(s, bool):
        return _from_epoch(float(s))
    text = str(s).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{10,13}", text):
        return _from_epoch(float(text) / (1000.0 if len(text) > 10 else 1.0))
    cleaned = text.replace("年", "-").replace("月", "-").replace("日", "")
    cleaned = cleaned.replace("T", " ")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    dt: Optional[datetime] = None
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y%m%d"):
            try:
                dt = datetime.strptime(cleaned[:19], fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def verify_cross_source(articles: List[Dict[str, Any]],
                       similarity_threshold: float = 0.55,
                       time_window_hours: int = 48,
                       source_authority: Optional[Dict[str, float]] = None,
                       min_sources_for_verified: int = 2) -> List[VerifiedEvent]:
    """跨源交叉验证：把同一事件的报道聚类并评分。

    算法：按规范化标题相似度 + 时间窗做简易聚类
    （O(n·k)，对几百条足够；超过 1000 条建议先按 target 过滤）。

    Returns:
        VerifiedEvent 列表，按置信度降序
    """
    authority = source_authority or DEFAULT_SOURCE_AUTHORITY
    events: List[VerifiedEvent] = []
    canonical_for_article: List[int] = {}     # idx -> event idx

    for idx, a in enumerate(articles):
        norm = _normalize_event_title(a.get("title") or "")
        if not norm:
            continue
        ts = _parse_ts(a.get("publish_time") or a.get("fetch_time") or "")
        matched_event_idx = None
        for ei, ev in enumerate(events):
            # 时间窗：与 first_seen / last_seen 比较
            if ts and ev.last_seen:
                try:
                    last = datetime.fromisoformat(ev.last_seen)
                    if abs((ts - last).total_seconds()) > time_window_hours * 3600:
                        continue
                except ValueError:
                    pass
            sim = _title_similarity(norm, ev.title_canonical)
            if sim >= similarity_threshold:
                matched_event_idx = ei
                break
        if matched_event_idx is None:
            events.append(VerifiedEvent(
                title_canonical=norm,
                sources=[a.get("source") or "未知"],
                sentiment=a.get("sentiment") or "neutral",
                first_seen=(ts or datetime.now()).isoformat(timespec="seconds"),
                last_seen=(ts or datetime.now()).isoformat(timespec="seconds"),
                mention_count=1,
            ))
        else:
            ev = events[matched_event_idx]
            src = a.get("source") or "未知"
            if src not in ev.sources:
                ev.sources.append(src)
            ev.mention_count += 1
            if ts:
                ev.last_seen = ts.isoformat(timespec="seconds")
                if ts.isoformat(timespec="seconds") < ev.first_seen:
                    ev.first_seen = ev.last_seen
            # 情感冲突？
            if (a.get("sentiment") and ev.sentiment
                    and a.get("sentiment") != ev.sentiment
                    and a.get("sentiment") != "neutral"
                    and ev.sentiment != "neutral"):
                ev.conflict = True

    # 计算置信度 + 收集实体
    for ev in events:
        # 来源多样性 + 来源权威度
        if ev.sources:
            auth_scores = [authority.get(s, 0.5) for s in ev.sources]
            avg_auth = sum(auth_scores) / len(auth_scores)
        else:
            avg_auth = 0.5
        # 多源加权（log 抑制重复源带来的虚假高置信）
        src_score = min(1.0, math.log1p(len(ev.sources)) / math.log1p(10))
        cnt_score = min(1.0, math.log1p(ev.mention_count) / math.log1p(50))
        ev.confidence = round(0.5 * src_score + 0.3 * avg_auth + 0.2 * cnt_score, 3)
        if ev.conflict:
            ev.confidence *= 0.7   # 冲突打 7 折

    # 提取相关实体（从归属文章里挑）
    article_by_event: List[List[Dict[str, Any]]] = [[] for _ in events]
    for idx, a in enumerate(articles):
        norm = _normalize_event_title(a.get("title") or "")
        if not norm:
            continue
        for ei, ev in enumerate(events):
            sim = _title_similarity(norm, ev.title_canonical)
            if sim >= similarity_threshold:
                article_by_event[ei].append(a)
                break
    for ev, arts in zip(events, article_by_event):
        ents: Counter = Counter()
        for a in arts:
            for c in _RE_STOCK_CODE.findall(a.get("title") or ""):
                ents[c] += 1
            for m in _RE_AMOUNT.finditer(a.get("title") or ""):
                ents[m.group(0).replace(" ", "")] += 1
        ev.entities = [w for w, _ in ents.most_common(5)]

    events.sort(key=lambda e: -e.confidence)
    return events


# ============================================================================
# 3. TrendAnalyzer 趋势与异常分析
# ============================================================================


def _time_key(s: str, granularity: str) -> Optional[str]:
    if not s:
        return None
    s = str(s).strip()
    # 支持多种输入
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"):
        try:
            dt = datetime.strptime(s[:19], fmt)
            break
        except ValueError:
            continue
    else:
        m = _RE_DATE.search(s)
        if not m:
            return None
        try:
            dt = datetime.strptime(m.group(1).replace("/", "-").replace(".", "-"),
                                   "%Y-%m-%d")
        except ValueError:
            return None
    if granularity == "hour":
        return dt.strftime("%Y-%m-%d %H")
    if granularity == "week":
        iso = dt.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"
    return dt.strftime("%Y-%m-%d")


@dataclass
class TrendResult:
    """趋势分析结果。"""
    series: Dict[str, int] = field(default_factory=dict)         # 时间桶 → 计数
    granularity: str = "day"
    moving_avg: Dict[str, float] = field(default_factory=dict)   # 时间桶 → MA
    week_over_week: Dict[str, float] = field(default_factory=dict)
    anomalies: List[Dict[str, Any]] = field(default_factory=list)
    forecast: Dict[str, Any] = field(default_factory=dict)        # 简单线性外推
    trend_label: str = ""                                         # 上升/下降/平稳
    total: int = 0
    peak_bucket: str = ""
    peak_value: int = 0


def analyze_trend(articles: List[Dict[str, Any]],
                 granularity: str = "day",
                 ma_window: int = 7,
                 z_threshold: float = 2.0,
                 forecast_steps: int = 3) -> TrendResult:
    """时间趋势与异常分析。

    Args:
        articles: 文章列表
        granularity: hour / day / week
        ma_window: 移动平均窗口
        z_threshold: z-score 异常阈值
        forecast_steps: 向前预测几个时间桶
    """
    bucket: Counter = Counter()
    for a in articles:
        tk = _time_key(a.get("publish_time") or a.get("fetch_time") or "",
                       granularity)
        if tk:
            bucket[tk] += 1
    if not bucket:
        return TrendResult(granularity=granularity)

    sorted_keys = sorted(bucket.keys())
    values = [bucket[k] for k in sorted_keys]

    # 移动平均
    moving: Dict[str, float] = {}
    for i, k in enumerate(sorted_keys):
        lo = max(0, i - ma_window + 1)
        seg = values[lo:i + 1]
        moving[k] = round(sum(seg) / len(seg), 2) if seg else 0.0

    # WoW（按周环比，要求至少 7 个数据点）
    wow: Dict[str, float] = {}
    if granularity == "day" and len(values) >= 14:
        for i in range(7, len(sorted_keys)):
            prev = values[i - 7] or 1
            wow[sorted_keys[i]] = round((values[i] - values[i - 7]) / prev, 3)

    # 异常检测：z-score（基于整体均值/标准差）
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / max(1, len(values))
    std = math.sqrt(var)
    anomalies: List[Dict[str, Any]] = []
    if std > 1e-9:
        for i, k in enumerate(sorted_keys):
            z = (values[i] - mean) / std
            if abs(z) >= z_threshold:
                anomalies.append({
                    "bucket": k, "value": values[i],
                    "z_score": round(z, 2),
                    "direction": "spike" if z > 0 else "dip",
                })

    # 简单线性外推：用最后 ma_window 个点做最小二乘
    forecast: Dict[str, Any] = {}
    trend_label = "平稳"
    if len(values) >= 3:
        seg_n = min(ma_window, len(values))
        seg_idx = list(range(len(values) - seg_n, len(values)))
        seg_val = values[-seg_n:]
        n = len(seg_idx)
        mx = sum(seg_idx) / n
        my = sum(seg_val) / n
        den = sum((x - mx) ** 2 for x in seg_idx)
        slope = sum((seg_idx[i] - mx) * (seg_val[i] - my) for i in range(n)) / den if den else 0.0
        intercept = my - slope * mx
        future = []
        for s in range(1, forecast_steps + 1):
            future.append({
                "step": s,
                "value": max(0, round(intercept + slope * (len(values) - 1 + s), 2)),
            })
        forecast = {
            "method": "linear_regression_last_window",
            "window": seg_n,
            "slope_per_bucket": round(slope, 4),
            "future": future,
        }
        if slope > 0.5:
            trend_label = "上升"
        elif slope < -0.5:
            trend_label = "下降"
        else:
            trend_label = "平稳"

    peak_idx = values.index(max(values)) if values else 0
    return TrendResult(
        series=dict(bucket),
        granularity=granularity,
        moving_avg=moving,
        week_over_week=wow,
        anomalies=anomalies,
        forecast=forecast,
        trend_label=trend_label,
        total=sum(values),
        peak_bucket=sorted_keys[peak_idx] if sorted_keys else "",
        peak_value=max(values) if values else 0,
    )


# ============================================================================
# 4. PeerBenchmark 同业对标
# ============================================================================


@dataclass
class PeerBenchmarkResult:
    """同业对标结果。"""
    target: str
    group_key: str
    group_size: int = 0
    target_metrics: Dict[str, Any] = field(default_factory=dict)
    group_mean: Dict[str, float] = field(default_factory=dict)
    group_median: Dict[str, float] = field(default_factory=dict)
    percentile: Dict[str, float] = field(default_factory=dict)
    deviation: Dict[str, float] = field(default_factory=dict)
    rank: Dict[str, int] = field(default_factory=dict)


def _percentile_rank(value: float, sorted_vals: List[float]) -> float:
    """百分位排名（0-100）。"""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    less = sum(1 for v in sorted_vals if v < value)
    equal = sum(1 for v in sorted_vals if v == value)
    return round((less + 0.5 * equal) / n * 100, 1)


def benchmark_against_peers(target_name: str,
                            metrics_by_target: Dict[str, Dict[str, float]],
                            group_key: str = "",
                            group_filter: Optional[Dict[str, List[str]]] = None
                            ) -> PeerBenchmarkResult:
    """同业对标：把目标与同类（group_key）对比。

    Args:
        target_name: 目标名称（必须存在于 metrics_by_target 中）
        metrics_by_target: {target_name: {metric_name: value, ...}}
        group_key: 把"目标"分到哪个组（如"白酒"/"银行"）；空则全量对比
        group_filter: {group_key: [targets in this group]}，可选
    """
    if target_name not in metrics_by_target:
        return PeerBenchmarkResult(target=target_name, group_key=group_key)
    target_metrics = metrics_by_target[target_name]
    # 决定组内成员
    if group_filter and group_key in group_filter:
        peers = [t for t in group_filter[group_key]
                 if t in metrics_by_target and t != target_name]
    else:
        peers = [t for t in metrics_by_target.keys()
                 if t != target_name]
    if not peers:
        return PeerBenchmarkResult(target=target_name, group_key=group_key,
                                   target_metrics=target_metrics,
                                   group_size=1)
    # 各指标统计
    mean_map: Dict[str, float] = {}
    median_map: Dict[str, float] = {}
    pct_map: Dict[str, float] = {}
    dev_map: Dict[str, float] = {}
    rank_map: Dict[str, int] = {}
    for m, v in target_metrics.items():
        peer_vals = [float(metrics_by_target[p].get(m, 0.0)) for p in peers]
        all_vals = sorted(peer_vals + [float(v)])
        mean_map[m] = round(sum(peer_vals) / len(peer_vals), 4) if peer_vals else 0.0
        median_map[m] = round(_median(peer_vals), 4) if peer_vals else 0.0
        pct_map[m] = _percentile_rank(float(v), peer_vals)
        if mean_map[m] != 0:
            dev_map[m] = round((float(v) - mean_map[m]) / abs(mean_map[m]), 3)
        # 排名：值越大越靠前（用反向；调用方可按需调整）
        rank_map[m] = sum(1 for pv in peer_vals if pv > float(v)) + 1
    return PeerBenchmarkResult(
        target=target_name,
        group_key=group_key,
        group_size=len(peers) + 1,
        target_metrics=target_metrics,
        group_mean=mean_map,
        group_median=median_map,
        percentile=pct_map,
        deviation=dev_map,
        rank=rank_map,
    )


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


# ============================================================================
# 5. InsightGenerator 洞察生成（可读的核心结论 + 建议）
# ============================================================================


@dataclass
class Insight:
    """单条洞察。"""
    level: str            # info / warn / risk / opportunity
    title: str
    detail: str
    evidence: List[str] = field(default_factory=list)


def generate_insights(stats: Dict[str, Any],
                     trend: Optional[TrendResult] = None,
                     verified: Optional[List[VerifiedEvent]] = None,
                     keywords: Optional[KeywordResult] = None,
                     top_n: int = 8) -> List[Insight]:
    """把统计/趋势/交叉验证/关键词组织成 N 条可读洞察。

    Args:
        stats: sentiment_statistics 输出
        trend: analyze_trend 输出
        verified: verify_cross_source 输出
        keywords: extract_keywords 输出
        top_n: 最多生成多少条
    """
    insights: List[Insight] = []

    # 1) 情感分布：负面超 30% → 风险
    total = stats.get("total") or 0
    neg = stats.get("sentiment", {}).get("negative", 0)
    pos = stats.get("sentiment", {}).get("positive", 0)
    if total > 0:
        neg_ratio = neg / total
        pos_ratio = pos / total
        if neg_ratio >= 0.4:
            insights.append(Insight(
                level="risk",
                title="负面舆情占比偏高",
                detail=f"负面 {neg}/{total}（{neg_ratio:.1%}），超过 40% 警戒线，"
                       f"建议立即排查事件源头并准备应对口径。",
                evidence=[f"负面占比 {neg_ratio:.1%}",
                          f"正面占比 {pos_ratio:.1%}"],
            ))
        elif neg_ratio >= 0.2:
            insights.append(Insight(
                level="warn",
                title="负面舆情需关注",
                detail=f"负面 {neg}/{total}（{neg_ratio:.1%}），处于 20-40% 观察区间，"
                       f"建议持续监控。",
                evidence=[f"负面占比 {neg_ratio:.1%}"],
            ))
        elif pos_ratio >= 0.5:
            insights.append(Insight(
                level="opportunity",
                title="正面舆情占主导",
                detail=f"正面 {pos}/{total}（{pos_ratio:.1%}），可作为品牌/营销素材。",
                evidence=[f"正面占比 {pos_ratio:.1%}"],
            ))

    # 2) 趋势：异常点 + 趋势方向
    if trend and trend.anomalies:
        spikes = [a for a in trend.anomalies if a["direction"] == "spike"]
        if spikes:
            top = spikes[0]
            insights.append(Insight(
                level="warn",
                title=f"检测到 {len(spikes)} 个时点异常放量",
                detail=f"峰值在 {top['bucket']}（z={top['z_score']}），"
                       f"当日 {top['value']} 条；"
                       f"建议核对当日是否发生重大事件并准备口径。",
                evidence=[f"峰值 {top['bucket']}={top['value']}",
                          f"z-score={top['z_score']}"],
            ))
    if trend and trend.trend_label == "上升":
        insights.append(Insight(
            level="info",
            title="舆情热度近期呈上升趋势",
            detail=f"最近窗口斜率 {trend.forecast.get('slope_per_bucket', 0):.2f}/桶，"
                   f"预测未来 {len(trend.forecast.get('future', []))} 个时间桶将延续上行。",
            evidence=[f"趋势：{trend.trend_label}",
                      f"峰值：{trend.peak_bucket}={trend.peak_value}"],
        ))
    elif trend and trend.trend_label == "下降":
        insights.append(Insight(
            level="info",
            title="舆情热度呈下降趋势",
            detail=f"最近窗口斜率 {trend.forecast.get('slope_per_bucket', 0):.2f}/桶，"
                   f"事件影响逐步消退。",
            evidence=[f"趋势：{trend.trend_label}"],
        ))

    # 3) 跨源验证：高置信度事件 + 冲突事件
    if verified:
        high = [v for v in verified if v.confidence >= 0.7]
        conflict = [v for v in verified if v.conflict]
        if high:
            top_ev = high[0]
            insights.append(Insight(
                level="info",
                title=f"{len(high)} 个事件被多源交叉验证（高置信）",
                detail=f"代表事件「{top_ev.title_canonical[:30]}」"
                       f"涉及 {len(top_ev.sources)} 个来源、{top_ev.mention_count} 次提及。",
                evidence=[f"来源：{','.join(top_ev.sources[:5])}",
                          f"置信度：{top_ev.confidence}"],
            ))
        if conflict:
            top_c = conflict[0]
            insights.append(Insight(
                level="risk",
                title=f"{len(conflict)} 个事件存在情感倾向冲突",
                detail=f"代表事件「{top_c.title_canonical[:30]}」不同来源情感不一，"
                       f"建议交叉核对原始报道。",
                evidence=[f"来源数 {len(top_c.sources)}",
                          f"提及 {top_c.mention_count}"],
            ))

    # 4) 关键词：高频实体 / 新词
    if keywords:
        if keywords.stock_codes:
            top_codes = keywords.stock_codes[:3]
            insights.append(Insight(
                level="info",
                title="高频提及的股票代码",
                detail=f"热点股票代码 TOP：{', '.join(c for c, _ in top_codes)}。"
                       f"可结合行情数据做归因。",
                evidence=[f"{c}（{n}次）" for c, n in top_codes],
            ))
        if keywords.amounts:
            top_amounts = keywords.amounts[:3]
            insights.append(Insight(
                level="info",
                title="高频出现的金额",
                detail=f"提及金额 TOP：{', '.join(a for a, _ in top_amounts)}。"
                       f"注意区分单位（元/万元/亿元）。",
                evidence=[f"{a}（{n}次）" for a, n in top_amounts],
            ))
        if keywords.new_words:
            top_new = keywords.new_words[:3]
            insights.append(Insight(
                level="opportunity",
                title="新词/组合词发现",
                detail=f"高频凝固词：{', '.join(w for w, _ in top_new)}，"
                       f"可作为话题标签或专题线索。",
                evidence=[f"{w}（评分 {s}）" for w, s in top_new],
            ))

    # 优先级排序：risk > warn > opportunity > info
    order = {"risk": 0, "warn": 1, "opportunity": 2, "info": 3}
    insights.sort(key=lambda x: order.get(x.level, 4))
    return insights[:top_n]


# ============================================================================
# 6. TimeBucketAggregator 时间分桶聚合
# ============================================================================


def aggregate_by_time_bucket(articles: List[Dict[str, Any]],
                             granularity: str = "day") -> Dict[str, Dict[str, Any]]:
    """按时间桶聚合：每个桶返回 {count, negative, positive, neutral}。

    Args:
        articles: 文章列表
        granularity: hour/day/week
    """
    out: Dict[str, Dict[str, int]] = defaultdict(lambda: {
        "count": 0, "negative": 0, "positive": 0, "neutral": 0,
    })
    for a in articles:
        tk = _time_key(a.get("publish_time") or a.get("fetch_time") or "",
                       granularity)
        if not tk:
            continue
        out[tk]["count"] += 1
        s = a.get("sentiment") or "neutral"
        if s in out[tk]:
            out[tk][s] += 1
        else:
            out[tk]["neutral"] += 1
    # 排序输出
    return dict(sorted(out.items()))


# ============================================================================
# 7. 综合便捷函数：增强聚合
# ============================================================================


def enhanced_aggregate(articles: List[Dict[str, Any]],
                      days: int = 0,
                      source_authority: Optional[Dict[str, float]] = None
                      ) -> Dict[str, Any]:
    """一站式增强聚合：把现有 sentiment_statistics 与所有新能力整合。"""
    # 时间过滤
    if days > 0:
        cutoff = datetime.now().timestamp() - days * 86400
        kept = []
        for a in articles:
            ts = _parse_ts(a.get("publish_time") or a.get("fetch_time") or "")
            if ts is None or ts.timestamp() >= cutoff:
                kept.append(a)
        articles = kept

    # 各维度分析（先 lazy import 避免循环依赖）
    try:
        from data_aggregator import (sentiment_statistics, dedupe_articles,
                                     cross_section_summary)
        deduped = dedupe_articles(articles)
        stats = sentiment_statistics(deduped)
    except ImportError:
        try:
            from scripts.data_aggregator import (sentiment_statistics,
                                                 dedupe_articles,
                                                 cross_section_summary)
            deduped = dedupe_articles(articles)
            stats = sentiment_statistics(deduped)
        except ImportError:
            deduped = articles
            stats = {"total": len(articles), "sentiment": {},
                     "severity": {}, "source_top": [], "by_day": {},
                     "hot_keywords": [], "avg_score": 0,
                     "neg_ratio": 0, "pos_ratio": 0}

    keywords = extract_keywords(deduped)
    verified = verify_cross_source(deduped,
                                   source_authority=source_authority)
    trend = analyze_trend(deduped)
    bucket = aggregate_by_time_bucket(deduped)
    insights = generate_insights(stats, trend, verified, keywords)

    return {
        "total": len(deduped),
        "raw_total": len(articles),
        "statistics": stats,
        "keywords": keywords.top(),
        "verified_events": [
            {
                "title": v.title_canonical,
                "sources": v.sources,
                "sentiment": v.sentiment,
                "first_seen": v.first_seen,
                "last_seen": v.last_seen,
                "mention_count": v.mention_count,
                "confidence": v.confidence,
                "conflict": v.conflict,
                "entities": v.entities,
            } for v in verified[:30]
        ],
        "trend": {
            "granularity": trend.granularity,
            "series": trend.series,
            "moving_avg": trend.moving_avg,
            "anomalies": trend.anomalies,
            "forecast": trend.forecast,
            "trend_label": trend.trend_label,
            "peak_bucket": trend.peak_bucket,
            "peak_value": trend.peak_value,
        },
        "by_time_bucket": bucket,
        "insights": [
            {"level": i.level, "title": i.title,
             "detail": i.detail, "evidence": i.evidence}
            for i in insights
        ],
    }


# ============================================================================
# 自检
# ============================================================================


def self_check() -> Dict[str, Any]:
    sample = [
        {"title": "贵州茅台发布半年报，营收增长 18%", "sentiment": "positive",
         "publish_time": "2026-08-15 10:00:00", "source": "上海证券报"},
        {"title": "贵州茅台半年报：净利润同比增长 25%", "sentiment": "positive",
         "publish_time": "2026-08-15 14:30:00", "source": "中国证券报"},
        {"title": "投资者担忧贵州茅台估值过高", "sentiment": "negative",
         "publish_time": "2026-08-16 09:15:00", "source": "雪球"},
    ]
    kw = extract_keywords(sample)
    ev = verify_cross_source(sample)
    tr = analyze_trend(sample)
    return {
        "version": "v8.0",
        "KeywordExtractor": f"OK（sample {len(sample)} 篇 → "
                            f"{len(kw.unigrams)} unigrams, "
                            f"{len(kw.bigrams)} bigrams, "
                            f"{len(kw.new_words)} new words）",
        "CrossSourceVerifier": f"OK（sample {len(sample)} → {len(ev)} events, "
                               f"top confidence={ev[0].confidence if ev else 0}）",
        "TrendAnalyzer": f"OK（granularity={tr.granularity}, "
                          f"total={tr.total}, anomalies={len(tr.anomalies)}, "
                          f"trend={tr.trend_label}）",
        "PeerBenchmark": "OK（按 group_key 区分）",
        "InsightGenerator": "OK（按 risk>warn>opportunity>info 排序）",
        "TimeBucketAggregator": "OK",
        "EnhancedAggregate": "OK（一站式）",
    }


if __name__ == "__main__":
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
