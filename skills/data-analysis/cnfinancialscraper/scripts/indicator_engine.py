# -*- coding: utf-8 -*-
"""技术指标与时序分析引擎（v11.0.0 新增）。

全部基于 Python 标准库（math / statistics），不依赖 numpy / pandas / TA-Lib，
因此核心层零 pip 依赖仍可运行。

覆盖三类能力：
1. 经典技术指标（均线 / 振荡 / 趋势 / 波动 / 量价）
2. 统计检验（相关、滞后互相关、稳健回归、分位、异常检测）
3. 信号合成（多指标共振评分 + 可核验的分项理由）

约定：所有指标函数返回与输入等长的 list，前置不足处用 None 占位，
调用方可以直接与原始 K 线对齐绘图或做逐点判断。
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Bar", "bars_from_rows", "closes_of", "highs_of", "lows_of", "volumes_of",
    "IndicatorSpec", "INDICATOR_REGISTRY", "compute", "compute_last",
    "available_indicators", "register_indicator",
    "sma", "ema", "wma", "hma",
    "macd", "rsi", "stochastic", "kdj", "bollinger", "atr", "adx",
    "obv", "vwap", "mfi", "cci", "williams_r", "roc", "donchian", "volume_ratio",
    "returns", "annualized_volatility", "ewma_volatility",
    "sharpe_ratio", "sortino_ratio", "calmar_ratio", "max_drawdown",
    "drawdown_series", "percentile_rank", "zscores", "mad_zscores",
    "pearson", "spearman", "lag_cross_correlation", "ols", "theil_sen",
    "rolling_correlation", "rolling_beta",
    "detect_anomalies", "AnomalyFlag",
    "SignalHit", "detect_signals", "composite_score", "analyze_bars",
    "summarize_series",
]


# ─── 数据整形 ────────────────────────────────────────────────────────────────

@dataclass
class Bar:
    """单根 K 线 / 单个时点快照。volume 与 amount 可选。"""

    ts: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0.0
    amount: float = 0.0
    symbol: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bar":
        g = _getter(d)
        vol = g("volume", "vol", "v", "成交量")
        amt = g("amount", "amt", "成交额")
        return cls(
            ts=str(g("ts", "time", "date", "datetime", "日期", default="") or ""),
            open=_f(g("open", "开盘", "今开", default=0.0)),
            high=_f(g("high", "最高", default=None)),
            low=_f(g("low", "最低", default=None)),
            close=_f(g("close", "now", "price", "nav", "收盘", "最新", "单位净值", default=None)),
            volume=_f(vol, default=0.0),
            amount=_f(amt, default=0.0),
            symbol=str(g("symbol", "code", "ts_code", "代码", default="") or ""),
        )

    def __post_init__(self) -> None:
        # 单点快照（只有 close）时补齐 OHLC，保证 TR / Stochastic 不炸
        if self.high in (0.0, None) or self.low in (0.0, None):
            self.high = self.close if not self.high else self.high
            self.low = self.close if not self.low else self.low
        if not self.open:
            self.open = self.close


def _f(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _getter(d: Dict[str, Any]) -> Callable[..., Any]:
    def g(*keys: str, default: Any = None) -> Any:
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return default
    return g


def bars_from_rows(rows: Iterable[Any]) -> List[Bar]:
    """把 dict 列表 / Bar 列表 / 纯数字列表统一成 Bar 列表。"""
    out: List[Bar] = []
    for row in rows or []:
        if isinstance(row, Bar):
            out.append(row)
        elif isinstance(row, dict):
            out.append(Bar.from_dict(row))
        elif isinstance(row, (list, tuple)) and len(row) >= 4:
            out.append(Bar(ts=str(row[0]), open=float(row[1]), high=float(row[2]),
                           low=float(row[3]), close=float(row[4]) if len(row) > 4 else float(row[3]),
                           volume=float(row[5]) if len(row) > 5 else 0.0))
        elif isinstance(row, (int, float)):
            out.append(Bar(close=float(row)))
    return out


def _nums(series: Sequence[Any]) -> List[float]:
    """过滤掉 None / NaN，返回纯 float 序列（保持顺序）。"""
    out: List[float] = []
    for v in series or []:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(f) or math.isinf(f):
            continue
        out.append(f)
    return out


def closes_of(series: Iterable[Any]) -> List[float]:
    return _nums([_attr(r, "close") for r in _iter(series)])


def highs_of(series: Iterable[Any]) -> List[float]:
    return _nums([_attr(r, "high") for r in _iter(series)])


def lows_of(series: Iterable[Any]) -> List[float]:
    return _nums([_attr(r, "low") for r in _iter(series)])


def volumes_of(series: Iterable[Any]) -> List[float]:
    return _nums([_attr(r, "volume") for r in _iter(series)])


def _iter(series: Iterable[Any]) -> List[Any]:
    if series is None:
        return []
    if isinstance(series, dict):
        # 支持 {"bars": [...]} 形态
        for key in ("bars", "data", "rows", "items"):
            if isinstance(series.get(key), list):
                return series[key]
        return list(series.values())
    return list(series)


def _attr(row: Any, name: str) -> Any:
    if isinstance(row, Bar):
        return getattr(row, name)
    if isinstance(row, dict):
        aliases = {
            "close": ("close", "now", "price", "nav", "收盘", "最新", "单位净值"),
            "high": ("high", "最高"),
            "low": ("low", "最低"),
            "volume": ("volume", "vol", "v", "成交量"),
        }.get(name, (name,))
        g = _getter(row)
        return g(*aliases)
    return getattr(row, name, None)


# ─── 均线族 ──────────────────────────────────────────────────────────────────

def sma(values: Sequence[float], n: int = 20) -> List[Optional[float]]:
    """简单移动平均。"""
    vals = _nums(values)
    if n <= 0:
        raise ValueError("window must be positive")
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) < n:
        return out
    window_sum = sum(vals[:n])
    out[n - 1] = window_sum / n
    for i in range(n, len(vals)):
        window_sum += vals[i] - vals[i - n]
        out[i] = window_sum / n
    return out


def ema(values: Sequence[float], n: int = 20) -> List[Optional[float]]:
    """指数移动平均，以前 n 项 SMA 播种（与主流行情软件一致）。"""
    vals = _nums(values)
    if n <= 0:
        raise ValueError("window must be positive")
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) < n:
        return out
    seed = sum(vals[:n]) / n
    out[n - 1] = seed
    k = 2.0 / (n + 1.0)
    prev = seed
    for i in range(n, len(vals)):
        prev = vals[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def wma(values: Sequence[float], n: int = 20) -> List[Optional[float]]:
    """线性加权移动平均，越靠近当前权重越大。"""
    vals = _nums(values)
    if n <= 0:
        raise ValueError("window must be positive")
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) < n:
        return out
    denom = n * (n + 1) / 2.0
    for i in range(n - 1, len(vals)):
        seg = vals[i - n + 1:i + 1]
        out[i] = sum(v * (j + 1) for j, v in enumerate(seg)) / denom
    return out


def hma(values: Sequence[float], n: int = 20) -> List[Optional[float]]:
    """Hull 移动平均：WMA(sqrt(n), 2*WMA(n/2) - WMA(n))，显著降低滞后。"""
    vals = _nums(values)
    out: List[Optional[float]] = [None] * len(vals)
    if n <= 1 or len(vals) < n:
        return out
    half = max(1, n // 2)
    root = max(1, int(round(math.sqrt(n))))
    w_half = wma(vals, half)
    w_full = wma(vals, n)
    raw: List[Optional[float]] = [None] * len(vals)
    for i in range(len(vals)):
        if w_half[i] is None or w_full[i] is None:
            continue
        raw[i] = 2.0 * w_half[i] - w_full[i]
    dense = [v for v in raw if v is not None]
    smoothed = wma(dense, root)
    idx = 0
    for i, v in enumerate(raw):
        if v is None:
            continue
        out[i] = smoothed[idx] if idx < len(smoothed) else None
        idx += 1
    return out


# ─── 趋势类 ──────────────────────────────────────────────────────────────────

def macd(values: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9, hist_mult: float = 2.0) -> Dict[str, List[Optional[float]]]:
    """MACD。hist_mult=2.0 为国内软件惯例（柱状图放大一倍）；国际惯例传 1.0。

    返回 {"dif", "dea", "hist"}，三条序列均与输入等长。
    """
    vals = _nums(values)
    e_fast = ema(vals, fast)
    e_slow = ema(vals, slow)
    dif: List[Optional[float]] = [
        None if (a is None or b is None) else a - b for a, b in zip(e_fast, e_slow)
    ]
    dense = [v for v in dif if v is not None]
    dea_dense = ema(dense, signal)
    dea: List[Optional[float]] = [None] * len(vals)
    j = 0
    for i, v in enumerate(dif):
        if v is None:
            continue
        dea[i] = dea_dense[j] if j < len(dea_dense) else None
        j += 1
    hist = [None if (d is None or s is None) else (d - s) * hist_mult
            for d, s in zip(dif, dea)]
    return {"dif": dif, "dea": dea, "hist": hist}


def bollinger(values: Sequence[float], n: int = 20, k: float = 2.0) -> Dict[str, List[Optional[float]]]:
    """布林带。返回 mid / upper / lower / pct_b / bandwidth。"""
    vals = _nums(values)
    mid = sma(vals, n)
    out: Dict[str, List[Optional[float]]] = {
        "mid": mid,
        "upper": [None] * len(vals),
        "lower": [None] * len(vals),
        "pct_b": [None] * len(vals),
        "bandwidth": [None] * len(vals),
    }
    for i in range(n - 1, len(vals)):
        if mid[i] is None:
            continue
        seg = vals[i - n + 1:i + 1]
        sd = statistics.pstdev(seg) if len(seg) > 1 else 0.0
        up = mid[i] + k * sd
        lo = mid[i] - k * sd
        out["upper"][i] = up
        out["lower"][i] = lo
        span = up - lo
        if span > 0:
            out["pct_b"][i] = (vals[i] - lo) / span
        out["bandwidth"][i] = span / mid[i] if mid[i] else None
    return out


def donchian(highs: Sequence[float], lows: Sequence[float], n: int = 20) -> Dict[str, List[Optional[float]]]:
    """唐奇安通道（突破判定的基准）。"""
    h = _nums(highs)
    l = _nums(lows)
    size = max(len(h), len(l))
    out: Dict[str, List[Optional[float]]] = {
        "upper": [None] * size, "lower": [None] * size, "mid": [None] * size}
    if n <= 0:
        raise ValueError("window must be positive")
    for i in range(size):
        seg_h = h[max(0, i - n + 1):i + 1]
        seg_l = l[max(0, i - n + 1):i + 1]
        if len(seg_h) < n or len(seg_l) < n:
            continue
        out["upper"][i] = max(seg_h)
        out["lower"][i] = min(seg_l)
        out["mid"][i] = (out["upper"][i] + out["lower"][i]) / 2.0
    return out


def adx(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 14) -> Dict[str, List[Optional[float]]]:
    """Wilder ADX / +DI / -DI。衡量趋势强度而非方向。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    h, l, c = h[:size], l[:size], c[:size]
    out: Dict[str, List[Optional[float]]] = {
        "plus_di": [None] * size, "minus_di": [None] * size,
        "dx": [None] * size, "adx": [None] * size}
    if size <= n:
        return out

    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr: List[float] = []
    for i in range(1, size):
        up = h[i] - h[i - 1]
        down = l[i - 1] - l[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))

    def _wilder(seq: List[float]) -> List[float]:
        acc = [sum(seq[:n])]
        for v in seq[n:]:
            acc.append(acc[-1] - acc[-1] / n + v)
        return acc

    sm_tr = _wilder(tr)
    sm_plus = _wilder(plus_dm)
    sm_minus = _wilder(minus_dm)
    dxs: List[float] = []
    for idx in range(len(sm_tr)):
        base = sm_tr[idx]
        pdi = 100.0 * sm_plus[idx] / base if base else 0.0
        mdi = 100.0 * sm_minus[idx] / base if base else 0.0
        total = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / total if total else 0.0
        # Wilder 平滑把 n 个原始值压成 1 个，序列因此整体后移 n 根：
        # sm_tr[idx] 对应的是第 idx+n 根 K 线，不是第 idx+1 根。写错位置会让
        # DI/DX 提前 n-1 根出现，而最后 n-1 根（恰恰是最新、最该看的）全是空。
        pos = idx + n
        if pos >= size:
            break
        out["plus_di"][pos] = pdi
        out["minus_di"][pos] = mdi
        out["dx"][pos] = dx
        dxs.append(dx)
    if len(dxs) >= n:
        adx_acc = [sum(dxs[:n]) / n]
        out["adx"][n + n - 1] = adx_acc[-1]
        for k in range(n, len(dxs)):
            adx_acc.append((adx_acc[-1] * (n - 1) + dxs[k]) / n)
            pos = k + n
            if pos < size:
                out["adx"][pos] = adx_acc[-1]
    return out


# ─── 振荡类 ──────────────────────────────────────────────────────────────────

def rsi(values: Sequence[float], n: int = 14) -> List[Optional[float]]:
    """Wilder RSI。"""
    vals = _nums(values)
    out: List[Optional[float]] = [None] * len(vals)
    if len(vals) <= n:
        return out
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(vals)):
        delta = vals[i] - vals[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    out[n] = _rsi_from(avg_gain, avg_loss)
    for i in range(n, len(gains)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out[i + 1] = _rsi_from(avg_gain, avg_loss)
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def stochastic(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14) -> Dict[str, List[Optional[float]]]:
    """随机指标 %K / %D。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    h, l, c = h[:size], l[:size], c[:size]
    k: List[Optional[float]] = [None] * size
    d: List[Optional[float]] = [None] * size
    for i in range(n - 1, size):
        seg_h = max(h[i - n + 1:i + 1])
        seg_l = min(l[i - n + 1:i + 1])
        span = seg_h - seg_l
        k[i] = 50.0 if span == 0 else (c[i] - seg_l) / span * 100.0
    dense = [v for v in k if v is not None]
    d_dense = sma(dense, 3)
    j = 0
    for i, v in enumerate(k):
        if v is None:
            continue
        d[i] = d_dense[j] if j < len(d_dense) else None
        j += 1
    return {"k": k, "d": d}


def kdj(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 9, k_smooth: int = 3, d_smooth: int = 3) -> Dict[str, List[Optional[float]]]:
    """KDJ（国内软件口径：K、D 用 1/3 递归平滑，J = 3K - 2D）。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    h, l, c = h[:size], l[:size], c[:size]
    out: Dict[str, List[Optional[float]]] = {
        "k": [None] * size, "d": [None] * size, "j": [None] * size}
    if size < n:
        return out
    prev_k, prev_d = 50.0, 50.0
    for i in range(n - 1, size):
        seg_h = max(h[i - n + 1:i + 1])
        seg_l = min(l[i - n + 1:i + 1])
        span = seg_h - seg_l
        rsv = 50.0 if span == 0 else (c[i] - seg_l) / span * 100.0
        prev_k = ((k_smooth - 1) * prev_k + rsv) / k_smooth
        prev_d = ((d_smooth - 1) * prev_d + prev_k) / d_smooth
        out["k"][i] = prev_k
        out["d"][i] = prev_d
        out["j"][i] = 3.0 * prev_k - 2.0 * prev_d
    return out


def williams_r(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
               n: int = 14) -> List[Optional[float]]:
    """威廉指标，取值 -100 ~ 0（超卖 < -80，超买 > -20）。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    out: List[Optional[float]] = [None] * size
    for i in range(n - 1, size):
        seg_h = max(h[i - n + 1:i + 1])
        seg_l = min(l[i - n + 1:i + 1])
        span = seg_h - seg_l
        out[i] = -50.0 if span == 0 else (seg_h - c[i]) / span * -100.0
    return out


def roc(values: Sequence[float], n: int = 12) -> List[Optional[float]]:
    """变动率（百分比动量）。"""
    vals = _nums(values)
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(n, len(vals)):
        base = vals[i - n]
        out[i] = 0.0 if base == 0 else (vals[i] / base - 1.0) * 100.0
    return out


def cci(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 20) -> List[Optional[float]]:
    """顺势指标（典型价偏离其均值的标准化值）。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(size)]
    out: List[Optional[float]] = [None] * size
    for i in range(n - 1, size):
        seg = tp[i - n + 1:i + 1]
        m = sum(seg) / n
        md = sum(abs(v - m) for v in seg) / n
        out[i] = 0.0 if md == 0 else (tp[i] - m) / (0.015 * md)
    return out


def mfi(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        volumes: Sequence[float], n: int = 14) -> List[Optional[float]]:
    """资金流量指标（带成交量的 RSI）。"""
    h, l, c, v = _nums(highs), _nums(lows), _nums(closes), _nums(volumes)
    size = min(len(h), len(l), len(c), len(v))
    out: List[Optional[float]] = [None] * size
    if size < n + 1:
        return out
    tp = [(h[i] + l[i] + c[i]) / 3.0 for i in range(size)]
    raw = [tp[i] * v[i] for i in range(size)]
    for i in range(n, size):
        pos = sum(raw[j] for j in range(i - n + 1, i + 1) if tp[j] > tp[j - 1])
        neg = sum(raw[j] for j in range(i - n + 1, i + 1) if tp[j] < tp[j - 1])
        if neg == 0:
            out[i] = 100.0 if pos > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1.0 + pos / neg)
    return out


# ─── 波动与量价 ──────────────────────────────────────────────────────────────

def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
        n: int = 14, method: str = "wilder") -> List[Optional[float]]:
    """平均真实波幅。method: wilder | sma。"""
    h, l, c = _nums(highs), _nums(lows), _nums(closes)
    size = min(len(h), len(l), len(c))
    out: List[Optional[float]] = [None] * size
    if size <= n:
        return out
    trs: List[float] = []
    for i in range(1, size):
        trs.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    if method == "sma":
        dense = sma(trs, n)
        for i, v in enumerate(dense):
            if v is not None:
                out[i + 1] = v
        return out
    acc = sum(trs[:n]) / n
    out[n] = acc
    for i in range(n, len(trs)):
        acc = (acc * (n - 1) + trs[i]) / n
        out[i + 1] = acc
    return out


def obv(closes: Sequence[float], volumes: Sequence[float]) -> List[Optional[float]]:
    """能量潮（累积量）。"""
    c, v = _nums(closes), _nums(volumes)
    size = min(len(c), len(v))
    if size == 0:
        return []
    out: List[Optional[float]] = [0.0] * size
    for i in range(1, size):
        if c[i] > c[i - 1]:
            out[i] = out[i - 1] + v[i]  # type: ignore[assignment]
        elif c[i] < c[i - 1]:
            out[i] = out[i - 1] - v[i]  # type: ignore[assignment]
        else:
            out[i] = out[i - 1]
    return out


def vwap(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float],
         volumes: Sequence[float]) -> List[Optional[float]]:
    """累计成交加权均价（从序列起点累计，日内口径）。"""
    h, l, c, v = _nums(highs), _nums(lows), _nums(closes), _nums(volumes)
    size = min(len(h), len(l), len(c), len(v))
    out: List[Optional[float]] = [None] * size
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(size):
        typical = (h[i] + l[i] + c[i]) / 3.0
        cum_pv += typical * v[i]
        cum_v += v[i]
        out[i] = cum_pv / cum_v if cum_v else c[i]
    return out


def volume_ratio(values: Sequence[float], n: int = 5) -> List[Optional[float]]:
    """量比：当前量 / 前 n 日均量（不含当日）。"""
    vals = _nums(values)
    out: List[Optional[float]] = [None] * len(vals)
    for i in range(n, len(vals)):
        base = vals[i - n:i]
        avg = sum(base) / n if base else 0.0
        out[i] = 0.0 if avg == 0 else vals[i] / avg
    return out


# ─── 统计与收益 ──────────────────────────────────────────────────────────────

def returns(values: Sequence[float], kind: str = "simple") -> List[float]:
    """收益率序列。kind: simple | log。"""
    vals = _nums(values)
    out: List[float] = []
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if prev == 0:
            continue
        if kind == "log":
            if vals[i] <= 0:
                continue
            out.append(math.log(vals[i] / prev))
        else:
            out.append(vals[i] / prev - 1.0)
    return out


def annualized_volatility(values: Sequence[float], periods: int = 252,
                          kind: str = "simple") -> Optional[float]:
    """年化波动率（百分比）。样本不足 3 点返回 None。"""
    r = returns(values, kind)
    if len(r) < 3:
        return None
    return statistics.pstdev(r) * math.sqrt(periods) * 100.0


def ewma_volatility(values: Sequence[float], lam: float = 0.94,
                    periods: int = 252, kind: str = "simple") -> List[float]:
    """RiskMetrics EWMA 波动率序列（日频 σ 估计，百分比）。"""
    r = returns(values, kind)
    if not r:
        return []
    out = [abs(r[0])]
    for x in r[1:]:
        out.append(math.sqrt(lam * out[-1] ** 2 + (1.0 - lam) * x ** 2))
    scale = math.sqrt(periods) * 100.0
    return [v * scale for v in out]


def sharpe_ratio(values: Sequence[float], risk_free_annual: float = 0.0,
                 periods: int = 252, kind: str = "simple") -> Optional[float]:
    r = returns(values, kind)
    if len(r) < 3:
        return None
    rf = risk_free_annual / periods
    excess = [x - rf for x in r]
    sd = statistics.pstdev(excess)
    if sd == 0:
        return None
    return (statistics.mean(excess) / sd) * math.sqrt(periods)


def sortino_ratio(values: Sequence[float], risk_free_annual: float = 0.0,
                  periods: int = 252, kind: str = "simple") -> Optional[float]:
    r = returns(values, kind)
    if len(r) < 3:
        return None
    rf = risk_free_annual / periods
    excess = [x - rf for x in r]
    downside = [x for x in excess if x < 0]
    if not downside:
        return None
    dd = math.sqrt(sum(x * x for x in downside) / len(excess))
    if dd == 0:
        return None
    return (statistics.mean(excess) / dd) * math.sqrt(periods)


def max_drawdown(values: Sequence[float]) -> Dict[str, Any]:
    """最大回撤及发生位置。max_drawdown_pct 为**正数**百分比（与 analyzer.py 一致）。"""
    vals = _nums(values)
    if len(vals) < 2:
        return {"max_drawdown_pct": 0.0, "peak_index": -1, "trough_index": -1,
                "duration_bars": 0}
    peak = vals[0]
    peak_idx = 0
    best_dd = 0.0
    best_peak_idx = best_trough_idx = 0
    best_dur = 0
    cur_dur = 0
    for i, v in enumerate(vals):
        if v > peak:
            peak = v
            peak_idx = i
            cur_dur = 0
        else:
            cur_dur += 1
            dd = (1.0 - v / peak) if peak else 0.0
            if dd > best_dd:
                best_dd = dd
                best_peak_idx = peak_idx
                best_trough_idx = i
                best_dur = cur_dur
    return {"max_drawdown_pct": round(best_dd * 100.0, 4),
            "peak_index": best_peak_idx, "trough_index": best_trough_idx,
            "duration_bars": best_dur}


def calmar_ratio(values: Sequence[float], periods: int = 252) -> Optional[float]:
    vals = _nums(values)
    if len(vals) < 3 or vals[0] == 0:
        return None
    mdd = abs(max_drawdown(vals)["max_drawdown_pct"]) / 100.0
    if mdd == 0:
        return None
    total = vals[-1] / vals[0] - 1.0
    years = len(vals) / periods
    if years <= 0:
        return None
    ann = (1.0 + total) ** (1.0 / years) - 1.0 if total > -1 else -1.0
    return ann / mdd


def drawdown_series(values: Sequence[float]) -> List[float]:
    """逐点回撤（相对历史新高，正数百分比，>=0）。"""
    vals = _nums(values)
    out: List[float] = []
    peak = None
    for v in vals:
        peak = v if peak is None or v > peak else peak
        out.append((1.0 - v / peak) * 100.0 if peak else 0.0)
    return out


def percentile_rank(values: Sequence[float], x: float) -> float:
    """x 在序列中的分位（<= x 的占比，0-100）。"""
    vals = _nums(values)
    if not vals:
        return 0.0
    below = sum(1 for v in vals if v <= x)
    return below / len(vals) * 100.0


def zscores(values: Sequence[float]) -> List[float]:
    """总体 z-score。标准差为 0 时全部返回 0。"""
    vals = _nums(values)
    if len(vals) < 2:
        return [0.0] * len(vals)
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    if sd == 0:
        return [0.0] * len(vals)
    return [(v - m) / sd for v in vals]


def mad(values: Sequence[float]) -> float:
    """中位数绝对偏差。"""
    vals = _nums(values)
    if not vals:
        return 0.0
    m = statistics.median(vals)
    return statistics.median([abs(v - m) for v in vals])


def mad_zscores(values: Sequence[float]) -> List[float]:
    """稳健 z-score（基于 MAD，0.6745 为 MAD→σ 换算系数）。"""
    vals = _nums(values)
    m = statistics.median(vals) if vals else 0.0
    scale = mad(vals) * 1.4826
    if scale == 0:
        return [0.0] * len(vals)
    return [(v - m) / scale for v in vals]


def pearson(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """皮尔逊相关系数（按位置对齐，剔除任一方的 None/NaN）。"""
    xs, ys = _align(a, b)
    if len(xs) < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def _ranks(xs: Sequence[float]) -> List[float]:
    """升序平均秩（并列取平均，Spearman 必需）。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    """斯皮尔曼秩相关（对异常值和单调非线性更稳健）。"""
    xs, ys = _align(a, b)
    if len(xs) < 3:
        return None
    return pearson(_ranks(xs), _ranks(ys))


def _align(a: Sequence[float], b: Sequence[float]) -> Tuple[List[float], List[float]]:
    xs = list(a)
    ys = list(b)
    size = min(len(xs), len(ys))
    px: List[float] = []
    py: List[float] = []
    for i in range(size):
        x, y = xs[i], ys[i]
        if x is None or y is None:
            continue
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            continue
        if math.isnan(fx) or math.isnan(fy):
            continue
        px.append(fx)
        py.append(fy)
    return px, py


def lag_cross_correlation(a: Sequence[float], b: Sequence[float],
                          max_lag: int = 5) -> Dict[str, Any]:
    """滞后互相关：a 领先 b 多少期相关最强。lag>0 表示 a 领先 b。

    完全线性序列会让所有 lag 都得到 1.0，此时按 |lag| 最小（即同期）胜出，
    避免把"无滞后信息"误报成"存在领先关系"。
    """
    xs, ys = _align(a, b)
    table: Dict[int, Optional[float]] = {}
    for lag in range(-max_lag, max_lag + 1):
        if lag >= 0:
            c = pearson(xs[:len(xs) - lag] if lag else xs, ys[lag:])
        else:
            c = pearson(xs[-lag:], ys[:len(ys) + lag])
        table[lag] = None if c is None else round(c, 4)
    scored = [(abs(v), -abs(k), k, v) for k, v in table.items() if v is not None]
    best = {"lag": 0, "corr": None}
    if scored:
        _, _, k, v = max(scored)
        best = {"lag": k, "corr": v}
    return {"best_lag": best["lag"], "best_corr": best["corr"],
            "contemporaneous": table.get(0), "by_lag": table}


def ols(x: Sequence[float], y: Sequence[float]) -> Dict[str, Any]:
    """最小二乘回归：斜率、截距、R²、标准误、t 值。"""
    xs, ys = _align(x, y)
    n = len(xs)
    if n < 3:
        return {"ok": False, "reason": "样本不足", "n": n}
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((v - mx) ** 2 for v in xs)
    if sxx == 0:
        return {"ok": False, "reason": "自变量无变异", "n": n}
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    sse = sum(r * r for r in resid)
    sst = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - sse / sst if sst else 0.0
    dof = n - 2
    se_slope = math.sqrt(sse / dof / sxx) if dof > 0 and sxx else 0.0
    return {"ok": True, "n": n, "slope": slope, "intercept": intercept,
            "r_squared": round(r2, 6),
            "std_err_slope": round(se_slope, 8) if se_slope else 0.0,
            "t_stat": round(slope / se_slope, 4) if se_slope else None,
            "residual_std": round(math.sqrt(sse / dof), 8) if dof > 0 else None}


def theil_sen(x: Sequence[float], y: Sequence[float], max_pairs: int = 4000) -> Dict[str, Any]:
    """Theil–Sen 斜率：点对斜率取中位数，抗离群点。"""
    xs, ys = _align(x, y)
    n = len(xs)
    if n < 3:
        return {"ok": False, "reason": "样本不足", "n": n}
    slopes: List[float] = []
    limit = n * (n - 1) // 2
    stride = 1 if limit <= max_pairs else int(math.ceil(math.sqrt(limit / max_pairs)))
    for i in range(0, n - 1, stride):
        for j in range(i + 1, n, stride):
            dx = xs[j] - xs[i]
            if dx == 0:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return {"ok": False, "reason": "自变量无变异", "n": n}
    med = statistics.median(slopes)
    mx, my = statistics.mean(xs), statistics.mean(ys)
    return {"ok": True, "n": n, "slope": med, "intercept": my - med * mx,
            "pairs_used": len(slopes)}


def rolling_correlation(a: Sequence[float], b: Sequence[float],
                        n: int = 20) -> List[Optional[float]]:
    """滚动相关系数：与 rolling_beta 同一口径，吃价格序列、对**收益**求相关。

    对价格水平直接求相关是没用的：两条上涨序列不管基本面联不联动都会给 0.99+，
    那是共同趋势的产物。输出第 i 项对应第 i+1 根 K 线为止的 n 窗口（收益少一根）。
    """
    ra, rb = returns(a), returns(b)
    out: List[Optional[float]] = []
    size = min(len(ra), len(rb))
    for i in range(size):
        if i + 1 < n:
            out.append(None)
            continue
        out.append(pearson(ra[i - n + 1:i + 1], rb[i - n + 1:i + 1]))
    return out


def rolling_beta(asset: Sequence[float], benchmark: Sequence[float],
                 n: int = 60) -> List[Optional[float]]:
    """滚动 beta：资产收益对基准收益回归的斜率。"""
    ra = returns(asset)
    rb = returns(benchmark)
    out: List[Optional[float]] = []
    size = min(len(ra), len(rb))
    for i in range(size):
        if i + 1 < n:
            out.append(None)
            continue
        res = ols(rb[i - n + 1:i + 1], ra[i - n + 1:i + 1])
        out.append(res["slope"] if res.get("ok") else None)
    return out


# ─── 异常检测 ────────────────────────────────────────────────────────────────

@dataclass
class AnomalyFlag:
    index: int
    value: float
    method: str
    score: float
    direction: str
    expected: float
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "value": round(self.value, 6),
                "method": self.method, "score": round(self.score, 4),
                "direction": self.direction, "expected": round(self.expected, 6),
                "detail": self.detail}


def _degenerate_score(value: float, base: float) -> float:
    """离散度为 0 时用的分数：偏离平基线的幅度（%，基线为 0 则用绝对幅度）。"""
    if base:
        return abs(value - base) / abs(base) * 100.0
    return abs(value - base)


def detect_anomalies(values: Sequence[float], method: str = "robust",
                     threshold: float = 3.0, tail: int = 0) -> List[AnomalyFlag]:
    """时序异常检测。

    method:
      zscore — 总体 z-score（对离群点敏感）
      iqr    — Tukey 箱线围栏
      ewma   — RiskMetrics 波动率锥（对波动聚集更合适）
      robust — MAD 修正 z（默认，离群点不污染基线）
      vote   — 四种方法投票，>=2 票才报
    tail: 只检查最近 N 点（0 = 全部）

    平基线特例：MAD / IQR / EWMA 的尺度都靠历史离散度算，历史全平（停牌、
    固定净值、刚建仓）时尺度恰为 0。此时任何偏离都是极端异常，不能因为
    "除以 0"就静默放行——只有 zscore 会报是错的。
    """
    vals = _nums(values)
    n = len(vals)
    if n < 5:
        return []
    start = max(0, n - tail) if tail > 0 else 0
    flags: List[AnomalyFlag] = []
    for i in range(start, n):
        window = vals[:i + 1]
        if len(window) < 5:
            continue
        candidates: List[AnomalyFlag] = []
        if method in ("zscore", "vote"):
            z = zscores(window)
            if abs(z[-1]) >= threshold:
                candidates.append(AnomalyFlag(i, vals[i], "zscore", abs(z[-1]),
                                              "up" if z[-1] > 0 else "down",
                                              statistics.mean(window)))
        if method in ("iqr", "vote"):
            q1, q3 = _quartiles(window)
            span = q3 - q1
            median = statistics.median(window)
            if span > 0:
                lo, hi = q1 - 1.5 * span, q3 + 1.5 * span
                if vals[i] < lo or vals[i] > hi:
                    score = abs(vals[i] - (hi if vals[i] > hi else lo)) / span
                    candidates.append(AnomalyFlag(i, vals[i], "iqr", score,
                                                  "up" if vals[i] > hi else "down",
                                                  median))
            elif vals[i] != q1:
                candidates.append(AnomalyFlag(i, vals[i], "iqr",
                                              _degenerate_score(vals[i], q1),
                                              "up" if vals[i] > q1 else "down", q1))
        if method in ("ewma", "vote"):
            vol = ewma_volatility(window)
            if len(vol) >= 3:
                sigma = vol[-2] / 100.0 if len(vol) >= 2 else 0.0
                prev = window[-2]
                if prev:
                    move = vals[i] / prev - 1.0
                    if sigma > 0:
                        if abs(move) >= threshold * sigma:
                            candidates.append(AnomalyFlag(i, vals[i], "ewma",
                                                          abs(move) / sigma,
                                                          "up" if move > 0 else "down", prev))
                    elif move:
                        candidates.append(AnomalyFlag(i, vals[i], "ewma",
                                                      _degenerate_score(vals[i], prev),
                                                      "up" if move > 0 else "down", prev))
        if method in ("robust", "vote"):
            median = statistics.median(window)
            scale = mad(window) * 1.4826
            if scale > 0:
                z = (vals[i] - median) / scale
                if abs(z) >= threshold:
                    candidates.append(AnomalyFlag(i, vals[i], "robust", abs(z),
                                                  "up" if z > 0 else "down", median))
            elif vals[i] != median:
                candidates.append(AnomalyFlag(i, vals[i], "robust",
                                              _degenerate_score(vals[i], median),
                                              "up" if vals[i] > median else "down", median))
        if method == "vote":
            if len(candidates) >= 2:
                best = max(candidates, key=lambda f: f.score)
                best.method = "vote:" + "+".join(f.method for f in candidates)
                best.score = statistics.mean(f.score for f in candidates)
                flags.append(best)
        else:
            flags.extend(candidates)
    return flags


def _quartiles(vals: Sequence[float]) -> Tuple[float, float]:
    """线性插值四分位数（不依赖 numpy）。"""
    s = sorted(vals)
    if len(s) == 1:
        return s[0], s[0]

    def at(p: float) -> float:
        pos = (len(s) - 1) * p
        lo = int(math.floor(pos))
        hi = min(lo + 1, len(s) - 1)
        return s[lo] + (s[hi] - s[lo]) * (pos - lo)

    return at(0.25), at(0.75)


# ─── 指标注册表 ──────────────────────────────────────────────────────────────

class InsufficientData(ValueError):
    """样本量低于指标声明的 min_len。

    catalog 里每个指标都带 min_len，Resource 正文还写着"min_len 不足的序列不要硬算"——
    但如果 compute 不拦，调用方拿到的照样是一个看着像真的的数字（39 根算出的 ADX、
    9 个收益算出的夏普）。宁可当场报错，也不要造出无法回溯的"事实"。
    """


@dataclass
class IndicatorSpec:
    name: str
    fn: Callable[..., Any]
    needs: Tuple[str, ...] = ("close",)
    params: Dict[str, Any] = field(default_factory=dict)
    min_len: int = 30
    cn_name: str = ""
    note: str = ""

    @property
    def is_multi(self) -> bool:
        return isinstance(self.params, dict) and bool(self.params)


def _apply_spec(spec: IndicatorSpec, bars: List[Bar]) -> Any:
    feeds: Dict[str, List[float]] = {
        "close": [b.close for b in bars],
        "high": [b.high for b in bars],
        "low": [b.low for b in bars],
        "volume": [b.volume for b in bars],
    }
    args = [feeds[k] for k in spec.needs]
    kwargs = {k: v for k, v in spec.params.items() if isinstance(v, (int, float))}
    if spec.name in ("sharpe_ratio", "sortino_ratio") and feeds["volume"]:
        kwargs.pop("volume", None)
    return spec.fn(*args, **kwargs)


INDICATOR_REGISTRY: Dict[str, IndicatorSpec] = {}


def register_indicator(spec: IndicatorSpec) -> None:
    INDICATOR_REGISTRY[spec.name] = spec


def _seed_registry() -> None:
    S = IndicatorSpec
    register_indicator(S("sma", sma, params={"n": 20}, note="简单移动平均"))
    register_indicator(S("ema", ema, params={"n": 20}, note="指数移动平均"))
    register_indicator(S("wma", wma, params={"n": 20}, note="线性加权移动平均"))
    register_indicator(S("hma", hma, params={"n": 20}, note="Hull 均线（低滞后）"))
    register_indicator(S("macd", macd, params={"fast": 12, "slow": 26, "signal": 9},
                         min_len=35, cn_name="MACD", note="DIF/DEA/HIST 三线"))
    register_indicator(S("rsi", rsi, params={"n": 14}, cn_name="RSI", note="相对强弱"))
    register_indicator(S("kdj", kdj, ("high", "low", "close"),
                         params={"n": 9}, cn_name="KDJ", note="国内口径 1/3 平滑"))
    register_indicator(S("stochastic", stochastic, ("high", "low", "close"),
                         params={"n": 14}, cn_name="KDJ", note="%K/%D 国际口径"))
    register_indicator(S("bollinger", bollinger, params={"n": 20, "k": 2.0},
                         cn_name="BOLL", note="中轨/上下轨/%B/带宽"))
    register_indicator(S("atr", atr, ("high", "low", "close"), params={"n": 14},
                         cn_name="ATR", note="平均真实波幅"))
    register_indicator(S("adx", adx, ("high", "low", "close"), params={"n": 14},
                         min_len=40, cn_name="ADX", note="趋势强度"))
    register_indicator(S("cci", cci, ("high", "low", "close"), params={"n": 20},
                         cn_name="CCI", note="顺势指标"))
    register_indicator(S("williams_r", williams_r, ("high", "low", "close"),
                         params={"n": 14}, cn_name="W%R", note="威廉指标"))
    register_indicator(S("roc", roc, params={"n": 12}, cn_name="ROC", note="变动率"))
    register_indicator(S("mfi", mfi, ("high", "low", "close", "volume"),
                         params={"n": 14}, cn_name="MFI", note="资金流量"))
    register_indicator(S("obv", obv, ("close", "volume"), min_len=2, cn_name="OBV",
                         note="能量潮"))
    register_indicator(S("vwap", vwap, ("high", "low", "close", "volume"),
                         min_len=2, cn_name="VWAP", note="成交加权均价"))
    register_indicator(S("volume_ratio", volume_ratio, ("volume",), params={"n": 5},
                         min_len=6, cn_name="量比", note="当前量/前 N 日均量"))
    register_indicator(S("donchian", donchian, ("high", "low"), params={"n": 20},
                         cn_name="唐奇安通道", note="突破基准"))
    register_indicator(S("max_drawdown", max_drawdown, min_len=3, cn_name="最大回撤",
                         note="回撤百分比与区间"))
    register_indicator(S("annualized_volatility", annualized_volatility, min_len=5,
                         cn_name="年化波动率", note="总体标准差 × √252"))
    register_indicator(S("sharpe_ratio", sharpe_ratio, min_len=30, cn_name="夏普比率",
                         note="年化超额收益/波动"))
    register_indicator(S("sortino_ratio", sortino_ratio, min_len=30, cn_name="索提诺比率",
                         note="只惩罚下行波动"))
    register_indicator(S("calmar_ratio", calmar_ratio, min_len=30, cn_name="卡玛比率",
                         note="年化收益/最大回撤"))


_seed_registry()


def available_indicators() -> List[Dict[str, Any]]:
    """指标清单，供 MCP Resource / 工具枚举使用。"""
    out = []
    for name, spec in sorted(INDICATOR_REGISTRY.items()):
        out.append({"name": name, "cn_name": spec.cn_name or name,
                    "needs": list(spec.needs), "params": spec.params,
                    "min_len": spec.min_len, "note": spec.note})
    return out


def compute(name: str, series: Iterable[Any], **params: Any) -> Any:
    """按名计算指标。series 可以是 Bar 列表 / dict 列表 / 纯数字列表。"""
    spec = INDICATOR_REGISTRY.get(name)
    if spec is None:
        raise KeyError(f"未知指标: {name}；可用: {', '.join(sorted(INDICATOR_REGISTRY))}")
    bars = bars_from_rows(series)
    if len(bars) < spec.min_len:
        raise InsufficientData(
            f"{spec.cn_name or name}（{name}）需要至少 {spec.min_len} 根样本，"
            f"当前只有 {len(bars)} 根；确需短窗口请直接调用 indicator_engine.{name}()")
    feeds = {
        "close": [b.close for b in bars], "high": [b.high for b in bars],
        "low": [b.low for b in bars], "volume": [b.volume for b in bars],
    }
    args = [feeds[k] for k in spec.needs]
    kwargs = dict(spec.params)
    kwargs.update({k: v for k, v in params.items() if k in spec.params})
    for k, v in params.items():
        if k not in spec.params and k in ("periods", "method", "kind", "lam", "threshold"):
            kwargs[k] = v
    return spec.fn(*args, **kwargs)


def compute_last(name: str, series: Iterable[Any], **params: Any) -> Any:
    """只取最新值。多线指标返回 dict（每个分量取最新）。样本不足回 None。"""
    try:
        raw = compute(name, series, **params)
    except InsufficientData:
        return None
    return _last_of(raw)


def _last_of(raw: Any) -> Any:
    if isinstance(raw, dict):
        return {k: _last_of(v) for k, v in raw.items()}
    if isinstance(raw, list):
        for v in reversed(raw):
            if v is not None:
                return v
        return None
    return raw


# ─── 信号合成 ────────────────────────────────────────────────────────────────

@dataclass
class SignalHit:
    name: str
    direction: str          # bullish | bearish | neutral
    weight: float           # -1 ~ 1
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "direction": self.direction,
                "weight": round(self.weight, 3), "detail": self.detail}


def _cross(a: List[Optional[float]], b: List[Optional[float]]) -> Optional[str]:
    """检测最后两根 K 线上 a 与 b 的交叉。返回 golden / death / None。"""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < 2:
        return None
    (p1x, p1y), (p2x, p2y) = pairs[-2], pairs[-1]
    if p1x <= p1y and p2x > p2y:
        return "golden"
    if p1x >= p1y and p2x < p2y:
        return "death"
    return None


def detect_signals(bars: Sequence[Any]) -> List[SignalHit]:
    """基于最新一根 K 线的多指标信号扫描。"""
    bl = bars_from_rows(bars)
    closes = [b.close for b in bl]
    highs = [b.high for b in bl]
    lows = [b.low for b in bl]
    vols = [b.volume for b in bl]
    out: List[SignalHit] = []
    if len(closes) < 20:
        return out
    last = closes[-1]

    m = macd(closes)
    cross = _cross(m["dif"], m["dea"])
    if cross == "golden":
        out.append(SignalHit("MACD金叉", "bullish", 0.8, "DIF 上穿 DEA"))
    elif cross == "death":
        out.append(SignalHit("MACD死叉", "bearish", -0.8, "DIF 下穿 DEA"))
    else:
        hist = [v for v in m["hist"] if v is not None]
        if len(hist) >= 3:
            rising = hist[-1] > hist[-2] > hist[-3]
            falling = hist[-1] < hist[-2] < hist[-3]
            if rising and hist[-1] > 0:
                out.append(SignalHit("MACD柱走强", "bullish", 0.35, "红柱连续放大"))
            elif falling and hist[-1] < 0:
                out.append(SignalHit("MACD柱走弱", "bearish", -0.35, "绿柱连续放大"))

    s20, s60 = sma(closes, 20), sma(closes, min(60, len(closes)))
    ma_cross = _cross(s20, s60)
    if ma_cross == "golden":
        out.append(SignalHit("均线金叉", "bullish", 0.6, "MA20 上穿 MA60"))
    elif ma_cross == "death":
        out.append(SignalHit("均线死叉", "bearish", -0.6, "MA20 下穿 MA60"))
    if s20[-1] is not None:
        out.append(SignalHit("价格相对MA20",
                             "bullish" if last > s20[-1] else "bearish",
                             0.25 if last > s20[-1] else -0.25,
                             f"现价 {last:.4g} {'高于' if last > s20[-1] else '低于'} MA20 {s20[-1]:.4g}"))

    r = [v for v in rsi(closes, 14) if v is not None]
    if r:
        cur = r[-1]
        if cur >= 70:
            out.append(SignalHit("RSI超买", "bearish", -0.5, f"RSI14={cur:.1f}"))
        elif cur <= 30:
            out.append(SignalHit("RSI超卖", "bullish", 0.5, f"RSI14={cur:.1f}"))

    k = kdj(highs, lows, closes)
    kdj_cross = _cross(k["k"], k["d"])
    if kdj_cross == "golden" and (k["j"][-1] or 50) < 20:
        out.append(SignalHit("KDJ低位金叉", "bullish", 0.55, "低位 J 区金叉"))
    elif kdj_cross == "death" and (k["j"][-1] or 50) > 80:
        out.append(SignalHit("KDJ高位死叉", "bearish", -0.55, "高位 J 区死叉"))

    bb = bollinger(closes, 20, 2.0)
    if bb["upper"][-1] is not None:
        if last > bb["upper"][-1]:
            out.append(SignalHit("突破布林上轨", "bearish", -0.3,
                                 f"现价 {last:.4g} > 上轨 {bb['upper'][-1]:.4g}"))
        elif last < bb["lower"][-1]:
            out.append(SignalHit("跌破布林下轨", "bullish", 0.3,
                                 f"现价 {last:.4g} < 下轨 {bb['lower'][-1]:.4g}"))

    don = donchian(highs, lows, 20)
    if don["upper"][-2] is not None and len(closes) > 21:
        if closes[-1] > don["upper"][-2]:
            out.append(SignalHit("唐奇安通道上破", "bullish", 0.5,
                                 f"突破前 20 日高 {don['upper'][-2]:.4g}"))
        elif closes[-1] < don["lower"][-2]:
            out.append(SignalHit("唐奇安通道下破", "bearish", -0.5,
                                 f"跌破前 20 日低 {don['lower'][-2]:.4g}"))

    if any(v > 0 for v in vols):
        vr = volume_ratio(vols, 5)
        if vr[-1] is not None and vr[-1] >= 2.0:
            trend_up = len(closes) >= 6 and closes[-1] >= closes[-6]
            out.append(SignalHit("放量", "bullish" if trend_up else "bearish",
                                 0.4 if trend_up else -0.4, f"量比 {vr[-1]:.2f}"))

    ad = adx(highs, lows, closes)
    if ad["adx"][-1] is not None and ad["adx"][-1] >= 25:
        pdi, mdi = ad["plus_di"][-1] or 0.0, ad["minus_di"][-1] or 0.0
        out.append(SignalHit("ADX趋势确认", "bullish" if pdi > mdi else "bearish",
                             0.45 if pdi > mdi else -0.45,
                             f"ADX={ad['adx'][-1]:.1f}，"
                             f"{'+DI' if pdi > mdi else '-DI'} 占优"))

    a = atr(highs, lows, closes)
    if a[-1] is not None and last:
        rng = last - min(lows[-20:]) if len(lows) >= 20 else 0.0
        if a[-1] / last > 0.04:
            out.append(SignalHit("高波动", "neutral", 0.0,
                                 f"ATR/价格={a[-1] / last * 100:.1f}%，风险敞口偏大"))
        del rng
    return out


def composite_score(bars: Sequence[Any]) -> Dict[str, Any]:
    """把信号加权成 0-100 综合分（50 为中性），并给出可核验的分项。"""
    bl = bars_from_rows(bars)
    signals = detect_signals(bl)
    if not signals:
        return {"score": 50.0, "direction": "neutral", "confidence": 0.0,
                "signals": [], "note": "样本不足或无信号"}
    weighted = sum(s.weight for s in signals)
    cap = sum(abs(s.weight) for s in signals)
    score = 50.0 + (weighted / cap if cap else 0.0) * 50.0
    score = max(0.0, min(100.0, score))
    direction = "bullish" if score >= 58 else "bearish" if score <= 42 else "neutral"
    confidence = min(1.0, cap / 2.5)
    return {"score": round(score, 2), "direction": direction,
            "confidence": round(confidence, 3),
            "n_signals": len(signals),
            "signals": [s.to_dict() for s in signals]}


# ─── 一站式分析 ──────────────────────────────────────────────────────────────

def summarize_series(values: Sequence[Any]) -> Dict[str, Any]:
    """一条序列的描述统计 + 收益 + 风险概览。"""
    vals = _nums(values)
    if not vals:
        return {"count": 0}
    n = len(vals)
    q1, q3 = _quartiles(vals)
    chg = (vals[-1] / vals[0] - 1.0) * 100.0 if vals[0] else None
    out: Dict[str, Any] = {
        "count": n,
        "first": vals[0], "last": vals[-1],
        "change_pct": None if chg is None else round(chg, 4),
        "min": min(vals), "max": max(vals),
        "mean": round(statistics.mean(vals), 6),
        "median": round(statistics.median(vals), 6),
        "stdev": round(statistics.stdev(vals), 6) if n > 1 else 0.0,
        "iqr": round(q3 - q1, 6),
        "percentile_of_last": round(percentile_rank(vals, vals[-1]), 2),
    }
    dd = max_drawdown(vals)
    out["max_drawdown_pct"] = dd["max_drawdown_pct"]
    vol = annualized_volatility(vals)
    out["annualized_volatility_pct"] = None if vol is None else round(vol, 4)
    out["sharpe"] = None if sharpe_ratio(vals) is None else round(sharpe_ratio(vals), 4)
    out["sortino"] = None if sortino_ratio(vals) is None else round(sortino_ratio(vals), 4)
    out["calmar"] = None if calmar_ratio(vals) is None else round(calmar_ratio(vals), 4)
    reg = ols(list(range(n)), vals)
    if reg.get("ok"):
        out["trend_slope_per_bar"] = round(reg["slope"], 8)
        out["trend_r_squared"] = reg["r_squared"]
        out["trend_t_stat"] = reg["t_stat"]
        out["trend_direction"] = "up" if reg["slope"] > 0 else "down" if reg["slope"] < 0 else "flat"
    ts = theil_sen(list(range(n)), vals)
    if ts.get("ok"):
        out["robust_slope"] = round(ts["slope"], 8)
    return out


def analyze_bars(bars: Sequence[Any], benchmark: Optional[Sequence[Any]] = None,
                 anomaly_threshold: float = 3.0) -> Dict[str, Any]:
    """完整技术+统计诊断。benchmark 提供时额外输出超额收益、beta、相关性。

    输出刻意保持"事实 + 可核验数字"，不写结论性散文——文案交给上层模型，
    这样每条判断都能回溯到具体指标值。
    """
    bl = bars_from_rows(bars)
    closes = [b.close for b in bl]
    result: Dict[str, Any] = {
        "bars": len(bl),
        "summary": summarize_series(closes),
        "indicators": {
            "sma": {"n20": _last_of(sma(closes, 20)), "n60": _last_of(sma(closes, min(60, len(closes))))},
            "ema": {"n12": _last_of(ema(closes, 12)), "n26": _last_of(ema(closes, 26))},
            "macd": {k: _last_of(v) for k, v in macd(closes).items()},
            "rsi14": _last_of(rsi(closes, 14)),
            "kdj": {k: _last_of(v) for k, v in kdj([b.high for b in bl],
                                                   [b.low for b in bl], closes).items()},
            "bollinger": {k: _last_of(v) for k, v in bollinger(closes).items()},
            "atr14": _last_of(atr([b.high for b in bl], [b.low for b in bl], closes)),
            "adx14": _last_of(adx([b.high for b in bl], [b.low for b in bl], closes)["adx"]),
            "obv": _last_of(obv(closes, [b.volume for b in bl])),
            "volume_ratio": _last_of(volume_ratio([b.volume for b in bl])),
        },
        "signals": composite_score(bl),
        "anomalies": [f.to_dict() for f in detect_anomalies(closes, "robust",
                                                            anomaly_threshold, tail=30)][:10],
    }
    for key, val in result["indicators"].items():
        if isinstance(val, float):
            result["indicators"][key] = round(val, 6)
    if benchmark:
        bc = _nums([_attr(b, "close") for b in _iter(benchmark)])
        if len(bc) >= 5 and len(closes) >= 5:
            joint = min(len(bc), len(closes))
            mine = closes[-joint:]
            theirs = bc[-joint:]
            res = {
                "pearson": None,
                "spearman": None,
                "beta": None,
                "excess_return_pct": None,
                "tracking_error_pct": None,
                "best_lag": None,
            }
            p = pearson(returns(mine), returns(theirs))
            res["pearson"] = None if p is None else round(p, 4)
            sp = spearman(returns(mine), returns(theirs))
            res["spearman"] = None if sp is None else round(sp, 4)
            reg = ols(returns(theirs), returns(mine))
            res["beta"] = round(reg["slope"], 4) if reg.get("ok") else None
            res["excess_return_pct"] = round(
                (mine[-1] / mine[0] - theirs[-1] / theirs[0]) * 100.0, 4)
            rr_self = returns(mine)
            rr_bench = returns(theirs)
            size = min(len(rr_self), len(rr_bench))
            if size >= 3:
                diff = [rr_self[-size:][i] - rr_bench[-size:][i] for i in range(size)]
                res["tracking_error_pct"] = round(statistics.pstdev(diff) * math.sqrt(252) * 100.0, 4)
            res["best_lag"] = lag_cross_correlation(rr_self, rr_bench, max_lag=5)
            result["relative_to_benchmark"] = res
    return result
