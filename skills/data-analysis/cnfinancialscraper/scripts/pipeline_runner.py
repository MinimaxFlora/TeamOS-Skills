# -*- coding: utf-8 -*-
"""
pipeline_runner.py — v12.0.0 信息获取一键编排 + 数据结论化

解决两件事（都来自 SkillHub TRACE 反馈）：
  1. **「不知道先试哪个」**：96 个工具、70+ 触发词，但没有一条把
     *检索 → 抓取 → 识别 → 融合 → 分析 → 打包* 串起来的入口。本模块给出一条：
     ``gather_info("贵州茅台最近7天负面舆情", package=True)``。
  2. **「不知道结果长什么样、能回答什么」**：每次运行都固定返回
     ``quality``（数据质量分）、``answer_ability``（这批数据能/不能回答什么）、
     ``gaps``（缺口）、``next_step``（下一步），失败按 ``PIPELINE_ERRORS`` 给原因和动作，
     不把调试成本丢回给用户。

设计约束：
  - **零依赖可跑**：``dry_run`` 与所有离线分析只吃标准库；网络/第三方只在具体
    fetcher 内部按需 import，缺依赖时降级为一条带 ``next_step`` 的错误，不抛栈。
  - **可注入**：``records=`` 直接喂数据、``fetcher=`` 换抓取实现，测试不联网。
  - **只读默认**：不落盘、不打包，除非 ``out_dir`` / ``package`` 显式开启。
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

log = logging.getLogger("cnfs.pipeline")

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

# 包内各模块历史上用扁平导入（import browser_scraper）；从包外以
# scripts.xxx 进入时这些导入会静默失败（浏览器兜底直接哑火），这里补齐。
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

# ============================================================================
# 错误码表：code → (原因, 下一步)
# ============================================================================

#: 流水线级错误码。HTTP 状态码级诊断仍由 ``http_utils.get_diagnostic`` 负责，
#: 这里只覆盖"整条链路失败"的情形。
PIPELINE_ERRORS: Dict[str, Dict[str, str]] = {
    "E_TOPIC_EMPTY": {
        "cause": "既没给 topic，也没给现成 records",
        "next_step": "gather_info('贵州茅台最近7天负面舆情') 或 gather_info(records=[...])",
    },
    "E_DEP_MISSING": {
        "cause": "该数据源需要额外依赖（未安装）",
        "next_step": "python setup_env.py --recommended 或 --full；缺什么按提示装",
    },
    "E_SOURCE_EMPTY": {
        "cause": "所有数据源都返回空结果",
        "next_step": "放宽 days、加 source_categories、或换 intent 重试；"
                     "先用 dry_run=True 看计划再决定",
    },
    "E_SOURCE_TIMEOUT": {
        "cause": "抓取超出预算时间，只拿到部分结果",
        "next_step": "提高 timeout（秒）或减小 limit；部分结果已在 records 里，可直接分析",
    },
    "E_FETCH_FAILED": {
        "cause": "抓取步骤抛异常",
        "next_step": "看 errors[].detail 定位源站；网络类失败先跑 get_anti_block_status()",
    },
    "E_ROUTE_NOT_FOUND": {
        "cause": "指定的 intent 不存在",
        "next_step": "list_intents() 查看全部 intent",
    },
    "E_SAMPLE_TOO_SMALL": {
        "cause": "样本量不足以支撑结论（低于该分析的最小样本要求）",
        "next_step": "扩大 days 或 limit 重新采集；指标类分析样本不足一律返回 None",
    },
    "E_PACKAGE_FAILED": {
        "cause": "打包步骤失败",
        "next_step": "确认 out_dir 可写、磁盘空间足够；records 与 markdown 已单独落盘",
    },
    "E_LOGIN_REQUIRED": {
        "cause": "目标页需要登录，当前没有可用登录档案或档案已过期",
        "next_step": "login_session.add_profile(...) 导入 Cookie，"
                     "或 wait_for_manual_login(name, login_url) 扫码后自动接管",
    },
}


def _err(code: str, **extra: Any) -> Dict[str, Any]:
    spec = PIPELINE_ERRORS.get(code, {})
    item = {"code": code,
            "cause": spec.get("cause", "未归类错误"),
            "next_step": spec.get("next_step", "查看 TROUBLESHOOTING.md")}
    item.update({k: v for k, v in extra.items() if v not in (None, "")})
    return item


#: 采集层失败占位条目的标题前缀（与 sentiment_crawler 保持一致）
PLACEHOLDER_PREFIX = "[未能直接抓取]"


# ============================================================================
# INTENT 路由表（唯一真值来源：文档与测试都从这里反查，不手写数字）
# ============================================================================

#: 每个 intent 都是一份**契约**：问法、链路、输入、输出、不能做什么、依赖层级。
INTENTS: List[Dict[str, Any]] = [
    {
        "id": "sentiment",
        "label": "机构舆情 / 新闻",
        "ask": ["爬一下贵州茅台最近7天的舆情", "某基金公司的负面新闻", "全网舆情监测XX银行"],
        "match": ["舆情", "新闻", "资讯", "报道", "负面", "正面", "利好", "利空", "口碑"],
        "chain": ["search_media_sources", "crawl_articles", "dedupe_and_classify",
                  "fuse_evidence", "analyze", "export"],
        "entry": "crawl_sentiment(targets=[topic], days, max_articles)",
        "input": "机构名或机构类别 + 时间窗（天）+ 情感倾向（正/负/全部）",
        "output": "文章记录列表（title/url/source/publish_time/sentiment/severity）"
                  "+ 情感与来源分布 + 共识事件 + 洞察",
        "not_for": ["盘中实时行情（用 intent=market）", "付费墙正文",
                    "需要登录才可见的社区帖（先建登录档案）", "投资建议"],
        "requires": "核心（零 pip 依赖）",
        "seconds_typical": "30-120（受 max_total_seconds 预算约束）",
    },
    {
        "id": "web",
        "label": "指定网页抓取与识别",
        "ask": ["把这个网页抓成结构化数据", "抓这个页面的表格", "归档这篇公告"],
        "match": ["网页", "页面", "这个链接", "url", "http", "归档", "抓取", "存档"],
        "chain": ["fetch_html", "smart_extract", "fuse_evidence", "analyze", "export"],
        "entry": "quick_archive(url) / smart_extract(html, url)",
        "input": "一个或多个 URL（http/https）",
        "output": "页面类型判别 + 表格/键值/条目/分页/数字抽取结果 + 正文",
        "not_for": ["整站爬取（需自行限定深度与预算）", "APP 内嵌页",
                    "SPA 静态抓不到时须走浏览器（需 Playwright）"],
        "requires": "核心；动态页需 Playwright",
        "seconds_typical": "2-20 / 页",
    },
    {
        "id": "search_crawl",
        "label": "搜索后再抓取",
        "ask": ["搜索贵州茅台2026半年报然后把结果爬下来", "找一下关于XX的报道并汇总"],
        "match": ["搜索", "搜一下", "查找", "找一下", "百度的结果", "搜狗"],
        "chain": ["multi_engine_search", "dedup_urls", "batch_smart_crawl",
                  "smart_extract", "fuse_evidence", "analyze", "export"],
        "entry": "quick_search(query) → batch_smart_crawl(urls)",
        "input": "自然语言查询词 + 结果条数上限",
        "output": "命中链接列表（带引擎与可信度）+ 每个链接的抓取正文与抽取记录",
        "not_for": ["私有数据", "登录后可见内容（先建档案）", "超出并发上限的批量"],
        "requires": "核心",
        "seconds_typical": "10-90（并发默认 4）",
    },
    {
        "id": "institution",
        "label": "机构名单与信息",
        "ask": ["查一下所有基金管理公司", "华夏基金的官网是什么", "券商名单统计"],
        "match": ["机构", "名单", "公司列表", "银行", "券商", "信托", "保险公司",
                  "基金管理公司", "期货公司"],
        "chain": ["registry_query", "enrich_website", "export"],
        "entry": "ScrapableRegistry().search(topic) / list_by_type(type)",
        "input": "机构类型（43 大类之一）或关键词",
        "output": "机构名 + 类型 + 官网 + 是否可爬（scrapable 标记）",
        "not_for": ["工商司法类尽调结论（背调类技能更合适）", "未覆盖的小微机构"],
        "requires": "核心（本地 JSON 注册表，不联网）",
        "seconds_typical": "<1",
    },
    {
        "id": "market",
        "label": "行情 / 指标 / 宏观数据",
        "ask": ["贵州茅台现在多少钱", "510300 最近60根分钟线", "CPI 多少", "MACD 什么形态"],
        "match": ["行情", "报价", "价格", "净值", "K线", "分钟线", "指标", "MACD", "RSI",
                  "CPI", "GDP", "Shibor", "期货", "汇率", "板块"],
        "chain": ["pick_query_type", "multi_source_fallback", "indicators", "export"],
        "entry": "query_financial_data(query_type, **params) / get_hub().quotes([...])",
        "input": "代码（股票/基金/转债/指数）或 query_type（60+ 种）+ 时间/条数",
        "output": "带 attempts（逐源成败记录）与 source（最终命中源）的数据；"
                  "指标类样本不足返回 None 而非硬造",
        "not_for": ["逐笔 Level-2 行情", "交易下单", "历史全量分钟线",
                    "把它当实时决策依据（快照有秒级延迟）"],
        "requires": "核心（akshare/yfinance 类查询需装对应包，缺失自动降级）",
        "seconds_typical": "1-15",
    },
    {
        "id": "announcement",
        "label": "上市公司公告 / 研报 / 财报",
        "ask": ["下载贵州茅台的最新公告", "找券商关于宁德时代的研报", "读一下这份年报"],
        "match": ["公告", "研报", "年报", "季报", "半年报", "财报", "披露", "定期报告"],
        "chain": ["cninfo_search", "pdf_download", "parse_document", "interpret", "export"],
        "entry": "search_cninfo_announcements(keyword) → download_file(url)",
        "input": "股票名/代码 + 时间范围 + 公告类别",
        "output": "公告标题/日期/类型/PDF 链接（下载后为本地文件）+ 解读要点",
        "not_for": ["未公开信息", "付费研报库正文（有版权，只到公开索引）"],
        "requires": "推荐（PDF 解析需 bs4/lxml）",
        "seconds_typical": "5-60",
    },
    {
        "id": "document",
        "label": "本地文档解析与对比",
        "ask": ["分析这份 PDF", "把这两份 Word 做对比", "整理这个目录里的报告"],
        "match": ["文档", "pdf", "word", "docx", "excel", "xlsx", "ppt", "解析", "对比文件"],
        "chain": ["parse", "classify", "extract_sections", "compare", "export"],
        "entry": "DocumentAnalyzer().analyze(file_path) / .compare([paths])",
        "input": "本地文件路径或目录（PDF/Word/Excel/PPT/HTML/TXT/MD）",
        "output": "章节结构 + 财务指标 + 风险要素 + 摘要；多文件时给差异表",
        "not_for": ["扫描件（无文字层的图片型 PDF 需先 OCR）", "加密文件"],
        "requires": "推荐",
        "seconds_typical": "1-30 / 份",
    },
    {
        "id": "login_page",
        "label": "登录后可见页面",
        "ask": ["爬登录后才能看的那一页", "我已经登录了，用我的会话抓", "雪球个人组合页"],
        "match": ["登录", "登录后", "会员", "扫码", "cookie", "会话", "我的页面"],
        # 命中这些词说明"非登录不可"，直接压过同时出现的"页面/链接"类词
        "boost": ["登录", "会话", "cookie", "扫码", "已登录", "账号"],
        "chain": ["load_profile", "check_login", "inject_session", "fetch",
                  "smart_extract", "export"],
        "entry": "login_session.check_login(name) → apply_to_session(...) → 抓取",
        "input": "站点档案名 + 目标 URL（Cookie 由用户自己导出或扫码产生）",
        "output": "登录态健康度（fresh/expiring/expired）+ 抓取结果；档案输出全程脱敏",
        "not_for": ["托管账号密码自动登录", "破解验证码", "代打第三方付费接口",
                    "违反站点服务条款的批量抓取"],
        "requires": "核心；扫码接管需 Playwright",
        "seconds_typical": "<1（探测）/ 1-30（抓取）",
    },
]

_INTENT_BY_ID = {i["id"]: i for i in INTENTS}

#: 兜底：识别不出时的默认 intent
DEFAULT_INTENT = "sentiment"


def list_intents() -> List[Dict[str, Any]]:
    """全部 intent 契约（文档与模型选路由用，数字不手写）。"""
    return [dict(i) for i in INTENTS]


def describe(intent: str) -> Dict[str, Any]:
    """单个 intent 的完整契约；intent 名写错时报错并列出可选，而不是返回空。"""
    key = (intent or "").strip().lower()
    found = _INTENT_BY_ID.get(key)
    if not found:
        raise PipelineError("E_ROUTE_NOT_FOUND", intent=intent or "")
    return dict(found)


class PipelineError(Exception):
    """带 code / cause / next_step 的流水线错误。"""

    def __init__(self, code: str, **extra: Any):
        item = _err(code, **extra)
        super().__init__(f"[{code}] {item['cause']} → {item['next_step']}")
        self.code = code
        self.item = item

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.item)


def route(topic: str = "", intent: str = "auto") -> Dict[str, Any]:
    """按问法选 intent。返回 {intent, confidence, matched, alternatives}。

    ``intent`` 显式指定时直接采纳（除非名字不存在）；``auto`` 时按命中关键词数打分，
    一个都没命中则落到 ``DEFAULT_INTENT`` 并标 ``confidence='low'``——让调用方知道
    这是猜的，而不是装作很确定。
    """
    text = (topic or "").strip().lower()
    want = (intent or "auto").strip().lower()
    if want and want != "auto":
        if want not in _INTENT_BY_ID:
            raise PipelineError("E_ROUTE_NOT_FOUND", intent=want)
        return {"intent": want, "confidence": "explicit",
                "matched": [], "alternatives": [],
                "label": _INTENT_BY_ID[want]["label"]}

    scored: List[Tuple[int, str, List[str]]] = []
    for item in INTENTS:
        hits = [kw for kw in item["match"] if kw.lower() in text]
        boosts = [kw for kw in item.get("boost") or [] if kw.lower() in text]
        if hits or boosts:
            scored.append((len(hits) + (2 if boosts else 0), item["id"],
                           hits + [b for b in boosts if b not in hits]))
    if not scored:
        return {"intent": DEFAULT_INTENT, "confidence": "low", "matched": [],
                "alternatives": [i["id"] for i in INTENTS],
                "label": _INTENT_BY_ID[DEFAULT_INTENT]["label"],
                "note": "关键词没命中，按舆情兜底；建议显式传 intent"}
    scored.sort(key=lambda x: -x[0])
    best = scored[0]
    return {"intent": best[1],
            "confidence": "high" if best[0] >= 2 else "medium",
            "matched": best[2],
            "alternatives": [s[1] for s in scored[1:]],
            "label": _INTENT_BY_ID[best[1]]["label"]}


def plan_for(intent_id: str, topic: str, **opts: Any) -> List[Dict[str, Any]]:
    """把 intent 的 chain 展开成步骤计划（dry_run 与日志共用同一份形状）。"""
    spec = _INTENT_BY_ID.get(intent_id) or _INTENT_BY_ID[DEFAULT_INTENT]
    steps = []
    for i, step in enumerate(spec["chain"], 1):
        steps.append({"no": i, "step": step,
                      "purpose": _STEP_PURPOSE.get(step, step),
                      "tool": _STEP_TOOL.get(step, "internal"),
                      "status": "planned"})
    return steps


_STEP_PURPOSE = {
    "search_media_sources": "按目标与媒体类别确定要问哪些源",
    "multi_engine_search": "多引擎检索拿候选链接（中文优先）",
    "dedup_urls": "去掉重复与已抓过的 URL",
    "crawl_articles": "从媒体源抓正文与时间戳",
    "batch_smart_crawl": "API→HTTP→浏览器逐级降级抓取候选页",
    "fetch_html": "取回目标网页 HTML",
    "fetch": "用带登录态的会话取回页面",
    "smart_extract": "识别表格/键值/条目/分页/金融数字",
    "dedupe_and_classify": "模糊去重 + 情感/严重度分级",
    "fuse_evidence": "跨源归一、聚类、加权共识、冲突台账",
    "pick_query_type": "选择 query_type 并排数据源优先级",
    "multi_source_fallback": "逐源降级取数，记录每个源的成功/失败",
    "indicators": "算技术指标与信号（样本不足返回 None）",
    "registry_query": "查本地机构注册表",
    "enrich_website": "补全官网与可爬标记",
    "cninfo_search": "检索巨潮公告索引",
    "pdf_download": "下载 PDF（断点续传 + 校验）",
    "parse_document": "解析文档结构",
    "classify": "判别文档类型",
    "extract_sections": "抽章节/指标/风险要素",
    "compare": "多文档差异对比",
    "interpret": "定期报告规则化解读",
    "load_profile": "读取登录档案",
    "check_login": "探测登录态是否仍有效",
    "inject_session": "把 Cookie 注入会话",
    "analyze": "聚合出分布/趋势/关键词/洞察",
    "export": "导出 Markdown/JSON，必要时打包",
}
_STEP_TOOL = {
    "crawl_articles": "sentiment_crawler.crawl_sentiment",
    "multi_engine_search": "search_engine.quick_search",
    "batch_smart_crawl": "crawl_enhancer.batch_smart_crawl",
    "fetch_html": "fullpage_archiver.quick_archive",
    "fetch": "login_session.apply_to_session + http_utils.http_get",
    "smart_extract": "web_recognizer.smart_extract",
    "dedupe_and_classify": "data_aggregator.dedupe_articles",
    "fuse_evidence": "evidence_fusion.fusion_aggregate",
    "multi_source_fallback": "financial_data_mcp.query_financial_data",
    "indicators": "indicator_engine.analyze_bars",
    "registry_query": "scrapable_registry.ScrapableRegistry",
    "cninfo_search": "cninfo_scraper.search_cninfo_announcements",
    "pdf_download": "http_utils.download_file",
    "parse_document": "document_analyzer.DocumentAnalyzer.analyze",
    "compare": "document_analyzer.DocumentAnalyzer.compare",
    "check_login": "login_session.check_login",
    "analyze": "analysis_enhanced.enhanced_aggregate",
    "export": "crawl_packager.CrawlPackager",
}


# ============================================================================
# Fetchers：把 intent 落到真实入口（全部惰性 import，缺依赖只降级不抛栈）
# ============================================================================

Fetcher = Callable[[str, Dict[str, Any]], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]]


def _import_target(dotted: str):
    """按扁平模块名优先导入，回退到 ``scripts.x``。

    包内其余模块之间一直是扁平互导（``import http_utils``）；若这里先取
    ``scripts.http_utils``，同一份代码会以两个模块身份各跑一遍——
    缓存、会话和测试里的 monkeypatch 都对不上号。
    """
    import importlib
    leaf = dotted.split(".")[-1]
    for modname in (leaf, dotted):
        try:
            return importlib.import_module(modname)
        except ImportError:
            continue
    raise ImportError(dotted)


def _split_placeholders(
        records: List[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把真实内容与"抓取失败占位条目"分开。

    采集层会故意保留 ``[未能直接抓取]`` 这类占位记录用于记账；但它们没有正文，
    一旦计入样本，空跑也能刷出 B 级质量分——这是最典型的假成功。
    """
    real: List[Dict[str, Any]] = []
    placeholders: List[Dict[str, Any]] = []
    for r in records:
        title = str(r.get("title") or "").strip()
        body = (str(r.get("content") or "").strip()
                or str(r.get("summary") or "").strip())
        if title.startswith(PLACEHOLDER_PREFIX) or not body:
            placeholders.append(r)
        else:
            real.append(r)
    return real, placeholders


def _fetch_sentiment(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    mod = _import_target("scripts.sentiment_crawler")
    names = [t.strip() for t in re_split(topic) if t.strip()]
    snap = mod.crawl_sentiment(
        targets=names or None,
        days=int(opts.get("days", 7)),
        max_articles=int(opts.get("limit", 60)),
        max_total_seconds=int(opts.get("timeout", 45)),
        negative_only=bool(opts.get("negative_only", False)),
        positive_only=bool(opts.get("positive_only", False)),
    )
    data = snap.to_dict() if hasattr(snap, "to_dict") else {}
    arts = [dict(a) for a in (data.get("articles") or [])]
    arts, blocked = _split_placeholders(arts)
    meta = [{"kind": "snapshot", "snapshot_id": data.get("snapshot_id", ""),
             "stats": data.get("stats") or {}}]
    if blocked:
        meta.append({
            "kind": "placeholders_dropped", "count": len(blocked),
            "sources": sorted({str(a.get("source", "")) for a in blocked
                               if a.get("source")})[:12],
            "note": f"{len(blocked)} 条是抓取失败占位（没有正文），已从样本中剔除",
            "next_step": "跑 get_anti_block_status() 判断是否被拦；"
                         "需要浏览器兜底先装 playwright（pip install playwright）",
        })
    return arts, meta


def _fetch_web(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    urls = [t.strip() for t in re_split(topic) if t.strip().startswith("http")]
    if not urls:
        raise PipelineError("E_TOPIC_EMPTY", detail="web intent 需要 URL")
    arch = _import_target("scripts.fullpage_archiver")
    rec = _import_target("scripts.web_recognizer")
    out: List[Dict[str, Any]] = []
    meta: List[Dict[str, Any]] = []
    for url in urls:
        try:
            res = arch.quick_archive(url)
        except Exception as exc:
            meta.append({"kind": "error", "url": url, **_err("E_FETCH_FAILED", detail=str(exc))})
            continue
        html = ""
        if isinstance(res, dict):
            html = res.get("html") or res.get("content") or ""
        elif isinstance(res, str):
            html = res
        item = {"title": (res.get("title") if isinstance(res, dict) else "") or url,
                "url": url, "source": urlparse(url).netloc,
                "content": (html[:2000] if html else ""),
                "publish_time": "", "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if html:
            try:
                item["extracted"] = rec.smart_extract(html, url)
            except Exception as exc:
                meta.append({"kind": "warn", "url": url,
                             "note": f"smart_extract 失败: {exc}"})
        out.append(item)
    return out, meta


def _fetch_search_crawl(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    se = _import_target("scripts.search_engine")
    results = se.quick_search(topic, max_results=int(opts.get("limit", 20)))
    urls = [r.url for r in results if getattr(r, "url", "")]
    meta = [{"kind": "search", "query": topic, "hits": len(results),
             "engines": sorted({getattr(r, "engine", "") for r in results})}]
    if not urls:
        return [], meta
    enh = _import_target("scripts.crawl_enhancer")
    rec = _import_target("scripts.web_recognizer")
    crawled = enh.batch_smart_crawl(urls[:int(opts.get("limit", 20))],
                                    concurrency=int(opts.get("concurrency", 4)))
    out: List[Dict[str, Any]] = []
    for row in crawled or []:
        if not isinstance(row, dict):
            continue
        html = row.get("html") or row.get("content") or ""
        url = row.get("url", "")
        item = {"title": row.get("title") or url, "url": url,
                "source": urlparse(url).netloc,
                "content": html[:2000],
                "publish_time": row.get("publish_time", ""),
                "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        if html:
            try:
                item["extracted"] = rec.smart_extract(html, url)
            except Exception:
                pass
        out.append(item)
    return out, meta


def _fetch_institution(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    reg_mod = _import_target("scripts.scrapable_registry")
    registry = reg_mod.ScrapableRegistry()
    keyword = (topic or "").strip()
    rows = registry.search(keyword) if keyword else registry.list_scrapable()
    stats = registry.get_statistics()
    out = [{"title": f"{r.get('name', '')} [{r.get('type', '')}]",
            "url": r.get("website", "") or "",
            "source": "institution_registry",
            "content": "可爬" if r.get("scrapable") else "无官网",
            "publish_time": "", "type": r.get("type", ""),
            "scrapable": bool(r.get("scrapable"))}
           for r in (rows or [])[:int(opts.get("limit", 50))]]
    meta = [{"kind": "registry", "total": stats.get("total", 0),
             "scrapable": stats.get("scrapable", 0), "returned": len(out)}]
    return out, meta


def _fetch_market(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    code = (topic or "").strip()
    hub_mod = _import_target("scripts.realtime_data_hub")
    hub = hub_mod.get_hub()
    quotes = hub.quotes([code]) if hasattr(hub, "quotes") else {}
    rows: List[Dict[str, Any]] = []
    data = quotes.get("data") if isinstance(quotes, dict) else None
    items = data if isinstance(data, list) else (quotes if isinstance(quotes, list) else [])
    for q in items or []:
        if not isinstance(q, dict):
            continue
        rows.append({"title": f"{q.get('name', '')} {q.get('symbol', '')}".strip(),
                     "url": "", "source": q.get("source", ""),
                     "content": json.dumps(q, ensure_ascii=False)[:1200],
                     "publish_time": q.get("asof", q.get("timestamp", ""))})
    meta = [{"kind": "market", "requested": code,
             "attempts": quotes.get("attempts", []) if isinstance(quotes, dict) else [],
             "missing": quotes.get("missing", []) if isinstance(quotes, dict) else []}]
    return rows, meta


def _fetch_announcement(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    cn = _import_target("scripts.cninfo_scraper")
    rows = cn.search_cninfo_announcements(topic, limit=int(opts.get("limit", 20)))
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        out.append({"title": r.get("title", r.get("announcement_title", "")),
                    "url": r.get("url", r.get("pdf_url", r.get("adjunctUrl", ""))),
                    "source": "cninfo",
                    "content": r.get("summary", "") or "",
                    "publish_time": r.get("time", r.get("announcementTime", ""))})
    return out, [{"kind": "announcement", "returned": len(out)}]


def _fetch_document(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    da = _import_target("scripts.document_analyzer")
    paths = [p.strip() for p in re_split(topic) if p.strip()]
    out, meta = [], []
    analyzer = da.DocumentAnalyzer()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_file():
            meta.append({"kind": "error", "path": raw,
                         **_err("E_FETCH_FAILED",
                                detail="文件不存在；document intent 传本地文件路径（逗号分隔）")})
            continue
        try:
            res = analyzer.analyze(str(path))
        except Exception as exc:
            meta.append({"kind": "error", "path": raw,
                         **_err("E_FETCH_FAILED", detail=f"{type(exc).__name__}: {exc}")})
            continue
        if res.get("error"):
            meta.append({"kind": "error", "path": raw,
                         **_err("E_FETCH_FAILED", detail=str(res["error"]))})
            continue
        out.append({"title": path.name, "url": str(path), "source": "local_document",
                    "content": (res.get("summary") or "")[:2000],
                    "publish_time": "",
                    "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "document": res})
    return out, meta


def _fetch_login_page(topic: str, opts: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ls = _import_target("scripts.login_session")
    http = _import_target("scripts.http_utils")
    profile = opts.get("profile", "")
    url = next((t for t in re_split(topic) if t.strip().startswith("http")), "")
    if not url:
        raise PipelineError("E_TOPIC_EMPTY", detail="login_page intent 需要目标 URL")
    status = ls.check_login(name=profile, url=url) if profile else \
        ls.check_login(url=url)
    if status.get("logged_in") is False and profile:
        raise PipelineError("E_LOGIN_REQUIRED", profile=profile,
                            detail=status.get("verdict", ""))
    host = urlparse(url).netloc
    sess = http.get_session(reuse=False)
    injected = ls.apply_to_session(sess, host, profile) if profile else 0
    if profile and injected == 0:
        raise PipelineError("E_LOGIN_REQUIRED", profile=profile,
                            detail="档案里没有适用于该域的 Cookie")
    resp = http.http_get(url, session=sess, timeout=int(opts.get("timeout", 20)),
                         use_cache=False)
    if resp is None:
        raise PipelineError("E_FETCH_FAILED", detail=f"抓取失败：{host}")
    body = getattr(resp, "text", "") or ""
    out = [{"title": url, "url": url, "source": host, "content": body[:2000],
            "publish_time": "", "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}]
    meta = [{"kind": "login", "profile": profile, "cookies_injected": injected,
             "login_verdict": status.get("verdict"), "health": status.get("health")}]
    return out, meta


FETCHERS: Dict[str, Fetcher] = {
    "sentiment": _fetch_sentiment,
    "web": _fetch_web,
    "search_crawl": _fetch_search_crawl,
    "institution": _fetch_institution,
    "market": _fetch_market,
    "announcement": _fetch_announcement,
    "document": _fetch_document,
    "login_page": _fetch_login_page,
}


def re_split(text: str) -> List[str]:
    """逗号/顿号/换行/分号分隔——支持中文输入法里的全角逗号。"""
    import re as _re
    return [t for t in _re.split(r"[,，;；、\n\r]+", text or "") if t.strip()]


# ============================================================================
# 数据质量与"能回答什么"
# ============================================================================


def _field_fill_rate(records: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    if not records:
        return {}
    keys = ("title", "url", "source", "publish_time", "content")
    out = {}
    for k in keys:
        filled = sum(1 for r in records
                     if str(r.get(k, "") or "").strip())
        out[k] = round(filled / len(records), 3)
    return out


def _freshness(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """按 publish_time/fetch_time 估计数据新鲜度；解析不了就说不知道。"""
    stamps = []
    for r in records:
        raw = str(r.get("publish_time") or r.get("fetch_time") or "").strip()
        if not raw:
            continue
        epoch = _to_epoch_local(raw)
        if epoch:
            stamps.append(epoch)
    if not stamps:
        return {"status": "unknown", "newest": None, "oldest": None,
                "note": "记录里没有可解析的时间戳，无法判断新鲜度"}
    now = time.time()
    newest, oldest = max(stamps), min(stamps)
    age_days = (now - newest) / 86400.0
    status = ("realtime_like" if age_days < 1 else
              "recent" if age_days < 8 else
              "stale" if age_days < 45 else "historical")
    return {"status": status,
            "newest": _iso_local(newest), "oldest": _iso_local(oldest),
            "newest_age_days": round(age_days, 2),
            "note": {"realtime_like": "最新一条在 24 小时内；注意这仍是抓取快照，非实时推送",
                     "recent": "最新一条在 8 天内",
                     "stale": "最新一条在 8-45 天前，做当期结论要标明时间窗",
                     "historical": "全部超过 45 天，只适合做历史对照"}[status]}


def _to_epoch_local(value: str) -> Optional[float]:
    mod = _import_target("scripts.login_session")  # 复用同一套时间解析，避免两处漂移
    return mod._to_epoch(value)


def _iso_local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def quality_score(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """给这批数据打 0-100 的质量分并拆成可解释的四项。

    分量：volume（样本量）、fill（关键字段填充率）、source（来源分散度）、
    freshness（新鲜度可判）。缺项不遮，直接进 ``issues``。
    """
    n = len(records)
    fill = _field_fill_rate(records)
    sources = {str(r.get("source", "") or "").strip() for r in records}
    sources.discard("")
    urls = {str(r.get("url", "") or "").strip() for r in records} - {""}
    fresh = _freshness(records)

    vol = 0.0 if n == 0 else min(30.0, 6.0 + 24.0 * min(1.0, n / 40.0))
    core_fill = [fill.get(k, 0.0) for k in ("title", "source", "publish_time")]
    fill_score = round(25.0 * (sum(core_fill) / len(core_fill)), 1) if n else 0.0
    src_score = round(20.0 * min(1.0, len(sources) / 4.0), 1) if n else 0.0
    if n == 0:
        fresh_score = 0.0  # 没有记录，谈不上新鲜度，不给"未知"兜底分
    elif fresh["status"] == "unknown":
        fresh_score = 5.0
    elif fresh["status"] in ("realtime_like", "recent"):
        fresh_score = 25.0
    elif fresh["status"] == "stale":
        fresh_score = 12.0
    else:
        fresh_score = 8.0
    total = round(min(100.0, vol + fill_score + src_score + fresh_score), 1)

    issues: List[str] = []
    if n == 0:
        issues.append("没有记录，任何统计都不成立")
    elif n < 10:
        issues.append(f"样本仅 {n} 条，占比类结论容易抖动，建议 days/limit 放大后复核")
    if fill.get("publish_time", 0) < 0.6:
        issues.append("publish_time 填充率低于 60%，时间窗过滤与趋势分析不可靠")
    if fill.get("url", 0) < 0.5:
        issues.append("url 填充率低，引用回溯会缺链")
    if len(sources) < 2 and n >= 5:
        issues.append("只有单一来源，跨源一致性无法验证")
    if fresh["status"] in ("stale", "historical"):
        issues.append(fresh["note"])

    grade = ("A" if total >= 85 else "B" if total >= 70 else
             "C" if total >= 50 else "D")
    return {
        "score": total, "grade": grade,
        "components": {"volume": round(vol, 1), "field_fill": fill_score,
                       "source_spread": src_score, "freshness": fresh_score},
        "counts": {"records": n, "sources": len(sources), "unique_urls": len(urls)},
        "fill_rate": fill, "freshness": fresh, "issues": issues,
    }


def answer_ability(intent: str, records: Sequence[Dict[str, Any]],
                   quality: Dict[str, Any]) -> Dict[str, Any]:
    """明确说：这批数据**能**回答什么、**不能**回答什么。

    这一项直接对着 TRACE 的「能力边界是否可预期」——宁可说不支持，
    也不给一个撑不住的结论。
    """
    spec = _INTENT_BY_ID.get(intent, _INTENT_BY_ID[DEFAULT_INTENT])
    n = len(records)
    has_time = quality["fill_rate"].get("publish_time", 0) >= 0.6
    multi_source = quality["counts"]["sources"] >= 2
    can = [f"{spec['label']}的清单与条目枚举（当前 {n} 条）"]
    if n >= 10 and has_time:
        can += ["按时间桶的趋势与峰值时段", "情感/严重度分布与占比"]
    if multi_source:
        can += ["跨源是否一致（同一事件的多源印证）", "冲突值与共识值对比"]
    if any((r.get("content") or "") for r in records):
        can.append("关键词与实体抽取")
    cannot = [f"{spec['not_for'][0]}"] if spec.get("not_for") else []
    cannot.append("因果结论（本技能只做检索与统计，不做归因推断）")
    if not has_time:
        cannot.append("『最近 N 天内发生了哪些』——时间戳缺失，时间窗过滤不可信")
    if not multi_source:
        cannot.append("跨源一致性验证——只有单一来源")
    if n < 10:
        cannot.append("比例/排名类结论——样本不足，容易受个别记录牵动")
    confidence = ("high" if n >= 30 and has_time and multi_source else
                  "medium" if n >= 10 else "low")
    return {
        "can_answer": can,
        "cannot_answer": cannot,
        "confidence": confidence,
        "why": {"records": n, "time_field_ok": has_time,
                "cross_source_ok": multi_source},
    }


# ============================================================================
# 分析 / 融合 / 导出
# ============================================================================


def _analyze(records: List[Dict[str, Any]], days: int) -> Optional[Dict[str, Any]]:
    try:
        ae = _import_target("scripts.analysis_enhanced")
        return ae.enhanced_aggregate(records, days=days) if records else None
    except ImportError:
        log.info("analysis_enhanced 不可用，跳过分析")
        return None
    except Exception as exc:
        log.warning("enhanced_aggregate 失败: %s", exc)
        return None


def _fuse(records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    try:
        ef = _import_target("scripts.evidence_fusion")
        return ef.fusion_aggregate(records, top=5) if records else None
    except ImportError:
        return None
    except Exception as exc:
        log.warning("fusion_aggregate 失败: %s", exc)
        return None


def _mark_plan(result: Dict[str, Any], step: str, status: str) -> None:
    """把真实执行状态写回 plan——回执里不该整条链路都挂着 planned。"""
    for p in result.get("plan") or []:
        if p.get("step") == step:
            p["status"] = status
            return


#: 采集段之后由 gather_info 自己执行的步骤；它们之前的都归"采集链"
_POST_FETCH_STEPS = {"fuse_evidence", "analyze", "export"}


def _mark_fetch_steps(result: Dict[str, Any], status: str) -> None:
    """逐步标采集段的状态。

    一条 intent 的选源/抓取/去重由同一个 fetcher 一次跑完，但回执要按真实步骤标出来，
    否则用户看到一串 planned，会以为还有几步得自己做——这正是评测说的"链路衔接不清楚"。
    """
    for p in result.get("plan") or []:
        if p.get("step") in _POST_FETCH_STEPS:
            return
        p["status"] = status


def _next_step(result: Dict[str, Any]) -> str:
    if not result["records"]:
        ph = next((g for g in result.get("gaps") or []
                   if isinstance(g, dict)
                   and g.get("kind") == "placeholders_dropped"), None)
        if ph:
            return (f"这一轮 {ph['count']} 条全是抓取失败占位（被拦/超时/需登录），"
                    f"没有一条拿到正文；先 get_anti_block_status() 判断拦截，"
                    f"再决定装 playwright 走浏览器兜底或建登录档案")
        return "先 dry_run=True 看计划，确认源与时间窗后再实跑"
    if result["quality"]["grade"] in ("C", "D"):
        return "放大 days/limit 或补 source_categories 再采一轮，质量到 B 以上再出报告"
    if result.get("artifacts", {}).get("zip_path"):
        return f"下载包已生成：{result['artifacts']['zip_path']}（可 verify_package 校验）"
    return "需要交付物就加 package=True；只要文本结论用 render_pipeline_markdown()"


def gather_info(topic: str = "",
                intent: str = "auto",
                records: Optional[Sequence[Dict[str, Any]]] = None,
                fetcher: Optional[Fetcher] = None,
                days: int = 7,
                limit: int = 60,
                timeout: int = 45,
                profile: str = "",
                analyze: bool = True,
                fuse: bool = True,
                package: bool = False,
                out_dir: str = "",
                dry_run: bool = False,
                **opts: Any) -> Dict[str, Any]:
    """一次调用跑完整条信息获取链路。

    Args:
        topic: 自然语言主题 / 机构名 / URL / 代码，按 intent 解释
        intent: auto 或 ``list_intents()`` 里的 id；写错会报 E_ROUTE_NOT_FOUND
        records: 直接喂现成记录（跳过抓取，用于二次分析与测试）
        fetcher: 自定义抓取实现 ``(topic, opts) -> (records, meta)``
        days/limit/timeout: 时间窗、条数上限、抓取预算秒数
        profile: 登录档案名（intent=login_page 用）
        analyze: 是否跑增强聚合（关键词/趋势/跨源/洞察）
        fuse: 是否跑多源证据融合（共识值 + 冲突台账 + 引用包）
        package: 是否连同 Markdown/JSON 一起打成 zip（交付物）
        out_dir: 落盘目录；为空则只在内存返回，不写盘
        dry_run: 只出计划与契约，不发任何请求

    Returns:
        见模块 docstring——固定包含 intent / plan / records / quality /
        answer_ability / gaps / errors / next_step。
    """
    started = time.time()
    routed = route(topic, intent)
    intent_id = routed["intent"]
    spec = _INTENT_BY_ID[intent_id]
    result: Dict[str, Any] = {
        "ok": True,
        "topic": topic,
        "intent": intent_id,
        "intent_label": spec["label"],
        "confidence": routed["confidence"],
        "matched_keywords": routed["matched"],
        "contract": {"input": spec["input"], "output": spec["output"],
                     "not_for": spec["not_for"], "requires": spec["requires"],
                     "typical_seconds": spec["seconds_typical"]},
        "plan": plan_for(intent_id, topic, days=days, limit=limit),
        "params": {"days": days, "limit": limit, "timeout": timeout,
                   "profile": profile},
        "records": [], "counts": {}, "meta": [], "analysis": None, "evidence": None,
        "quality": quality_score([]), "answer_ability": {},
        "gaps": [], "errors": [], "artifacts": {},
        "elapsed_seconds": 0.0,
    }

    if dry_run:
        result["mode"] = "dry_run"
        result["plan"] = [{**p, "status": "planned"} for p in result["plan"]]
        result["next_step"] = ("确认无误后去掉 dry_run 实跑；"
                               f"该 intent 典型耗时 {spec['seconds_typical']}")
        return result

    # ── 采集 ────────────────────────────────────────────────
    if records is not None:
        got = [dict(r) for r in records]
        meta: List[Dict[str, Any]] = [{"kind": "injected", "records": len(got)}]
        _mark_fetch_steps(result, "skipped(input records)")
    else:
        fn = fetcher or FETCHERS.get(intent_id)
        if fn is None:
            raise PipelineError("E_ROUTE_NOT_FOUND", intent=intent_id)
        try:
            got, meta = fn(topic, {"days": days, "limit": limit, "timeout": timeout,
                                   "profile": profile, **opts})
        except PipelineError as exc:
            result["errors"].append(exc.to_dict())
            result["ok"] = False
            got, meta = [], []
        except ImportError as exc:
            result["errors"].append(_err("E_DEP_MISSING", detail=str(exc)))
            result["ok"] = False
            got, meta = [], []
        except Exception as exc:
            result["errors"].append(_err("E_FETCH_FAILED", detail=f"{type(exc).__name__}: {exc}"))
            result["ok"] = False
            got, meta = [], []
        # 一条 intent 的采集段（选源/抓取/去重）由同一个 fetcher 内部一次跑完，
        # 回执里逐步标状态，别让"planned"骗人以为还有几步要手动做
        _mark_fetch_steps(result, "done(采集链)" if got else "empty(采集链)")

    result["records"] = got
    result["meta"] = meta
    result["counts"] = {
        "records": len(got),
        "sources": len({str(r.get("source", "") or "") for r in got} - {""}),
        "unique_urls": len({str(r.get("url", "") or "") for r in got} - {""}),
    }
    if not got and result["ok"]:
        result["errors"].append(_err("E_SOURCE_EMPTY"))
        result["ok"] = False
    result["quality"] = quality_score(got)
    result["answer_ability"] = answer_ability(intent_id, got, result["quality"])

    # ── 缺口：显式说出"哪里没答上"，而不是让用户自己猜 ──────────
    for m in meta:
        if m.get("kind") in ("error", "placeholders_dropped"):
            result["gaps"].append({k: v for k, v in m.items() if v})
        if m.get("kind") == "market" and m.get("missing"):
            result["gaps"].append({"kind": "market_missing", "codes": m["missing"],
                                   "note": "这些代码三个源都没答上"})
    if result["quality"]["issues"]:
        result["gaps"].extend({"kind": "quality", "note": i}
                              for i in result["quality"]["issues"])

    # ── 分析 / 融合 ─────────────────────────────────────────
    if got and analyze:
        result["analysis"] = _analyze(got, days)
        _mark_plan(result, "analyze", "done" if result["analysis"] else "failed")
        if result["analysis"]:
            kept = int(result["analysis"].get("total") or 0)
            if kept == 0 and got:
                result["gaps"].append({
                    "kind": "time_window",
                    "note": f"采集到 {len(got)} 条，但 days={days} 时间过滤后没有样本"
                            f"进入分析——放大 days 或把 days 设为 0 可全量分析",
                })
    if got and fuse:
        result["evidence"] = _fuse(got)
        _mark_plan(result, "fuse_evidence", "done" if result["evidence"] else "failed")

    # ── 落盘 / 打包 ─────────────────────────────────────────
    if got and out_dir:
        try:
            result["artifacts"] = _write_artifacts(
                result, Path(out_dir), package=package)
            _mark_plan(result, "export", "done")
        except Exception as exc:
            result["errors"].append(_err("E_PACKAGE_FAILED", detail=str(exc)))
            _mark_plan(result, "export", "failed")
    elif got:
        _mark_plan(result, "export", "skipped(no out_dir)")

    result["elapsed_seconds"] = round(time.time() - started, 2)
    result["next_step"] = _next_step(result)
    return result


def _write_artifacts(result: Dict[str, Any], out_dir: Path,
                     package: bool = False) -> Dict[str, Any]:
    """写 JSON + Markdown 回执；package=True 时再压成 zip（含 manifest）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = re_split(result.get("topic", "")) or [result.get("intent", "pipeline")]
    base = str(slug[0])[:24].replace(" ", "_")
    json_path = out_dir / f"{base}_{stamp}.json"
    md_path = out_dir / f"{base}_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2,
                                    default=str), encoding="utf-8")
    md_path.write_text(render_pipeline_markdown(result), encoding="utf-8")
    artifacts = {"json_path": str(json_path), "markdown_path": str(md_path)}
    if package:
        packager = _import_target("scripts.crawl_packager")
        artifacts.update(packager.build_delivery_package(
            zip_name=f"{base}_{stamp}",
            files=[str(json_path), str(md_path)],
            manifest_payload={
                "topic": result.get("topic", ""),
                "intent": result.get("intent", ""),
                "records": result["counts"]["records"],
                "quality_score": result["quality"]["score"],
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            output_dir=str(out_dir),
        ))
    return artifacts


# ============================================================================
# Markdown 回执（结论卡形状，可直接贴给用户）
# ============================================================================


#: 统计概览的字段中文名（回执给人读，不直接 dump dict repr）
_STAT_LABELS = {
    "sentiment": "情感分布", "severity": "严重度分布", "source_top": "来源分布",
    "by_day": "按天条数", "by_source_type": "来源类型", "hot_keywords": "高频词",
    "avg_score": "平均情感分", "neg_ratio": "负面占比", "pos_ratio": "正面占比",
    "date_range": "时间范围", "entities": "实体",
}


def _format_stat_value(value: Any) -> str:
    if isinstance(value, dict):
        return "、".join(f"{k} {v}" for k, v in list(value.items())[:8])
    if isinstance(value, (list, tuple)):
        parts = []
        for item in value[:8]:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                parts.append(f"{item[0]} {item[1]}")
            else:
                parts.append(str(item))
        return "、".join(parts)
    return str(value)


def render_pipeline_markdown(result: Dict[str, Any]) -> str:
    """把一次 gather_info 渲染成 Markdown 回执——含边界与缺口，不只报喜。"""
    q = result["quality"]
    aa = result["answer_ability"]
    lines = [f"# 信息获取回执 · {result.get('intent_label', '')}", ""]
    lines.append(f"- 主题：{result.get('topic') or '（直接喂入 records）'}")
    lines.append(f"- 路由：intent=`{result.get('intent')}`"
                 f"（置信度 {result.get('confidence')}）")
    c = result.get("counts") or {}
    if result.get("mode") == "dry_run":
        lines.append(f"- 采集：未执行（dry_run）— 计划 {len(result.get('plan') or [])} 步，"
                     f"窗口 days={result.get('params', {}).get('days')}")
    else:
        lines.append(f"- 采集：{c.get('records', 0)} 条 / "
                     f"{c.get('sources', 0)} 个来源 / "
                     f"{c.get('unique_urls', 0)} 个唯一链接")
    lines.append(f"- 质量分：**{q['score']}（{q['grade']}）** — "
                 f"样本 {q['components']['volume']} / 填充 {q['components']['field_fill']} / "
                 f"来源分散 {q['components']['source_spread']} / "
                 f"新鲜度 {q['components']['freshness']}")
    lines.append(f"- 耗时：{result.get('elapsed_seconds', 0)} 秒")
    lines.append("")
    if result.get("contract"):
        lines.append("## 这批数据的边界")
        lines.append(f"- 能回答：{'；'.join(aa.get('can_answer') or ['—'])}")
        lines.append(f"- 不能回答：{'；'.join(aa.get('cannot_answer') or ['—'])}")
        lines.append(f"- 不支持场景：{'；'.join(result['contract']['not_for'])}")
        lines.append(f"- 结论置信度：{aa.get('confidence', 'low')}")
        lines.append("")
    ev = result.get("evidence") or {}
    bundles = ev.get("top_evidence") if isinstance(ev, dict) else None
    if bundles:
        lines.append("## 跨源共识事件")
        for b in bundles[:5]:
            if not isinstance(b, dict):
                lines.append(f"- {b}")
                continue
            text = (b.get("statement") or b.get("title")
                    or b.get("event") or "（无标题）")
            srcs = "、".join(str(s) for s in (b.get("supporting_sources") or [])[:4])
            conf = b.get("confidence")
            extra = []
            if srcs:
                extra.append(f"来源 {srcs}")
            if b.get("sentiment"):
                extra.append(str(b["sentiment"]))
            if isinstance(conf, (int, float)):
                extra.append(f"置信 {conf:.2f}")
            gap = b.get("gaps") or []
            if gap:
                extra.append(f"缺口：{gap[0]}")
            lines.append(f"- {text}" + (f"（{'；'.join(extra)}）" if extra else ""))
        conflicts = ev.get("conflicts", 0)
        n_conflicts = len(conflicts) if isinstance(conflicts, (list, tuple)) else conflicts
        if n_conflicts:
            lines.append(f"- ⚠️ 存在 {n_conflicts} 处跨源分歧，详见 evidence.conflicts")
        lines.append("")
    an = result.get("analysis") or {}
    if isinstance(an, dict):
        insights = an.get("insights") or []
        if insights:
            lines.append("## 自动洞察")
            for ins in insights[:8]:
                if isinstance(ins, dict):
                    body = ins.get("detail") or ins.get("text") or ""
                    head = ins.get("title") or body[:40]
                    lines.append(f"- [{ins.get('level', 'info')}] {head} — {body}")
                    ev_items = ins.get("evidence") or []
                    if ev_items:
                        lines.append(f"  - 依据：{'；'.join(str(e) for e in ev_items[:4])}")
                else:
                    lines.append(f"- {ins}")
            lines.append("")
        stats = an.get("statistics") or {}
        if stats:
            lines.append("## 统计概览")
            lines.append(f"- 进入分析：{an.get('total', 0)} 条"
                         f"（采集 {an.get('raw_total', 0)} 条，"
                         f"days={result.get('params', {}).get('days', 0)} 时间过滤）")
            for k, v in list(stats.items())[:12]:
                if k == "total" or v in (None, "", [], {}, 0, 0.0):
                    continue
                lines.append(f"- {_STAT_LABELS.get(k, k)}：{_format_stat_value(v)}")
            lines.append("")
    if result.get("gaps"):
        lines.append("## 缺口与风险")
        for g in result["gaps"]:
            if isinstance(g, dict):
                lines.append(f"- {g.get('note') or g.get('reason') or g}")
            else:
                lines.append(f"- {g}")
        lines.append("")
    if result.get("errors"):
        lines.append("## 失败步骤")
        for e in result["errors"]:
            lines.append(f"- `{e['code']}` {e['cause']}")
            lines.append(f"  - 下一步：{e['next_step']}")
        lines.append("")
    if result.get("plan"):
        lines.append("## 执行轨迹")
        for p in result["plan"]:
            lines.append(f"- {p['no']}. {p['step']}（{p.get('tool', '')}）"
                         f"→ {p.get('status', '')}")
        lines.append("")
    art = result.get("artifacts") or {}
    if art:
        lines.append("## 交付物")
        for k, v in art.items():
            if v:
                lines.append(f"- {k}: {v}")
        lines.append("")
    lines.append(f"> 下一步：{result.get('next_step', '')}")
    return "\n".join(lines)


def self_check() -> Dict[str, Any]:
    """离线自检：路由、契约、质量分、渲染都不依赖网络。"""
    out: Dict[str, Any] = {"module": "pipeline_runner", "ok": True}
    try:
        ids = [i["id"] for i in INTENTS]
        out["intents"] = len(ids)
        out["intents_unique"] = len(set(ids)) == len(ids)
        out["fetchers_cover_intents"] = all(i in FETCHERS for i in ids)
        out["routes_negative"] = route("贵州茅台最近7天负面舆情")["intent"] == "sentiment"
        out["routes_market"] = route("510300 现在价格")["intent"] == "market"
        out["routes_login"] = route("用我的登录会话抓这个页面")["intent"] == "login_page"
        out["routes_fallback"] = route("")["confidence"] in ("low",)
        demo = [{"title": f"样本 {i}", "url": f"https://a.com/{i}",
                 "source": ["第一财经", "东方财富", "财联社"][i % 3],
                 "publish_time": "2026-10-2" + str(i % 6) + " 10:00:00",
                 "content": "净值上涨 3.2%"} for i in range(12)]
        out["quality_demo"] = quality_score(demo)["grade"]
        out["quality_empty_ok"] = quality_score([])["score"] == 0.0
        out["ability_has_cannot"] = bool(answer_ability(
            "sentiment", demo, quality_score(demo))["cannot_answer"])
        out["render_ok"] = "# 信息获取回执" in render_pipeline_markdown(
            gather_info(records=demo, intent="sentiment"))
        out["dry_run_no_records"] = gather_info(topic="工银瑞信基金",
                                                intent="sentiment",
                                                dry_run=True)["mode"] == "dry_run"
        for k, v in out.items():
            if k != "module" and v is False:
                out["ok"] = False
    except Exception as exc:
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


__all__ = [
    "INTENTS", "PIPELINE_ERRORS", "PipelineError", "answer_ability",
    "describe", "gather_info", "list_intents", "plan_for", "quality_score",
    "render_pipeline_markdown", "route", "self_check",
]


# ==================== CLI 入口 ====================


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：`python scripts/pipeline_runner.py "贵州茅台最近7天负面舆情"`。

    退出码 0=ok，1=不 ok（空结果/抓取失败/自检失败），便于脚本判断。
    """
    import argparse

    ap = argparse.ArgumentParser(
        description="信息获取一键编排（路由 → 抓取 → 质量分 → 回执 → 交付包）")
    ap.add_argument("topic", nargs="*", default=[],
                    help="自然语言主题 / 机构名 / URL / 代码")
    ap.add_argument("--intent", default="auto",
                    help="auto 或 " + "/".join(i["id"] for i in INTENTS))
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--profile", default="", help="登录档案名（intent=login_page）")
    ap.add_argument("--out-dir", default="", help="产物落盘目录（绝对路径）")
    ap.add_argument("--package", action="store_true", help="连同回执打成 sha256 交付包")
    ap.add_argument("--dry-run", action="store_true", help="只出计划与契约，不发请求")
    ap.add_argument("--route-only", action="store_true", help="只打印路由结论与备选")
    ap.add_argument("--self-check", action="store_true", help="离线自检三条线")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON 而非 Markdown 回执")
    args = ap.parse_args(list(argv) if argv is not None else None)

    # 只在 CLI 里改编码：本模块也会被 MCP 进程导入，导入期动 stdout 会打断协议帧
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.self_check:
        report = self_check()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("ok") else 1

    topic = " ".join(args.topic).strip()
    if args.route_only:
        print(json.dumps(route(topic, args.intent), ensure_ascii=False, indent=2))
        return 0

    res = gather_info(topic=topic, intent=args.intent, days=args.days,
                      limit=args.limit, profile=args.profile, package=args.package,
                      out_dir=args.out_dir, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        print(render_pipeline_markdown(res))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
