# -*- coding: utf-8 -*-
"""金融产品分析模块 v7.7.0（在 v5.0.0 基础上增强）

相比 v4.x 占位级实现，v5.0.0 提供：
- **真实风险指标**：基于净值时间序列计算年化收益/年化波动/最大回撤/夏普/卡玛/正收益天数
  （数据不足 30 点时返回 insufficient_data，不再伪造数值）
- **数据驱动的投资风格分析**：持仓集中度(CR5/CR10) + 行业暴露 + 风格定位
- **数据驱动的组合复刻**：同行业+市值接近的替代标的（fallback 到旧映射并标注来源）
- **数值距离相似推荐**：特征向量欧氏距离替代写死映射

v7.7.0 新增：
- **指标扩展**：Sortino 比率（下行波动口径）/ 胜率 / 单日最佳最差收益；
  风险指标内存缓存（同序列重复计算命中缓存，批量分析提速）
- **nav_trend_analysis 趋势分析**：区间收益/回归斜率年化/距峰值回撤/近期波动比，
  输出强上行/缓步上行/震荡/下行四档定性标签

向后兼容：所有公开函数签名保持 v4.x（calculate_risk_metrics 增加可选入参
risk_free_rate；旧 dict 入参路径走估算并标注 estimate:true）。
"""

import json
import math
import statistics
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union, Sequence

SKILL_DATA_DIR = Path(__file__).parent.parent / "data"

# 无风险利率默认值（年化 2%）
DEFAULT_RISK_FREE = 0.02
# 一年交易日数
TRADING_DAYS = 252

# v7.7 风险指标缓存（同序列重复计算，如批量分析/报告生成）
_METRICS_CACHE: Dict[Tuple, Dict[str, Any]] = {}
_METRICS_CACHE_MAX = 256

# 行业映射（股票代码 → 行业，够用即可；完整映射依赖外部注册表）
_CODE_INDUSTRY_HINT = {
    "6": "沪市主板", "0": "深市主板", "3": "创业板", "68": "科创板", "8": "北交所",
    "4": "北交所", "9": "B股",
}

# 行业关键词（股票名 → 行业），用于数据驱动替代标的选择
_INDUSTRY_HINTS = [
    (["白酒", "酒业", "茅台", "五粮液", "泸州", "古井", "洋河", "酒鬼"], "白酒"),
    (["银行"], "银行"),
    (["证券", "券商"], "券商"),
    (["保险"], "保险"),
    (["宁德", "比亚迪", "锂", "电池", "新能源"], "新能源"),
    (["地产", "万科", "保利", "金地"], "地产"),
    (["医药", "药", "医疗", "生物"], "医药"),
    (["科技", "芯片", "半导体", "软件"], "科技"),
    (["通信", "移动", "电信"], "通信"),
]


def _industry_of(name: str) -> str:
    """按股票名关键词判断行业（简单词库）。"""
    if not name:
        return ""
    for keywords, tag in _INDUSTRY_HINTS:
        if any(k in name for k in keywords):
            return tag
    return ""


def _normalize_series(nav_data: Union[List[Dict[str, Any]], Dict[str, Any]]) -> List[float]:
    """从各种入参形态提取升序净值序列。

    支持:
      - [{"date":..., "nav":1.2}, ...]
      - [1.1, 1.2, 1.3]（纯数值列表）
      - {"dates": [...], "navs": [...]}
    空值/NaN 过滤；返回浮点列表。
    """
    values: List[float] = []
    if isinstance(nav_data, list):
        for item in nav_data:
            if isinstance(item, dict):
                v = item.get("nav")
                if v is None:
                    v = item.get("value")
            else:
                v = item
            values.append(_to_float(v))
    elif isinstance(nav_data, dict):
        navs = nav_data.get("navs") or nav_data.get("values") or []
        values = [_to_float(v) for v in navs]
    return [v for v in values if v is not None and not math.isnan(v) and v > 0]


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct_changes(values: Sequence[float]) -> List[float]:
    """逐日收益率序列。"""
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev > 0:
            out.append(values[i] / prev - 1.0)
    return out


def _max_drawdown(values: Sequence[float]) -> float:
    """最大回撤（正数，如 0.25 = 回撤 25%）。"""
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = 1.0 - v / peak
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _annualized_return(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    start, end = values[0], values[-1]
    if start <= 0:
        return 0.0
    total_return = end / start - 1.0
    years = (len(values) - 1) / TRADING_DAYS
    if years <= 0:
        return total_return
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def calculate_risk_metrics(nav_data: Union[List[Dict[str, Any]], Dict[str, Any]],
                           risk_free_rate: float = DEFAULT_RISK_FREE) -> Dict[str, Any]:
    """计算真实风险指标（v5.0.0）。

    Args:
        nav_data: 两种入参均支持（向后兼容）:
          - List[{"date":..., "nav":...}] 净值时间序列（升序）→ 真实指标
          - Dict 旧格式 {"1month":2.3,"3month":5.6,"1year":15.7} → 估算，标注 estimate:true
        risk_free_rate: 无风险利率（年化，默认 0.02）

    Returns:
        {annualized_return, annualized_volatility, max_drawdown, sharpe_ratio,
         calmar_ratio, sortino_ratio, win_rate, best_day, worst_day,
         positive_days, total_days}；
        数据不足 30 点 → {"insufficient_data": True, "points": n}（不伪造数值）。
    """
    # 旧 dict 格式（周期收益率，无时间序列）
    if isinstance(nav_data, dict) and not (nav_data.get("navs") or nav_data.get("values")):
        return _estimate_metrics_from_periods(nav_data, risk_free_rate)

    values = _normalize_series(nav_data)
    if len(values) < 30:
        return {"insufficient_data": True, "points": len(values)}

    # v7.7 缓存（同序列+同无风险利率直接命中）
    cache_key = (tuple(values), round(float(risk_free_rate), 6))
    cached = _METRICS_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    returns = _pct_changes(values)
    if not returns:
        return {"insufficient_data": True, "points": len(values)}

    ann_ret = _annualized_return(values)
    ann_vol = (statistics.pstdev(returns) if len(returns) > 1 else 0.0) * math.sqrt(TRADING_DAYS)
    max_dd = _max_drawdown(values)
    sharpe = (ann_ret - risk_free_rate) / ann_vol if ann_vol > 1e-9 else 0.0
    calmar = ann_ret / max_dd if max_dd > 1e-9 else 0.0
    positive_days = sum(1 for r in returns if r > 0)

    # v7.7 Sortino：下行偏差口径（只惩罚亏损波动）
    downside_sq = [min(r, 0.0) ** 2 for r in returns]
    downside_dev = math.sqrt(sum(downside_sq) / len(downside_sq)) * math.sqrt(TRADING_DAYS) \
        if downside_sq else 0.0
    sortino = (ann_ret - risk_free_rate) / downside_dev if downside_dev > 1e-9 else 0.0

    result = {
        "annualized_return": round(ann_ret, 6),
        "annualized_volatility": round(ann_vol, 6),
        "max_drawdown": round(max_dd, 6),
        "sharpe_ratio": round(sharpe, 4),
        "calmar_ratio": round(calmar, 4),
        "sortino_ratio": round(sortino, 4),
        "win_rate": round(positive_days / len(returns), 4),
        "best_day": round(max(returns), 6),
        "worst_day": round(min(returns), 6),
        "positive_days": positive_days,
        "total_days": len(returns),
        "points": len(values),
    }
    if len(_METRICS_CACHE) >= _METRICS_CACHE_MAX:
        _METRICS_CACHE.pop(next(iter(_METRICS_CACHE)))   # FIFO 淘汰最旧
    _METRICS_CACHE[cache_key] = dict(result)
    return result


def _estimate_metrics_from_periods(nav_data: Dict[str, Any],
                                   risk_free_rate: float = DEFAULT_RISK_FREE) -> Dict[str, Any]:
    """旧格式估算（仅 1year/3year 等周期收益率，无序列）— 标注 estimate:true。"""
    one_year = _to_float(nav_data.get("1year"))
    three_year = _to_float(nav_data.get("3year"))
    max_dd = _to_float(nav_data.get("max_drawdown"))
    out: Dict[str, Any] = {"estimate": True}
    if one_year is not None:
        out["annual_return"] = one_year / 100.0
    if three_year is not None:
        try:
            out["annual_return_3y"] = (1 + three_year / 100.0) ** (1 / 3) - 1
        except (ValueError, TypeError):
            pass
    if max_dd is not None and max_dd != 0:
        out["calmar_ratio"] = abs(out.get("annual_return", 0.0) / (max_dd / 100.0))
    return out


def nav_trend_analysis(nav_data: Union[List[Dict[str, Any]], Dict[str, Any]],
                       windows: Sequence[int] = (20, 60)) -> Dict[str, Any]:
    """v7.7 净值趋势分析（纯函数，可离线单测）。

    Returns:
        points / return_{w}d（区间收益）/ daily_slope（回归斜率，相对口径）/
        annualized_slope / off_peak（距峰值回撤）/ at_high（是否高位附近）/
        vol_ratio_recent20（近 20 日波动/全期波动）/ trend（定性标签）；
        数据不足 10 点 → {"insufficient_data": True, "points": n}。
    """
    values = _normalize_series(nav_data)
    if len(values) < 10:
        return {"insufficient_data": True, "points": len(values)}
    out: Dict[str, Any] = {"points": len(values)}
    for w in windows:
        if len(values) > w:
            seg = values[-(w + 1):]
            out[f"return_{w}d"] = round(seg[-1] / seg[0] - 1.0, 6)
    # 线性回归斜率（相对口径，免量纲）
    n = len(values)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    den = sum((i - mean_x) ** 2 for i in range(n))
    slope = sum((i - mean_x) * (values[i] - mean_y) for i in range(n)) / den if den else 0.0
    daily_slope = slope / mean_y if mean_y > 0 else 0.0
    out["daily_slope"] = round(daily_slope, 8)
    out["annualized_slope"] = round(daily_slope * TRADING_DAYS, 6)
    peak = max(values)
    out["off_peak"] = round(1.0 - values[-1] / peak, 6)
    out["at_high"] = values[-1] >= peak * 0.999
    rets = _pct_changes(values)
    if len(rets) >= 40:
        all_vol = statistics.pstdev(rets)
        out["vol_ratio_recent20"] = round(statistics.pstdev(rets[-20:]) / all_vol, 3) \
            if all_vol > 1e-12 else None
    ann_slope = out["annualized_slope"]
    if ann_slope > 0.10 and out["off_peak"] < 0.05:
        out["trend"] = "强上行"
    elif ann_slope > 0.02:
        out["trend"] = "缓步上行"
    elif ann_slope >= -0.02:
        out["trend"] = "震荡"
    else:
        out["trend"] = "下行"
    return out


# ============================================================
# 2. 投资风格分析（数据驱动）
# ============================================================

def analyze_investment_style(holdings: Dict[str, Any],
                             nav_data: Union[List[Dict[str, Any]], Dict[str, float]] = None) -> str:
    """分析投资风格，返回风格标签字符串（向后兼容返回 str）。

    增强点（v5.0.0）：
      - 持仓集中度：CR5 / CR10（按权重降序）
      - 市值暴露：基于股票代码（沪主板/深主板/创业板/科创板/北交所）
      - 风格定位：基于 1year 收益或净值序列斜率
    """
    labels = []
    stocks = holdings.get("stocks") or []
    if stocks:
        # 集中度
        weights = sorted((_to_float(s.get("weight")) or 0.0 for s in stocks), reverse=True)
        cr5 = sum(weights[:5])
        if stocks and cr5 >= 50:
            labels.append("集中")
        else:
            labels.append("分散")
        # 板块暴露
        boards = {"沪主板": 0, "深主板": 0, "创业板": 0, "科创板": 0, "北交所": 0}
        for s in stocks:
            code = str(s.get("code", ""))
            if code.startswith("68"):
                boards["科创板"] += 1
            elif code.startswith(("30", "300")):
                boards["创业板"] += 1
            elif code.startswith(("4", "8", "920")):
                boards["北交所"] += 1
            elif code.startswith("6"):
                boards["沪主板"] += 1
            elif code.startswith(("0", "2", "3")):
                boards["深主板"] += 1
        main = max(boards.items(), key=lambda kv: kv[1])
        if main[1] > 0:
            labels.append(main[0])
    # 风格定位：基于收益
    if nav_data is not None:
        if isinstance(nav_data, list):
            values = _normalize_series(nav_data)
            if len(values) >= 5:
                momentum = values[-1] / values[0] - 1.0
                if momentum > 0.30:
                    labels.append("积极成长")
                elif momentum > 0.10:
                    labels.append("成长")
                elif momentum > 0.0:
                    labels.append("均衡")
                else:
                    labels.append("稳健")
        else:
            one_year = _to_float(nav_data.get("1year"))
            if one_year is not None:
                if one_year > 30:
                    labels.append("积极成长")
                elif one_year > 15:
                    labels.append("成长")
                elif one_year > 5:
                    labels.append("均衡")
                else:
                    labels.append("稳健")
    return "-".join(labels) if labels else "均衡型"


# ============================================================
# 3. 相似产品推荐（数值距离）
# ============================================================

_FEATURE_DIMS = ("annual_return", "volatility", "max_drawdown")

_CANDIDATE_FEATURE_KEYS = {
    "annual_return": ("annual_return", "return_1y", "年化收益率"),
    "volatility": ("volatility", "vol", "年化波动率"),
    "max_drawdown": ("max_drawdown", "drawdown", "最大回撤"),
}


def _product_features(product_info: Dict[str, Any]) -> Dict[str, float]:
    """把目标产品折算成与候选库同量纲的特征（三项都是百分数，如 15.0 = 15%）。

    只返回真正算得出的维度。旧 dict 格式只有各区间收益率，早期版本把
    [1year, 6month, 3month] 当成 [收益, 波动, 回撤] 送去比距离——三个数
    含义完全不同，排序结果纯属巧合。
    """
    hist = product_info.get("historical_nav") or {}

    if isinstance(hist, list):
        metrics = calculate_risk_metrics(_normalize_series(hist))
        if metrics.get("insufficient_data"):
            return {}
        return {
            "annual_return": metrics.get("annualized_return", 0.0) * 100,
            "volatility": metrics.get("annualized_volatility", 0.0) * 100,
            "max_drawdown": metrics.get("max_drawdown", 0.0) * 100,
        }

    if isinstance(hist, dict):
        out: Dict[str, float] = {}
        ann = _to_float(hist.get("1year"))
        if ann is None:
            ann = _to_float(hist.get("annual_return"))
        if ann is None:
            half = _to_float(hist.get("6month"))
            if half is not None:
                ann = ((1.0 + half / 100.0) ** 2 - 1.0) * 100.0
        if ann is not None:
            out["annual_return"] = ann
        vol = _to_float(hist.get("volatility"))
        dd = _to_float(hist.get("max_drawdown"))
        if vol is not None:
            out["volatility"] = vol
        if dd is not None:
            out["max_drawdown"] = dd
        return out

    return {}


def _candidate_features(item: Dict[str, Any]) -> Dict[str, float]:
    """候选条目取同名字段；三维全缺的条目直接不参与，别再按 0 补。"""
    out: Dict[str, float] = {}
    for dim, keys in _CANDIDATE_FEATURE_KEYS.items():
        for key in keys:
            v = _to_float(item.get(key))
            if v is not None:
                out[dim] = v
                break
    return out


def _feature_distance(a: Dict[str, float], b: Dict[str, float],
                      metric: str = "euclidean") -> Tuple[float, int]:
    """只在双方都有值的维度上算距离，再按维度数归一。

    不归一的话，"只比得出收益率"的条目会比"三维都比得出"的条目系统性地
    显得更近（少一项就少一份平方），排序会把数据缺失当成相似。
    """
    keys = [k for k in _FEATURE_DIMS if k in a and k in b]
    if not keys:
        return float("inf"), 0
    diffs = [a[k] - b[k] for k in keys]
    if metric == "manhattan":
        raw = sum(abs(d) for d in diffs) / len(keys)
    else:
        raw = math.sqrt(sum(d * d for d in diffs) / len(keys))
    return raw, len(keys)


def suggest_similar_products(product_info: Dict[str, Any], limit: int = 3,
                             metric: str = "euclidean") -> List[Dict[str, Any]]:
    """推荐相似产品 — 基于特征向量数值距离（v5.0.0 替代写死映射）。

    候选来自本地基金数据库 data/fund_managers_distilled.json（如存在）。
    库内 annual_return / volatility / max_drawdown 均按百分数填写。
    """
    db_path = SKILL_DATA_DIR / "fund_managers_distilled.json"
    if not db_path.exists():
        return [{"note": "本地数据库不可用，请先更新基金数据"}]

    used_metric = metric if metric in ("euclidean", "manhattan") else "euclidean"
    target = _product_features(product_info)
    target_type = str(product_info.get("product_type", "") or "")
    target_style = str(product_info.get("investment_style", "") or "")

    try:
        with open(db_path, "r", encoding="utf-8") as f:
            fund_db = json.load(f)
        fund_data = fund_db.get("managers", []) if isinstance(fund_db, dict) else fund_db
    except Exception as e:
        return [{"error": f"数据库查询失败: {e}"}]

    if not target:
        return [{"note": "目标产品缺少可比数值特征，需要 historical_nav 净值序列或年化收益率"}]

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for item in fund_data[:2000]:
        cand = _candidate_features(item)
        if not cand:
            continue
        dist, dims = _feature_distance(target, cand, used_metric)
        if not math.isfinite(dist):
            continue
        type_match = bool(target_type) and target_type in str(item.get("fund_type") or "")
        style_match = bool(target_style) and target_style in str(item.get("investment_style") or "")
        # 同类型/同风格按乘性折价而不是减固定常数：距离本身是百分数尺度，
        # 减 5.0 在低波动样本上等于直接封顶排名。
        penalty = (0.75 if type_match else 1.0) * (0.85 if style_match else 1.0)
        scored.append((dist * penalty, {
            "name": item.get("name", ""),
            "fund_name": item.get("current_fund_name", ""),
            "company": item.get("company_name", ""),
            "fund_type": item.get("fund_type", ""),
            "style": item.get("investment_style", ""),
            "distance": round(dist, 4),
            "similarity": round(1.0 / (1.0 + dist), 4),
            "compared_dims": dims,
            "type_match": type_match,
            "style_match": style_match,
            "metric": used_metric,
        }))

    if not scored:
        return [{"note": "候选库没有与目标产品可比的数值特征"}]

    scored.sort(key=lambda x: x[0])
    return [payload for _, payload in scored[:limit]]


# ============================================================
# 4. 组合复刻（数据驱动替代标的）
# ============================================================

# 旧硬编码替代映射（v5.0.0 起仅作 fallback，命中时标注 mapping_source=fallback）
_LEGACY_ALTERNATIVES = {
    "贵州茅台": ["五粮液", "泸州老窖", "洋河股份"],
    "宁德时代": ["比亚迪", "国轩高科", "亿纬锂能"],
    "五粮液": ["贵州茅台", "泸州老窖", "古井贡酒"],
    "比亚迪": ["宁德时代", "理想汽车", "小鹏汽车"],
    "招商银行": ["宁波银行", "平安银行", "兴业银行"],
    "中国平安": ["中国人寿", "新华保险", "太平洋保险"],
    "腾讯控股": ["阿里巴巴", "美团", "京东"],
    "阿里巴巴": ["腾讯控股", "拼多多", "京东"],
}


def suggest_alternatives(stock_name: str) -> List[str]:
    """推荐股票替代品（fallback 映射，v5.0.0 保留作降级）。"""
    return _LEGACY_ALTERNATIVES.get(stock_name, ["同行业龙头股"])


def generate_portfolio_replication(product_info: Dict[str, Any],
                                   candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """生成投资组合复刻建议。

    v5.0.0 增强：
      - allocation.alternative 优先数据驱动（同行业 + 市值接近），标注 mapping_source
      - 候选缺失时回退旧映射（mapping_source=fallback）
    """
    replication = {
        "target_product": product_info.get("product_name", "未知"),
        "target_code": product_info.get("product_code", ""),
        "target_style": "",
        "allocation": [],
        "similar_products": [],
        "total_suggestion": "",
        "mapping_source": "data-driven",
    }

    holdings = product_info.get("holdings", {})
    nav_data = product_info.get("historical_nav", {})
    style = analyze_investment_style(holdings, nav_data)
    replication["target_style"] = style

    stocks = holdings.get("stocks") or []
    if stocks:
        for i, stock in enumerate(stocks[:10], 1):
            alt = suggest_alternatives(stock.get("name", ""))
            src = "fallback"
            if candidates:
                # 数据驱动：同行业 + 权重接近的候选
                alt = _data_driven_alternatives(stock, candidates, limit=2)
                src = "data-driven"
            replication["allocation"].append({
                "rank": i,
                "stock_code": stock.get("code", ""),
                "stock_name": stock.get("name", ""),
                "weight": stock.get("weight", 0),
                "alternative": alt,
                "mapping_source": src,
            })

    replication["similar_products"] = suggest_similar_products(product_info)
    replication["total_suggestion"] = (
        f"基于{replication['target_product']}的分析：\n"
        f"- 投资风格：{style}\n"
        f"- 建议持有周期：3-5 年\n"
        f"- 适合投资者类型：{'积极型' if '成长' in style or '集中' in style else '稳健型'}\n"
        f"- 配置建议：可将总资产的20-30%配置于同类风格产品"
    )
    return replication


def _data_driven_alternatives(stock: Dict[str, Any], candidates: List[Dict[str, Any]],
                              limit: int = 2) -> List[str]:
    """数据驱动替代：同行业（词库优先）+ 同板块 + 权重接近的候选股票名。"""
    code = str(stock.get("code", ""))
    board = _CODE_INDUSTRY_HINT.get(code[:1], "") or _CODE_INDUSTRY_HINT.get(code[:2], "")
    stock_name = stock.get("name", "")
    industry = _industry_of(stock_name)
    weight = _to_float(stock.get("weight")) or 0.0
    scored = []
    for c in candidates:
        c_code = str(c.get("code", ""))
        c_board = _CODE_INDUSTRY_HINT.get(c_code[:1], "") or _CODE_INDUSTRY_HINT.get(c_code[:2], "")
        c_name = c.get("name", "")
        if not c_name or c_name == stock_name:
            continue
        score = 0.0
        # 行业匹配权重最高
        if industry and _industry_of(c_name) == industry:
            score += 2.0
        if board and c_board == board:
            score += 0.5
        c_weight = _to_float(c.get("weight")) or 0.0
        score += 0.3 * (1.0 - min(abs(c_weight - weight) / 10.0, 1.0))  # 权重接近加分
        scored.append((score, c_name))
    scored.sort(key=lambda x: -x[0])
    out = [n for _, n in scored[:limit] if _ > 0]
    return out if out else suggest_alternatives(stock_name)


# ============================================================
# 5. 综合分析报告（保持 analyze_product 输出格式）
# ============================================================

def analyze_product(product_info: Dict[str, Any]) -> str:
    """综合分析产品并生成报告（v5.0.0 用真实风险指标替换占位估算）。"""
    name = product_info.get("product_name", "未知")
    code = product_info.get("product_code", "")
    product_type = product_info.get("product_type", "")
    company = product_info.get("company", "")
    manager = product_info.get("manager", "")
    risk = product_info.get("risk_level", "")

    nav = product_info.get("nav", {})
    historical = product_info.get("historical_nav", {})
    holdings = product_info.get("holdings", {})
    risk_metrics = product_info.get("risk_metrics", {})

    style = analyze_investment_style(holdings, historical)
    metrics_calc = calculate_risk_metrics(historical)
    trend_calc = nav_trend_analysis(historical) \
        if isinstance(historical, list) else None

    report = f"""
【产品分析报告】

■ 基本信息
名称：{name}
代码：{code}
类型：{product_type}
公司：{company}
经理：{manager}
风险等级：{risk}

■ 收益表现
{"-"*40}
"""
    if nav:
        report += f"最新净值：{nav.get('current', 'N/A')}\n"
        report += f"日涨跌幅：{nav.get('daily_change', 'N/A')}%\n"

    if isinstance(historical, dict):
        report += "\n历史收益：\n"
        for period, value in historical.items():
            if period in ("1month", "3month", "6month", "1year", "3year"):
                report += f"  近{period}：{value}%\n"

    report += f"""
■ 风险指标（v5.0.0 真实计算）
{"-"*40}
"""
    for k, v in risk_metrics.items():
        report += f"{k}：{v}\n"
    if metrics_calc.get("insufficient_data"):
        report += f"净值数据不足（{metrics_calc.get('points', 0)} 点），无法计算真实风险指标\n"
    else:
        labels = {
            "annualized_return": "年化收益", "annualized_volatility": "年化波动",
            "max_drawdown": "最大回撤", "sharpe_ratio": "夏普比率",
            "calmar_ratio": "卡玛比率", "sortino_ratio": "索提诺比率",
            "win_rate": "胜率", "best_day": "单日最佳", "worst_day": "单日最差",
            "positive_days": "正收益天数",
        }
        for k, v in metrics_calc.items():
            if k in labels:
                report += f"{labels[k]}：{v:.4f}\n" if isinstance(v, float) else f"{labels[k]}：{v}\n"
    if trend_calc and not trend_calc.get("insufficient_data"):
        report += (f"\n趋势（v7.7）：{trend_calc.get('trend', '')}，"
                   f"距峰值 {trend_calc.get('off_peak', 0) * 100:.2f}%，"
                   f"年化斜率 {trend_calc.get('annualized_slope', 0) * 100:.1f}%\n")

    report += f"""
■ 持仓分析
{"-"*40}
"""
    if holdings.get("stocks"):
        report += "前十大重仓股：\n"
        for i, stock in enumerate(holdings["stocks"][:10], 1):
            report += f"  {i}. {stock.get('name', '')}({stock.get('code', '')}) - {stock.get('weight', 0)}%\n"
    if holdings.get("top_industry"):
        report += f"\n重点行业：{holdings['top_industry']}\n"

    report += f"""
■ 风格定位
{"-"*40}
{style}

■ 综合建议
{"-"*40}
"""
    if "成长" in style:
        report += "该产品为成长风格，适合积极型投资者，建议定投介入，持有周期3年以上。\n"
    elif "均衡" in style:
        report += "该产品为均衡风格，适合稳健型投资者，可作为资产配置的一部分。\n"
    elif "稳健" in style:
        report += "该产品为稳健风格，适合保守型投资者，可用于资产保值。\n"
    else:
        report += "建议根据自身风险偏好适量配置。\n"

    return report


if __name__ == "__main__":
    # 测试
    test_product = {
        "product_name": "华夏成长混合",
        "product_code": "000001",
        "product_type": "混合型-灵活配置",
        "company": "华夏基金",
        "manager": "张明",
        "risk_level": "中高风险",
        "nav": {"current": 3.4567, "daily_change": 1.23},
        "historical_nav": {"1month": 2.34, "3month": 5.89, "6month": 8.45, "1year": 15.67},
        "holdings": {
            "stocks": [
                {"code": "600519", "name": "贵州茅台", "weight": 5.2},
                {"code": "000858", "name": "五粮液", "weight": 4.8},
                {"code": "300750", "name": "宁德时代", "weight": 4.5}
            ],
            "top_industry": "食品饮料"
        }
    }

    # 真实风险指标（合成 120 天序列）
    synthetic = [{"date": f"2026-01-{i:02d}", "nav": 1.0 * (1 + 0.001 * i + (0.002 if i % 3 == 0 else 0))}
                 for i in range(1, 121)]
    print("真实风险指标（120点）:")
    print(json.dumps(calculate_risk_metrics(synthetic), ensure_ascii=False, indent=2))
    print("\n旧格式估算:")
    print(json.dumps(calculate_risk_metrics({"1year": 15.67}), ensure_ascii=False, indent=2))
    print("\n" + "=" * 50)
    print(analyze_product(test_product))
