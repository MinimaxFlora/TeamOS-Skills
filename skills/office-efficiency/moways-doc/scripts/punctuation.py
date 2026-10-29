#!/usr/bin/env python3
"""
中文标点符号处理模块（依据国标 GB/T 15834—2011）

提供两个核心函数：
- fix_chinese_punctuation(text): 修正中文上下文里的半角标点，返回 (new_text, change_count)
- audit_chinese_punctuation(text): 检测中文上下文里的标点问题，返回 issue 列表

设计原则：
1. 标点两侧任一为中文 → 该标点必须全角
2. 白名单不动：URL、邮箱、文件路径、版本号、千分位、小数点、行内代码
3. 自动修正只做"绝对安全"的项；不确定的项留给 audit 报告由人判断
"""

import re

# =============================================================================
# 字符集与白名单正则
# =============================================================================

CJK = r'一-鿿'
RE_CJK_CHAR = re.compile(f'[{CJK}]')

# 白名单：扫描前先把这些区域用 \x00N\x00 占位符替换，避免误改
RE_URL = re.compile(r'https?://[^\s一-鿿]+')
RE_EMAIL = re.compile(r'\b[\w.+-]+@[\w.-]+\.\w+\b')
RE_PATH_WIN = re.compile(r'[A-Za-z]:\\[^\s一-鿿]*')
RE_PATH_UNIX = re.compile(r'(?<![/\w])/[\w./-]+/[\w./-]+')
RE_VERSION = re.compile(r'\bv?\d+\.\d+(?:\.\d+)+\b')  # 如 1.2.3, v3.11.0
RE_INLINE_CODE = re.compile(r'`[^`\n]+`')
RE_DECIMAL = re.compile(r'\d+\.\d+')  # 3.14, 1.5
RE_THOUSAND = re.compile(r'\d{1,3}(?:,\d{3})+')  # 1,500,000
RE_ENGLISH_ABBR = re.compile(r'\b(?:Inc|Ltd|Co|Corp|No|Dr|Mr|Mrs|Ms|Prof|Vol|Fig|St|Ave)\.(?=\s|$)')
RE_TIME = re.compile(r'\b\d{1,2}:\d{2}(?::\d{2})?\b')
RE_DATE_ISO = re.compile(r'\b\d{4}-\d{2}-\d{2}\b')
RE_PAREN_REF = re.compile(r'\([A-Z][a-z]+(?:\s*&\s*[A-Z][a-z]+)?,\s*\d{4}[a-z]?\)')  # (Smith, 2020) (Smith & Jones, 2020a)
# 注意：不在白名单内放"括号内全是英文缩写"，因为前后中文上下文也要求全角括号
# 仅当上下文也是英文时，才不动半角括号——这由 paren 规则的 outer-CJK 检查兜底

_WHITELIST_PATTERNS = [
    RE_URL, RE_EMAIL, RE_PATH_WIN, RE_PATH_UNIX, RE_VERSION,
    RE_INLINE_CODE, RE_DECIMAL, RE_THOUSAND, RE_ENGLISH_ABBR,
    RE_TIME, RE_DATE_ISO, RE_PAREN_REF,
]


def _mask_whitelist(text):
    """把白名单区域用占位符 \\x00N\\x00 替换。返回 (masked_text, masks_list)。"""
    masks = []

    def _mask(m):
        masks.append(m.group(0))
        return f'\x00{len(masks) - 1}\x00'

    out = text
    for pat in _WHITELIST_PATTERNS:
        out = pat.sub(_mask, out)
    return out, masks


def _unmask(text, masks):
    """还原占位符。"""
    def _u(m):
        idx = int(m.group(1))
        return masks[idx] if 0 <= idx < len(masks) else m.group(0)
    return re.sub(r'\x00(\d+)\x00', _u, text)


def _has_cjk(s):
    return bool(RE_CJK_CHAR.search(s))


def _is_cjk_char(c):
    return bool(c) and '一' <= c <= '鿿'


# =============================================================================
# 配对引号转换
# =============================================================================

def _convert_paired_quotes(text, ascii_quote, left, right):
    """把 ASCII 直引号 "xxx" 配对转换为弯引号 "xxx"。

    只在配对的两个直引号之间含至少一个 CJK 字符时才转换。
    奇数个直引号（无法配对）保留不动。
    """
    result = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == ascii_quote:
            # 找到下一个同类直引号
            j = i + 1
            has_cjk_inside = False
            while j < n and text[j] != ascii_quote:
                if _is_cjk_char(text[j]):
                    has_cjk_inside = True
                j += 1
            if j < n and has_cjk_inside:
                # 配对成功且含中文：替换
                result.append(left)
                result.append(text[i + 1:j])
                result.append(right)
                i = j + 1
                continue
        result.append(c)
        i += 1
    return ''.join(result)


# =============================================================================
# 自动修正
# =============================================================================

_FULLWIDTH_MAP = {',': '，', '.': '。', ';': '；', ':': '：', '?': '？', '!': '！'}


def _final_sweep(masked, window=10):
    """扫尾：在中文上下文里的半角 , . ; : ? ! 全部全角化。
    保护小数点（数字.数字）和时间冒号（数字:数字）。
    占位符 \x00N\x00 内部不动。"""
    chars = list(masked)
    n = len(chars)
    for i in range(n):
        c = chars[i]
        if c not in _FULLWIDTH_MAP:
            continue
        # 不动占位符内部
        if chars[i - 1] == '\x00' if i > 0 else False:
            continue
        # 检查 ±window 范围内是否有 CJK
        start = max(0, i - window)
        end = min(n, i + window + 1)
        has_cjk = False
        for j in range(start, end):
            if j != i and '一' <= chars[j] <= '鿿':
                has_cjk = True
                break
        if not has_cjk:
            continue
        # 小数点保护
        if c == '.' and i > 0 and i + 1 < n and chars[i - 1].isdigit() and chars[i + 1].isdigit():
            continue
        # 三级标题序号点保护（行首形如 "1." "2." "12." 等）
        if c == '.' and i > 0 and chars[i - 1].isdigit():
            j = i - 1
            while j >= 0 and chars[j].isdigit():
                j -= 1
            if j < 0 or chars[j] in '\n\r\t ':
                continue
        # 时间冒号保护
        if c == ':' and i > 0 and i + 1 < n and chars[i - 1].isdigit() and chars[i + 1].isdigit():
            continue
        chars[i] = _FULLWIDTH_MAP[c]
    return ''.join(chars)


def fix_chinese_punctuation(text):
    """
    修正中文上下文里的半角标点。

    Returns:
        (new_text, change_count)
    """
    if not text or not _has_cjk(text):
        return text, 0

    masked, masks = _mask_whitelist(text)
    original = masked

    # ---- 必须先处理多字符标点，避免被单字符规则拆掉 ----

    # M1. 三个或更多英文点 → 中文省略号 ……（两侧含中文时）
    masked = re.sub(rf'([{CJK}])\s*\.{{3,}}\s*', r'\1……', masked)
    masked = re.sub(rf'\s*\.{{3,}}\s*([{CJK}])', r'……\1', masked)
    # M1b. 中文里独立出现的 \.{3,} (不与CJK直接相邻但段内有中文) 也替换
    if _has_cjk(masked):
        masked = re.sub(r'\.{3,}', '……', masked)

    # M2. 双（或更多）连字符 → 破折号 ——（两侧含中文时）
    masked = re.sub(rf'([{CJK}])\s*-{{2,}}\s*', r'\1——', masked)
    masked = re.sub(rf'\s*-{{2,}}\s*([{CJK}])', r'——\1', masked)

    # ---- 单字符半角标点 ----

    # 1. 半角逗号 ↔ 全角
    masked = re.sub(rf'([{CJK}]),', r'\1，', masked)
    masked = re.sub(rf',([{CJK}])', r'，\1', masked)

    # 2. 半角句号（小数点已 mask，所以这里 \d 检查仍然必要以防漏网）
    masked = re.sub(rf'([{CJK}])\.(?!\d)', r'\1。', masked)
    masked = re.sub(rf'(?<!\d)\.([{CJK}])', r'。\1', masked)

    # 3. 半角分号
    masked = re.sub(rf'([{CJK}]);', r'\1；', masked)
    masked = re.sub(rf';([{CJK}])', r'；\1', masked)

    # 4. 半角冒号（时间格式已 mask）
    masked = re.sub(rf'([{CJK}]):(?!\d)', r'\1：', masked)
    masked = re.sub(rf'(?<!\d):([{CJK}])', r'：\1', masked)

    # 5. 半角问号
    masked = re.sub(rf'([{CJK}])\?', r'\1？', masked)
    masked = re.sub(rf'\?([{CJK}])', r'？\1', masked)

    # 6. 半角叹号
    masked = re.sub(rf'([{CJK}])!', r'\1！', masked)
    masked = re.sub(rf'!([{CJK}])', r'！\1', masked)

    # 7. ASCII 直引号 → 弯引号（配对替换）
    masked = _convert_paired_quotes(masked, '"', '“', '”')
    masked = _convert_paired_quotes(masked, "'", '‘', '’')

    # 8. 半角括号 → 全角括号
    #    规则：括号内含 CJK，或前/后紧邻字符是 CJK
    def _replace_paren(m):
        inner = m.group(1)
        s, e = m.start(), m.end()
        before_char = masked[s - 1] if s > 0 else ''
        after_char = masked[e] if e < len(masked) else ''
        has_cjk_outside = _is_cjk_char(before_char) or _is_cjk_char(after_char)
        has_cjk_inside = RE_CJK_CHAR.search(inner)
        # 学术引用格式 (Smith, 2020) 不动
        if re.match(r'^[A-Z][a-z]+,\s*\d{4}$', inner):
            return m.group(0)
        if has_cjk_inside or has_cjk_outside:
            return f'（{inner}）'
        return m.group(0)
    masked = re.sub(r'\(([^()\n]+)\)', _replace_paren, masked)

    # 9. 尖括号冒充书名号（启发式：内部含中文且长度 ≤ 40，无可疑符号）
    def _replace_angle(m):
        inner = m.group(1)
        if (_has_cjk(inner)
                and len(inner) <= 40
                and not re.search(r'[<>="\'/]', inner)):
            return f'《{inner}》'
        return m.group(0)
    masked = re.sub(r'<([^<>\n]{1,40})>', _replace_angle, masked)

    # 10. 数字之间 ~ → ～
    masked = re.sub(r'(\d)~(\d)', r'\1～\2', masked)

    # 11. 三级标题序号 1、 → 1. （行首）
    masked = re.sub(r'(?m)^(\d+)、', r'\1.', masked)

    # 12. 书名号、引号间多余的顿号
    masked = re.sub(r'》\s*、\s*《', '》《', masked)
    masked = re.sub(r'[”]\s*、\s*[“]', '”“', masked)

    # 13. 百分号前空格
    masked = re.sub(r'(\d)\s+%', r'\1%', masked)

    # ---- 后处理：全角标点后多余的空格（紧跟 CJK 时）去掉 ----
    for p in '，。；：！？、）》”':
        masked = re.sub(rf'{p}[ \t]+(?=[{CJK}])', p, masked)

    # 扫尾：在中文上下文里 ±10 字符内有 CJK 的半角标点全部全角化
    masked = _final_sweep(masked)

    # 后处理（再跑一次）：全角标点后多余的空格（紧跟 CJK 时）去掉
    for p in '，。；：！？、）》”':
        masked = re.sub(rf'{p}[ \t]+(?=[{CJK}])', p, masked)

    # 还原白名单占位符
    new_text = _unmask(masked, masks)

    if new_text == text:
        return text, 0

    # 粗略统计差异字符数
    changes = sum(1 for a, b in zip(text, new_text) if a != b)
    changes += abs(len(new_text) - len(text))

    return new_text, changes


# =============================================================================
# 标点审查
# =============================================================================

class PunctIssue:
    """一条标点问题"""
    __slots__ = ('severity', 'code', 'message', 'suggestion', 'sample')

    def __init__(self, severity, code, message, suggestion='', sample=''):
        self.severity = severity
        self.code = code
        self.message = message
        self.suggestion = suggestion
        self.sample = sample

    def to_dict(self):
        return {
            'severity': self.severity,
            'code': self.code,
            'message': self.message,
            'suggestion': self.suggestion,
            'sample': self.sample,
        }


def _add(issues, sev, code, msg, sugg, sample=''):
    """去重添加：同一段内同一 code 只报一次。"""
    if any(i.code == code for i in issues):
        return
    issues.append(PunctIssue(sev, code, msg, sugg, sample))


def audit_chinese_punctuation(text):
    """
    检测中文上下文里的标点问题。

    Returns:
        list[PunctIssue]
    """
    issues = []
    if not text or not _has_cjk(text):
        return issues

    masked, _ = _mask_whitelist(text)

    # P1 半角逗号
    m = re.search(rf'[{CJK}],|,[{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P1',
             '中文上下文中出现半角逗号 ","',
             '改为全角逗号"，"或顿号"、"（并列项）', sample=m.group(0))

    # P2 半角句号
    m = re.search(rf'[{CJK}]\.(?!\d)|(?<!\d)\.[{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P2',
             '中文上下文中出现半角句号 "."',
             '改为全角句号"。"', sample=m.group(0))

    # P3 半角分号
    m = re.search(rf'[{CJK}];|;[{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P3',
             '中文上下文中出现半角分号 ";"',
             '改为全角分号"；"', sample=m.group(0))

    # P4 半角冒号
    m = re.search(rf'[{CJK}]:(?!\d)|(?<!\d):[{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P4',
             '中文上下文中出现半角冒号 ":"',
             '改为全角冒号"："', sample=m.group(0))

    # P5 半角问号
    m = re.search(rf'[{CJK}]\?|\?[{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P5',
             '中文上下文中出现半角问号 "?"',
             '改为全角问号"？"', sample=m.group(0))

    # P6 半角叹号
    m = re.search(rf'[{CJK}]!|![{CJK}]', masked)
    if m:
        _add(issues, 'ERROR', 'P6',
             '中文上下文中出现半角叹号 "!"',
             '改为全角叹号"！"', sample=m.group(0))

    # P7 ASCII 直双引号包中文
    m = re.search(rf'"[^"\n]*[{CJK}][^"\n]*"', masked)
    if m:
        _add(issues, 'ERROR', 'P7',
             'ASCII 直双引号包中文',
             '改为弯引号"…"', sample=m.group(0)[:30])

    # P8 ASCII 直单引号包中文
    m = re.search(rf"'[^'\n]*[{CJK}][^'\n]*'", masked)
    if m:
        _add(issues, 'WARNING', 'P8',
             'ASCII 直单引号包中文',
             "改为弯单引号'…'", sample=m.group(0)[:30])

    # P9 半角括号包中文（或外侧紧邻中文）
    for m in re.finditer(r'\(([^()\n]+)\)', masked):
        inner = m.group(1)
        s, e = m.start(), m.end()
        before_char = masked[s - 1] if s > 0 else ''
        after_char = masked[e] if e < len(masked) else ''
        has_cjk_outside = _is_cjk_char(before_char) or _is_cjk_char(after_char)
        if _has_cjk(inner) or has_cjk_outside:
            if not re.match(r'^[A-Z][a-z]+,\s*\d{4}$', inner):  # 跳过 (Smith, 2020)
                _add(issues, 'ERROR', 'P9',
                     '半角括号（中文上下文）',
                     '改为全角括号"（…）"', sample=m.group(0)[:30])
                break

    # P10 尖括号冒充书名号
    m = re.search(rf'<[^<>\n]*[{CJK}][^<>\n]{{0,40}}>', masked)
    if m:
        _add(issues, 'ERROR', 'P10',
             '尖括号冒充书名号',
             '改为书名号"《…》"', sample=m.group(0))

    # P11 双连字符冒充破折号
    if re.search(r'-{2,}', masked) and _has_cjk(masked):
        _add(issues, 'WARNING', 'P11',
             '使用两个连字符 "--" 冒充破折号',
             '改为全角破折号"——"（两个 EM dash 连用）',
             sample='--')

    # P12 单 EM dash 在中文上下文
    for m in re.finditer(r'(?<!—)—(?!—)', masked):
        s, e = m.start(), m.end()
        ctx = masked[max(0, s - 2):min(len(masked), e + 2)]
        if _has_cjk(ctx):
            _add(issues, 'INFO', 'P12',
                 '中文上下文中出现单个一字线"—"',
                 '若为破折号请改为"——"；若为日期/数值范围连接符可保留',
                 sample=ctx)
            break

    # P13 三个英文点冒充省略号
    if re.search(r'\.{3,}', masked) and _has_cjk(masked):
        _add(issues, 'WARNING', 'P13',
             '使用三个或更多英文点 "..." 冒充省略号',
             '改为中文省略号"……"', sample='...')

    # P14 单 … 字符
    for m in re.finditer(r'(?<!…)…(?!…)', masked):
        if _has_cjk(masked):
            _add(issues, 'INFO', 'P14',
                 '中文上下文中出现单个省略号字符"…"',
                 '标准用法是"……"（两个 U+2026 连用，共六点）',
                 sample='…')
            break

    # P15 数字范围用 -
    m = re.search(r'(?<!\d)\d+\s?-\s?\d+(?!-\d)', masked)
    if m and _has_cjk(masked):
        _add(issues, 'INFO', 'P15',
             '数值范围使用半角连字符 "-"',
             '中文上下文中数值范围应使用波浪号"～"',
             sample=m.group(0))

    # P16 数字之间半角波浪
    m = re.search(r'\d~\d', masked)
    if m and _has_cjk(masked):
        _add(issues, 'WARNING', 'P16',
             '数字之间使用半角波浪号 "~"',
             '改为全角波浪号"～"', sample=m.group(0))

    # P17 书名号间顿号
    if re.search(r'》\s*、\s*《', masked):
        _add(issues, 'WARNING', 'P17',
             '多个书名号之间使用顿号',
             '多个书名号并列直接连写，不加顿号：《A》《B》',
             sample='》、《')

    # P18 引号间顿号
    if re.search(r'[”]\s*、\s*[“]|"\s*、\s*"', masked):
        _add(issues, 'WARNING', 'P18',
             '多个引号之间使用顿号',
             '多个引号并列直接连写，不加顿号',
             sample='"、"')

    # P19 三级标题序号用顿号（行首）
    if re.match(r'^\d+、', text):
        _add(issues, 'ERROR', 'P19',
             '三级标题序号使用顿号 "1、"',
             '阿拉伯数字序号后应使用下角点 "1."',
             sample=text[:8])

    # P20 百分号前空格
    m = re.search(r'\d\s+%', masked)
    if m:
        _add(issues, 'WARNING', 'P20',
             '数字与百分号之间有空格',
             '数字与%之间不留空格', sample=m.group(0))

    return issues


# =============================================================================
# 自测
# =============================================================================

if __name__ == '__main__':
    samples = [
        ('公司的主营业务包括: 跨境电商, 国际物流, 海外仓储.',
         '公司的主营业务包括：跨境电商，国际物流，海外仓储。'),
        ('公司提出"做强做优做大"的战略目标。',
         '公司提出“做强做优做大”的战略目标。'),
        ('年均增长率(CAGR)达到15%。',
         '年均增长率（CAGR）达到15%。'),
        ('根据<中华人民共和国公司法>第十八条',
         '根据《中华人民共和国公司法》第十八条'),
        ('供应链能力--博维的核心竞争力之一',
         '供应链能力——博维的核心竞争力之一'),
        ('物流仓储...金融服务',
         '物流仓储……金融服务'),
        ('年增长 15~30%', '年增长 15～30%'),
        ('1、推动产业链协同', '1.推动产业链协同'),
        ('详见 https://example.com/path?a=1,b=2 了解更多。',
         '详见 https://example.com/path?a=1,b=2 了解更多。'),
        ('使用 Python 3.11.0 运行。', '使用 Python 3.11.0 运行。'),
        ('销售额达到 1,500,000 元，同比增长 20%。',
         '销售额达到 1,500,000 元，同比增长 20%。'),
        ('增长率为 3.14% 左右。', '增长率为 3.14% 左右。'),
        ('该结论来源于 (Smith, 2020) 的研究。',
         '该结论来源于 (Smith, 2020) 的研究。'),
        ('依据《公司法》、《证券法》的规定',
         '依据《公司法》《证券法》的规定'),
    ]

    print('=== fix_chinese_punctuation 自测 ===')
    passed = 0
    for src, expected in samples:
        out, n = fix_chinese_punctuation(src)
        ok = out == expected
        if ok:
            passed += 1
        mark = '✓' if ok else '✗'
        print(f'{mark} 输入: {src!r}')
        if not ok:
            print(f'  输出: {out!r}')
            print(f'  期望: {expected!r}')
    print(f'\nfix 通过: {passed}/{len(samples)}')

    print('\n=== audit_chinese_punctuation 自测 ===')
    bad = '公司的主营业务包括: 跨境电商, 国际物流. 详见"重点工作", (具体见附录) 及<指南>.'
    issues = audit_chinese_punctuation(bad)
    print(f'样本: {bad!r}')
    for iss in issues:
        print(f'  [{iss.severity}] {iss.code}: {iss.message} → {iss.suggestion}')
    print(f'\naudit 检出: {len(issues)} 条')
