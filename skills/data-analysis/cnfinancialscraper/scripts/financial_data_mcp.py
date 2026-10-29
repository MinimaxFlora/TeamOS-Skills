# -*- coding: utf-8 -*-
"""
金融数据 MCP 桥接模块 v9.0 (financial_data_mcp.py)

统一接入多源权威金融数据，按优先级自动选择最佳数据源。
设计原则：
1. 优先使用结构化 API（零爬虫风险）
2. 自动降级到备用数据源
3. 结果标准化为统一格式
4. 所有查询经过反爬管道保护

支持的数据源层级：
  Layer 0: akshare（权威结构化数据，13000+接口）
  Layer 1: 直连官方 API（东方财富/新浪/巨潮等公开接口）
  Layer 2: yfinance（全球市场数据）
  Layer 3: RSS / RSSHub 公开信息流
  Layer 4: 搜索引擎聚合兜底
"""
from __future__ import annotations

import json
import logging
import re
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

# ============================================================================
# 1. 数据源枚举与注册
# ============================================================================

class DataSource(Enum):
    """支持的金融数据源。"""
    AKSHARE = auto()        # akshare — 中国金融数据之王
    EASTMONEY_API = auto()  # 东方财富直连 API
    SINA_HQ = auto()        # 新浪行情接口
    YAHOO_FINANCE = auto()  # yfinance (Yahoo Finance)
    FINNHUB = auto()        # Finnhub（美股/全球）
    ALPHA_VANTAGE = auto()  # Alpha Vantage
    TUSHARE = auto()        # Tushare（需token）
    RSS_FEEDS = auto()      # RSS 信息流
    SEARCH_FALLBACK = auto()# 搜索引擎兜底
    CNINFO = auto()         # 巨潮资讯（公告/报告）
    SSE_SZSE = auto()       # 上交所/深交所官方
    CSRC = auto()           # 证监会
    PBOC = auto()           # 央行
    MOF = auto()            # 财政部
    SAFE = auto()           # 外汇管理局
    NBS = auto()            # 国家统计局
    COINGECKO = auto()      # CoinGecko（无需 key 的加密币行情）
    EXCHANGERATE = auto()   # exchangerate.host（无需 key 的外汇）
    TENCENT_RT = auto()     # 腾讯实时行情（realtime_data_hub，强制绕开 HTTP 缓存）


@dataclass
class SourceHealth:
    """数据源健康状态。"""
    source: DataSource
    available: bool = True
    last_success: Optional[str] = None
    last_failure: Optional[str] = None
    consecutive_failures: int = 0
    total_queries: int = 0
    total_success: int = 0
    avg_latency_ms: float = 0.0
    priority: int = 0  # 越高越优先

    @property
    def success_rate(self) -> float:
        return self.total_success / self.total_queries if self.total_queries else 0.0


# ============================================================================
# 2. 数据源能力注册表
# ============================================================================

# 每个数据源支持的查询类型
SOURCE_CAPABILITIES: Dict[DataSource, List[str]] = {
    DataSource.AKSHARE: [
        "stock_quote", "fund_nav", "fund_list", "fund_manager",
        "bond_yield", "futures", "macro_gdp", "macro_cpi", "macro_ppi",
        "macro_pmi", "shibor", "repo_rate", "forex", "etf_flow",
        "margin_trading", "block_trade", "dragon_tiger",
        "hk_stock", "us_stock", "convertible_bond",
        "industry_board", "concept_board", "stock_financial",
        "etf_list", "etf_realtime", "index_quote",
        "macro_money_supply", "macro_social_financing",
        "macro_trade_balance", "macro_fdi",
        "bse_stock", "neeq_stock",
    ],
    DataSource.EASTMONEY_API: [
        "stock_quote", "fund_nav", "fund_list", "news",
        "announcements", "margin_trading", "block_trade",
        "etf_flow", "dragon_tiger", "shareholder_change",
        "stock_financial", "industry_board", "index_quote",
    ],
    DataSource.SINA_HQ: [
        "stock_quote", "index_quote", "hk_stock", "us_stock",
        "forex", "futures",
    ],
    DataSource.YAHOO_FINANCE: [
        "stock_quote", "us_stock", "stock_financial",
        "etf_list", "fund_nav", "index_quote",
        "hk_stock", "forex", "futures",
    ],
    DataSource.FINNHUB: [
        "stock_quote", "us_stock", "stock_financial",
        "news", "earnings",
    ],
    DataSource.ALPHA_VANTAGE: [
        "stock_quote", "us_stock", "forex", "futures",
        "stock_financial",
        "crypto_daily_vantage", "fx_daily_vantage", "av_stock_daily",
    ],
    DataSource.TUSHARE: [
        "stock_quote", "fund_nav", "fund_list",
        "stock_financial", "announcements", "macro_gdp",
        "tushare_stock_basic", "tushare_daily", "tushare_daily_basic",
        "tushare_income", "tushare_balancesheet", "tushare_cashflow",
        "tushare_fund_basic", "tushare_fund_nav",
        "tushare_index_basic", "tushare_index_daily",
        "tushare_futures_basic", "tushare_fut_daily",
    ],
    DataSource.CNINFO: [
        "announcements", "company_report",
    ],
    DataSource.SSE_SZSE: [
        "announcements", "stock_list",
    ],
    DataSource.PBOC: [
        "macro_money_supply", "shibor", "repo_rate",
        "lpr_rate", "bond_yield",
    ],
    DataSource.NBS: [
        "macro_gdp", "macro_cpi", "macro_ppi", "macro_pmi",
        "macro_trade_balance", "macro_fdi",
    ],
    DataSource.RSS_FEEDS: [
        "news", "regulatory",
    ],
    DataSource.SEARCH_FALLBACK: [
        "news", "announcements", "stock_quote",
    ],
    DataSource.COINGECKO: [
        "crypto_quote", "crypto_markets", "crypto_history",
        "crypto_ohlc", "crypto_global", "crypto_trending",
    ],
    DataSource.EXCHANGERATE: [
        "forex_realtime", "forex_pair", "forex_all_rates",
    ],
    DataSource.TENCENT_RT: [
        "rt_quote", "rt_overview", "rt_kline", "rt_fund_estimate", "rt_board",
        "stock_quote", "index_quote",
    ],
}

# 查询类型 → 默认数据源优先级（列表第一个是最优）
SOURCE_PRIORITY: Dict[str, List[DataSource]] = {
    "stock_quote":       [DataSource.AKSHARE, DataSource.EASTMONEY_API, DataSource.SINA_HQ,
                          DataSource.TENCENT_RT, DataSource.YAHOO_FINANCE],
    "fund_nav":          [DataSource.AKSHARE, DataSource.EASTMONEY_API, DataSource.TUSHARE],
    "fund_list":         [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "fund_manager":      [DataSource.AKSHARE],
    "bond_yield":        [DataSource.AKSHARE, DataSource.PBOC],
    "futures":           [DataSource.AKSHARE, DataSource.SINA_HQ, DataSource.YAHOO_FINANCE],
    "macro_gdp":         [DataSource.AKSHARE, DataSource.NBS, DataSource.TUSHARE],
    "macro_cpi":         [DataSource.AKSHARE, DataSource.NBS],
    "macro_ppi":         [DataSource.AKSHARE, DataSource.NBS],
    "macro_pmi":         [DataSource.AKSHARE, DataSource.NBS],
    "shibor":            [DataSource.AKSHARE, DataSource.PBOC],
    "repo_rate":         [DataSource.AKSHARE, DataSource.PBOC],
    "forex":             [DataSource.AKSHARE, DataSource.SINA_HQ, DataSource.YAHOO_FINANCE],
    "etf_flow":          [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "margin_trading":    [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "block_trade":       [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "dragon_tiger":      [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "hk_stock":          [DataSource.AKSHARE, DataSource.SINA_HQ, DataSource.YAHOO_FINANCE],
    "us_stock":          [DataSource.YAHOO_FINANCE, DataSource.AKSHARE, DataSource.FINNHUB],
    "convertible_bond":  [DataSource.AKSHARE],
    "industry_board":    [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "concept_board":     [DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "stock_financial":   [DataSource.AKSHARE, DataSource.EASTMONEY_API, DataSource.YAHOO_FINANCE],
    "etf_list":          [DataSource.AKSHARE, DataSource.YAHOO_FINANCE],
    "etf_realtime":      [DataSource.AKSHARE],
    "index_quote":       [DataSource.AKSHARE, DataSource.SINA_HQ, DataSource.EASTMONEY_API],
    "news":              [DataSource.EASTMONEY_API, DataSource.RSS_FEEDS, DataSource.SEARCH_FALLBACK],
    "announcements":     [DataSource.CNINFO, DataSource.EASTMONEY_API, DataSource.SSE_SZSE],
    "company_report":    [DataSource.CNINFO],
    "shareholder_change":[DataSource.AKSHARE, DataSource.EASTMONEY_API],
    "macro_money_supply":[DataSource.AKSHARE, DataSource.PBOC],
    "lpr_rate":          [DataSource.AKSHARE, DataSource.PBOC],
    "regulatory":        [DataSource.RSS_FEEDS, DataSource.SEARCH_FALLBACK],
    "bse_stock":         [DataSource.AKSHARE],
    "neeq_stock":        [DataSource.AKSHARE],
    "earnings":          [DataSource.FINNHUB, DataSource.YAHOO_FINANCE],
    # v10 扩充轨：不写进这张表，router.query("crypto_quote") 只会得到
    # "All 0 sources exhausted"——适配器注册了也没人调用它。
    "crypto_quote":      [DataSource.COINGECKO, DataSource.ALPHA_VANTAGE],
    "crypto_markets":    [DataSource.COINGECKO],
    "crypto_history":    [DataSource.COINGECKO],
    "crypto_ohlc":       [DataSource.COINGECKO],
    "crypto_global":     [DataSource.COINGECKO],
    "crypto_trending":   [DataSource.COINGECKO],
    "crypto_daily_vantage": [DataSource.ALPHA_VANTAGE],
    "forex_realtime":    [DataSource.EXCHANGERATE, DataSource.SINA_HQ, DataSource.AKSHARE],
    "forex_pair":        [DataSource.EXCHANGERATE, DataSource.SINA_HQ],
    "forex_all_rates":   [DataSource.EXCHANGERATE],
    "fx_daily_vantage":  [DataSource.ALPHA_VANTAGE],
    "av_stock_daily":    [DataSource.ALPHA_VANTAGE, DataSource.YAHOO_FINANCE],
    "tushare_stock_basic": [DataSource.TUSHARE],
    "tushare_daily":     [DataSource.TUSHARE, DataSource.AKSHARE],
    "tushare_daily_basic": [DataSource.TUSHARE, DataSource.AKSHARE],
    "tushare_income":    [DataSource.TUSHARE, DataSource.AKSHARE],
    "tushare_balancesheet": [DataSource.TUSHARE],
    "tushare_cashflow":  [DataSource.TUSHARE],
    "tushare_fund_basic": [DataSource.TUSHARE],
    "tushare_fund_nav":  [DataSource.TUSHARE, DataSource.AKSHARE],
    "tushare_index_basic": [DataSource.TUSHARE],
    "tushare_index_daily": [DataSource.TUSHARE],
    "tushare_futures_basic": [DataSource.TUSHARE],
    "tushare_fut_daily": [DataSource.TUSHARE],
    # v11 实时轨：走 realtime_data_hub（多源 + 绕开 HTTP 缓存）
    "rt_quote":          [DataSource.TENCENT_RT, DataSource.SINA_HQ, DataSource.EASTMONEY_API],
    "rt_overview":       [DataSource.TENCENT_RT],
    "rt_kline":          [DataSource.EASTMONEY_API, DataSource.TENCENT_RT],
    "rt_fund_estimate":  [DataSource.EASTMONEY_API],
    "rt_board":          [DataSource.EASTMONEY_API],
}


# ============================================================================
# 3. akshare 适配器
# ============================================================================

class AkshareAdapter:
    """akshare 数据源适配器 — 中国金融数据首选。"""

    def __init__(self):
        self._available = False
        self._ak = None
        self._check()

    def _check(self):
        try:
            import akshare as ak
            self._ak = ak
            self._available = True
        except ImportError:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def query(self, query_type: str, params: Dict[str, Any]) -> Optional[Any]:
        """路由查询到对应的 akshare 接口。"""
        if not self._available:
            return None

        try:
            fn = self._ROUTE_TABLE.get(query_type)
            if fn:
                return fn(self, params)
        except Exception as e:
            log.warning(f"akshare query '{query_type}' failed: {e}")
        return None

    def _q_stock_quote(self, params: Dict) -> Any:
        """A股实时行情。给了 code 就只返回那一只——
        原先三个分支都返回全市场 5000+ 行表，等于把筛选推回调用方。"""
        code = str(params.get("code", "") or "").strip()
        spot = self._ak.stock_zh_a_spot_em()
        if not code or spot is None:
            return spot
        try:
            col = "代码" if "代码" in spot.columns else spot.columns[1]
            hit = spot[spot[col].astype(str).str.zfill(6) == code.zfill(6)]
            return hit if not hit.empty else None
        except Exception as e:
            log.warning(f"stock_quote filter by {code} failed: {e}")
            return None

    def _q_fund_nav(self, params: Dict) -> Any:
        """基金净值历史。"""
        code = params.get("code", "")
        if code:
            return self._ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
        return None

    def _q_fund_list(self, params: Dict) -> Any:
        """基金列表。"""
        return self._ak.fund_name_em()

    def _q_fund_manager(self, params: Dict) -> Any:
        """基金经理信息。"""
        return self._ak.fund_manager()

    def _q_bond_yield(self, params: Dict) -> Any:
        """国债收益率曲线。"""
        return self._ak.bond_zh_us_rate(start_date=params.get("start", "20240101"))

    def _q_futures(self, params: Dict) -> Any:
        """期货现货数据。"""
        symbol = params.get("symbol", "全部")
        return self._ak.futures_zh_spot(symbol=symbol)

    def _q_macro_gdp(self, params: Dict) -> Any:
        """GDP 数据。"""
        return self._ak.macro_china_gdp()

    def _q_macro_cpi(self, params: Dict) -> Any:
        """CPI 数据。"""
        return self._ak.macro_china_cpi()

    def _q_macro_ppi(self, params: Dict) -> Any:
        """PPI 数据。"""
        return self._ak.macro_china_ppi()

    def _q_macro_pmi(self, params: Dict) -> Any:
        """PMI 数据。"""
        return self._ak.macro_china_pmi()

    def _q_shibor(self, params: Dict) -> Any:
        """Shibor 利率。"""
        return self._ak.rate_interbank(market="上海银行同业拆借市场", symbol="Shibor人民币", indicator="今日")

    def _q_repo_rate(self, params: Dict) -> Any:
        """回购利率。"""
        return self._ak.rate_interbank(market="中国银行同业拆借市场", symbol="Chibor人民币", indicator="今日")

    def _q_forex(self, params: Dict) -> Any:
        """外汇汇率。"""
        symbol = params.get("symbol", "美元兑人民币")
        return self._ak.fx_spot_quote()

    def _q_etf_flow(self, params: Dict) -> Any:
        """ETF 资金流向。"""
        return self._ak.fund_etf_fund_daily_em()

    def _q_margin_trading(self, params: Dict) -> Any:
        """融资融券。"""
        return self._ak.stock_margin_detail_szse()

    def _q_block_trade(self, params: Dict) -> Any:
        """大宗交易。"""
        return self._ak.stock_dzjy_sctj()

    def _q_dragon_tiger(self, params: Dict) -> Any:
        """龙虎榜。"""
        return self._ak.stock_lhb_detail_em()

    def _q_hk_stock(self, params: Dict) -> Any:
        """港股行情。"""
        return self._ak.stock_hk_spot_em()

    def _q_convertible_bond(self, params: Dict) -> Any:
        """可转债数据。"""
        return self._ak.bond_cb_jsl()

    def _q_industry_board(self, params: Dict) -> Any:
        """行业板块。"""
        return self._ak.stock_board_industry_name_em()

    def _q_concept_board(self, params: Dict) -> Any:
        """概念板块。"""
        return self._ak.stock_board_concept_name_em()

    def _q_stock_financial(self, params: Dict) -> Any:
        """股票财务数据。"""
        code = params.get("code", "")
        if code:
            return self._ak.stock_financial_abstract_ths(symbol=code)
        return None

    def _q_etf_list(self, params: Dict) -> Any:
        """ETF 列表。"""
        return self._ak.fund_etf_spot_em()

    def _q_etf_realtime(self, params: Dict) -> Any:
        """ETF 实时行情。"""
        return self._ak.fund_etf_spot_em()

    def _q_index_quote(self, params: Dict) -> Any:
        """指数行情。"""
        return self._ak.stock_zh_index_spot_em()

    def _q_shareholder_change(self, params: Dict) -> Any:
        """股东增减持。"""
        return self._ak.stock_dzjy_mrtj()

    def _q_macro_money_supply(self, params: Dict) -> Any:
        """货币供应量(M0/M1/M2)。"""
        return self._ak.macro_china_money_supply()

    def _q_lpr_rate(self, params: Dict) -> Any:
        """LPR 利率。"""
        return self._ak.macro_china_lpr()

    def _q_bse_stock(self, params: Dict) -> Any:
        """北交所股票。"""
        return self._ak.stock_bj_a_spot_em()

    def _q_us_stock(self, params: Dict) -> Any:
        """美股行情(通过 akshare)。"""
        symbol = params.get("symbol", "")
        if symbol:
            return self._ak.stock_us_spot_em()
        return self._ak.stock_us_spot_em()

    _ROUTE_TABLE = {
        "stock_quote": _q_stock_quote,
        "fund_nav": _q_fund_nav,
        "fund_list": _q_fund_list,
        "fund_manager": _q_fund_manager,
        "bond_yield": _q_bond_yield,
        "futures": _q_futures,
        "macro_gdp": _q_macro_gdp,
        "macro_cpi": _q_macro_cpi,
        "macro_ppi": _q_macro_ppi,
        "macro_pmi": _q_macro_pmi,
        "shibor": _q_shibor,
        "repo_rate": _q_repo_rate,
        "forex": _q_forex,
        "etf_flow": _q_etf_flow,
        "margin_trading": _q_margin_trading,
        "block_trade": _q_block_trade,
        "dragon_tiger": _q_dragon_tiger,
        "hk_stock": _q_hk_stock,
        "convertible_bond": _q_convertible_bond,
        "industry_board": _q_industry_board,
        "concept_board": _q_concept_board,
        "stock_financial": _q_stock_financial,
        "etf_list": _q_etf_list,
        "etf_realtime": _q_etf_realtime,
        "index_quote": _q_index_quote,
        "shareholder_change": _q_shareholder_change,
        "macro_money_supply": _q_macro_money_supply,
        "lpr_rate": _q_lpr_rate,
        "bse_stock": _q_bse_stock,
        "us_stock": _q_us_stock,
    }


# ============================================================================
# 4. 东方财富直连 API 适配器
# ============================================================================

class EastmoneyApiAdapter:
    """东方财富公开 API 适配器（直接 HTTP，不经过爬虫管道）。"""

    # 已知的公开 API 端点
    ENDPOINTS = {
        "stock_quote": "https://push2.eastmoney.com/api/qt/stock/get",
        "stock_list": "https://push2.eastmoney.com/api/qt/clist/get",
        "fund_nav": "https://api.fund.eastmoney.com/f10/lsjz",
        "margin_trading": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "block_trade": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "etf_flow": "https://push2.eastmoney.com/api/qt/clist/get",
        "dragon_tiger": "https://datacenter-web.eastmoney.com/api/data/v1/get",
        "news": "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns",
        "announcements": "https://np-anotice-stock.eastmoney.com/api/security/ann",
        "index_quote": "https://push2.eastmoney.com/api/qt/ulist.np/get",
    }

    def __init__(self):
        self._session = None
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def query(self, query_type: str, params: Dict[str, Any]) -> Optional[Any]:
        """路由查询到东方财富 API。"""
        try:
            endpoint = self.ENDPOINTS.get(query_type)
            if not endpoint:
                return None
            return self._fetch_api(endpoint, query_type, params)
        except Exception as e:
            log.warning(f"Eastmoney API '{query_type}' failed: {e}")
            return None

    def _fetch_api(self, endpoint: str, query_type: str, params: Dict) -> Optional[Dict]:
        """发起 API 请求。"""
        try:
            try:
                from http_utils import http_get_json
            except ImportError:
                from scripts.http_utils import http_get_json

            api_params = self._build_params(query_type, params)
            url = endpoint
            if api_params:
                from urllib.parse import urlencode
                url = f"{endpoint}?{urlencode(api_params)}"

            return http_get_json(url, timeout=15)
        except Exception as e:
            log.debug(f"Eastmoney API fetch failed: {e}")
            return None

    def _build_params(self, query_type: str, params: Dict) -> Dict:
        """根据查询类型构建 API 参数。"""
        code = params.get("code", "")

        if query_type == "stock_quote" and code:
            # 东方财富个股行情接口
            market = "1" if code.startswith("6") else "0"  # 1=沪 0=深
            return {
                "secid": f"{market}.{code}",
                "fields": "f43,f44,f45,f46,f47,f48,f50,f51,f52,f55,f57,f58,f60,f116,f117,f162,f167,f168,f169,f170",
                "ut": "fa5fd1943c7b386f172d6893dbbd1d0c",
            }
        elif query_type == "stock_list":
            return {
                "pn": str(params.get("page", 1)),
                "pz": str(params.get("size", 50)),
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f1,f2,f3,f4,f5,f6,f7,f12,f14",
            }
        elif query_type == "index_quote":
            return {
                "fields": "f2,f3,f4,f12,f14",
                "secids": "1.000001,0.399001,0.399006,1.000688,1.000300",
            }
        return {}


# ============================================================================
# 5. yfinance 适配器
# ============================================================================

class YFinanceAdapter:
    """yfinance 适配器 — 全球市场数据。"""

    def __init__(self):
        self._available = False
        self._yf = None
        self._check()

    def _check(self):
        try:
            import yfinance as yf
            self._yf = yf
            self._available = True
        except ImportError:
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def query(self, query_type: str, params: Dict[str, Any]) -> Optional[Any]:
        if not self._available:
            return None
        try:
            fn = self._ROUTE_TABLE.get(query_type)
            if fn:
                return fn(self, params)
        except Exception as e:
            log.warning(f"yfinance query '{query_type}' failed: {e}")
        return None

    def _ticker(self, symbol: str):
        return self._yf.Ticker(symbol)

    def _q_us_stock(self, params: Dict) -> Any:
        symbol = params.get("symbol", "AAPL")
        t = self._ticker(symbol)
        return {
            "info": t.info,
            "price": t.fast_info.last_price if hasattr(t, 'fast_info') else None,
            "history": t.history(period=params.get("period", "1mo")).to_dict(),
        }

    def _q_stock_quote(self, params: Dict) -> Any:
        """A股通过yfinance获取（后缀.SS/.SZ）。"""
        code = params.get("code", "")
        if not code:
            return None
        suffix = ".SS" if code.startswith("6") else ".SZ"
        t = self._ticker(f"{code}{suffix}")
        info = t.fast_info
        return {
            "last_price": info.last_price,
            "market_cap": info.market_cap,
            "currency": info.currency,
        }

    def _q_hk_stock(self, params: Dict) -> Any:
        code = params.get("code", "0700")
        t = self._ticker(f"{code}.HK")
        info = t.fast_info
        return {
            "last_price": info.last_price,
            "market_cap": info.market_cap,
            "currency": info.currency,
        }

    def _q_forex(self, params: Dict) -> Any:
        pair = params.get("pair", "USDCNY=X")
        t = self._ticker(pair)
        return {"price": t.fast_info.last_price}

    def _q_futures(self, params: Dict) -> Any:
        symbol = params.get("symbol", "CL=F")
        t = self._ticker(symbol)
        return {"price": t.fast_info.last_price}

    def _q_etf_list(self, params: Dict) -> Any:
        symbols = params.get("symbols", ["SPY", "QQQ", "IWM", "GLD", "TLT"])
        results = []
        for sym in symbols:
            try:
                t = self._ticker(sym)
                results.append({"symbol": sym, "price": t.fast_info.last_price})
            except Exception:
                pass
        return results

    def _q_index_quote(self, params: Dict) -> Any:
        indices = params.get("indices", {
            "^GSPC": "S&P500", "^DJI": "道琼斯", "^IXIC": "纳斯达克",
            "^HSI": "恒生指数", "^N225": "日经225",
        })
        results = {}
        for sym, name in indices.items():
            try:
                t = self._ticker(sym)
                results[name] = t.fast_info.last_price
            except Exception:
                pass
        return results

    _ROUTE_TABLE = {
        "us_stock": _q_us_stock,
        "stock_quote": _q_stock_quote,
        "hk_stock": _q_hk_stock,
        "forex": _q_forex,
        "futures": _q_futures,
        "etf_list": _q_etf_list,
        "index_quote": _q_index_quote,
    }


# ============================================================================
# 6. Finnhub 适配器
# ============================================================================

class FinnhubAdapter:
    """Finnhub API 适配器（美股/全球，免费API Key）。"""

    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str = ""):
        self._api_key = api_key or ""
        self._available = bool(self._api_key)

    @property
    def available(self) -> bool:
        return self._available

    def set_api_key(self, key: str):
        self._api_key = key
        self._available = bool(key)

    def query(self, query_type: str, params: Dict[str, Any]) -> Optional[Any]:
        if not self._available:
            return None
        try:
            fn = self._ROUTE_TABLE.get(query_type)
            if fn:
                return fn(self, params)
        except Exception as e:
            log.warning(f"Finnhub query '{query_type}' failed: {e}")
        return None

    def _fetch(self, path: str, params: Dict = None) -> Optional[Dict]:
        try:
            try:
                from http_utils import http_get_json
            except ImportError:
                from scripts.http_utils import http_get_json

            from urllib.parse import urlencode
            p = dict(params or {})
            p["token"] = self._api_key
            url = f"{self.BASE_URL}{path}?{urlencode(p)}"
            return http_get_json(url, timeout=15)
        except Exception as e:
            log.debug(f"Finnhub fetch failed: {e}")
            return None

    def _q_us_stock(self, params: Dict) -> Any:
        symbol = params.get("symbol", "AAPL")
        return self._fetch("/quote", {"symbol": symbol})

    def _q_stock_quote(self, params: Dict) -> Any:
        code = params.get("code", "")
        if not code:
            return None
        return self._fetch("/quote", {"symbol": code})

    def _q_news(self, params: Dict) -> Any:
        category = params.get("category", "general")
        return self._fetch("/news", {"category": category, "minId": 0})

    _ROUTE_TABLE = {
        "us_stock": _q_us_stock,
        "stock_quote": _q_stock_quote,
        "news": _q_news,
    }


# ============================================================================
# 7. RSS 适配器
# ============================================================================

class RssFeedAdapter:
    """RSS 信息流适配器（零爬虫风险的公开信息源）。"""

    FEEDS = {
        "financial_news": [
            "https://rsshub.app/cls/telegraph",      # 财联社电报
            "https://rsshub.app/wallstreetcn/live",   # 华尔街见闻
            "https://rsshub.app/yicai/brief",         # 第一财经
            "https://rsshub.app/caixin/latest",       # 财新
        ],
        "regulatory": [
            "https://rsshub.app/csrc/news",           # 证监会
            "https://rsshub.app/pbc/goutongjiaoliu",  # 央行
        ],
        "market": [
            "https://rsshub.app/eastmoney/report",    # 东方财富研报
            "https://rsshub.app/gelonghui/home",      # 格隆汇
        ],
    }

    def __init__(self):
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def query(self, query_type: str, params: Dict[str, Any]) -> Optional[List[Dict]]:
        feeds = self.FEEDS.get(query_type, self.FEEDS.get("financial_news", []))
        limit = params.get("limit", 10)
        results = []
        for feed_url in feeds[:3]:  # 限制最多3个源
            try:
                items = self._fetch_feed(feed_url, limit)
                results.extend(items)
            except Exception as e:
                log.debug(f"RSS feed failed {feed_url}: {e}")
        return results[:limit] if results else None

    def _fetch_feed(self, url: str, limit: int = 10) -> List[Dict]:
        """解析 RSS feed（纯标准库实现）。"""
        try:
            try:
                from http_utils import fetch_text
            except ImportError:
                from scripts.http_utils import fetch_text

            xml_text = fetch_text(url, timeout=15, use_cache=True)
            if not xml_text:
                return []

            items = []
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_text)

            # RSS 2.0
            for item in root.findall(".//item"):
                if len(items) >= limit:
                    break
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")
                pub = item.findtext("pubDate", "")
                items.append({
                    "title": title, "url": link,
                    "summary": desc[:200], "published": pub,
                    "source": "rss",
                })

            # Atom
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            for entry in root.findall(".//atom:entry", ns):
                if len(items) >= limit:
                    break
                title = entry.findtext("atom:title", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}title", "")
                link_el = entry.find("atom:link", ns) or entry.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.get("href", "") if link_el is not None else ""
                summary = entry.findtext("atom:summary", "", ns) or entry.findtext("{http://www.w3.org/2005/Atom}summary", "")
                items.append({
                    "title": title, "url": link,
                    "summary": summary[:200], "source": "rss",
                })

            return items
        except Exception as e:
            log.debug(f"RSS parse failed: {e}")
            return []


# ============================================================================
# 8. 统一金融数据路由器
# ============================================================================

@dataclass
class QueryResult:
    """统一的查询结果。"""
    query_type: str
    source: str
    data: Any = None
    success: bool = False
    latency_ms: float = 0.0
    error: str = ""
    fallback_used: List[str] = field(default_factory=list)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")


class FinancialDataRouter:
    """金融数据统一路由器。

    按优先级依次尝试多个数据源，自动降级到备用源。
    每个源独立健康追踪，连续失败的源会暂时跳过。

    用法:
        router = FinancialDataRouter()
        result = router.query("stock_quote", code="600519")
        if result.success:
            print(result.data)
        else:
            print(f"All sources failed: {result.fallback_used}")
    """

    # 内置源：不注册适配器实例，由 _builtin_query 直连
    BUILTIN_SOURCES = (DataSource.SINA_HQ, DataSource.SEARCH_FALLBACK,
                       DataSource.TENCENT_RT)

    def __init__(self,
                 finnhub_key: str = "",
                 tushare_token: str = "",
                 custom_priorities: Optional[Dict[str, List[str]]] = None,
                 health_file: Optional[str] = None):
        # 初始化适配器
        self._adapters: Dict[DataSource, Any] = {}
        self._health: Dict[DataSource, SourceHealth] = {}
        self._lock = threading.Lock()

        # 注册所有适配器
        self._register_adapter(DataSource.AKSHARE, AkshareAdapter())
        self._register_adapter(DataSource.EASTMONEY_API, EastmoneyApiAdapter())
        self._register_adapter(DataSource.SINA_HQ, None)  # 使用 http_utils 直连
        self._register_adapter(DataSource.YAHOO_FINANCE, YFinanceAdapter())
        self._register_adapter(DataSource.FINNHUB, FinnhubAdapter(finnhub_key))
        self._register_adapter(DataSource.RSS_FEEDS, RssFeedAdapter())
        self._register_adapter(DataSource.SEARCH_FALLBACK, None)  # 使用搜索模块
        self._register_adapter(DataSource.TENCENT_RT, None)       # 实时中枢走 _builtin_query

        # 加载健康状态
        if health_file:
            self._load_health(health_file)

        log.info(f"FinancialDataRouter initialized with "
                 f"{sum(1 for a in self._adapters.values() if a and getattr(a, 'available', True))} "
                 f"active adapters")

    def _register_adapter(self, source: DataSource, adapter: Any):
        self._adapters[source] = adapter
        self._health[source] = SourceHealth(source=source)

    def list_sources(self) -> Dict[str, Any]:
        """列出所有数据源状态。"""
        result = {}
        for src, health in self._health.items():
            adapter = self._adapters.get(src)
            available = getattr(adapter, 'available', True) if adapter else adapter is not None
            result[src.name] = {
                "available": available,
                "success_rate": round(health.success_rate, 3),
                "total_queries": health.total_queries,
                "avg_latency_ms": round(health.avg_latency_ms, 1),
                "consecutive_failures": health.consecutive_failures,
                "capabilities": SOURCE_CAPABILITIES.get(src, []),
            }
        return result

    def list_capabilities(self) -> Dict[str, List[str]]:
        """列出每种查询类型的可用数据源列表。"""
        return {qt: [s.name for s in sources]
                for qt, sources in SOURCE_PRIORITY.items()}

    def query(self, query_type: str,
              preferred_source: Optional[str] = None,
              **params) -> QueryResult:
        """统一查询入口。

        Args:
            query_type: 查询类型（如 "stock_quote", "fund_nav", "macro_cpi"）
            preferred_source: 优先使用的数据源名称（可选）
            **params: 查询参数（code, symbol, start_date 等）

        Returns:
            QueryResult
        """
        start = time.monotonic()
        fallback_used = []

        # 确定数据源顺序
        sources = list(SOURCE_PRIORITY.get(query_type, []))

        # 如果指定了优先源，移到最前
        if preferred_source:
            try:
                preferred = DataSource[preferred_source.upper()]
                if preferred in sources:
                    sources.remove(preferred)
                    sources.insert(0, preferred)
            except KeyError:
                pass

        for source in sources:
            adapter = self._adapters.get(source)
            health = self._health.get(source)
            if not health:
                continue

            # 跳过不可用的源
            if adapter and not getattr(adapter, 'available', True):
                fallback_used.append(f"{source.name}(unavailable)")
                continue

            # 没有适配器、又不是内置源的，直接跳过并标 not_configured。
            # 让它进 _builtin_query 拿 None 会被记成 "(no_data)"，
            # 于是"这个源压根没接"和"接了但这次查不到"在台账里混成一件事。
            if adapter is None and source not in self.BUILTIN_SOURCES:
                fallback_used.append(f"{source.name}(not_configured)")
                continue

            # 跳过连续失败过多的源（冷却30秒）
            if health.consecutive_failures >= 3:
                if health.last_failure:
                    try:
                        last_fail = datetime.fromisoformat(health.last_failure)
                        if (datetime.now() - last_fail).total_seconds() < 30:
                            fallback_used.append(f"{source.name}(cooldown)")
                            continue
                    except ValueError:
                        pass

            try:
                latency_start = time.monotonic()
                data = None

                if adapter is None:
                    # 内置适配器（Sina HQ / Search fallback）
                    data = self._builtin_query(source, query_type, params)
                else:
                    data = adapter.query(query_type, params)

                latency = (time.monotonic() - latency_start) * 1000

                if data is not None:
                    # 成功
                    self._record_success(source, latency)
                    elapsed = (time.monotonic() - start) * 1000
                    return QueryResult(
                        query_type=query_type,
                        source=source.name,
                        data=data,
                        success=True,
                        latency_ms=round(elapsed, 1),
                        fallback_used=fallback_used,
                    )
                else:
                    fallback_used.append(f"{source.name}(no_data)")

            except Exception as e:
                fallback_used.append(f"{source.name}(error:{str(e)[:50]})")
                self._record_failure(source, str(e))

        # 所有源都失败
        elapsed = (time.monotonic() - start) * 1000
        return QueryResult(
            query_type=query_type,
            source="none",
            success=False,
            latency_ms=round(elapsed, 1),
            error=f"All {len(fallback_used)} sources exhausted",
            fallback_used=fallback_used,
        )

    def _builtin_query(self, source: DataSource, query_type: str,
                       params: Dict) -> Optional[Any]:
        """内置数据源查询（Sina HQ / Search / 实时中枢）。"""
        if source == DataSource.SINA_HQ:
            return self._query_sina_hq(query_type, params)
        elif source == DataSource.SEARCH_FALLBACK:
            return self._query_search(query_type, params)
        elif source == DataSource.TENCENT_RT:
            return self._query_realtime(query_type, params)
        return None

    def _query_realtime(self, query_type: str, params: Dict) -> Optional[Any]:
        """实时行情中枢（强制绕过 HTTP 响应缓存）。"""
        try:
            try:
                from realtime_data_hub import get_hub
            except ImportError:
                from scripts.realtime_data_hub import get_hub
        except ImportError:
            return None
        hub = get_hub()
        if query_type == "rt_board":
            rows = hub.board(params.get("board", "industry"),
                             order=params.get("order", "desc"),
                             count=int(params.get("count", 20)))
            return rows or None
        if query_type == "rt_fund_estimate":
            nav = hub.fund_nav(params.get("code", params.get("symbol", "")))
            return nav or None
        if query_type == "rt_kline":
            bars = hub.bars(params.get("code", params.get("symbol", "")),
                            period=params.get("period", "day"),
                            count=int(params.get("count", 120)),
                            adjust=params.get("adjust", "qfq"))
            return bars or None
        codes = params.get("codes") or params.get("code") or params.get("symbol")
        if query_type == "rt_overview":
            return hub.market_overview()
        if not codes:
            return None
        if isinstance(codes, str):
            codes = [c.strip() for c in re.split(r"[,，;；\s]+", codes) if c.strip()]
        res = hub.quotes(list(codes))
        return res.data if res.ok else None

    def _query_sina_hq(self, query_type: str, params: Dict) -> Optional[Dict]:
        """新浪行情接口直连。"""
        code = params.get("code", "")
        if not code:
            return None
        try:
            try:
                from http_utils import fetch_text
            except ImportError:
                from scripts.http_utils import fetch_text

            prefix = "sh" if code.startswith("6") else "sz"
            url = f"https://hq.sinajs.cn/list={prefix}{code}"
            text = fetch_text(url, headers={
                "Referer": "https://finance.sina.com.cn/",
            }, timeout=10)
            if text and "=" in text and '"' in text:
                parts = text.split('"')[1].split(",")
                if len(parts) > 10:
                    return {
                        "name": parts[0],
                        "open": parts[1],
                        "yesterday_close": parts[2],
                        "current": parts[3],
                        "high": parts[4],
                        "low": parts[5],
                        "volume": parts[8],
                        "amount": parts[9],
                        "date": parts[30] if len(parts) > 30 else "",
                        "time": parts[31] if len(parts) > 31 else "",
                    }
        except Exception as e:
            log.debug(f"Sina HQ query failed: {e}")
        return None

    def _query_search(self, query_type: str, params: Dict) -> Optional[List[Dict]]:
        """搜索引擎兜底。"""
        query = params.get("query", params.get("code", ""))
        if not query:
            return None
        try:
            try:
                from search_engine import quick_search
            except ImportError:
                from scripts.search_engine import quick_search
            results = quick_search(query, engines=["baidu_html"], max_results=5)
            return results if results else None
        except Exception as e:
            log.debug(f"Search fallback failed: {e}")
        return None

    def _record_success(self, source: DataSource, latency_ms: float):
        with self._lock:
            h = self._health[source]
            h.total_queries += 1
            h.total_success += 1
            h.consecutive_failures = 0
            h.last_success = datetime.now().isoformat(timespec="seconds")
            # 移动平均延迟
            if h.avg_latency_ms == 0:
                h.avg_latency_ms = latency_ms
            else:
                h.avg_latency_ms = h.avg_latency_ms * 0.8 + latency_ms * 0.2

    def _record_failure(self, source: DataSource, reason: str):
        with self._lock:
            h = self._health[source]
            h.total_queries += 1
            h.consecutive_failures += 1
            h.last_failure = datetime.now().isoformat(timespec="seconds")

    def get_health(self) -> Dict[str, Any]:
        """获取所有数据源健康状态。"""
        with self._lock:
            return {src.name: {
                "available": getattr(self._adapters.get(src), 'available', True),
                "success_rate": round(h.success_rate, 3),
                "consecutive_failures": h.consecutive_failures,
                "avg_latency_ms": round(h.avg_latency_ms, 1),
            } for src, h in self._health.items()}


# ============================================================================
# 9. 全局实例与便捷函数
# ============================================================================

_global_router: Optional[FinancialDataRouter] = None
_router_lock = threading.Lock()


def get_router(**kwargs) -> FinancialDataRouter:
    """获取全局金融数据路由器（单例）。"""
    global _global_router
    with _router_lock:
        if _global_router is None:
            _global_router = FinancialDataRouter(**kwargs)
        return _global_router


def query_financial_data(query_type: str, **params) -> QueryResult:
    """便捷函数：统一查询金融数据。

    Examples:
        >>> result = query_financial_data("stock_quote", code="600519")
        >>> result = query_financial_data("macro_cpi")
        >>> result = query_financial_data("fund_nav", code="110011")
    """
    return get_router().query(query_type, **params)


def list_available_sources() -> Dict[str, Any]:
    """便捷函数：列出所有数据源状态。"""
    return get_router().list_sources()


def list_query_types() -> Dict[str, List[str]]:
    """便捷函数：列出所有支持的查询类型。"""
    return get_router().list_capabilities()


# ============================================================================
# 10. 自检
# ============================================================================

def self_check() -> Dict[str, Any]:
    """数据源桥接模块自检。"""
    router = get_router()
    sources = router.list_sources()
    active = sum(1 for s in sources.values() if s["available"])
    return {
        "version": "v9.0",
        "total_sources": len(sources),
        "active_sources": active,
        "source_status": {k: ("✅" if v["available"] else "❌") + f" {v['success_rate']:.0%}"
                          for k, v in sources.items()},
        "supported_queries": len(SOURCE_PRIORITY),
        "query_types": list(SOURCE_PRIORITY.keys()),
        "akshare": "✅ installed" if AkshareAdapter().available else "❌ not installed (pip install akshare)",
        "yfinance": "✅ installed" if YFinanceAdapter().available else "❌ not installed (pip install yfinance)",
    }


if __name__ == "__main__":
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
