# -*- coding: utf-8 -*-
"""
MCP 数据获取扩充模块 v10.0 (mcp_data_expansion.py)

在 v9 financial_data_mcp.py 的 5 适配器基础上扩展，引入：
1. **QueryCache LRU+TTL 缓存层**：高频查询自动缓存，跨进程持久化（可选）
2. **TushareAdapter**：A 股全维度数据库（需 token，从 env TUSHARE_TOKEN 读取）
3. **AlphaVantageAdapter**：外汇/数字货币历史（免费层 25 次/天，从 env ALPHA_VANTAGE_KEY）
4. **CoinGeckoAdapter**：数字货币行情/市值/历史（无需 key）
5. **ExchangeRateAdapter**：实时外汇汇率（无需 key）

设计原则：
- 所有适配器零强依赖（Tushare/AlphaVantage 可选）
- 缓存层可独立 import，缺失依赖时降级为内存缓存
- 与 v9 FinancialDataRouter 共存（register_expansion_adapters() 一行接入）
- 线程安全；持久化数据尽量小

向后兼容：所有类独立，与 v9 financial_data_mcp.py 互补。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlencode

log = logging.getLogger(__name__)


# ============================================================================
# 1. QueryCache — LRU + TTL 缓存层
# ============================================================================

@dataclass
class CacheEntry:
    """缓存条目。"""
    key: str
    value: Any
    created_at: float
    ttl_seconds: float
    hit_count: int = 0
    source: str = ""

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.created_at) > self.ttl_seconds

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class QueryCache:
    """LRU + TTL 缓存层（内存版）。

    用法：
        cache = QueryCache(max_size=200, default_ttl=300)
        cache.set("stock:600519", {"price": 1820.0}, ttl=60)
        val = cache.get("stock:600519")
    """

    def __init__(self, max_size: int = 200,
                 default_ttl: float = 300.0,
                 persist_path: Optional[str] = None) -> None:
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._lock = threading.RLock()
        self._persist_path = Path(persist_path) if persist_path else None
        # 统计
        self.stats_hits = 0
        self.stats_misses = 0
        self.stats_evictions = 0
        self._load()

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for key, data in raw.get("entries", []):
                self._cache[key] = CacheEntry(**data)
            self.stats_hits = raw.get("hits", 0)
            self.stats_misses = raw.get("misses", 0)
            self.stats_evictions = raw.get("evictions", 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("QueryCache load failed: %s", exc)

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "entries": [(k, e.to_dict()) for k, e in self._cache.items()],
                "hits": self.stats_hits,
                "misses": self.stats_misses,
                "evictions": self.stats_evictions,
            }
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("QueryCache save failed: %s", exc)

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self.stats_misses += 1
                return None
            if entry.is_expired():
                del self._cache[key]
                self.stats_misses += 1
                return None
            entry.hit_count += 1
            self.stats_hits += 1
            # LRU: 移到末尾
            self._cache.move_to_end(key)
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None,
            source: str = "") -> None:
        with self._lock:
            ttl = ttl if ttl is not None else self.default_ttl
            self._cache[key] = CacheEntry(
                key=key, value=value,
                created_at=time.time(),
                ttl_seconds=ttl,
                source=source,
            )
            self._cache.move_to_end(key)
            # 超出容量 → 淘汰最早的
            while len(self._cache) > self.max_size:
                self._cache.popitem(last=False)
                self.stats_evictions += 1
            self._save()

    def delete(self, key: str) -> bool:
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._save()
                return True
        return False

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
            self._save()

    def keys(self) -> List[str]:
        with self._lock:
            return list(self._cache.keys())

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self.stats_hits + self.stats_misses
            hit_rate = self.stats_hits / total if total else 0.0
            return {
                "size": len(self._cache),
                "max_size": self.max_size,
                "hits": self.stats_hits,
                "misses": self.stats_misses,
                "evictions": self.stats_evictions,
                "hit_rate": round(hit_rate, 3),
                "default_ttl_s": self.default_ttl,
            }


# 全局默认缓存
_DEFAULT_CACHE: Optional[QueryCache] = None
_DEFAULT_CACHE_LOCK = threading.Lock()


def get_default_cache() -> QueryCache:
    """获取全局默认缓存（懒初始化）。"""
    global _DEFAULT_CACHE
    with _DEFAULT_CACHE_LOCK:
        if _DEFAULT_CACHE is None:
            _DEFAULT_CACHE = QueryCache(
                max_size=300,
                default_ttl=300.0,
                persist_path="data/cache/query_cache.json",
            )
        return _DEFAULT_CACHE


# ============================================================================
# 2. Tushare Pro 适配器
# ============================================================================

class TushareAdapter:
    """Tushare Pro 适配器（需 token，从环境变量 TUSHARE_TOKEN 读取）。

    支持的查询类型（按 Tushare Pro 接口）：
    - stock_basic：A 股/港股/美股基础信息
    - daily：日线行情
    - daily_basic：每日指标（PE/PB/换手率）
    - income：利润表
    - balancesheet：资产负债表
    - cashflow：现金流量表
    - fund_basic：基金基础信息
    - fund_nav：基金净值
    - index_basic：指数基础信息
    - index_daily：指数日线
    - futures_basic：期货基础
    - fut_daily：期货日线
    - options_basic：期权基础
    - opt_daily：期权日线

    可用性检查：
        adapter = TushareAdapter()
        adapter.available  # True/False
    """

    name = "tushare"
    base_url = "http://api.tushare.pro"

    def __init__(self, token: Optional[str] = None,
                 cache: Optional[QueryCache] = None) -> None:
        self.token = token or os.environ.get("TUSHARE_TOKEN", "")
        self.cache = cache or get_default_cache()
        self._ts: Any = None
        self._loaded = False
        self._load()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.token:
            log.debug("Tushare token not set")
            return
        try:
            import tushare  # type: ignore
            self._ts = tushare.pro_api(self.token)
            log.info("Tushare adapter loaded")
        except Exception as exc:  # noqa: BLE001
            log.debug("Tushare not available: %s", exc)

    @property
    def available(self) -> bool:
        return self._ts is not None

    def _call(self, api_name: str, fields: str = "",
              **kwargs: Any) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        try:
            df = self._ts.query(api_name, fields=fields, **kwargs)
            if df is None:
                return []
            return df.to_dict("records")
        except Exception as exc:  # noqa: BLE001
            log.warning("Tushare %s failed: %s", api_name, exc)
            return []

    def query(self, query_type: str, symbol: str = "",
              **kwargs: Any) -> List[Dict[str, Any]]:
        cache_key = f"tushare:{query_type}:{symbol}:{json.dumps(kwargs, sort_keys=True, default=str)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        # Tushare API: 直接映射到 query API
        result = self._call(query_type, symbol=symbol, **kwargs)
        if result:
            self.cache.set(cache_key, result, ttl=600, source="tushare")
        return result

    def health(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "has_token": bool(self.token),
            "name": self.name,
        }


# ============================================================================
# 3. Alpha Vantage 适配器
# ============================================================================

class AlphaVantageAdapter:
    """Alpha Vantage 适配器（免费层 25 次/天）。

    支持：
    - fx_daily：外汇日线（需要 from/to 货币对）
    - fx_intraday：外汇盘中（1/5/15/30/60 分钟）
    - crypto_daily：数字货币日线
    - digital_currency_daily：数字货币日线（含 USD volume）
    - time_series_daily：股票日线
    """

    name = "alpha_vantage"
    base_url = "https://www.alphavantage.co/query"

    def __init__(self, api_key: Optional[str] = None,
                 cache: Optional[QueryCache] = None) -> None:
        self.api_key = api_key or os.environ.get("ALPHA_VANTAGE_KEY", "")
        self.cache = cache or get_default_cache()
        self._requests: Any = None
        self._load()

    def _load(self) -> None:
        try:
            import requests  # type: ignore
            self._requests = requests
        except Exception:  # noqa: BLE001
            log.debug("requests not available, AlphaVantage will use urllib")

    def _http_get(self, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["apikey"] = self.api_key or "demo"
        url = f"{self.base_url}?{urlencode(params)}"
        try:
            if self._requests:
                resp = self._requests.get(url, timeout=15)
                resp.raise_for_status()
                return resp.json()
            else:
                from urllib.request import urlopen, Request
                req = Request(url, headers={"User-Agent": "cn-financial-scraper/10.0"})
                with urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("AlphaVantage GET failed: %s", exc)
            return {}

    def _call(self, function: str, **kwargs: Any) -> Dict[str, Any]:
        cache_key = f"alpha_vantage:{function}:{json.dumps(kwargs, sort_keys=True, default=str)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        params = {"function": function}
        params.update(kwargs)
        result = self._http_get(params)
        if result and "Error Message" not in result and "Note" not in result:
            self.cache.set(cache_key, result, ttl=3600, source="alpha_vantage")
        return result

    def query(self, query_type: str, symbol: str = "",
              market: str = "USD", **kwargs: Any) -> Dict[str, Any]:
        if query_type in ("fx_daily", "forex_daily"):
            return self._call("FX_DAILY", from_symbol=symbol,
                              to_symbol=kwargs.get("to_symbol", "USD"))
        if query_type == "fx_intraday":
            return self._call("FX_INTRADAY", from_symbol=symbol,
                              to_symbol=kwargs.get("to_symbol", "USD"),
                              interval=kwargs.get("interval", "5min"))
        if query_type == "crypto_daily":
            return self._call("DIGITAL_CURRENCY_DAILY", symbol=symbol, market=market)
        if query_type == "stock_daily":
            return self._call("TIME_SERIES_DAILY", symbol=symbol,
                              outputsize=kwargs.get("outputsize", "compact"))
        return {}

    def health(self) -> Dict[str, Any]:
        return {
            "available": bool(self.api_key),
            "has_key": bool(self.api_key),
            "name": self.name,
        }


# ============================================================================
# 4. CoinGecko 适配器
# ============================================================================

class CoinGeckoAdapter:
    """CoinGecko 适配器（无需 key，免费层 10-30 次/分钟）。

    支持：
    - coin_list：所有币种列表
    - coin_markets：行情（按市值排序）
    - coin_history：历史价格
    - coin_ohlc：OHLC K 线
    - global_data：全球加密市场数据
    - trending：热门搜索
    """

    name = "coingecko"
    base_url = "https://api.coingecko.com/api/v3"

    def __init__(self, cache: Optional[QueryCache] = None,
                 pro_api_key: Optional[str] = None) -> None:
        self.cache = cache or get_default_cache()
        self.pro_api_key = pro_api_key or os.environ.get("COINGECKO_PRO_KEY", "")
        self._requests: Any = None
        try:
            import requests  # type: ignore
            self._requests = requests
        except Exception:  # noqa: BLE001
            log.debug("requests not available, CoinGecko will use urllib")

    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urlencode(params)
        headers: Dict[str, str] = {"User-Agent": "cn-financial-scraper/10.0"}
        if self.pro_api_key:
            headers["x-cg-pro-api-key"] = self.pro_api_key
        try:
            if self._requests:
                resp = self._requests.get(url, headers=headers, timeout=15)
                resp.raise_for_status()
                return resp.json()
            else:
                from urllib.request import urlopen, Request
                req = Request(url, headers=headers)
                with urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("CoinGecko GET failed: %s", exc)
            return None

    def _call(self, path: str, params: Optional[Dict[str, Any]] = None,
              ttl: int = 300) -> Any:
        cache_key = f"coingecko:{path}:{json.dumps(params or {}, sort_keys=True, default=str)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._http_get(path, params)
        if result is not None:
            self.cache.set(cache_key, result, ttl=ttl, source="coingecko")
        return result

    def query(self, query_type: str, coin_id: str = "bitcoin",
              vs_currency: str = "usd", **kwargs: Any) -> Any:
        if query_type in ("coin_markets", "markets"):
            return self._call("/coins/markets", {
                "vs_currency": vs_currency,
                "ids": coin_id,
                "order": "market_cap_desc",
                "per_page": kwargs.get("per_page", 100),
                "page": kwargs.get("page", 1),
            }, ttl=120)
        if query_type == "coin_history":
            return self._call(f"/coins/{coin_id}/history", {
                "date": kwargs.get("date", ""),
                "localization": "false",
            }, ttl=3600)
        if query_type == "coin_ohlc":
            return self._call(f"/coins/{coin_id}/ohlc", {
                "vs_currency": vs_currency,
                "days": kwargs.get("days", 7),
            }, ttl=600)
        if query_type == "global":
            return self._call("/global", None, ttl=600)
        if query_type == "trending":
            return self._call("/search/trending", None, ttl=300)
        if query_type == "coin_list":
            return self._call("/coins/list", None, ttl=86400)
        return None

    def health(self) -> Dict[str, Any]:
        return {"available": True, "has_pro_key": bool(self.pro_api_key), "name": self.name}


# ============================================================================
# 5. ExchangeRate-API 适配器
# ============================================================================

class ExchangeRateAdapter:
    """ExchangeRate-API 适配器（外汇实时汇率，无需 key）。

    支持：
    - latest：最新汇率（基准货币）
    - pair：两个货币对的实时汇率
    - historical：历史某日汇率
    """

    name = "exchangerate_api"
    base_url = "https://open.er-api.com/v6/latest"

    def __init__(self, cache: Optional[QueryCache] = None) -> None:
        self.cache = cache or get_default_cache()
        self._requests: Any = None
        try:
            import requests  # type: ignore
            self._requests = requests
        except Exception:  # noqa: BLE001
            log.debug("requests not available, ExchangeRate will use urllib")

    def _http_get(self, url: str) -> Any:
        try:
            if self._requests:
                resp = self._requests.get(url, timeout=15)
                resp.raise_for_status()
                return resp.json()
            else:
                from urllib.request import urlopen, Request
                req = Request(url, headers={"User-Agent": "cn-financial-scraper/10.0"})
                with urlopen(req, timeout=15) as r:
                    return json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.warning("ExchangeRate GET failed: %s", exc)
            return None

    def _call(self, base: str = "USD", ttl: int = 3600) -> Any:
        cache_key = f"exchangerate:latest:{base}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        result = self._http_get(f"{self.base_url}/{base}")
        if result and result.get("result") == "success":
            self.cache.set(cache_key, result, ttl=ttl, source="exchangerate")
        return result

    def query(self, query_type: str, base: str = "USD",
              target: Optional[str] = None, **kwargs: Any) -> Any:
        if query_type in ("latest", "all_rates"):
            return self._call(base)
        if query_type == "pair" and target:
            data = self._call(base)
            if data and isinstance(data, dict):
                rates = data.get("rates", {})
                if target in rates:
                    return {
                        "base": base,
                        "target": target,
                        "rate": rates[target],
                        "timestamp": data.get("time_last_update_unix"),
                    }
        if query_type == "historical":
            # open.er-api.com 公开历史需付费，使用 exchangerate.host 公开 fallback
            date = kwargs.get("date", "")
            if date:
                url = f"https://api.exchangerate.host/{date}?base={base}"
                if target:
                    url += f"&symbols={target}"
                return self._http_get(url)
        return None

    def health(self) -> Dict[str, Any]:
        return {"available": True, "name": self.name}


# ============================================================================
# 6. 扩展路由器（与 v9 FinancialDataRouter 集成）
# ============================================================================

class ExpansionAdapterSet:
    """v10 新增适配器集合。可注册到 v9 FinancialDataRouter。"""

    def __init__(self, cache: Optional[QueryCache] = None) -> None:
        self.cache = cache or get_default_cache()
        self.tushare = TushareAdapter(cache=self.cache)
        self.alpha_vantage = AlphaVantageAdapter(cache=self.cache)
        self.coingecko = CoinGeckoAdapter(cache=self.cache)
        self.exchangerate = ExchangeRateAdapter(cache=self.cache)

    def all_health(self) -> Dict[str, Dict[str, Any]]:
        return {
            "tushare": self.tushare.health(),
            "alpha_vantage": self.alpha_vantage.health(),
            "coingecko": self.coingecko.health(),
            "exchangerate": self.exchangerate.health(),
        }

    def query(self, query_type: str, symbol: str = "", **kwargs: Any) -> Any:
        """按 query_type 路由到合适适配器。"""
        # 数字货币 → CoinGecko / AlphaVantage
        if query_type in ("crypto_quote", "crypto_markets", "crypto_history",
                          "crypto_ohlc", "crypto_global", "crypto_trending"):
            inner = query_type.replace("crypto_", "")
            coin_id = kwargs.pop("coin_id", symbol) or "bitcoin"
            return self.coingecko.query(inner, coin_id=coin_id, **kwargs)
        if query_type in ("crypto_daily_vantage",):
            return self.alpha_vantage.query("crypto_daily", symbol=symbol, **kwargs)
        # 外汇 → ExchangeRate
        if query_type in ("forex_realtime", "forex_pair", "forex_all_rates"):
            qt = query_type.replace("forex_", "")
            if qt == "realtime":
                qt = "pair"
            return self.exchangerate.query(qt, base=symbol,
                                           target=kwargs.get("target"), **kwargs)
        if query_type == "fx_daily_vantage":
            return self.alpha_vantage.query("fx_daily", symbol=symbol, **kwargs)
        # Tushare 通用
        if query_type.startswith("tushare_"):
            actual = query_type.replace("tushare_", "")
            return self.tushare.query(actual, symbol=symbol, **kwargs)
        # AlphaVantage 股票
        if query_type == "av_stock_daily":
            return self.alpha_vantage.query("stock_daily", symbol=symbol, **kwargs)
        return None

    def list_query_types(self) -> List[str]:
        return [
            "crypto_quote", "crypto_markets", "crypto_history",
            "crypto_ohlc", "crypto_global", "crypto_trending",
            "crypto_daily_vantage",
            "forex_realtime", "forex_pair", "forex_all_rates",
            "fx_daily_vantage",
            "av_stock_daily",
            "tushare_stock_basic", "tushare_daily", "tushare_daily_basic",
            "tushare_income", "tushare_balancesheet", "tushare_cashflow",
            "tushare_fund_basic", "tushare_fund_nav",
            "tushare_index_basic", "tushare_index_daily",
            "tushare_futures_basic", "tushare_fut_daily",
        ]


class RouterBridge:
    """把 v10 适配器的 (query_type, symbol=…, **kw) 签名适配成
    v9 路由器要求的 (query_type, params_dict) 协议。

    直接注册裸适配器是坏的：路由器会调用 adapter.query(qt, params)，
    params 这个 dict 会落到 symbol 形参上，适配器拿着字典去拼 URL。
    """

    def __init__(self, adapter_set: "ExpansionAdapterSet", name: str,
                 accepts: Callable[[str], bool]) -> None:
        self.set = adapter_set
        self.name = name
        self.accepts = accepts
        self._total = 0
        self._ok = 0

    @property
    def available(self) -> bool:
        return True

    def query(self, query_type: str, params: Optional[Dict[str, Any]] = None
              ) -> Any:
        if not self.accepts(query_type):
            return None
        p = dict(params or {})
        symbol = p.pop("symbol", None) or p.pop("code", None) or ""
        for dead in ("preferred_source", "query_type"):
            p.pop(dead, None)
        self._total += 1
        out = self.set.query(query_type, symbol=str(symbol), **p)
        if out is not None:
            self._ok += 1
        return out

    def health(self) -> Dict[str, Any]:
        return {"source": self.name, "available": True,
                "queries": self._total, "success": self._ok}


_ACCEPTS = {
    "tushare": lambda qt: qt.startswith("tushare_"),
    "alpha_vantage": lambda qt: qt in ("crypto_daily_vantage", "fx_daily_vantage",
                                        "av_stock_daily"),
    "coingecko": lambda qt: qt.startswith("crypto_") and "vantage" not in qt,
    "exchangerate": lambda qt: qt.startswith("forex_"),
}


def register_expansion_adapters(router: Any,
                                adapter_set: Optional["ExpansionAdapterSet"] = None
                                ) -> int:
    """把 v10 新适配器注册到 v9 FinancialDataRouter。

    router: FinancialDataRouter 实例
    返回：注册的适配器数。失败/不兼容返回 0。
    """
    if router is None:
        return 0
    set_ = adapter_set or getattr(router, "_cnfs_expansion_set", None) or ExpansionAdapterSet()
    try:
        router._cnfs_expansion_set = set_  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    count = 0
    # 尝试调用私有 _register_adapter（与 v9 实现对齐）
    if hasattr(router, "_register_adapter"):
        try:
            from financial_data_mcp import DataSource
        except ImportError:  # 包内导入
            from scripts.financial_data_mcp import DataSource
        pairs = (
            (DataSource.TUSHARE, "tushare"),
            (DataSource.ALPHA_VANTAGE, "alpha_vantage"),
            (DataSource.COINGECKO, "coingecko"),
            (DataSource.EXCHANGERATE, "exchangerate"),
        )
        for src, key in pairs:
            try:
                router._register_adapter(src, RouterBridge(set_, key, _ACCEPTS[key]))
                count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("register %s failed: %s", key, exc)
    # 兼容接口：尝试 public register_adapter(name, adapter)
    elif hasattr(router, "register_adapter"):
        for key in _ACCEPTS:
            try:
                router.register_adapter(key, RouterBridge(set_, key, _ACCEPTS[key]))
                count += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("register %s failed: %s", key, exc)
    else:
        log.warning("Router has no register API; ExpansionAdapterSet 需独立调用")
    return count


# ============================================================================
# 7. 公共导出
# ============================================================================

__all__ = [
    "CacheEntry",
    "QueryCache",
    "get_default_cache",
    "TushareAdapter",
    "AlphaVantageAdapter",
    "CoinGeckoAdapter",
    "ExchangeRateAdapter",
    "ExpansionAdapterSet",
    "register_expansion_adapters",
    "RouterBridge",
]