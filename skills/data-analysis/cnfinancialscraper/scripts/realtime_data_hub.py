# -*- coding: utf-8 -*-
"""实时行情中枢（v11.0.0 新增）。

与 v10 之前所有"取数"路径的根本差别：**绕开 HTTP 层默认的 1800 秒响应缓存**。
scripts.http_utils.http_get 默认 use_cache=True，同一个 URL 半小时内返回同一份
缓存体；照那条路取"实时"行情，拿到的必然是过期快照。本模块所有请求强制
use_cache=False，并把每个数据点的 asof 时间戳与新鲜度一起交回调用方。

设计约束：
- 零 pip 依赖：只用 urllib（经 http_utils）+ 标准库，未装 requests/akshare 也能跑。
- 全链路可注入 fetcher，因此离线可测；单元测试不碰网络。
- 多源自动降级：腾讯 → 新浪 → 东方财富，逐只标记实际命中的源。
- 不做无界阻塞：stream 有硬超时上限，防止 MCP 服务端被吊死。

数据点字段统一为 Quote dict，可直接喂 scripts.indicator_engine。
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "CN_TZ",
    "CodeInfo", "normalize_code", "resolve_symbol", "KNOWN_INDEXES",
    "MarketClock", "FetchResult", "parse_quote_timestamp",
    "TencentQuoteProvider", "SinaQuoteProvider", "EastmoneyQuoteProvider",
    "EastmoneyKlineProvider", "FundEstimateProvider", "EastmoneyBoardProvider",
    "BarStore", "Watchlist", "RealtimeHub", "get_hub",
]

CN_TZ = timezone(timedelta(hours=8))
SKILL_ROOT = Path(__file__).resolve().parent.parent
REALTIME_DIR = SKILL_ROOT / "data" / "realtime"

# 单次 stream 的硬上限：宁可不取全，也不能把 MCP 服务端挂住
MAX_STREAM_SECONDS = 120
MAX_STREAM_POINTS = 400
MIN_POLL_INTERVAL = 1.0

Fetcher = Callable[..., Optional[str]]


# ─── 代码归一 ────────────────────────────────────────────────────────────────

@dataclass
class CodeInfo:
    raw: str
    pure: str          # 6 位数字（或指数代码）
    market: str        # sh | sz | bj | hk | us
    kind: str          # stock | index | fund | eto
    name: str = ""

    @property
    def sina(self) -> str:
        if self.market == "hk":
            return f"hk{self.pure.zfill(5)}"
        if self.market == "us":
            return self.pure.lower()
        return f"{self.market}{self.pure}"

    @property
    def tencent(self) -> str:
        if self.market == "hk":
            return f"hk{self.pure.zfill(5)}"
        if self.market == "us":
            return f"us{self.pure.upper()}"
        return f"{self.market}{self.pure}"

    @property
    def secid(self) -> str:
        """东方财富 secid：1=沪, 0=深, 116=港股, 105/106/107=美股。"""
        if self.market == "sh":
            return f"1.{self.pure}"
        if self.market == "sz":
            return f"0.{self.pure}"
        if self.market == "bj":
            return f"0.{self.pure}"
        if self.market == "hk":
            return f"116.{self.pure.zfill(5)}"
        if self.market == "us":
            for pre in ("105.", "106.", "107."):
                if self.pure.startswith(pre):
                    return self.pure
            return f"105.{self.pure.upper()}"
        return self.pure

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update({"sina": self.sina, "tencent": self.tencent, "secid": self.secid})
        return d


# 常见宽基指数：中文名 → (market, code)
KNOWN_INDEXES: Dict[str, Tuple[str, str]] = {
    "上证指数": ("sh", "000001"), "上证综指": ("sh", "000001"),
    "深证成指": ("sz", "399001"), "创业板指": ("sz", "399006"),
    "沪深300": ("sh", "000300"), "中证500": ("sh", "000905"),
    "中证1000": ("sh", "000852"), "上证50": ("sh", "000016"),
    "科创50": ("sh", "000688"), "北证50": ("bj", "899050"),
    "中证全债": ("sh", "000012"),
}

_INDEX_CODE_NAMES = {v[1]: v[0] for v in KNOWN_INDEXES.values()}

_RE_PLAIN = re.compile(r"^\d{5,6}$")
_RE_SUFFIXED = re.compile(r"^(\d{5,6})\.(SH|SZ|BJ|SS|HK|US)$", re.IGNORECASE)
_RE_PREFIXED = re.compile(r"^(sh|sz|bj|hk|us)\.?(\w+)$", re.IGNORECASE)
_RE_EASTMONEY = re.compile(r"^(\d)\.(\d{5,6})$")


def _infer_market(pure: str) -> str:
    """按代码首位推断交易所。显式前缀永远优先于这里。"""
    if pure.startswith(("6", "9", "5")) or pure.startswith("688"):
        return "sh"
    if pure.startswith(("4", "8")) or pure.startswith("92"):
        return "bj"
    return "sz"


def _infer_kind(pure: str, market: str) -> str:
    # 000001 在沪是上证指数、在深是平安银行：代码表必须连市场一起比对，
    # 否则裸码 000001 会被静默标成指数。
    if _INDEX_CODE_NAMES.get(pure) == market or (market == "sh" and pure.startswith("000")):
        return "index"
    if market == "sz" and pure.startswith("399"):
        return "index"
    if market == "bj" and pure.startswith("899"):
        return "index"
    if pure.startswith(("15", "51", "52", "56", "58", "50")):
        return "etf"
    return "stock"


def normalize_code(raw: Any) -> Optional[CodeInfo]:
    """把用户给的各种写法归一成 CodeInfo。

    支持：600519 / 600519.SH / sh600519 / SH600519 / 上证指数 / 1.600519(东财) /
    hk00700 / AAPL。无法识别返回 None（不猜，避免把 000001 静默当成平安银行或上证）。

    裸码的市场一律按首位数字推断，kind 再由该市场推出，两者不会互相打架：
    所以 "000001" 是深市平安银行（股票），要拿上证指数得写 "上证指数"、
    "1.000001" 或 "sh000001"。同名指数请用 KNOWN_INDEXES 里的中文名。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text in KNOWN_INDEXES:
        market, code = KNOWN_INDEXES[text]
        return CodeInfo(raw=text, pure=code, market=market, kind="index", name=text)

    m = _RE_EASTMONEY.match(text)
    if m:
        market = "sh" if m.group(1) == "1" else "sz"
        return CodeInfo(raw=text, pure=m.group(2), market=market,
                        kind=_infer_kind(m.group(2), market))

    m = _RE_SUFFIXED.match(text)
    if m:
        suffix = m.group(2).upper()
        mapping = {"SH": "sh", "SS": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk", "US": "us"}
        market = mapping[suffix]
        return CodeInfo(raw=text, pure=m.group(1), market=market,
                        kind=_infer_kind(m.group(1), market))

    m = _RE_PREFIXED.match(text)
    if m:
        market = m.group(1).lower()
        body = m.group(2)
        if market == "us":
            return CodeInfo(raw=text, pure=body.upper(), market="us", kind="stock")
        if body.isdigit() and _RE_PLAIN.match(body):
            return CodeInfo(raw=text, pure=body, market=market,
                            kind=_infer_kind(body, market))
        return CodeInfo(raw=text, pure=body.upper(), market=market, kind="stock")

    if text.isdigit() and _RE_PLAIN.match(text):
        market = _infer_market(text)
        return CodeInfo(raw=text, pure=text, market=market, kind=_infer_kind(text, market))

    if re.match(r"^[A-Za-z][A-Za-z.\-]{0,9}$", text):
        return CodeInfo(raw=text, pure=text.upper(), market="us", kind="stock")
    return None


def resolve_symbol(raw: Any) -> str:
    """稳定展示符号：股票/基金用 6 位码，指数带市场前缀，美股用 ticker。"""
    info = normalize_code(raw)
    if not info:
        return str(raw).strip().upper()
    if info.market == "us":
        return info.pure
    if info.kind == "index":
        return f"{info.market}{info.pure}"
    return info.pure


# ─── 市场时钟 ────────────────────────────────────────────────────────────────

@dataclass
class MarketClock:
    """A 股市场时段判定。

    时段规则是交易所公开规则（9:15 集合竞价、9:30-11:30、13:00-15:00，
    北交所到 15:30）。节假日表是**可外部维护的建议性数据**：真正可靠的休市
    信号来自行情时间戳本身——工作日却发现最新成交仍是旧一天，则判为休市。
    `is_open` 会同时使用两者。
    """

    holiday_table: Dict[str, str] = field(default_factory=dict)
    half_day: Dict[str, str] = field(default_factory=dict)
    now_fn: Callable[[], datetime] = lambda: datetime.now(CN_TZ)

    def __post_init__(self) -> None:
        self.load_external_calendar()

    def load_external_calendar(self) -> Dict[str, str]:
        """可选加载 data/trading_calendar.json：{"holidays": ["2026-01-01", ...]}。"""
        path = SKILL_ROOT / "data" / "trading_calendar.json"
        if not path.exists():
            return self.holiday_table
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self.holiday_table
        for key in ("holidays", "休市", "holiday"):
            val = raw.get(key)
            if isinstance(val, list):
                self.holiday_table.update({str(d): "休市" for d in val})
        for key in ("half_days", "半日市"):
            val = raw.get(key)
            if isinstance(val, list):
                self.half_day.update({str(d): "半日市" for d in val})
        return self.holiday_table

    def today(self) -> date:
        return self.now_fn().date()

    def is_weekend(self, d: Optional[date] = None) -> bool:
        return (d or self.today()).weekday() >= 5

    def is_holiday(self, d: Optional[date] = None) -> bool:
        return (d or self.today()).isoformat() in self.holiday_table

    def is_trading_day(self, d: Optional[date] = None) -> bool:
        day = d or self.today()
        return not (self.is_weekend(day) or day.isoformat() in self.holiday_table)

    def state(self, d: Optional[date] = None) -> str:
        day = d or self.today()
        if not self.is_trading_day(day):
            return "holiday" if not self.is_weekend(day) else "weekend"
        now = self.now_fn()
        hm = now.hour * 60 + now.minute
        if hm < 9 * 60 + 15:
            return "pre_open"
        if hm < 9 * 60 + 30:
            return "call_auction"
        if hm < 11 * 60 + 30:
            return "morning"
        if hm < 13 * 60:
            return "lunch"
        if hm < 15 * 60:
            return "afternoon"
        if day.isoformat() in self.half_day:
            return "half_day_over"
        if hm < 15 * 60 + 30:
            return "bj_after_hours"
        return "closed"

    @property
    def is_open(self) -> bool:
        return self.state() in ("morning", "afternoon")

    def describe(self) -> Dict[str, Any]:
        now = self.now_fn()
        st = self.state()
        return {
            "cn_time": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(),
            "weekday": now.weekday() + 1,
            "state": st,
            "is_open": st in ("morning", "afternoon"),
            "is_trading_day": self.is_trading_day(),
            "next_open": self.next_open(),
            "holiday_source": "table" if self.is_holiday() else "inferred_or_weekday",
        }

    def next_open(self) -> str:
        """下一个开盘时点的字符串表示（供"该不该期待实时数据"判断）。"""
        now = self.now_fn()
        hm = now.hour * 60 + now.minute
        if self.is_trading_day() and hm < 9 * 60 + 30:
            return now.date().isoformat() + " 09:30"
        probe = now.date() + timedelta(days=1)
        for _ in range(10):
            if self.is_trading_day(probe):
                return probe.isoformat() + " 09:30"
            probe += timedelta(days=1)
        return "unknown"

    def staleness(self, asof: str, now_ts: Optional[float] = None) -> Optional[float]:
        """asof 字符串 → 距今秒数。无法解析返回 None（不假装新鲜）。

        "距今"按注入时钟算：交易时段判断、盘前盘后复盘都依赖这个钟，
        混用机器时间会让同一份报文在不同时刻得出不同结论。
        """
        ts = parse_quote_timestamp(asof)
        if ts is None:
            return None
        ref = now_ts if now_ts is not None else self.now_fn().timestamp()
        return max(0.0, ref - ts)


_DATE_RE = re.compile(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})")
_TIME_RE = re.compile(r"(\d{2}):(\d{2})(?::(\d{2}))?")


def parse_quote_timestamp(asof: str) -> Optional[float]:
    """解析各源时间戳：'2026-10-20 14:35:10' / '20261020143510' / '14:35:10'。"""
    if not asof:
        return None
    text = str(asof).strip()
    m = _DATE_RE.search(text)
    day = None
    if m:
        try:
            day = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            day = None
    tm = _TIME_RE.search(text)
    if tm:
        h, mi, s = int(tm.group(1)), int(tm.group(2)), int(tm.group(3) or 0)
    elif len(text) >= 14 and text.isdigit():
        h, mi, s = int(text[8:10]), int(text[10:12]), int(text[12:14])
    else:
        h = mi = s = 0
        if not m:
            return None
    if day is None:
        day = datetime.now(CN_TZ).date()
    try:
        return datetime(day.year, day.month, day.day, h, mi, s,
                        tzinfo=CN_TZ).timestamp()
    except ValueError:
        return None


# ─── 抓取与降级 ──────────────────────────────────────────────────────────────

@dataclass
class FetchResult:
    ok: bool
    data: Any = None
    source: str = ""
    error: str = ""
    attempts: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "source": self.source, "error": self.error,
                "attempts": self.attempts, "missing": self.missing, "data": self.data}


def _default_fetcher(url: str, *, encoding: str = "utf-8",
                     referer: str = "", headers: Optional[Dict[str, str]] = None,
                     timeout: int = 8) -> Optional[str]:
    """默认真实抓取：强制关缓存，这是"实时"成立的前提。"""
    try:
        from http_utils import http_get  # type: ignore
    except ImportError:
        from scripts.http_utils import http_get
    hdr = dict(headers or {})
    if referer:
        hdr["Referer"] = referer
    resp = http_get(url, headers=hdr, timeout=timeout, retries=1,
                    rate_limit_delay=0, use_cache=False)
    if resp is None:
        return None
    body = _decode(resp, encoding)
    if not body:
        return None
    return body


def _decode(resp: Any, encoding: str) -> str:
    """尽力解码：行情源大量是 GBK，且部分实现没有 .text 属性。"""
    if isinstance(resp, str):
        return resp
    text_attr = getattr(resp, "text", "")
    if isinstance(text_attr, str) and text_attr:
        # http_utils 已按 charset 解过；出现替换符说明解错了，退回字节重解
        if "\ufffd" not in text_attr:
            return text_attr
    content = getattr(resp, "content", None)
    if isinstance(content, bytes):
        for enc in (encoding, "gbk", "gb18030", "utf-8"):
            try:
                return content.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
    return text_attr or ""


def _num(value: Any) -> Optional[float]:
    """东财/腾讯用 '-' 表示停牌或无数据；这些必须成 None 而不是 0。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text in ("", "-", "--", "null", "None", "nan"):
        return None
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


# ─── 行情提供者 ──────────────────────────────────────────────────────────────

class _BaseProvider:
    name = "base"
    batch_limit = 50

    def __init__(self, fetcher: Optional[Fetcher] = None,
                 clock: Optional["MarketClock"] = None):
        self.fetcher = fetcher or _default_fetcher
        self.clock = clock
        self.last_error = ""

    def now_ts(self) -> float:
        """新鲜度用的时间戳：注入时钟优先，没有才用机器时间。"""
        return self.clock.now_fn().timestamp() if self.clock else time.time()

    def raw(self, url: str, *, encoding: str = "utf-8",
            referer: str = "", headers: Optional[Dict[str, str]] = None) -> Optional[str]:
        try:
            try:
                return self.fetcher(url, encoding=encoding, referer=referer, headers=headers)
            except TypeError:
                return self.fetcher(url)      # 只认 url 的老式 fetcher
        except Exception as exc:  # noqa: BLE001
            # "源挂了"和"源答上了但没这只票"是两回事，降级日志必须分得开
            self.last_error = type(exc).__name__
            return None

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    def available(self) -> bool:
        return True


class TencentQuoteProvider(_BaseProvider):
    """腾讯 qt.gtimg.cn：一次请求返回多只，含五档、量比、市值，最适合盘中轮询。"""

    name = "tencent"
    base_url = "https://qt.gtimg.cn/q="

    _IDX = {
        "name": 1, "code": 2, "price": 3, "prev_close": 4, "open": 5,
        "volume_hand": 6, "time": 30, "change": 31, "change_pct": 32,
        "high": 33, "low": 34, "volume": 36, "amount_wan": 37,
        "turnover": 38, "pe": 39, "amplitude": 43, "float_cap_yi": 44,
        "total_cap_yi": 45, "pb": 46, "limit_up": 47, "limit_down": 48,
        "volume_ratio": 49,
    }

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:
        infos = [i for i in (normalize_code(c) for c in codes) if i]
        if not infos:
            return []
        by_symbol = {i.tencent: i for i in infos}
        out: List[Dict[str, Any]] = []
        chunk: List[str] = []
        for sym in by_symbol:
            chunk.append(sym)
            if len(chunk) >= self.batch_limit:
                out.extend(self._fetch_chunk(chunk, by_symbol))
                chunk = []
        if chunk:
            out.extend(self._fetch_chunk(chunk, by_symbol))
        return out

    def _fetch_chunk(self, chunk: List[str], by_symbol: Dict[str, CodeInfo]) -> List[Dict[str, Any]]:
        url = self.base_url + ",".join(chunk)
        body = self.raw(url, encoding="gbk",
                        referer="https://gu.qq.com/",
                        headers={"User-Agent": "Mozilla/5.0"})
        if not body:
            return []
        rows: List[Dict[str, Any]] = []
        for line in body.splitlines():
            m = re.match(r'v_(\w+)="(.*)"', line.strip())
            if not m:
                continue
            info = by_symbol.get(m.group(1))
            parts = m.group(2).split("~")
            if info is None or len(parts) < 35:
                continue
            g = lambda k: _num(parts[self._IDX[k]]) if self._IDX[k] < len(parts) else None  # noqa: E731
            price = g("price")
            prev = g("prev_close")
            name = parts[self._IDX["name"]].strip() if len(parts) > self._IDX["name"] else ""
            quote = _make_quote(
                symbol=resolve_symbol(info.raw), name=name, price=price,
                prev_close=prev, open_=g("open"), high=g("high"), low=g("low"),
                # 腾讯 f36 是"手"、f37 是"万元"；统一成 股 / 元，与新浪、东财对齐，
                # 否则跨源融合会把同一成交量差出 100 倍。
                volume=g("volume") * 100 if g("volume") is not None else None,
                amount=(g("amount_wan") or 0) * 10000 if g("amount_wan") else None,
                change=g("change"), change_pct=g("change_pct"),
                turnover=g("turnover"), volume_ratio=g("volume_ratio"),
                pe=g("pe"), pb=g("pb"), amplitude=g("amplitude"),
                limit_up=g("limit_up"), limit_down=g("limit_down"),
                market_cap=(g("total_cap_yi") or 0) * 1e8 if g("total_cap_yi") else None,
                asof=parts[self._IDX["time"]] if len(parts) > self._IDX["time"] else "",
                source=self.name, kind=info.kind, now_ts=self.now_ts(),
            )
            quote["bid_1"] = _num(parts[9]) if len(parts) > 9 else None
            quote["ask_1"] = _num(parts[19]) if len(parts) > 19 else None
            rows.append(quote)
        return rows


class SinaQuoteProvider(_BaseProvider):
    """新浪 hq.sinajs.cn：必须带 Referer，否则 403。成交量单位是股、成交额是元。"""

    name = "sina"
    base_url = "https://hq.sinajs.cn/list="

    _F = {"name": 0, "open": 1, "prev_close": 2, "price": 3, "high": 4, "low": 5,
          "bid": 6, "ask": 7, "volume": 8, "amount": 9, "date": 30, "time": 31}

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:
        infos = [i for i in (normalize_code(c) for c in codes) if i]
        if not infos:
            return []
        by_sym = {i.sina: i for i in infos}
        out: List[Dict[str, Any]] = []
        keys = list(by_sym)
        for i in range(0, len(keys), self.batch_limit):
            chunk = keys[i:i + self.batch_limit]
            body = self.raw(self.base_url + ",".join(chunk), encoding="gbk",
                            referer="https://finance.sina.com.cn")
            if not body:
                continue
            for line in body.splitlines():
                m = re.match(r'var hq_str_(\w+)="(.*)";?', line.strip())
                if not m:
                    continue
                info = by_sym.get(m.group(1))
                parts = m.group(2).split(",")
                if info is None or len(parts) < 32:
                    continue
                g = lambda k: _num(parts[self._F[k]]) if self._F[k] < len(parts) else None  # noqa: E731
                price, prev = g("price"), g("prev_close")
                pct = None
                if price is not None and prev:
                    pct = round((price - prev) / prev * 100.0, 4)
                out.append(_make_quote(
                    symbol=resolve_symbol(info.raw),
                    name=parts[self._F["name"]].strip(), price=price, prev_close=prev,
                    open_=g("open"), high=g("high"), low=g("low"),
                    volume=g("volume"), amount=g("amount"), change_pct=pct,
                    change=None if price is None or prev is None else round(price - prev, 6),
                    bid_1=g("bid"), ask_1=g("ask"),
                    asof=f"{parts[self._F['date']]} {parts[self._F['time']]}"
                    if len(parts) > self._F["time"] else "",
                    source=self.name, kind=info.kind, now_ts=self.now_ts(),
                ))
        return out


class EastmoneyQuoteProvider(_BaseProvider):
    """东方财富 push2 ulist：字段最规整，停牌返回 '-'，且直接给涨跌幅/量比/换手。"""

    name = "eastmoney"
    base_url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    _FIELDS = ("f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f10,f15,f16,f17,f18,f20,f21,f23,f24,f124")
    _MAP = {"code": "f12", "name": "f14", "price": "f2", "change_pct": "f3",
            "change": "f4", "volume": "f5", "amount": "f6", "amplitude": "f7",
            "turnover": "f8", "pe": "f9", "volume_ratio": "f10", "high": "f15",
            "low": "f16", "open": "f17", "prev_close": "f18", "total_cap": "f20",
            "float_cap": "f21", "pb": "f23"}

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:
        infos = [i for i in (normalize_code(c) for c in codes) if i]
        if not infos:
            return []
        by_secid = {i.secid: i for i in infos}
        keys = list(by_secid)
        out: List[Dict[str, Any]] = []
        for i in range(0, len(keys), self.batch_limit):
            chunk = keys[i:i + self.batch_limit]
            url = (f"{self.base_url}?fltt=2&invt=2&ut=bd1d9ddb04089700cf9c27f6f7426281"
                   f"&fields={self._FIELDS}&secids=" + ",".join(chunk))
            body = self.raw(url, referer="https://quote.eastmoney.com/")
            if not body:
                continue
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            diff = ((payload.get("data") or {}).get("diff")) or []
            stamp = str((payload.get("data") or {}).get("timerefresh") or "")
            for row in diff:
                info = by_secid.get(f"{row.get('f13', '')}.{row.get('f12', '')}")
                if info is None:
                    # ulist 不回传 secid，按代码回查
                    info = next((v for v in by_secid.values()
                                 if v.pure == str(row.get("f12"))), None)
                if info is None:
                    continue
                g = lambda k: _num(row.get(self._MAP[k]))  # noqa: E731
                out.append(_make_quote(
                    symbol=resolve_symbol(info.raw),
                    name=str(row.get("f14") or "").strip(), price=g("price"),
                    prev_close=g("prev_close"), open_=g("open"), high=g("high"),
                    low=g("low"), volume=(g("volume") or 0) * 100 if g("volume") else None,
                    amount=g("amount"), change=g("change"), change_pct=g("change_pct"),
                    turnover=g("turnover"), volume_ratio=g("volume_ratio"),
                    pe=g("pe"), pb=g("pb"), amplitude=g("amplitude"),
                    market_cap=g("total_cap"), float_cap=g("float_cap"),
                    asof=stamp, source=self.name, kind=info.kind, now_ts=self.now_ts(),
                ))
        return out


class EastmoneyKlineProvider(_BaseProvider):
    """K 线：klt 1/5/15/30/60 分钟，10200=周，101=日。fqt 0 不复权 / 1 前复权 / 2 后复权。"""

    name = "eastmoney_kline"
    base_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    KLT = {"1": 1, "5": 5, "15": 15, "30": 30, "60": 60,
           "day": 101, "daily": 101, "week": 10200, "month": 10300}

    def __init__(self, fetcher: Optional[Fetcher] = None,
                 clock: Optional["MarketClock"] = None):
        super().__init__(fetcher, clock)
        # "5min" / "1h" 这类口语写法和裸数字等价，别让调用方猜格式
        self.KLT = {**self.KLT,
                    **{f"{k}min": v for k, v in (("1", 1), ("5", 5), ("15", 15),
                                                 ("30", 30), ("60", 60))},
                    "1h": 60, "hour": 60, "1d": 101, "weekly": 10200, "monthly": 10300}

    def bars(self, code: Any, period: str = "day", count: int = 120,
             adjust: str = "qfq") -> List[Dict[str, Any]]:
        info = normalize_code(code)
        if not info:
            return []
        klt = self.KLT.get(str(period).lower())
        if klt is None:
            raise ValueError(f"未知周期: {period}；可用: {', '.join(self.KLT)}")
        fqt = {"none": 0, "0": 0, "qfq": 1, "1": 1, "hfq": 2, "2": 2}.get(str(adjust).lower(), 1)
        url = (f"{self.base_url}?secid={info.secid}&klt={klt}&fqt={fqt}"
               f"&lmt={max(1, min(int(count), 1000))}&end_date=20500101"
               "&fields1=f1,f2,f3,f4,f5,f6"
               "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61")
        body = self.raw(url, referer="https://quote.eastmoney.com/")
        if not body:
            return []
        try:
            payload = json.loads(body)
        except ValueError:
            return []
        rows = ((payload.get("data") or {}).get("klines")) or []
        out: List[Dict[str, Any]] = []
        for line in rows:
            p = line.split(",")
            if len(p) < 7:
                continue
            out.append({
                "symbol": resolve_symbol(code), "ts": p[0],
                "open": _num(p[1]), "close": _num(p[2]), "high": _num(p[3]),
                "low": _num(p[4]), "volume": _num(p[5]) * 100 if _num(p[5]) is not None else None,
                "amount": _num(p[6]),
                "amplitude": _num(p[7]) if len(p) > 7 else None,
                "change_pct": _num(p[8]) if len(p) > 8 else None,
                "change": _num(p[9]) if len(p) > 9 else None,
                "turnover": _num(p[10]) if len(p) > 10 else None,
                "period": str(period), "adjust": str(adjust),
                "source": self.name,
            })
        return out


class FundEstimateProvider(_BaseProvider):
    """基金盘中估值（天天基金 fundgz）：gsz=估值，dwjz=最新公布净值，两者必须分开返回。

    估值只是估算，别拿它当净值；本模块用 kind='fund_estimate' 明确标出。
    """

    name = "fund_estimate"
    base_url = "https://fundgz.1234567.com.cn/js/"

    def nav(self, code: Any) -> Dict[str, Any]:
        info = normalize_code(code)
        pure = (info or CodeInfo(raw=str(code), pure=str(code), market="sz", kind="fund")).pure
        body = self.raw(f"{self.base_url}{pure}.js?rt={int(time.time() * 1000)}",
                        referer="https://fund.eastmoney.com/")
        est = self._parse_fundgz(body, pure)
        if est:
            return est
        return self._nav_from_lsjz(pure)

    def _parse_fundgz(self, body: str, pure: str) -> Dict[str, Any]:
        if not body:
            return {}
        m = re.search(r"jsonpgz\((.*)\)", body)
        if not m:
            return {}
        try:
            raw = json.loads(m.group(1))
        except ValueError:
            return {}
        return {
            "symbol": pure, "name": raw.get("name", ""),
            "estimated_nav": _num(raw.get("gsz")),
            "estimated_change_pct": _num(raw.get("gszzl")),
            "published_nav": _num(raw.get("dwjz")),
            "published_on": raw.get("jzrq", ""),
            "estimate_time": raw.get("gztime", ""),
            "kind": "fund_estimate", "source": self.name,
            "caveat": "gsz 为盘中估算，非披露净值",
        }

    def _nav_from_lsjz(self, pure: str) -> Dict[str, Any]:
        """历史净值接口兜底：fundgz 域外经常整域 404，这里拿的是披露净值，
        没有盘中估算值，所以 kind 明确标 fund_nav（别和估值混用）。"""
        body = self.raw(
            "https://api.fund.eastmoney.com/f10/lsjz?fundCode="
            f"{pure}&pageIndex=1&pageSize=1",
            referer="https://fundf10.eastmoney.com/",
        )
        if not body:
            return {}
        try:
            lst = (json.loads(body).get("Data") or {}).get("LSJZList") or []
        except (ValueError, AttributeError):
            return {}
        if not lst:
            return {}
        row = lst[0]
        return {
            "symbol": pure, "name": "",
            "estimated_nav": None, "estimated_change_pct": None,
            "published_nav": _num(row.get("DWJZ")),
            "accumulated_nav": _num(row.get("LJJZ")),
            "published_on": row.get("FSRQ", ""),
            "estimate_time": "",
            "kind": "fund_nav", "source": "eastmoney_lsjz",
            "caveat": "披露净值，非盘中估算",
        }

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:
        out = []
        for c in codes:
            d = self.nav(c)
            if d:
                out.append(d)
        return out


class EastmoneyBoardProvider(_BaseProvider):
    """板块 / 指数 / 涨幅榜快照（push2 clist）。fs 决定集合，详见 _FS_PRESETS。"""

    name = "eastmoney_board"
    base_url = "https://push2.eastmoney.com/api/qt/clist/get"
    _FS_PRESETS = {
        "industry": "m:90+t:2+f:!50",       # 行业板块
        "concept": "m:90+t:3+f:!50",        # 概念板块
        "region": "m:90+t:102+f:!50",       # 地域板块
        "stock_all": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "sh": "m:1+t:2", "sz": "m:0+t:6,m:0+t:80",
        "cyb": "m:0+t:80+s:40", "kcb": "m:1+t:23+s:40",
        "etf": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
    }
    _FIELDS = "f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f104,f105,f128"

    def list(self, board: str = "industry", order: str = "desc",
             count: int = 20, sort_field: str = "f3") -> List[Dict[str, Any]]:
        fs = self._FS_PRESETS.get(board)
        if fs is None:
            raise ValueError(f"未知板块类型: {board}；可用: {', '.join(self._FS_PRESETS)}")
        url = (f"{self.base_url}?pn=1&pz={max(1, min(int(count), 100))}&po={1 if order == 'desc' else 0}"
               f"&np=1&fltt=2&invt=2&ut=bd1d9ddb04089700cf9c27f6f7426281"
               f"&fid={sort_field}&fs={fs}&fields={self._FIELDS}")
        body = self.raw(url, referer="https://quote.eastmoney.com/")
        if not body:
            return []
        try:
            payload = json.loads(body)
        except ValueError:
            return []
        rows = ((payload.get("data") or {}).get("diff")) or []
        if isinstance(rows, dict):  # pn=1 时可能返回 dict
            rows = [rows]
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append({
                "code": row.get("f12"), "name": row.get("f14"),
                "price": _num(row.get("f2")), "change_pct": _num(row.get("f3")),
                "change": _num(row.get("f4")), "volume": _num(row.get("f5")),
                "amount": _num(row.get("f6")), "amplitude": _num(row.get("f7")),
                "turnover": _num(row.get("f8")), "volume_ratio": _num(row.get("f10")),
                "high": _num(row.get("f15")), "low": _num(row.get("f16")),
                "open": _num(row.get("f17")), "prev_close": _num(row.get("f18")),
                "total_cap": _num(row.get("f20")),
                "up_count": _num(row.get("f104")), "down_count": _num(row.get("f105")),
                "leader": row.get("f128"), "board": board, "source": self.name,
            })
        return out

    def quotes(self, codes: Sequence[Any]) -> List[Dict[str, Any]]:  # pragma: no cover
        return []


# ─── Quote 组装 ──────────────────────────────────────────────────────────────

def _make_quote(*, symbol: str, name: str = "", price: Optional[float] = None,
                prev_close: Optional[float] = None, open_: Optional[float] = None,
                high: Optional[float] = None, low: Optional[float] = None,
                volume: Optional[float] = None, amount: Optional[float] = None,
                change: Optional[float] = None, change_pct: Optional[float] = None,
                turnover: Optional[float] = None, volume_ratio: Optional[float] = None,
                pe: Optional[float] = None, pb: Optional[float] = None,
                amplitude: Optional[float] = None, limit_up: Optional[float] = None,
                limit_down: Optional[float] = None, market_cap: Optional[float] = None,
                float_cap: Optional[float] = None, bid_1: Optional[float] = None,
                ask_1: Optional[float] = None, asof: str = "", source: str = "",
                kind: str = "stock", now_ts: Optional[float] = None) -> Dict[str, Any]:
    ts = parse_quote_timestamp(asof)
    # now_ts 由 provider 从注入时钟取；不传才退到机器时间。新鲜度和 fetched_at
    # 必须同一个钟，否则跨源比对会被时钟抖动带偏，测试也无从复现。
    now = time.time() if now_ts is None else now_ts
    freshness = None if ts is None else round(max(0.0, now - ts), 1)
    if change is None and price is not None and prev_close:
        change = round(price - prev_close, 6)
    if change_pct is None and price is not None and prev_close:
        change_pct = round((price - prev_close) / prev_close * 100.0, 4)
    suspended = (price is None or price == 0) and (open_ in (None, 0))
    return {
        "symbol": symbol, "name": name, "kind": kind,
        "price": price, "prev_close": prev_close, "open": open_,
        "high": high, "low": low, "volume": volume, "amount": amount,
        "change": change, "change_pct": change_pct, "turnover": turnover,
        "volume_ratio": volume_ratio, "pe": pe, "pb": pb, "amplitude": amplitude,
        "limit_up": limit_up, "limit_down": limit_down,
        "market_cap": market_cap, "float_cap": float_cap,
        "bid_1": bid_1, "ask_1": ask_1,
        "asof": asof, "asof_ts": ts, "freshness_seconds": freshness,
        "source": source, "suspended": bool(suspended),
        "fetched_at": datetime.fromtimestamp(now, CN_TZ).isoformat(timespec="seconds"),
    }


# ─── 滚动数据仓与订阅 ───────────────────────────────────────────────────────

class BarStore:
    """轮询帧的滚动仓：保留最近 N 帧，可聚合成分钟 K 线喂给指标引擎。"""

    def __init__(self, maxlen: int = 600):
        self.maxlen = maxlen
        self._frames: Dict[str, List[Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def add(self, quote: Dict[str, Any]) -> None:
        price = quote.get("price")
        if price is None:
            return
        sym = quote.get("symbol") or "?"
        frame = {"ts": quote.get("asof") or datetime.now(CN_TZ).isoformat(timespec="seconds"),
                 "ts_val": quote.get("asof_ts") or time.time(),
                 "price": float(price), "volume": quote.get("volume") or 0.0,
                 "amount": quote.get("amount") or 0.0,
                 "source": quote.get("source", "")}
        with self._lock:
            buf = self._frames.setdefault(sym, [])
            if buf and buf[-1]["ts_val"] == frame["ts_val"] and buf[-1]["price"] == frame["price"]:
                return  # 同一时点重复帧不入仓
            buf.append(frame)
            if len(buf) > self.maxlen:
                del buf[:len(buf) - self.maxlen]

    def series(self, symbol: str) -> List[float]:
        with self._lock:
            return [f["price"] for f in self._frames.get(symbol, [])]

    def frames(self, symbol: str) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._frames.get(symbol, []))

    def bars(self, symbol: str, interval_minutes: int = 1) -> List[Dict[str, Any]]:
        """把轮询帧聚合成分钟 OHLC 条（interval_minutes>=1）。"""
        raw = self.frames(symbol)
        if not raw or interval_minutes < 1:
            return []
        bucket = interval_minutes * 60
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for f in raw:
            grouped.setdefault(int(f["ts_val"] // bucket) * bucket, []).append(f)
        out: List[Dict[str, Any]] = []
        for key in sorted(grouped):
            seg = grouped[key]
            prices = [s["price"] for s in seg]
            vol = sum(s["volume"] or 0 for s in seg)
            amt = sum(s["amount"] or 0 for s in seg)
            out.append({
                "symbol": symbol, "ts": datetime.fromtimestamp(key, CN_TZ).isoformat(timespec="seconds"),
                "open": prices[0], "close": prices[-1],
                "high": max(prices), "low": min(prices),
                "volume": vol, "amount": amt, "ticks": len(seg),
                "source": seg[-1].get("source", "poll"),
            })
        return out

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._frames)

    def clear(self, symbol: str = "") -> None:
        with self._lock:
            if symbol:
                self._frames.pop(symbol, None)
            else:
                self._frames.clear()


@dataclass
class Watchlist:
    name: str
    symbols: List[str]
    created_at: str = ""
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── 中枢门面 ────────────────────────────────────────────────────────────────

DEFAULT_CHAIN = ("tencent", "sina", "eastmoney")


class RealtimeHub:
    """统一实时入口。所有方法离线可注入 fetcher 后测试。"""

    def __init__(self, fetcher: Optional[Fetcher] = None,
                 chain: Sequence[str] = DEFAULT_CHAIN,
                 store: Optional[BarStore] = None,
                 clock: Optional[MarketClock] = None,
                 persist_dir: Optional[Path] = None):
        self.clock = clock or MarketClock()
        tencent = TencentQuoteProvider(fetcher, self.clock)
        sina = SinaQuoteProvider(fetcher, self.clock)
        em = EastmoneyQuoteProvider(fetcher, self.clock)
        self._providers: Dict[str, _BaseProvider] = {
            "tencent": tencent, "sina": sina, "eastmoney": em}
        self.kline = EastmoneyKlineProvider(fetcher, self.clock)
        self.funds = FundEstimateProvider(fetcher, self.clock)
        self.boards = EastmoneyBoardProvider(fetcher, self.clock)
        self.chain = list(chain)
        self.store = store or BarStore()
        self.persist_dir = persist_dir
        self._watchlists: Dict[str, Watchlist] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._status: Dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---------- 快照 ----------

    def quotes(self, codes: Sequence[Any], *, chain: Optional[Sequence[str]] = None,
               record: bool = True) -> FetchResult:
        """多源降级取快照。逐只补齐：某源缺一只时，用下一源补该只。"""
        wanted = [c for c in (normalize_code(x) for x in codes) if c]
        if not wanted:
            return FetchResult(ok=False, error="无法识别任何代码", data=[])
        pending = {resolve_symbol(c.raw): c for c in wanted}
        got: Dict[str, Dict[str, Any]] = {}
        attempts: List[str] = []
        for name in (chain or self.chain):
            provider = self._providers.get(name)
            if provider is None or not pending:
                continue
            provider.last_error = ""
            try:
                rows = provider.quotes(list(pending))
            except Exception as exc:  # noqa: BLE001
                attempts.append(f"{name}:ERR({type(exc).__name__})")
                continue
            hits = 0
            for row in rows:
                sym = row.get("symbol")
                # 停牌行的 price 是空的，但"今天停牌"本身就是有效答案：
                # 只有既无价格又没标停牌的行才算这个源没答上，继续降级。
                answered = row.get("price") is not None or bool(row.get("suspended"))
                if sym in pending and sym not in got and answered:
                    got[sym] = row
                    pending.pop(sym)
                    hits += 1
            if hits:
                attempts.append(f"{name}:{hits}")
            elif provider.last_error:
                attempts.append(f"{name}:ERR({provider.last_error})")
            else:
                attempts.append(f"{name}:EMPTY")
            if not pending:
                break
        ordered = [got[resolve_symbol(c.raw)] for c in wanted if resolve_symbol(c.raw) in got]
        if record:
            for q in ordered:
                self.store.add(q)
        with self._lock:
            self._status = {"last_quotes_at": self.clock.now_fn().isoformat(timespec="seconds"),
                            "requested": len(wanted), "returned": len(ordered),
                            "missing": sorted(pending), "attempts": attempts}
        return FetchResult(ok=bool(ordered), data=ordered,
                           source=ordered[0]["source"] if ordered else "",
                           attempts=attempts, missing=sorted(pending),
                           error="" if ordered else "全部源均无数据: " + ", ".join(sorted(pending)))

    def quote_one(self, code: Any, **kw: Any) -> Dict[str, Any]:
        res = self.quotes([code], **kw)
        return (res.data or [{}])[0] if res.ok else {}

    # ---------- K 线 / 板块 / 基金 ----------

    def bars(self, code: Any, period: str = "day", count: int = 120,
             adjust: str = "qfq") -> List[Dict[str, Any]]:
        return self.kline.bars(code, period=period, count=count, adjust=adjust)

    def board(self, kind: str = "industry", order: str = "desc",
              count: int = 20, sort_field: str = "f3") -> List[Dict[str, Any]]:
        return self.boards.list(kind, order=order, count=count, sort_field=sort_field)

    def fund_nav(self, code: Any) -> Dict[str, Any]:
        return self.funds.nav(code)

    def market_overview(self) -> Dict[str, Any]:
        """宽基指数 + 板块涨跌前排，一次拿齐，供"现在市场怎么样"起手。"""
        idx = ["上证指数", "深证成指", "创业板指", "沪深300", "中证500", "科创50"]
        res = self.quotes(idx)
        rows = res.data or []
        boards = self.board("industry", "desc", 8)
        losers = self.board("industry", "asc", 5)
        advance = sum(1 for r in rows if (r.get("change_pct") or 0) > 0)
        return {
            "indexes": rows,
            "indexes_advance": advance, "indexes_decline": len(rows) - advance,
            "top_industries": boards, "bottom_industries": losers,
            "clock": self.clock.describe(),
            "source_chain": res.attempts,
        }

    # ---------- 订阅与轮询 ----------

    def subscribe(self, codes: Sequence[Any], name: str = "default",
                  note: str = "") -> Watchlist:
        wl = Watchlist(name=name,
                       symbols=[resolve_symbol(c) for c in codes if normalize_code(c)],
                       created_at=datetime.now(CN_TZ).isoformat(timespec="seconds"),
                       note=note)
        self._watchlists[name] = wl
        if self.persist_dir:
            self._persist_watchlist(wl)
        return wl

    def watchlists(self) -> List[Dict[str, Any]]:
        return [w.to_dict() for w in self._watchlists.values()]

    def unsubscribe(self, name: str) -> bool:
        return self._watchlists.pop(name, None) is not None

    def _persist_watchlist(self, wl: Watchlist) -> None:
        try:
            self.persist_dir.mkdir(parents=True, exist_ok=True)
            path = self.persist_dir / f"watchlist_{re.sub(r'[^0-9A-Za-z_-]', '_', wl.name)}.json"
            path.write_text(json.dumps(wl.to_dict(), ensure_ascii=False, indent=1),
                            encoding="utf-8")
        except OSError:
            pass

    def poll(self, codes: Sequence[Any] = (), *, record: bool = True) -> Dict[str, Any]:
        """一次轮询：给订阅表或显式代码取快照，并写入滚动仓。"""
        target = list(codes) or (self._watchlists.get("default").symbols
                                 if "default" in self._watchlists else [])
        if not target:
            for wl in self._watchlists.values():
                target.extend(wl.symbols)
        res = self.quotes(target, record=record) if target else FetchResult(ok=False, data=[],
                                                                           error="无订阅代码")
        return {"at": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                "clock": self.clock.state(), "count": len(res.data or []),
                "quotes": res.data or [], "attempts": res.attempts,
                "missing": res.missing, "error": res.error}

    def stream(self, codes: Sequence[Any], seconds: float = 30,
               interval: float = 5, *, period: str = "1min") -> Dict[str, Any]:
        """有界轮询流：跑满 seconds 秒即返回，绝不死循环。

        MCP 服务端只能同步返回，所以"流"被实现成"在有限时间内采若干帧"，
        结果直接是可供指标引擎消费的分钟条。
        """
        secs = max(0.0, min(float(seconds), MAX_STREAM_SECONDS))
        gap = max(MIN_POLL_INTERVAL, min(float(interval), secs if secs else MIN_POLL_INTERVAL))
        target = list(codes)
        frames: List[Dict[str, Any]] = []
        t0 = time.time()
        while True:
            snap = self.poll(target)
            frames.append(snap)
            if len(frames) >= MAX_STREAM_POINTS or time.time() - t0 + gap > secs:
                break
            time.sleep(gap)
        symbols = [resolve_symbol(c) for c in target if normalize_code(c)]
        return {"requested_seconds": secs, "interval": gap, "frames": len(frames),
                "clock_span": [frames[0]["at"], frames[-1]["at"]] if frames else [],
                "symbols": symbols,
                "polls": frames,
                "bars": {s: self.store.bars(s) for s in symbols},
                "note": "帧数受 MAX_STREAM_SECONDS=120 / MAX_STREAM_POINTS=400 约束"}

    # ---------- 后台常驻 ----------

    def start(self, codes: Sequence[Any], interval: float = 15,
              name: str = "default") -> Dict[str, Any]:
        """后台线程轮询。与 stream 的取舍：stream 供一次性取数，start 供持续观察。"""
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "已在运行", "status": self.status()}
        self.subscribe(codes, name=name)
        self._stop.clear()
        gap = max(MIN_POLL_INTERVAL, float(interval))

        def _loop() -> None:
            while not self._stop.wait(gap):
                try:
                    self.poll()
                except Exception:  # noqa: BLE001
                    continue

        self._thread = threading.Thread(target=_loop, daemon=True,
                                        name="cnfs-realtime")
        self._thread.start()
        return {"ok": True, "interval": gap, "symbols": self._watchlists[name].symbols}

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return {"ok": True, "status": self.status()}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            snap = dict(self._status)
        snap.update({"running": bool(self._thread and self._thread.is_alive()),
                     "watchlists": self.watchlists(),
                     "stored_symbols": self.store.symbols(),
                     "clock": self.clock.describe()})
        return snap


_HUB: Optional[RealtimeHub] = None
_HUB_LOCK = threading.Lock()


def get_hub(fetcher: Optional[Fetcher] = None,
            persist: bool = True) -> RealtimeHub:
    """进程内共享 hub（共享滚动仓与订阅表）。传入 fetcher 时新建实例，便于测试。"""
    global _HUB
    if fetcher is not None:
        return RealtimeHub(fetcher=fetcher,
                           persist_dir=REALTIME_DIR if persist else None)
    with _HUB_LOCK:
        if _HUB is None:
            _HUB = RealtimeHub(persist_dir=REALTIME_DIR)
        return _HUB
