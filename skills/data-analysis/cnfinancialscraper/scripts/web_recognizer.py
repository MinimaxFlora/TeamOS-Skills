# -*- coding: utf-8 -*-
"""
网页内容识别引擎 v7.3.0 (web_recognizer.py)

增强"网页上相关内容的识别和爬取"：零依赖（正则 + html.parser）识别金融网页的
结构化内容，输出可直接入库/分析的 dict。

能力：
  - extract_tables      : 识别全部 <table>（含表头/行数据/合并单元格摊平）
  - extract_kv_blocks   : 识别键值数据块（dl/dt/dd、label:value 模式，如净值/涨跌幅）
  - extract_article_items: 列表页识别文章条目（标题/链接/日期），供后续批量爬取
  - extract_numbers_with_context: 正文中金额/百分比数字 + 上下文抽取
  - detect_pagination   : 分页识别（下一页链接 + 页码规律）
  - recognize_page_type : 页面类型判别（list/article/data/home）
  - smart_extract       : 一站式识别（自动按页面类型组合上述能力）

用法：
    from scripts.web_recognizer import smart_extract
    result = smart_extract(html, url="https://example.com/news")
    # {"page_type": "list", "articles": [...], "tables": [...], ...}
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

# ── 通用小工具 ─────────────────────────────────────────────────

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>",
                        re.DOTALL | re.IGNORECASE)


def strip_tags(html: str) -> str:
    """去标签 + 压缩空白（纯文本化）。"""
    text = _SCRIPT_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _clean(s: str) -> str:
    return _WS_RE.sub(" ", (s or "")).strip()


class _TableCollector(HTMLParser):
    """收集页面内全部 table 的原始单元格文本。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables: List[List[List[str]]] = []   # [table][row][cell]
        self._rows: Optional[List[List[str]]] = None
        self._cell: Optional[List[str]] = None
        self._depth = 0
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._rows = []
        elif self._depth == 1 and tag == "tr" and self._rows is not None:
            self._rows.append([])
        elif self._depth == 1 and tag in ("td", "th"):
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("td", "th") and self._cell is not None:
            if self._rows and self._rows[-1] is not None:
                self._rows[-1].append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "table":
            self._depth -= 1
            if self._depth == 0 and self._rows is not None:
                rows = [r for r in self._rows if r]
                if rows:
                    self.tables.append(rows)
                self._rows = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)


def extract_tables(html: str, min_rows: int = 2,
                   max_tables: int = 20) -> List[Dict[str, Any]]:
    """识别页面中的全部表格。

    Returns:
        [{"headers": [...], "rows": [[...]], "n_rows": n, "n_cols": n}, ...]
        表头启发式：首行无数字且列数一致时视为表头。
    """
    if not html:
        return []
    collector = _TableCollector()
    try:
        collector.feed(html)
    except Exception:
        return []
    out = []
    for raw in collector.tables[:max_tables]:
        if len(raw) < min_rows:
            continue
        n_cols = max(len(r) for r in raw)
        if n_cols < 2:
            continue
        headers: List[str] = []
        rows = raw
        first = raw[0]
        has_digit = any(re.search(r"\d", c) for c in first)
        if not has_digit and len(raw) > 1:
            headers = first
            rows = raw[1:]
        out.append({"headers": headers, "rows": rows,
                    "n_rows": len(rows), "n_cols": n_cols})
    return out


# ── 键值数据块识别 ─────────────────────────────────────────────

_DL_RE = re.compile(r"<dl[^>]*>(.*?)</dl>", re.DOTALL | re.IGNORECASE)
_DT_RE = re.compile(r"<dt[^>]*>(.*?)</dt>", re.DOTALL | re.IGNORECASE)
_DD_RE = re.compile(r"<dd[^>]*>(.*?)</dd>", re.DOTALL | re.IGNORECASE)

# 金融常见键名（用于散落在文本中的 label:value 识别）
_FIN_LABELS = (
    "最新净值|累计净值|单位净值|日增长率|涨跌幅|涨跌|今开|昨收|最高|最低|"
    "成交量|成交额|换手率|市盈率|市净率|总市值|流通市值|每股收益|每股净资产|"
    "净资产收益率|营业收入|净利润|总资产|净资产|负债|收益率|年化收益|"
    "最大回撤|夏普比率|成立日期|基金规模|基金经理|管理费率|托管费率|"
    "申购状态|赎回状态|风险等级|发行价|现价|利息|利率|余额|规模"
)
_KV_TEXT_RE = re.compile(
    r"(" + _FIN_LABELS + r")\s*[:：]\s*([^\s<>]{1,40})")


def extract_kv_blocks(html: str, max_items: int = 200) -> Dict[str, str]:
    """识别键值型金融数据块（净值/涨跌幅/财务指标等）。

    来源：1) <dl>/<dt>/<dd> 结构；2) 文本中「标签:值」模式。
    键冲突时保留先出现的值。
    """
    kv: Dict[str, str] = {}
    if not html:
        return kv
    # 1) dl 结构
    for dl in _DL_RE.findall(html):
        dts = _DT_RE.findall(dl)
        dds = _DD_RE.findall(dl)
        for k, v in zip(dts, dds):
            k, v = _clean(strip_tags(k)), _clean(strip_tags(v))
            if k and v and k not in kv and len(kv) < max_items:
                kv[k] = v
    # 2) 文本 label:value
    text = strip_tags(html)
    for m in _KV_TEXT_RE.finditer(text):
        k, v = _clean(m.group(1)), _clean(m.group(2))
        if k and v and k not in kv and len(kv) < max_items:
            kv[k] = v
    return kv


# ── 列表页文章条目识别 ─────────────────────────────────────────

_A_RE = re.compile(r"<a\s+([^>]*)>(.*?)</a>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_DATE_RE = re.compile(
    r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}|(\d+)[天日小时分钟]+前|"
    r"今天|昨天|前天|\d{1,2}:\d{2})")
_SKIP_LINK_RE = re.compile(
    r"(javascript:|^#$|^mailto:|login|register|app下载)", re.IGNORECASE)


def extract_article_items(html: str, base_url: str = "",
                          min_title_len: int = 6,
                          max_items: int = 100) -> List[Dict[str, str]]:
    """列表页识别：提取文章条目（标题/链接/日期）。

    启发式：链接文本 ≥ min_title_len 个字符、非导航词、
    相邻上下文含日期则一并提取。
    """
    if not html:
        return []
    items: List[Dict[str, str]] = []
    seen_titles = set()
    for m in _A_RE.finditer(html):
        attrs, inner = m.group(1), m.group(2)
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        href = hm.group(1).strip()
        if not href or _SKIP_LINK_RE.search(href):
            continue
        title = _clean(strip_tags(inner))
        if len(title) < min_title_len:
            continue
        if title in seen_titles:
            continue
        # 上下文 200 字符内找日期
        ctx = html[m.end():m.end() + 200]
        dm = _DATE_RE.search(strip_tags(ctx))
        item = {"title": title, "url": urljoin(base_url, href) if base_url else href}
        if dm:
            item["date"] = dm.group(1)
        seen_titles.add(title)
        items.append(item)
        if len(items) >= max_items:
            break
    return items


# ── 分页识别 ───────────────────────────────────────────────────

_NEXT_TEXT_RE = re.compile(r"(下一页|下页|更多|加载更多|next)", re.IGNORECASE)
_PAGE_URL_RE = re.compile(r"[?&](page|p|pn|pageNum|pageNo)=\d+", re.IGNORECASE)


def detect_pagination(html: str, base_url: str = "") -> Dict[str, Any]:
    """识别分页：返回下一页链接、是否存在页码参数、总页数估计。"""
    result: Dict[str, Any] = {"next_url": None, "has_page_param": False,
                              "max_page": 0}
    if not html:
        return result
    for m in _A_RE.finditer(html):
        attrs, inner = m.group(1), strip_tags(m.group(2))
        hm = _HREF_RE.search(attrs)
        if not hm:
            continue
        href = hm.group(1).strip()
        if _PAGE_URL_RE.search(href):
            result["has_page_param"] = True
            pm = re.search(r"[?&](?:page|p|pn|pageNum|pageNo)=(\d+)", href,
                           re.IGNORECASE)
            if pm:
                result["max_page"] = max(result["max_page"], int(pm.group(1)))
        if result["next_url"] is None and _NEXT_TEXT_RE.search(inner):
            result["next_url"] = urljoin(base_url, href) if base_url else href
    return result


# ── 数字与上下文抽取 ───────────────────────────────────────────

_NUM_RE = re.compile(
    r"(-?\d+(?:,\d{3})*(?:\.\d+)?)\s*(万亿|亿元|万元|亿|万|元|%|个百分点)?")


def extract_numbers_with_context(text: str, window: int = 20,
                                 max_items: int = 100) -> List[Dict[str, Any]]:
    """从正文抽取金融数字（金额/百分比）+ 前后上下文。"""
    out = []
    if not text:
        return out
    for m in _NUM_RE.finditer(text):
        raw, unit = m.group(1), m.group(2) or ""
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        lo = max(0, m.start() - window)
        hi = min(len(text), m.end() + window)
        ctx = _clean(text[lo:hi])
        out.append({"raw": raw + unit, "value": value, "unit": unit,
                    "context": ctx})
        if len(out) >= max_items:
            break
    return out


# ── 页面类型判别 ───────────────────────────────────────────────

def recognize_page_type(html: str, url: str = "") -> str:
    """判别页面类型：list / article / data / home。

    依据：文章条目数、表格数、键值块数、路径特征（纯启发式零依赖）。
    """
    if not html:
        return "unknown"
    path = urlparse(url).path.lower() if url else ""
    articles = extract_article_items(html, max_items=30)
    tables = extract_tables(html, max_tables=5)
    kv = extract_kv_blocks(html, max_items=50)

    if len(articles) >= 5:
        return "list"
    if len(tables) >= 2 or len(kv) >= 8 or re.search(
            r"(fund|stock|quote|data|行情)", path):
        return "data"
    text = strip_tags(html)
    if len(text) >= 300 and len(articles) < 5:
        return "article"
    if path in ("", "/") or re.search(r"(index|home|main)", path):
        return "home"
    return "home" if len(articles) <= 1 else "list"


# ── 一站式识别 ─────────────────────────────────────────────────

def smart_extract(html: str, url: str = "") -> Dict[str, Any]:
    """按页面类型自动组合识别能力，输出结构化结果。"""
    page_type = recognize_page_type(html, url)
    result: Dict[str, Any] = {
        "url": url, "page_type": page_type,
        "text": strip_tags(html)[:2000],
    }
    if page_type == "list":
        result["articles"] = extract_article_items(html, base_url=url)
        result["pagination"] = detect_pagination(html, base_url=url)
    elif page_type == "article":
        result["numbers"] = extract_numbers_with_context(result["text"])
    elif page_type == "data":
        result["tables"] = extract_tables(html)
        result["kv"] = extract_kv_blocks(html)
    else:
        result["articles"] = extract_article_items(html, base_url=url)
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python scripts/web_recognizer.py <url>")
        sys.exit(1)
    try:
        from .http_utils import http_get
    except ImportError:
        from http_utils import http_get  # type: ignore
    resp = http_get(sys.argv[1])
    if resp is None:
        print("抓取失败")
        sys.exit(1)
    import json as _json
    out = smart_extract(resp.text, url=sys.argv[1])
    out.pop("text", None)
    for k in ("articles",):
        if k in out:
            out[k] = out[k][:10]
    print(_json.dumps(out, ensure_ascii=False, indent=1)[:3000])
