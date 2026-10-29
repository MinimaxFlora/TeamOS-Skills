# -*- coding: utf-8 -*-
"""
MCP Resource + Prompt 暴露模块 (mcp_resources_prompts.py)

在 tool 之外再暴露两组"可直接读"的能力，清单以 RESOURCE_REGISTRY / PROMPT_REGISTRY 为准：
- **Resource**：媒体源/机构库/查询类型/报告模板/缓存索引/数据源健康/反爬状态/调度状态
  + v11.0 实时中枢状态与技术指标目录
- **Prompt 模板**：高频查询/分析/导出场景 + v11.0 实时速览、技术诊断、证据优先汇总、冲突核对

设计原则：
- 零强依赖：所有 Resource Provider 都内置降级实现
- 函数式注册：`register_resources_and_prompts(server)` 一行接入
- 线程安全；持久化数据尽量小
- 与 financial_data_mcp / anti_block_enhanced / crawler_dispatcher / realtime_data_hub 联动

向后兼容：所有函数独立，不修改任何现有模块。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


# ============================================================================
# 1. Resource Providers（每个返回 (text, mime_type)）
# ============================================================================

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _safe_read_json(filename: str, default: Any = None) -> Any:
    """安全读取 data 目录下的 JSON。"""
    p = DATA_DIR / filename
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log.warning("read %s failed: %s", filename, exc)
        return default if default is not None else {}


def _to_text(value: Any, indent: int = 2) -> str:
    """统一 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, indent=indent, default=str)


# ---------- Resource 1: media-sources（68 个媒体源） ----------

def resource_media_sources() -> str:
    """cnfs://media-sources — 媒体源清单（含搜索引擎 + 权威 + 垂直 + 自媒体 + 国际）。"""
    sources = _safe_read_json("sentiment_sources.json", default={})
    if not sources:
        # 降级：从现有 sentiment_crawler 模块加载
        try:
            from scripts.sentiment_crawler import SENTIMENT_SOURCES  # type: ignore
            sources = SENTIMENT_SOURCES  # type: ignore
        except Exception:  # noqa: BLE001
            sources = {"note": "sentiment_sources.json not found"}
    return _to_text(sources)


# ---------- Resource 2: institutions（机构库摘要） ----------

def resource_institutions() -> str:
    """cnfs://institutions — 机构库摘要（前 100 家 + 类型统计）。"""
    registry = _safe_read_json("institution_registry.json", default=[])
    # 兼容 CSV-style dict（{"_f": "...", "c": [...], "d": [...]}）
    if isinstance(registry, dict):
        if "d" in registry and "c" in registry:
            columns = registry.get("c", [])
            rows = registry.get("d", [])
            records = [dict(zip(columns, row)) for row in rows]
            summary = {
                "total": len(records),
                "by_type": {},
                "sample": records[:100],
            }
            for inst in records:
                t = inst.get("type", "unknown")
                summary["by_type"][t] = summary["by_type"].get(t, 0) + 1
            return _to_text(summary)
        return _to_text(registry)
    if isinstance(registry, list):
        summary = {
            "total": len(registry),
            "by_type": {},
            "sample": registry[:100],
        }
        for inst in registry:
            t = inst.get("type", "unknown")
            summary["by_type"][t] = summary["by_type"].get(t, 0) + 1
        return _to_text(summary)
    return _to_text(registry)


# ---------- Resource 3: query-types（60+ 查询类型） ----------

def resource_query_types() -> str:
    """cnfs://query-types — 查询类型清单（v9 + v10 全部）。"""
    v9_types: List[str] = []
    v10_types: List[str] = []
    # v9: 来自 financial_data_mcp
    try:
        from scripts.financial_data_mcp import SOURCE_CAPABILITIES
        for src, caps in SOURCE_CAPABILITIES.items():
            for cap in caps:
                if cap not in v9_types:
                    v9_types.append(cap)
    except Exception as exc:  # noqa: BLE001
        log.debug("load v9 capabilities failed: %s", exc)
    # v10: 来自 mcp_data_expansion
    try:
        from scripts.mcp_data_expansion import ExpansionAdapterSet
        set_ = ExpansionAdapterSet()
        v10_types = set_.list_query_types()
    except Exception as exc:  # noqa: BLE001
        log.debug("load v10 types failed: %s", exc)
    result = {
        "v9_count": len(v9_types),
        "v10_count": len(v10_types),
        "total": len(v9_types) + len(v10_types),
        "v9_query_types": sorted(v9_types),
        "v10_query_types": sorted(v10_types),
    }
    return _to_text(result)


# ---------- Resource 4: report-templates（报告模板清单） ----------

def resource_report_templates() -> str:
    """cnfs://report-templates — 可用报告模板。"""
    templates: Dict[str, Any] = {"templates": []}
    try:
        from scripts.report_templates import TEMPLATES  # type: ignore
        for tid, tdef in TEMPLATES.items():
            templates["templates"].append({
                "id": tid,
                "name": getattr(tdef, "name", tid),
                "sections": getattr(tdef, "sections", []),
            })
    except Exception as exc:  # noqa: BLE001
        templates["note"] = f"failed to load templates: {exc}"
        templates["fallback_list"] = [
            "fund_research_report", "stock_research_report",
            "sentiment_summary", "policy_brief",
            "institution_brief", "research_report",
            "market_overview", "risk_alert",
        ]
    return _to_text(templates)


# ---------- Resource 5: cache-index（缓存索引） ----------

def resource_cache_index() -> str:
    """cnfs://cache-index — 查询缓存索引（来自 QueryCache）。"""
    try:
        from scripts.mcp_data_expansion import get_default_cache
        cache = get_default_cache()
        keys = cache.keys()[:100]
        return _to_text({
            "stats": cache.stats(),
            "sample_keys": keys,
            "total_keys": len(cache.keys()),
        })
    except Exception as exc:  # noqa: BLE001
        return _to_text({"error": str(exc)})


# ---------- Resource 6: data-source-health（数据源健康） ----------

def resource_data_source_health() -> str:
    """cnfs://data-source-health — v9 + v10 全部数据源健康状态。"""
    result: Dict[str, Any] = {"v9_sources": {}, "v10_sources": {}, "timestamp": datetime.now().isoformat(timespec="seconds")}

    try:
        from scripts.financial_data_mcp import FinancialDataRouter
        router = FinancialDataRouter()
        result["v9_sources"] = router.list_sources()
    except Exception as exc:  # noqa: BLE001
        result["v9_sources"] = {"error": str(exc)}

    try:
        from scripts.mcp_data_expansion import ExpansionAdapterSet
        set_ = ExpansionAdapterSet()
        result["v10_sources"] = set_.all_health()
    except Exception as exc:  # noqa: BLE001
        result["v10_sources"] = {"error": str(exc)}

    return _to_text(result)


# ---------- Resource 7: anti-block-status（反爬状态） ----------

def resource_anti_block_status() -> str:
    """cnfs://anti-block-status — 反爬系统当前状态。"""
    status: Dict[str, Any] = {"components": {}, "timestamp": datetime.now().isoformat(timespec="seconds")}

    try:
        from scripts.anti_block_enhanced import (
            ProxyPool, HealthTracker as ABHealthTracker,
            HeaderOrderRandomizer, FingerprintSession,
            RequestTempoScheduler, FreeProxyDiscovery,
        )
        # 创建示例实例并报告
        try:
            from scripts.anti_block_enhanced import AntiBlockPipeline
            pipe = AntiBlockPipeline()
            status["components"]["pipeline"] = pipe.report() if hasattr(pipe, "report") else "loaded"
        except Exception:  # noqa: BLE001
            status["components"]["pipeline"] = "unavailable"
        status["components"]["header_order_randomizer"] = "ready (chrome/firefox/safari templates)"
        status["components"]["fingerprint_session"] = "ready (30min / 100 req rotation)"
        status["components"]["tempo_scheduler"] = "ready (70% fast / 20% read / 10% long pause)"
        status["components"]["free_proxy_discovery"] = "ready (3 public pools)"
    except Exception as exc:  # noqa: BLE001
        status["components"]["error"] = str(exc)

    return _to_text(status)


# ---------- Resource 8: crawl-state（调度状态） ----------

def resource_crawl_state() -> str:
    """cnfs://crawl-state — CrawlerDispatcher 调度状态（含 registry/health/resume）。"""
    try:
        from scripts.crawler_dispatcher import (
            get_default_registry, CrawlerDispatcher, DispatchConfig,
        )
        # 创建临时 dispatcher 来读取状态
        with tempfile_TemporaryDirectory() as td:
            cfg = DispatchConfig(state_dir=td, enable_health_persist=False)
            reg = get_default_registry()
            d = CrawlerDispatcher(reg, cfg)
            return _to_text({
                "registry": reg.stats(),
                "health": d.health.report(),
                "resume": d.resume.progress(),
                "strategy": d.config.strategy.name,
                "note": "状态为当前进程；磁盘持久化数据见 data/crawl_state/",
            })
    except Exception as exc:  # noqa: BLE001
        return _to_text({"error": str(exc), "note": "crawler_dispatcher unavailable"})


def tempfile_TemporaryDirectory():
    import tempfile
    return tempfile.TemporaryDirectory()


# ---------- Resource 9: realtime-status（实时数据中枢） ----------

def resource_realtime_status() -> str:
    """cnfs://realtime-status — 实时行情中枢状态：交易时钟 / 订阅表 / 滚动仓 / 源链。"""
    try:
        try:
            from scripts.realtime_data_hub import get_hub  # type: ignore
        except ImportError:
            from realtime_data_hub import get_hub  # type: ignore
        hub = get_hub()
        st = hub.status()
        stored = st.get("stored_symbols") or []
        return _to_text({
            "clock": st.get("clock"),
            "watchlists": st.get("watchlists") or [],
            "stored_symbols": stored,
            "stored_count": len(stored),
            "last_poll": {k: st.get(k) for k in
                          ("last_quotes_at", "requested", "returned", "attempts")},
            "source_chain": list(hub.chain),
            "note": "只读内存快照，不触发网络；取数用 rt_realtime_quote 或 rt_watchlist(action=poll)",
        })
    except Exception as exc:  # noqa: BLE001
        return _to_text({"error": str(exc), "note": "realtime_data_hub unavailable"})


# ---------- Resource 10: indicator-catalog（技术指标目录） ----------

def resource_indicator_catalog() -> str:
    """cnfs://indicator-catalog — 全部技术指标的名称/中文名/所需字段/参数/最小样本量。"""
    try:
        try:
            from scripts.indicator_engine import available_indicators  # type: ignore
        except ImportError:
            from indicator_engine import available_indicators  # type: ignore
        rows = available_indicators()
        return _to_text({
            "count": len(rows),
            "indicators": rows,
            "anomaly_methods": ["zscore", "iqr", "ewma", "robust", "vote"],
            "note": "min_len 不足的序列不要硬算；needs 说明该指标要 high/low/volume 等哪些字段",
        })
    except Exception as exc:  # noqa: BLE001
        return _to_text({"error": str(exc), "note": "indicator_engine unavailable"})



# ============================================================================
# 2. Resource URI 映射
# ============================================================================

RESOURCE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "cnfs://media-sources": {
        "name": "media_sources",
        "title": "媒体源清单",
        "description": "68 个舆情媒体源（搜索引擎 + 权威 + 垂直 + 自媒体 + 国际）",
        "mime_type": "application/json",
        "provider": resource_media_sources,
    },
    "cnfs://institutions": {
        "name": "institutions",
        "title": "机构库摘要",
        "description": "2334 家金融机构清单 + 类型统计",
        "mime_type": "application/json",
        "provider": resource_institutions,
    },
    "cnfs://query-types": {
        "name": "query_types",
        "title": "查询类型清单",
        "description": "v9 + v10 全部查询类型（60+ 种）",
        "mime_type": "application/json",
        "provider": resource_query_types,
    },
    "cnfs://report-templates": {
        "name": "report_templates",
        "title": "报告模板",
        "description": "可用的研究报告/分析报告/简报模板",
        "mime_type": "application/json",
        "provider": resource_report_templates,
    },
    "cnfs://cache-index": {
        "name": "cache_index",
        "title": "查询缓存索引",
        "description": "高频查询缓存命中统计 + key 列表",
        "mime_type": "application/json",
        "provider": resource_cache_index,
    },
    "cnfs://data-source-health": {
        "name": "data_source_health",
        "title": "数据源健康状态",
        "description": "v9 5 适配器 + v10 4 适配器的健康度/成功率/延迟",
        "mime_type": "application/json",
        "provider": resource_data_source_health,
    },
    "cnfs://anti-block-status": {
        "name": "anti_block_status",
        "title": "反爬系统状态",
        "description": "v9.0 反爬深度增强的当前状态（pipeline + 指纹 + 节奏）",
        "mime_type": "application/json",
        "provider": resource_anti_block_status,
    },
    "cnfs://crawl-state": {
        "name": "crawl_state",
        "title": "爬取调度状态",
        "description": "CrawlerDispatcher 当前状态（registry / health / resume / strategy）",
        "mime_type": "application/json",
        "provider": resource_crawl_state,
    },
    "cnfs://realtime-status": {
        "name": "realtime_status",
        "title": "实时数据中枢状态",
        "description": "交易时钟 + 自选订阅表 + 滚动仓样本 + 多源降级链（v11.0）",
        "mime_type": "application/json",
        "provider": resource_realtime_status,
    },
    "cnfs://indicator-catalog": {
        "name": "indicator_catalog",
        "title": "技术指标目录",
        "description": "全部技术指标的名称/所需字段/参数/最小样本量（v11.0）",
        "mime_type": "application/json",
        "provider": resource_indicator_catalog,
    },
}


# ============================================================================
# 3. Prompt 模板（10 个）
# ============================================================================

PROMPT_REGISTRY: Dict[str, Dict[str, Any]] = {
    "analyze_institution_sentiment_30d": {
        "title": "分析机构最近 30 天舆情",
        "description": "对指定金融机构最近 30 天的舆情快照做情感分布 + 风险事件 + 趋势分析",
        "template": (
            "请分析「{institution_name}」最近 {days} 天的舆情数据：\n"
            "1. 调用 crawl_global_sentiment 抓取所有源\n"
            "2. 用 sentiment_statistics 分析情感分布\n"
            "3. 用 cross_section_summary 总结关键事件\n"
            "4. 用 generate_insights 输出风险/机会洞察\n"
            "5. 输出 Markdown 报告，标注关键发现"
        ),
    },
    "export_quarterly_report": {
        "title": "导出季度报告",
        "description": "把指定机构/基金/股票的季度数据导出为 Word/Excel/PDF",
        "template": (
            "请生成「{subject}」的 {period} 季度报告：\n"
            "1. 调 get_stock_realtime / get_fund_nav_history 拉数据\n"
            "2. 调 calculate_risk_metrics 计算风险指标\n"
            "3. 调 generate_research_report 生成 Markdown\n"
            "4. 调 export_research_report 导出 {format} 文件\n"
            "5. 输出文件路径给用户"
        ),
    },
    "query_stock_full_view": {
        "title": "查询股票全维度",
        "description": "拉取指定股票的全维度数据：行情 + 财务 + 资金流 + 舆情",
        "template": (
            "请查股票「{symbol}」的全维度数据：\n"
            "1. 行情：get_stock_realtime / get_financial_data(stock_quote)\n"
            "2. 财务：tushare_daily_basic + tushare_income + tushare_balancesheet\n"
            "3. 资金流：get_margin_trading + get_block_trades + get_shareholder_changes\n"
            "4. 舆情：crawl_global_sentiment + sentiment_statistics\n"
            "5. 综合分析并给出投资逻辑"
        ),
    },
    "crawl_url_list": {
        "title": "爬取 URL 列表",
        "description": "对一组 URL 批量爬取（带降级链 + 反爬保护 + 增量判断）",
        "template": (
            "请爬取以下 URL 列表（共 {count} 个）：\n"
            "{url_list}\n"
            "要求：\n"
            "1. 使用 quick_dispatch 自动按健康度选源\n"
            "2. 启用增量（ETag/Last-Modified/hash）跳过未变更\n"
            "3. 失败的 URL 自动断点续爬（ResumeManager）\n"
            "4. 输出汇总报告（成功/失败/跳过）"
        ),
    },
    "interpret_announcement": {
        "title": "解读公告",
        "description": "下载并解读指定公告（PDF/Word），输出关键信息摘要",
        "template": (
            "请解读公告 {announcement_id}：\n"
            "1. 调 search_announcements 找到原文\n"
            "2. 调 download_announcement 下载文件\n"
            "3. 调 parse_document 解析内容\n"
            "4. 调 interpret_financial_report 提取财务数字\n"
            "5. 输出结构化摘要：标题/日期/关键事项/财务影响"
        ),
    },
    "search_policy_documents": {
        "title": "搜索政策文件",
        "description": "搜索并下载金融监管政策文件",
        "template": (
            "请搜索「{keyword}」相关的金融监管政策：\n"
            "1. 调 search_policy_documents（覆盖证监会/央行/银保监/交易所）\n"
            "2. 调 download_policy_document 下载原文\n"
            "3. 调 analyze_policy_document 分析政策影响\n"
            "4. 输出时间线 + 影响评估"
        ),
    },
    "compare_two_funds": {
        "title": "对比两只基金",
        "description": "对两只基金做多维度对比（持仓/收益/风险/经理）",
        "template": (
            "请对比基金 {fund_a} vs {fund_b}：\n"
            "1. 调 get_fund_nav_history 拉 1 年净值\n"
            "2. 调 compare_series / backtest_series 做收益对比\n"
            "3. 调 calculate_risk_metrics 算风险（Sharpe/MaxDD/Vol）\n"
            "4. 输出对比表 + 选基建议"
        ),
    },
    "generate_research_report": {
        "title": "生成研究报告",
        "description": "为指定主题生成结构化研究报告",
        "template": (
            "请生成主题「{topic}」的研究报告：\n"
            "1. 调 quick_search + search_and_fetch 收集素材\n"
            "2. 调 search_announcements + search_policy_documents 找一手资料\n"
            "3. 调 generate_research_report 写正文\n"
            "4. 调 export_research_report 导出 {output_format}\n"
            "5. 输出完整报告路径"
        ),
    },
    "monitor_page_changes": {
        "title": "监控页面变化",
        "description": "监控目标页面，定期检测变化并通知",
        "template": (
            "请监控以下页面（{interval} 一次）：\n"
            "{url_list}\n"
            "要求：\n"
            "1. 用 RealtimePageMonitor 定期快照\n"
            "2. 哈希对比检测变化\n"
            "3. 变化时调 crawl_global_sentiment 抓详情\n"
            "4. 调 export_sentiment_report 输出变更摘要"
        ),
    },
    "test_anti_block_effect": {
        "title": "测试反爬效果",
        "description": "对指定目标测试当前反爬配置的有效性",
        "template": (
            "请测试「{target_url}」的反爬效果：\n"
            "1. 调 quick_dispatch 不带反爬 → 记录结果\n"
            "2. 调 AntiBlockPipeline 启用全功能 → 记录结果\n"
            "3. 对比：成功率/平均延迟/被拦截次数\n"
            "4. 调 get_anti_block_status 报告反爬系统状态\n"
            "5. 输出对比表 + 优化建议"
        ),
    },
    # ---------- v11.0：实时获取 + 分析 ----------
    "realtime_market_brief": {
        "title": "实时市场速览",
        "description": "带时间戳与新鲜度的盘中/盘后市场速览，指数+板块+关注标的一步到位",
        "template": (
            "请给我一份 {window} 的市场速览，重点看 {focus}：\n"
            "1. rt_market_overview 拿宽基指数涨跌与板块前 8 / 后 5，"
            "先看 clock 判断是盘中、午休还是盘后\n"
            "2. rt_sector_board(kind=concept, order=desc, count=15) 补概念板块热度\n"
            "3. rt_realtime_quote(codes={codes}) 取关注标的快照，逐个引用 asof 与 freshness_seconds\n"
            "4. 要看当天走势用 rt_realtime_kline(period=1min, count=240)，"
            "要持续观察用 rt_watchlist(action=add) 再 rt_watchlist(action=poll)\n"
            "5. 输出：市场状态一句话（含时间）→ 指数表 → 板块冷热 → 标的异动 → 数据来源与新鲜度\n"
            "要求：每个数字标明来源与几点几分；休市、停牌、数据陈旧必须明说，"
            "不许把收盘价包装成实时价，也不许编造没取到的数据。"
        ),
    },
    "technical_snapshot": {
        "title": "技术面诊断",
        "description": "对一只标的做指标+信号+异常+基准对比的技术诊断，样本不足时如实说明",
        "template": (
            "请对 {code} 做一次技术诊断（周期 {period}，样本 {count} 根）：\n"
            "1. rt_technical_snapshot(code={code}, period={period}, count={count}, "
            "benchmark_code={benchmark}) 一次拿指标、信号、异常点与相对基准表现\n"
            "2. 手上已有净值序列或导出数据时改用 series_diagnose，不要重复联网\n"
            "3. 指标口径有疑问先查 indicator_catalog 的 needs 与 min_len；"
            "样本不足就写无法判断，不要硬算\n"
            "4. 需要复核单个指标用 indicator_compute(name, series_json)\n"
            "5. 输出顺序：价格位置 → 趋势（MACD/ADX）→ 超买超卖（RSI/KDJ）→ "
            "波动（BOLL/ATR）→ 量能 → 异常点 → 相对基准（beta/相关性/超额）\n"
            "要求：每条结论后用括号标出指标名与数值；只做事实陈述与风险提示，"
            "不给买卖指令、不预测具体点位。"
        ),
    },
    "evidence_first_summary": {
        "title": "证据优先的多源汇总",
        "description": "把多源信息聚成事件簇，输出带出处、带分歧、带缺口的汇总",
        "template": (
            "请把关于 {topic} 的多源信息汇总成一份结论带出处的简报（近 {days} 天）：\n"
            "1. crawl_financial_news / search_web / crawl_global_sentiment 收集原始条目，"
            "保留 source、url、published_at 三要素\n"
            "2. fuse_multi_source(records_json) 做字段归一、去重、聚类与加权共识，先看 stats 再看 digest\n"
            "3. 单个事件要深挖用 fuse_evidence_report(records_json, index)\n"
            "4. 有数值打架时用 conflict_ledger(action=list) 登记，"
            "结论里保留分歧，不要偷偷取平均或只挑一个源\n"
            "5. 输出：一句话结论 → 已交叉验证的事实（每条至少 2 个独立来源）→ "
            "分歧点 → 缺口（单源或未取到）→ 编号引用清单\n"
            "要求：单源结论标注仅 1 源并降低置信表述；正文每个数字都能在引用清单里回溯到出处；"
            "权威度低但时间更新的信息要写清是更新更快还是更可信。"
        ),
    },
    "conflict_reconciliation": {
        "title": "跨源数值冲突核对",
        "description": "把不同来源打架的数字回溯到原始口径，给出采信依据并写回台账",
        "template": (
            "请核对 {topic} 相关的跨源数值分歧（字段 {field}）：\n"
            "1. conflict_ledger(action=list, field={field}) 取未决冲突；"
            "台账为空时先跑 fuse_multi_source(persist_ledger=True)\n"
            "2. 对每条冲突按 values 里的 source 与 published_at 回溯原始出处，"
            "优先级：监管公示与公司公告 > 权威媒体 > 垂直媒体 > 自媒体\n"
            "3. 能用接口直接验证的（行情、财务、净值）用 get_financial_data 或 "
            "rt_realtime_quote 复核，不要只比文章\n"
            "4. 定论后用 conflict_ledger(action=resolve, key, resolution) 写回\n"
            "5. 输出：冲突清单 → 每条的取值分布 → 判定依据 → 最终采信值 → 仍未决项\n"
            "要求：说明采信某个值的理由（来源级别、时间、统计口径）；"
            "口径差异（母公司/合并、含不含少数股东权益、万元/亿元、时点/区间）要单独指出，"
            "不能把口径不同当成数据错误。"
        ),
    },
}


# ============================================================================
# 4. 注册入口
# ============================================================================

def register_resources_and_prompts(server: Any) -> Dict[str, int]:
    """把 RESOURCE_REGISTRY / PROMPT_REGISTRY 的全量条目注册到 FastMCP server。

    用法（在 mcp_server.py 中）：
        from scripts.mcp_resources_prompts import register_resources_and_prompts
        register_resources_and_prompts(server)

    返回 {"resources": N, "prompts": M, "failed": [...]}，N/M 由注册表长度决定。
    """
    result: Dict[str, Any] = {"resources": 0, "prompts": 0, "failed": []}

    # ---------- 注册 Resource ----------
    for uri, meta in RESOURCE_REGISTRY.items():
        try:
            provider = meta["provider"]

            def make_handler(_provider: Callable[[], str], _uri: str) -> Callable[[], str]:
                def handler() -> str:
                    return _provider()
                handler.__name__ = f"resource_{meta['name']}"
                return handler

            handler = make_handler(provider, uri)
            server.resource(
                uri,
                name=meta["name"],
                title=meta.get("title"),
                description=meta.get("description"),
                mime_type=meta.get("mime_type", "application/json"),
            )(handler)
            result["resources"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("register resource %s failed: %s", uri, exc)
            result["failed"].append(f"resource:{uri}")

    # ---------- 注册 Prompt ----------
    for name, meta in PROMPT_REGISTRY.items():
        try:
            template = meta["template"]
            description = meta.get("description", "")
            title = meta.get("title", name)

            # 创建闭包：把 template 捕获进函数
            def make_prompt(_tpl: str, _title: str, _desc: str) -> Callable[..., str]:
                def prompt_fn(**kwargs: Any) -> str:
                    try:
                        return _tpl.format(**kwargs)
                    except KeyError:
                        # 缺字段时返回模板 + 提示
                        missing = [k for k in _tpl.split("{") if k and not k.startswith("{")]
                        return _tpl + "\n\n[NOTE] 提供以下参数以获得完整模板: " + ", ".join(set(missing))
                prompt_fn.__name__ = f"prompt_{name}"
                return prompt_fn

            pfn = make_prompt(template, title, description)
            server.prompt(
                name=name,
                title=title,
                description=description,
            )(pfn)
            result["prompts"] += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("register prompt %s failed: %s", name, exc)
            result["failed"].append(f"prompt:{name}")

    return result


def list_resources() -> List[Dict[str, str]]:
    """列出所有可用 Resource（用于 SKILL.md 文档）。"""
    return [
        {"uri": uri, "name": meta["name"], "title": meta["title"],
         "description": meta["description"]}
        for uri, meta in RESOURCE_REGISTRY.items()
    ]


def list_prompts() -> List[Dict[str, str]]:
    """列出所有可用 Prompt（用于 SKILL.md 文档）。"""
    return [
        {"name": name, "title": meta["title"], "description": meta["description"]}
        for name, meta in PROMPT_REGISTRY.items()
    ]


# ============================================================================
# 5. 公共导出
# ============================================================================

__all__ = [
    "RESOURCE_REGISTRY",
    "PROMPT_REGISTRY",
    "resource_media_sources",
    "resource_institutions",
    "resource_query_types",
    "resource_report_templates",
    "resource_cache_index",
    "resource_data_source_health",
    "resource_anti_block_status",
    "resource_crawl_state",
    "resource_realtime_status",
    "resource_indicator_catalog",
    "register_resources_and_prompts",
    "list_resources",
    "list_prompts",
]