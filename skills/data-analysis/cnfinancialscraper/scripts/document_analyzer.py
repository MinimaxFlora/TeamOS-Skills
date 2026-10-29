# -*- coding: utf-8 -*-
"""
金融文档分析整理模块
在 document_parser 基础上构建结构化分析层：
- 单文档深度分析（分类/元数据/章节/财务指标/风险因素/术语表）
- 批量整理目录（扫描+逐份分析+分类聚合+索引+汇总）
- 多文档并排对比（同维度提取+差异高亮）
零新外部依赖（复用 PyPDF2/python-docx/openpyxl，正则+启发式分类）。
"""

import re
import json
import math
import os as _os
from collections import Counter
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

try:
    from document_parser import parse_document, extract_financial_numbers
except ImportError:
    def parse_document(file_path, **kw):
        return {"error": "document_parser not available"}
    def extract_financial_numbers(text):
        return {}

SKILL_DATA_DIR = Path(__file__).parent.parent / "data"
DOC_INDEX_DIR = SKILL_DATA_DIR / "document_index"


# ==================== 文档分类 ====================

DOC_CATEGORIES = [
    ("券商研报", ["研究报告", "评级推荐", "目标价", "分析师", "投资建议", "证券研究报告"], 10),
    ("公告", ["公告编号", "证券代码", "临时公告", "重大事项"], 9),
    ("年报", ["年度报告", "annual report", "年报摘要"], 8),
    ("半年报", ["半年度报告", "半年报", "中期报告"], 7),
    ("季报", ["季度报告", "一季报", "三季报"], 6),
    ("基金合同", ["基金合同", "基金托管协议", "基金管理人"], 5),
    ("招募说明书", ["招募说明书", "招募更新"], 4),
    ("财务报告", ["财务报告", "财务报表", "审计报告", "合并报表"], 3),
    ("通用金融文档", [], 0),
]

SECTION_PATTERNS = {
    "h1": re.compile(r'第[一二三四五六七八九十百零\d]+[章节篇部]\s*[^\n]{0,80}'),
    "h2": re.compile(r'^[一二三四五六七八九十]+\s*[、.．]\s*[^\n]{0,80}', re.MULTILINE),
    "h3": re.compile(r'^\d+[\.．]\s*(?!\d)[^\n]{0,80}', re.MULTILINE),
    "h4": re.compile(r'^[（\(]\d+[）\)]\s*[^\n]{0,80}', re.MULTILINE),
}

# 机构名抽取的排除词：第三类兜底模式会把"仅供投资者参考"里的"仅供投资"当成机构。
_INST_REJECT_RE = re.compile(
    r'^(?:仅供|请勿|未经|不构成|不得|关于|本次|此外|以及|或者|应当|请投资者|特提醒)'
)

# 指标标签与数字之间允许出现的一切"合法噪声"：单位括号、英文缩写、冒号、系动词。
# 每一段都必须是显式可选的——早期的 [（(]?万元?[）)]? 把"万"写成了必选，
# 导致"营业收入1234.56亿元"这种最常见的写法一个都命中不了。
_IND_UNIT = r'(?:\s*[（(][^（）()\n]{0,8}[）)])?'
_IND_ABBR = r'(?:\s*[A-Za-z]{1,6})?'
_IND_CONN = r'\s*[:：]?\s*(?:为|约|达到|是|即)?\s*'
_IND_NUM = r'([+-]?\d[\d,]*\.?\d*)'
_IND_TAIL = r'\s*(?:亿|万)?\s*元?'
_IND_PCT = r'\s*[%％]?'


def _ind(label: str, tail: str = _IND_TAIL) -> str:
    # label 可能带 | ，必须包成非捕获组，否则 | 会把后面的数字段吞进右分支
    return r'(?:' + label + r')' + _IND_UNIT + _IND_ABBR + _IND_CONN + _IND_NUM + tail


FINANCIAL_INDICATOR_PATTERNS = {
    "营业收入": _ind(r'营业(?:总)?收入'),
    "净利润": _ind(r'(?:归属[于]?[^\n，,。]{0,10}的)?净利润'),
    "总资产": _ind(r'资产总计|总资产'),
    "净资产": _ind(r'归属于发行人股东的净资产|净资产'),
    "每股收益_EPS": _ind(r'(?:基本|稀释)?每股收益', r'\s*元?'),
    "市盈率_PE": _ind(r'市盈率', r'\s*(?:倍|PE)?'),
    "市净率_PB": _ind(r'市净率', r'\s*(?:倍|PB)?'),
    "净资产收益率_ROE": _ind(r'净资产收益率|加权净资产收益率', _IND_PCT),
    "毛利率": _ind(r'毛利率', _IND_PCT),
    "资产负债率": _ind(r'资产负债率', _IND_PCT),
    "同比增速": _ind(r'同比增长|同比下降|同比增速|同比增减', _IND_PCT),
}


# ==================== 辅助函数 ====================

def classify_document(text: str, file_path: str = "") -> Tuple[str, int]:
    """启发式文档分类。返回 (类别, 置信度 0-100)。"""
    text_head = (text or "")[:3000].lower()
    file_lower = Path(file_path).name.lower() if file_path else ""

    best_category = "通用金融文档"
    best_score = 0
    max_possible = 60

    for cat_name, keywords, priority in DOC_CATEGORIES:
        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if re.search(kw_lower, text_head):
                score += 15
            if file_lower and re.search(kw_lower, file_lower):
                score += 20
        if score > best_score:
            best_score = score
            best_category = cat_name

    confidence = min(100, int(best_score / max_possible * 100)) if max_possible > 0 else 10
    return best_category, max(confidence, 10)


def extract_metadata(text: str, file_path: str = "") -> Dict[str, str]:
    """从文本提取元数据：标题、机构、日期、股票代码。"""
    meta = {"title": "", "institution": "", "date": "", "stock_code": ""}
    text_head = (text or "")[:2000]

    lines = [l.strip() for l in text_head.split("\n") if l.strip()]
    if lines:
        meta["title"] = lines[0][:120]
    title_match = re.search(r'(?:标题|题目|报告名称)[：:]\s*(.+?)(?:\n|$)', text_head)
    if title_match:
        meta["title"] = title_match.group(1).strip()[:120]

    inst_patterns = [
        r'(?:发布机构|研究机构|出处|来源|作者单位|管理人|托管人)[：:]\s*([^\n，,。；;]{2,40})',
        r'([\u4e00-\u9fff]{2,12}(?:股份)?有限(?:公司|责任公司))',
        r'([\u4e00-\u9fff]{2,8}(?:证券|基金|期货|信托|银行|保险|资产管理|资本|投资|研究))(?:(?:股份)?有限(?:公司|责任公司))?',
    ]
    for pat in inst_patterns:
        m = re.search(pat, text_head)
        if m:
            inst = m.group(1).strip()
            if len(inst) >= 4 and not _INST_REJECT_RE.search(inst):
                meta["institution"] = inst
                break

    date_patterns = [
        r'(\d{4})\s*[年\-\/.]\s*(\d{1,2})\s*[月\-\/.]\s*(\d{1,2})\s*日?',
        r'(\d{4})-(\d{2})-(\d{2})',
        r'(?:报告日期|发布日期|公告日期|日期)[：:]\s*(\d{4}[\-\/.]\d{1,2}[\-\/.]\d{1,2})',
    ]
    for pat in date_patterns:
        m = re.search(pat, text_head)
        if m:
            if len(m.groups()) >= 3:
                y, mo, d = m.group(1), m.group(2), m.group(3)
                try:
                    meta["date"] = f"{int(y)}-{int(mo):02d}-{int(d):02d}"
                except ValueError:
                    pass
            elif len(m.groups()) == 1:
                meta["date"] = m.group(1)
            break

    code_match = re.search(r'(?:股票代码|证券代码|标的代码|code)[：:]\s*(\d{6})', text_head)
    if code_match:
        meta["stock_code"] = code_match.group(1)
    else:
        code_match2 = re.search(r'[\(（](\d{6})[\._\s]?(?:SH|SZ|sh|sz|上证|深证)?[\)）]', text_head)
        if code_match2:
            meta["stock_code"] = code_match2.group(1)

    return meta


def extract_sections(text: str) -> List[Dict[str, Any]]:
    """提取文档章节结构。"""
    sections = []
    seen_titles = set()
    lines = text.split("\n")

    for line_no, line in enumerate(lines):
        line = line.strip()
        if not line or len(line) < 3:
            continue
        if len(line) > 150:
            continue

        level = None
        title = None

        for lvl_name, pattern in SECTION_PATTERNS.items():
            m = pattern.match(line)
            if m:
                title = m.group(0).strip()
                if title.replace(" ", "").replace(".", "").replace("．", "").isdigit():
                    continue
                level = int(lvl_name[1])
                break

        if title and level:
            norm = re.sub(r'\s+', '', title)
            if norm in seen_titles:
                continue
            seen_titles.add(norm)

            preview_start = line_no + 1
            preview_parts = []
            for l in lines[preview_start:preview_start + 5]:
                if l.strip():
                    preview_parts.append(l.strip())
            preview = " ".join(preview_parts)[:150]

            sections.append({
                "title": title,
                "level": level,
                "preview": preview,
                "line": line_no + 1,
            })

    return sections[:60]


_RISK_HEAD_RE = re.compile(
    r'(?:重大风险提示|风险因素|风险提示|风险分析|风险警示|可能面对的风险)'
)
_RISK_ENUM_LEAD_RE = re.compile(
    r'^\s*[（\(]?\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[）\)]?\s*[、.．)）]?\s*'
)
_RISK_ENUM_SPLIT_RE = re.compile(
    r'(?<=[\s；;。，,：:])\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[、.．)）]\s*'
)
_RISK_CUE_RE = re.compile(
    r'风险|不利|下滑|下降|波动|不确定|减值|违约|逾期|处罚|诉讼|仲裁|监管|'
    r'压力|亏损|涨价|短缺|退市|警示|整改|竞争|汇率|利率|政策|集中'
)
_RISK_STOP_RE = re.compile(
    r'第[一二三四五六七八九十\d]+[章节部分篇]|重要提示|释义|备查文件'
)


def _risk_item_clean(raw: str, min_len: int = 4) -> str:
    s = (raw or "").strip().lstrip("•·●◆■->》】 \t")
    s = re.sub(r'\s+', ' ', s).strip(" ：:，,、。；;")
    s = re.sub(r'\s+', ' ', _RISK_ENUM_LEAD_RE.sub('', s)).strip(" ：:，,、。；;")
    if len(s) < min_len or len(s) > 200:
        return ""
    if _RISK_STOP_RE.search(s) or not _RISK_CUE_RE.search(s):
        return ""
    return s


def _risk_items_from_chunk(chunk: str) -> List[str]:
    """先认显式列表项，没有列表再按行内序号/分号拆。

    公告正文里"风险提示：1. 市场风险 2. 原材料风险"挤在同一行是最常见写法，
    早期版本只匹配行首编号，这种一条都提不出来。
    """
    items = re.findall(r'[•·●◆■]\s*([^\n•·●◆■]{4,200})', chunk)
    if not items:
        items = re.findall(
            r'(?:^|\n)\s*(?:[（\(]?\s*(?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*[）\)]?\s*[、.．)）])'
            r'\s*([^\n]{4,200})',
            chunk,
        )
    if not items:
        items = []
        for seg in re.split(r'[；;。\n]', chunk):
            seg = seg.strip()
            if not seg:
                continue
            items.extend(_RISK_ENUM_SPLIT_RE.split(seg))

    out: List[str] = []
    for raw in items:
        cleaned = _risk_item_clean(raw)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def extract_risk_factors(text: str) -> List[str]:
    """提取风险因素/风险提示条目。"""
    text = text or ""
    risks: List[str] = []

    for head in _RISK_HEAD_RE.finditer(text):
        chunk = text[head.end():head.end() + 3000]
        stop = _RISK_STOP_RE.search(chunk)
        if stop:
            chunk = chunk[:stop.start()]
        for item in _risk_items_from_chunk(chunk):
            if item not in risks:
                risks.append(item)
        if len(risks) >= 20:
            break

    if not risks:
        # 没有"风险"小标题：先捡显式项目符号里带"风险"字样的条目，
        # 再退到逐句扫描。没有小标题做锚点时必须收紧，否则会把
        # "财务亮点"之类的普通列表也当成风险。
        for bullet in re.findall(r'[•·●◆■]\s*([^\n•·●◆■]{4,200})', text):
            cleaned = _risk_item_clean(bullet)
            if cleaned and "风险" in cleaned and cleaned not in risks:
                risks.append(cleaned)

    if not risks:
        for seg in re.split(r'[；;。\n]', text):
            cleaned = _risk_item_clean(seg, min_len=12)
            if cleaned and cleaned not in risks:
                risks.append(cleaned)
            if len(risks) >= 10:
                break

    return risks[:20]


def extract_glossary(text: str) -> Dict[str, str]:
    """提取术语定义：匹配 "XX 指/系/是指/谓/意为" 模式。"""
    glossary = {}
    pattern = re.compile(
        r'([\u4e00-\u9fffA-Za-z]{2,15})\s*(?:指|系|是指|谓|意为|即|又称)\s*(.{5,120}?)(?:[。；;]|$|\n)'
    )
    for m in re.finditer(pattern, text[:5000]):
        term = m.group(1).strip()
        definition = m.group(2).strip()
        if term and definition and term not in glossary:
            glossary[term] = definition[:120]

    return dict(list(glossary.items())[:30])


def extract_financial_indicators(text: str) -> Dict[str, Any]:
    """扩展的财务指标提取（比 extract_financial_numbers 更全面）。"""
    indicators = {}
    for key, pattern in FINANCIAL_INDICATOR_PATTERNS.items():
        m = re.search(pattern, text)
        if m:
            try:
                val = m.group(1).replace(",", "")
                indicators[key] = float(val) if '.' in val else int(val)
            except (ValueError, AttributeError, IndexError):
                pass
    return indicators


# ==================== 摘要生成 ====================

_SENT_BREAKS = "。！？；\n\r"
_BOILERPLATE_RE = re.compile(
    r"免责声明|仅供.{0,8}参考|不构成.{0,10}(?:投资建议|意见|依据)|版权所有|"
    r"未经许可|禁止转载|第\s*\d+\s*页"
)
_NUMERIC_FACT_RE = re.compile(r"\d[\d,.]*\s*(?:亿|万|%|％|元|个百分点|倍)")


def _cjk_bigrams(s: str) -> List[str]:
    """取中文连续段的全部二字串；英文与数字不参与主题显著度，避免噪声。"""
    out: List[str] = []
    for run in re.findall(r'[\u4e00-\u9fff]{2,}', s or ""):
        out.extend(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _split_sentences(text: str, min_len: int = 8) -> List[Tuple[int, str]]:
    """按中文句读切句，返回 (原文偏移, 句子)。偏移用于还原原文顺序。"""
    out: List[Tuple[int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in _SENT_BREAKS:
            seg = text[start:i].strip()
            if len(seg) >= min_len:
                out.append((start, seg))
            start = i + 1
    tail = text[start:].strip()
    if len(tail) >= min_len:
        out.append((start, tail))
    return out


def _score_sentences(sentences: List[Tuple[int, str]],
                     bg_freq: Counter,
                     title: str = "") -> List[Tuple[float, int, str]]:
    """显著度打分：主题二字串密度 + 数字事实 + 篇首加成，免责声明与超长句降权。"""
    scored: List[Tuple[float, int, str]] = []
    for idx, (_, sent) in enumerate(sentences):
        grams = _cjk_bigrams(sent)
        if not grams:
            continue
        score = sum(bg_freq[g] for g in grams) / math.sqrt(len(grams))
        if title and any(g in title for g in set(grams)):
            score += 3.0
        if _NUMERIC_FACT_RE.search(sent):
            score += 2.0
        if idx == 0:
            score += 2.0
        elif idx <= 2:
            score += 1.0
        if _BOILERPLATE_RE.search(sent):
            score *= 0.2
        if len(sent) > 120:
            score *= 0.7
        scored.append((score, idx, sent))
    return scored


def _select_top_sentences(scored: List[Tuple[float, int, str]],
                          *,
                          max_sentences: int = 5,
                          max_chars: int = 320) -> List[str]:
    """按显著度取句，二字串重合过高的视为重复丢弃，最后按原文顺序输出。"""
    chosen: List[Tuple[int, str, set]] = []
    used = 0
    for score, idx, sent in sorted(scored, key=lambda x: -x[0]):
        if len(chosen) >= max_sentences or used >= max_chars:
            break
        grams = set(_cjk_bigrams(sent))
        if not grams:
            continue
        dup = False
        for _, _, other in chosen:
            overlap = len(grams & other) / max(1, min(len(grams), len(other)))
            if overlap > 0.6:
                dup = True
                break
        if dup:
            continue
        chosen.append((idx, sent, grams))
        used += len(sent)
    return [s for _, s, _ in sorted(chosen, key=lambda x: x[0])]


def _is_heading_line(s: str) -> bool:
    """是否是纯标题行——章节标题已经进 sections 了，不该再占摘要的位。"""
    return any(p.match(s) for p in SECTION_PATTERNS.values())


def build_document_summary(text: str,
                           *,
                           category: str = "",
                           metadata: Optional[Dict[str, Any]] = None,
                           sections: Optional[List[Dict[str, Any]]] = None,
                           indicators: Optional[Dict[str, Any]] = None,
                           risks: Optional[List[str]] = None,
                           glossary: Optional[Dict[str, str]] = None,
                           max_chars: int = 600) -> str:
    """用已经算出来的元数据/章节/指标/风险拼一份带标签的摘要。

    正文部分走抽取式取句而不是从头截断——金融文档开头多是封面与免责，
    前 600 字往往一个有效信息都没有。结构信息不足时才回退到头部原文。
    """
    meta = metadata or {}
    title = str(meta.get("title") or "").strip()
    parts: List[str] = []

    head_bits = [str(meta.get(k) or "").strip() for k in ("institution", "date")]
    if meta.get("stock_code"):
        head_bits.append(f"代码 {meta['stock_code']}")
    head = f"【{category}】{title}" if category else title
    if head_bits:
        head += "｜" + " · ".join(b for b in head_bits if b)
    if head.strip("｜【】 "):
        parts.append(re.sub(r"\s+", " ", head).strip())

    sentences = [
        (off, s) for off, s in _split_sentences(text)
        if len(s) > 16 and s != title and not _is_heading_line(s)
    ]
    if sentences:
        bg_freq: Counter = Counter(_cjk_bigrams(text))
        bullets = _select_top_sentences(_score_sentences(sentences, bg_freq, title))
        if bullets:
            parts.append("要点：" + " ".join(
                f"{i + 1}) {b[:100]}" for i, b in enumerate(bullets)))

    if sections:
        names = [str(s.get("title") or "").strip() for s in sections[:4] if s.get("title")]
        if names:
            more = f"…共 {len(sections)} 节" if len(sections) > len(names) else ""
            parts.append("章节：" + " / ".join(names[:4]) + more)

    if indicators:
        items = list(indicators.items())[:6]
        parts.append("指标：" + "、".join(f"{k} {v}" for k, v in items))

    if risks:
        parts.append(f"风险：{len(risks)} 条，首要「{str(risks[0])[:40]}」")

    if glossary:
        parts.append(f"术语：{len(glossary)} 条（如 {'、'.join(list(glossary)[:3])}）")

    if len(parts) <= 1:
        head_text = re.sub(r"\s+", " ", (text or "")[:max_chars]).strip()
        return head_text or (title or "（无法生成摘要）")

    out = ""
    for p in parts:
        candidate = f"{out}\n{p}" if out else p
        if len(candidate) > max_chars:
            room = max_chars - len(out) - 1
            if room > 40:
                out = f"{out}\n{p[:room]}…"
            break
        out = candidate
    return out


# ==================== DocumentAnalyzer 类 ====================

class DocumentAnalyzer:
    """金融文档分析整理器。"""

    def __init__(self):
        self._cache = {}

    def analyze(self, file_path: str) -> Dict[str, Any]:
        """
        单文档深度分析。
        返回：{file_path, file_type, doc_category, category_confidence,
               metadata, summary, sections, financial_indicators,
               risk_factors, glossary, stats}
        """
        cache_key = _os.path.abspath(file_path)
        if cache_key in self._cache:
            return self._cache[cache_key]

        result = {
            "file_path": file_path,
            "file_type": Path(file_path).suffix.lower().lstrip("."),
            "doc_category": "通用金融文档",
            "category_confidence": 0,
            "metadata": {},
            "summary": "",
            "sections": [],
            "financial_indicators": {},
            "risk_factors": [],
            "glossary": {},
            "stats": {},
        }

        parsed = parse_document(file_path)
        if parsed.get("error"):
            result["error"] = parsed["error"]
            return result

        text_content = parsed.get("text_content", "") or ""
        if not text_content.strip():
            result["error"] = "文档内容为空或无法提取文本"
            return result

        category, confidence = classify_document(text_content, file_path)
        result["doc_category"] = category
        result["category_confidence"] = confidence
        result["metadata"] = extract_metadata(text_content, file_path)
        result["sections"] = extract_sections(text_content)
        result["financial_indicators"] = extract_financial_indicators(text_content)
        # 合并 document_parser 的简单数字提取
        simple_nums = extract_financial_numbers(text_content)
        for k, v in simple_nums.items():
            if k not in result["financial_indicators"] and k not in ("amounts_yi", "total_yi", "percentages"):
                result["financial_indicators"][k] = v
        result["risk_factors"] = extract_risk_factors(text_content)
        result["glossary"] = extract_glossary(text_content)
        result["summary"] = build_document_summary(
            text_content,
            category=category,
            metadata=result["metadata"],
            sections=result["sections"],
            indicators=result["financial_indicators"],
            risks=result["risk_factors"],
            glossary=result["glossary"],
        )
        result["stats"] = {
            "char_count": len(text_content),
            "section_count": len(result["sections"]),
            "indicator_count": len(result["financial_indicators"]),
            "risk_count": len(result["risk_factors"]),
            "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        self._cache[cache_key] = result
        return result

    def organize_directory(self, dir_path: str, recursive: bool = True) -> Dict[str, Any]:
        """
        批量整理文档目录。
        返回：{total, categories, index_file, summary_file}
        """
        dir_p = Path(dir_path)
        if not dir_p.is_dir():
            return {"error": f"目录不存在: {dir_path}"}

        supported_ext = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".csv"}
        files = []
        if recursive:
            for f in dir_p.rglob("*"):
                if f.is_file() and f.suffix.lower() in supported_ext:
                    files.append(f)
        else:
            for f in dir_p.iterdir():
                if f.is_file() and f.suffix.lower() in supported_ext:
                    files.append(f)

        if not files:
            return {"error": "目录中没有支持的文档文件", "total": 0, "categories": {}}

        categories: Dict[str, List[Dict]] = {}
        for fp in files:
            try:
                analysis = self.analyze(str(fp))
                cat = analysis.get("doc_category", "通用金融文档")
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append({
                    "file": fp.name,
                    "path": str(fp),
                    "type": analysis.get("file_type", ""),
                    "metadata": analysis.get("metadata", {}),
                    "confidence": analysis.get("category_confidence", 0),
                    "indicator_count": len(analysis.get("financial_indicators", {})),
                })
            except Exception as e:
                if "处理错误" not in categories:
                    categories["处理错误"] = []
                categories["处理错误"].append({"file": fp.name, "error": str(e)})

        total = sum(len(v) for v in categories.values())

        DOC_INDEX_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        index_file = DOC_INDEX_DIR / f"index_{timestamp}.json"
        summary_file = DOC_INDEX_DIR / f"summary_{timestamp}.md"

        index_data = {
            "source_dir": str(dir_p),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_documents": total,
            "category_count": len(categories),
            "categories": {},
        }
        for cat, items in categories.items():
            index_data["categories"][cat] = {"count": len(items), "files": items}
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)

        md_lines = [
            "# 文档目录整理报告\n",
            f"- 来源目录: {dir_p}",
            f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 文档总数: {total}",
            f"- 类别数: {len(categories)}\n",
        ]
        for cat, items in sorted(categories.items(), key=lambda x: -len(x[1])):
            md_lines.append(f"## {cat} ({len(items)}份)")
            for item in items[:50]:
                meta = item.get("metadata", {})
                title = meta.get("title", item["file"])[:80]
                date_str = meta.get("date", "")
                inst = meta.get("institution", "")
                code = meta.get("stock_code", "")
                extras = " / ".join(filter(None, [date_str, inst, code]))
                line = f"- **{title}** ({item['type']})"
                if extras:
                    line += f" — {extras}"
                md_lines.append(line)
            md_lines.append("")
        with open(summary_file, "w", encoding="utf-8") as f:
            f.write("\n".join(md_lines))

        return {
            "total": total,
            "categories": {cat: len(items) for cat, items in categories.items()},
            "index_file": str(index_file),
            "summary_file": str(summary_file),
        }

    def compare(self, file_paths: List[str]) -> Dict[str, Any]:
        """多文档并排对比。"""
        results = []
        for fp in file_paths:
            r = self.analyze(fp)
            results.append(r)

        if not results:
            return {"compared": 0, "files": [], "dimensions": {}, "differences": []}

        dimensions = {
            "文档类型": [], "文件类型": [], "日期": [], "机构": [],
            "股票代码": [], "文档规模（字符数）": [], "章节数": [],
            "财务指标数": [], "风险因素数": [], "关键指标": [],
        }

        for r in results:
            dimensions["文档类型"].append(r.get("doc_category", ""))
            dimensions["文件类型"].append(r.get("file_type", ""))
            dimensions["日期"].append(r.get("metadata", {}).get("date", ""))
            dimensions["机构"].append(r.get("metadata", {}).get("institution", ""))
            dimensions["股票代码"].append(r.get("metadata", {}).get("stock_code", ""))
            dimensions["文档规模（字符数）"].append(r.get("stats", {}).get("char_count", 0))
            dimensions["章节数"].append(r.get("stats", {}).get("section_count", 0))
            dimensions["财务指标数"].append(r.get("stats", {}).get("indicator_count", 0))
            dimensions["风险因素数"].append(r.get("stats", {}).get("risk_count", 0))
            dimensions["关键指标"].append(r.get("financial_indicators", {}))

        differences = []
        for dim, values in dimensions.items():
            if dim == "关键指标":
                continue
            unique_vals = set(str(v) for v in values)
            if len(unique_vals) > 1:
                file_names = [Path(fp).name for fp in file_paths]
                labeled = [f"{file_names[i]}: {values[i]}" for i in range(len(values))]
                differences.append(f"[{dim}] {', '.join(labeled)}")

        return {
            "compared": len(results),
            "files": [
                {"name": Path(fp).name, "path": fp,
                 "category": r.get("doc_category", ""),
                 "summary": r.get("summary", "")[:200]}
                for fp, r in zip(file_paths, results)
            ],
            "dimensions": dimensions,
            "differences": differences,
        }

    def to_markdown(self, analysis: Dict[str, Any]) -> str:
        """将 analyze() 结果转换为结构化 Markdown 报告。"""
        if analysis.get("error"):
            return f"## 文档分析失败\n\n错误: {analysis['error']}"

        lines = [
            "# 文档分析报告\n",
            f"- **文件**: {Path(analysis['file_path']).name}",
            f"- **格式**: {analysis.get('file_type', '').upper()}",
            f"- **分类**: {analysis.get('doc_category', '未知')} (置信度: {analysis.get('category_confidence', 0)}%)",
            f"- **分析时间**: {analysis.get('stats', {}).get('analyzed_at', '')}\n",
        ]

        meta = analysis.get("metadata", {})
        if any(meta.values()):
            lines.append("## 元数据\n")
            if meta.get("title"):
                lines.append(f"- **标题**: {meta['title']}")
            if meta.get("institution"):
                lines.append(f"- **机构**: {meta['institution']}")
            if meta.get("date"):
                lines.append(f"- **日期**: {meta['date']}")
            if meta.get("stock_code"):
                lines.append(f"- **股票代码**: {meta['stock_code']}")
            lines.append("")

        lines.append("## 内容摘要\n")
        summary_text = str(analysis.get("summary") or "无法提取摘要")[:600]
        for s_line in (summary_text.splitlines() or ["无法提取摘要"]):
            lines.append(f"> {s_line}")
        lines.append("")

        sections = analysis.get("sections", [])
        if sections:
            lines.append(f"## 章节结构 ({len(sections)} 个章节)\n")
            for sec in sections[:30]:
                indent = "  " * (sec.get("level", 1) - 1)
                preview = sec.get('preview', '')
                lines.append(f"{indent}- **{sec['title']}**  _{preview[:80].replace(chr(10), ' ')}_\n")
            lines.append("")

        indicators = analysis.get("financial_indicators", {})
        if indicators:
            lines.append(f"## 财务指标 ({len(indicators)} 项)\n")
            for key, value in indicators.items():
                if key not in ("amounts_yi", "total_yi", "percentages"):
                    lines.append(f"- **{key}**: {value}")
            lines.append("")

        risks = analysis.get("risk_factors", [])
        if risks:
            lines.append(f"## 风险因素 ({len(risks)} 条)\n")
            for i, risk in enumerate(risks, 1):
                lines.append(f"{i}. {risk}")
            lines.append("")

        glossary = analysis.get("glossary", {})
        if glossary:
            lines.append(f"## 关键术语 ({len(glossary)} 条)\n")
            for term, defn in list(glossary.items())[:15]:
                lines.append(f"- **{term}**: {defn}")

        stats = analysis.get("stats", {})
        lines.append(f"\n---\n*文档字符数: {stats.get('char_count', 0):,}  |  "
                     f"章节: {stats.get('section_count', 0)}  |  "
                     f"指标: {stats.get('indicator_count', 0)}  |  "
                     f"风险: {stats.get('risk_count', 0)}*")

        return "\n".join(lines)


# ==================== CLI 入口 ====================

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("用法: python document_analyzer.py <命令> <参数>")
        print("  analyze <文件路径>       -- 深度分析单份文档")
        print("  organize <目录路径>      -- 批量整理文档目录")
        print("  compare <文件1,文件2,...> -- 对比多份文档")
        print("  markdown <文件路径>      -- 深度分析并以 Markdown 输出")
        sys.exit(1)

    cmd = sys.argv[1]
    target = sys.argv[2]
    analyzer = DocumentAnalyzer()

    if cmd == "analyze":
        result = analyzer.analyze(target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "organize":
        result = analyzer.organize_directory(target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "compare":
        paths = [p.strip() for p in target.split(",") if p.strip()]
        result = analyzer.compare(paths)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif cmd == "markdown":
        result = analyzer.analyze(target)
        print(analyzer.to_markdown(result))
    else:
        print(f"未知命令: {cmd}")
        sys.exit(1)
