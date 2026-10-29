# -*- coding: utf-8 -*-
"""
HTTP 公共基础设施 v7.7 — 零外部依赖纯标准库实现
所有 scraper 模块共用，消除重复代码。

v7.7.0: 下载优化 — 断点续传（.part + Range 206）/ sha256 完整性校验 /
      已存在跳过 / .part 原子落盘（失败不残留半成品）/ batch_download 并发批量。

v7.5.0: 编码修复与爬取加固 —
      charset 归一化/任意合法编码识别（gb2312/gbk 统一 gb18030）、
      声明编码与正文不符时启发式纠错、GBK 系下载文件名修复、
      download_file 走重试链+Referer 注入+反爬页校验+文本转码、
      SSL 证书失败协议降级重试、空响应自动重试、域名编码记忆。

v7.2.0: 软拦截检测接线（200 反爬页面自动识别并重试）、Retry-After 遵从、
      修复空 POST 退化为 GET / download_file 保存错误页 / 熔断 sleep 持锁等 bug。

v4.0: 增强反爬能力 — UA 轮换池、多策略重试、自适应限流、指纹随机化、
      编码检测增强、代理支持。全部向后兼容，零破坏性变更。

v3.0: requests → urllib 标准库，零 pip 依赖即可运行核心爬虫功能。
      optional: scrapling/playwright 用户可选安装用于动态渲染。

兼容接口（与旧 requests 版完全一致）：
  http_get(), http_post(), fetch_text(), http_get_json(),
  download_file(), get_session(), rate_limit(), clear_cache()

v2.2 遗留 API（保留）：
  DEFAULT_UA, DEFAULT_HEADERS, DEFAULT_TIMEOUT, DOWNLOAD_TIMEOUT,
  sanitize_filename(), LRUCache
"""
from __future__ import annotations

import codecs
import os
import re
import ssl
import time
import json
import random
import logging
import hashlib
import threading
import urllib.request
import urllib.error
import urllib.parse
from enum import Enum, auto
from collections import OrderedDict
import socket
from pathlib import Path
from typing import Optional, Dict, Any, Union, List, Tuple, Callable
from http.cookiejar import CookieJar

# ─── 可选依赖 ──────────────────────────────────────────────────────────────────
try:
    import chardet as _chardet
    HAS_CHARDET = True
except ImportError:
    _chardet = None  # type: ignore
    HAS_CHARDET = False

# v7.2: 软拦截检测（anti_block_utils 纯函数，导入失败时静默降级关闭）
try:
    from anti_block_utils import detect_block as _detect_block
except ImportError:
    try:
        from scripts.anti_block_utils import detect_block as _detect_block
    except ImportError:
        _detect_block = None  # type: ignore

# ─── 日志 ────────────────────────────────────────────────────────────────────
log = logging.getLogger("http_utils")

# ─── 常量 ────────────────────────────────────────────────────────────────────

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# ─── UA 轮换池（v9.0 — 15+ 种真实浏览器 UA，覆盖2025-2026主流版本） ──────────────

UA_POOL_DESKTOP: List[str] = [
    # Chrome 137 Win (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    # Chrome 136 Win (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    # Chrome 135 Win (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Chrome 134 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    # Chrome 133 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Chrome 132 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    # Chrome 135 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Chrome 133 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    # Edge 137 Win (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
    # Edge 135 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    # Edge 133 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
    # Firefox 138 Win (2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0",
    # Firefox 136 Win
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    # Firefox 134 Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:134.0) Gecko/20100101 Firefox/134.0",
    # Chrome 135 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    # Safari 18 Mac (native WebKit)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15",
]

UA_POOL_MOBILE: List[str] = [
    # iPhone Safari 18
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
    # iPhone Safari 17
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Mobile/15E148 Safari/604.1",
    # Android Chrome 137
    "Mozilla/5.0 (Linux; Android 15; Pixel 9 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.6821.119 Mobile Safari/537.36",
    # Android Chrome 135 (Samsung)
    "Mozilla/5.0 (Linux; Android 14; SM-S938B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.7049.100 Mobile Safari/537.36",
    # iPad Safari 18
    "Mozilla/5.0 (iPad; CPU OS 18_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Mobile/15E148 Safari/604.1",
    # Android Chrome 133 (Xiaomi)
    "Mozilla/5.0 (Linux; Android 14; 2304FPN6DC) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.137 Mobile Safari/537.36",
    # iPhone Chrome 136
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/136.0.7103.91 Mobile/15E148 Safari/604.1",
    # Huawei Browser
    "Mozilla/5.0 (Linux; Android 14; NOH-AN00) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.6723.107 Mobile Safari/537.36 HuaweiBrowser/15.0.4.312",
]

# 合并池
UA_POOL_ALL: List[str] = UA_POOL_DESKTOP + UA_POOL_MOBILE

# ─── 请求指纹池（v4.0 — 每次请求随机组合） ──────────────────────────────────────────

ACCEPT_LANGUAGE_POOL: List[str] = [
    "zh-CN,zh;q=0.9",
    "zh-CN,zh;q=0.9,en;q=0.8",
    "zh-CN,zh;q=0.9,en;q=0.8,en-US;q=0.7",
    "zh-CN,zh;q=0.8,en;q=0.6",
]

ACCEPT_ENCODING_POOL: List[str] = [
    "gzip, deflate",
    "gzip, deflate, br",
]

SEC_CH_UA_POOL: List[str] = [
    '"Google Chrome";v="137", "Chromium";v="137", "Not=A?Brand";v="24"',
    '"Google Chrome";v="136", "Chromium";v="136", "Not=A?Brand";v="24"',
    '"Google Chrome";v="135", "Chromium";v="135", "Not=A?Brand";v="24"',
    '"Google Chrome";v="134", "Chromium";v="134", "Not=A?Brand";v="24"',
    '"Google Chrome";v="133", "Chromium";v="133", "Not=A?Brand";v="24"',
    '"Google Chrome";v="132", "Chromium";v="132", "Not=A?Brand";v="24"',
    '"Chromium";v="137", "Not A(Brand";v="24", "Microsoft Edge";v="137"',
    '"Chromium";v="135", "Not A(Brand";v="24", "Microsoft Edge";v="135"',
    '"Chromium";v="133", "Not A(Brand";v="24", "Microsoft Edge";v="133"',
]

SEC_CH_UA_PLATFORM_POOL: List[str] = [
    '"Windows"',
    '"macOS"',
]

DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": DEFAULT_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}

DEFAULT_TIMEOUT = 30  # HTTP 请求超时（DNS 解析另由 socket.setdefaulttimeout 控制）
DOWNLOAD_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5
RATE_LIMIT_DELAY = 2.0
JITTER_FACTOR = 0.3

# ─── 重试策略枚举（v4.0） ──────────────────────────────────────────────────────

class RetryStrategy(Enum):
    """多策略重试 — 每次重试采用不同反爬策略"""
    UA_ROTATE = auto()        # 换一个 UA
    MOBILE_UA = auto()        # 模拟移动端
    FINGERPRINT = auto()      # 换 Accept-Language / Sec-Ch-Ua 指纹
    ADD_AJAX_HEADER = auto()  # 添加 X-Requested-With: XMLHttpRequest
    REFERRER_CHANGE = auto()  # 更换 Referer
    NO_CACHE = auto()         # 绕过缓存直接请求
    DELAY_INCREASE = auto()   # 增加延迟再试

# 默认重试策略序列
DEFAULT_RETRY_STRATEGIES: List[RetryStrategy] = [
    RetryStrategy.UA_ROTATE,
    RetryStrategy.FINGERPRINT,
    RetryStrategy.ADD_AJAX_HEADER,
    RetryStrategy.MOBILE_UA,
    RetryStrategy.DELAY_INCREASE,
]

# ─── UA 轮换与指纹随机化工具（v4.0） ────────────────────────────────────────────

def random_ua(mobile: bool = False) -> str:
    """从 UA 池随机选择一个"""
    pool = UA_POOL_MOBILE if mobile else UA_POOL_DESKTOP
    return random.choice(pool)


def random_fingerprint_headers(base_ua: Optional[str] = None) -> Dict[str, str]:
    """生成随机指纹 headers 集合（Accept-Language, Accept-Encoding, Sec-Ch-Ua 等）"""
    headers: Dict[str, str] = {
        "Accept-Language": random.choice(ACCEPT_LANGUAGE_POOL),
        "Accept-Encoding": random.choice(ACCEPT_ENCODING_POOL),
    }
    if base_ua:
        headers["User-Agent"] = base_ua
    # 对 Chrome/Edge UA 添加 Sec-Ch-Ua 系列 header
    ua = base_ua or ""
    if "Chrome" in ua or "Edg" in ua:
        headers["Sec-Ch-Ua"] = random.choice(SEC_CH_UA_POOL)
        headers["Sec-Ch-Ua-Platform"] = random.choice(SEC_CH_UA_PLATFORM_POOL)
        headers["Sec-Ch-Ua-Mobile"] = "?0"
    # v9.0: 添加 Sec-Fetch-* 系列 headers（Chrome/Edge 始终发送）
    if "Chrome" in ua or "Edg" in ua:
        headers["Sec-Fetch-Dest"] = "document"
        headers["Sec-Fetch-Mode"] = "navigate"
        headers["Sec-Fetch-Site"] = random.choice(["none", "same-origin", "cross-site"])
        headers["Sec-Fetch-User"] = "?1"
    return headers


def _randomize_header_order(headers: Dict[str, str]) -> List[Tuple[str, str]]:
    """v9.0: 随机化 HTTP header 顺序以抵抗指纹检测。

    某些 WAF/CDN（如 Cloudflare）会检查 header 的顺序作为指纹。
    真实浏览器的 header 顺序因浏览器类型、版本和操作系统而异。
    此函数模拟这种随机性。

    Returns:
        按随机顺序排列的 header 列表
    """
    items = list(headers.items())
    # 分为「固定位置 headers」和「可变位置 headers」
    # Host 和 User-Agent 通常在前面但顺序不固定
    fixed_order = {"Host", "User-Agent"}
    fixed = [(k, v) for k, v in items if k in fixed_order]
    variable = [(k, v) for k, v in items if k not in fixed_order]
    random.shuffle(variable)
    return fixed + variable


def adaptive_jitter(base: float, site_difficulty: str = "normal") -> float:
    """v9.0: 自适应抖动延迟，模拟不同网站的阅读难度。

    Args:
        base: 基础延迟秒数
        site_difficulty: 站点难度 - "easy" (新闻/公告类), "normal" (通用),
                        "hard" (严格反爬如雪球/集思录)
    """
    import math
    # 使用对数正态分布模拟人类请求间隔（更自然）
    # 大多数请求间隔短，偶尔有较长间隔
    sigma = {"easy": 0.3, "normal": 0.5, "hard": 0.7}.get(site_difficulty, 0.5)
    mu = math.log(base) - sigma ** 2 / 2
    jittered = random.lognormvariate(mu, sigma)
    # 限幅到合理范围
    return max(0.5, min(jittered, base * 4))


# 按域名记住最近成功的 UA（部分网站对特定 UA 更友好）
_successful_ua_by_domain: Dict[str, str] = {}
_ua_domain_lock = threading.Lock()


def record_successful_ua(domain: str, ua: str):
    """记录某域名下最近成功的 UA"""
    with _ua_domain_lock:
        _successful_ua_by_domain[domain] = ua
        # 限制字典大小
        if len(_successful_ua_by_domain) > 200:
            # 清理一半
            keys = list(_successful_ua_by_domain.keys())
            for k in keys[:100]:
                del _successful_ua_by_domain[k]


def get_best_ua_for_domain(domain: str, mobile: bool = False) -> str:
    """获取某域名下最佳 UA（有历史成功记录则用历史的，否则随机）"""
    with _ua_domain_lock:
        if domain in _successful_ua_by_domain:
            cached = _successful_ua_by_domain[domain]
            # 如果请求的是 mobile 但缓存的是 desktop，重新随机
            if mobile and "Mobile" not in cached and "Android" not in cached:
                return random_ua(mobile=True)
            return cached
    return random_ua(mobile=mobile)


# ─── StdlibResponse ───────────────────────────────────────────────────────────

class HTTPError(urllib.error.HTTPError):
    """requests 风格的 HTTPError（兼容 raise_for_status 用法）

    v7.2 修复：此前定义的 url property 与父类 addinfourl 的
    self.url 赋值冲突，导致 raise_for_status 抛 AttributeError。
    """
    def __init__(self, code: int, msg: str, hdrs: Dict, fp: Any, url: str = ""):
        super().__init__(url or "?", code, msg, hdrs, fp)
        self.code = code  # urllib.error.HTTPError uses .code internally

    @property
    def status_code(self) -> int:
        return self.code


def _auto_decompress(data: bytes, headers: Dict[str, str]) -> bytes:
    """v7.0.0: 自动解压 gzip/deflate 响应体（urllib 不解压，指纹头发了
    Accept-Encoding: gzip 时服务器会返回压缩流，导致 json()/text() 乱码）。"""
    if not data:
        return data
    ce = (headers.get("Content-Encoding")
          or headers.get("content-encoding") or "").lower()
    try:
        if "gzip" in ce or data[:2] == b"\x1f\x8b":
            import gzip
            return gzip.decompress(data)
        if "deflate" in ce:
            import zlib
            try:
                return zlib.decompress(data)
            except zlib.error:
                return zlib.decompress(data, -zlib.MAX_WBITS)
        if "br" in ce:
            try:
                import brotli
                return brotli.decompress(data)
            except ImportError:
                pass
    except Exception:
        return data
    return data


class StdlibResponse:
    """
    urllib.response 包装器，对标 requests.Response 接口。
    让调用方无感切换到 urllib。
    """
    __slots__ = ('_url', '_code', '_headers', '_data', '_encoding')

    def __init__(self, url: str, code: int, headers: Dict[str, str], data: bytes, encoding: str = "utf-8"):
        self._url = url
        self._code = code
        self._headers = headers
        self._data = _auto_decompress(data, headers)
        self._encoding = encoding

    @property
    def url(self) -> str:
        return self._url

    @property
    def code(self) -> int:
        return self._code

    @property
    def status_code(self) -> int:
        return self._code

    @property
    def headers(self) -> Dict[str, str]:
        return self._headers

    @property
    def content(self) -> bytes:
        return self._data

    @property
    def text(self) -> str:
        return self._data.decode(self._encoding, errors="replace")

    def json(self) -> Any:
        # v7.5: GBK/GB2312 JSON API 兼容（json.loads 对 bytes 仅支持 utf-8/16/32）
        try:
            return json.loads(self._data)
        except (UnicodeDecodeError, ValueError):
            return json.loads(self._data.decode(self._encoding, errors="replace"))

    def raise_for_status(self):
        if self._code >= 400:
            raise HTTPError(
                self._code,
                f"HTTP {self._code} error",
                self._headers,
                None,
                self._url
            )

    def __repr__(self):
        return f"<StdlibResponse [{self._code}]>"


# ─── StdlibSession ───────────────────────────────────────────────────────────

class StdlibSession:
    """
    对标 requests.Session 的标准库实现。
    内部使用 urllib.request.OpenerDirector + CookieJar。

    v4.0: 支持代理、UA 轮换、指纹随机化。
    """
    def __init__(self, headers: Optional[Dict[str, str]] = None,
                 proxy: Optional[str] = None,
                 rotate_ua: bool = False,
                 randomize_fingerprint: bool = False,
                 mobile: bool = False):
        self.headers = {**DEFAULT_HEADERS, **(headers or {})}
        self.cookies = CookieJar()
        self.proxy = proxy
        self.rotate_ua = rotate_ua
        self.randomize_fingerprint = randomize_fingerprint
        self.mobile = mobile

        # 如果启用 UA 轮换，随机选取初始 UA
        if rotate_ua and "User-Agent" not in (headers or {}):
            self.headers["User-Agent"] = random_ua(mobile=mobile)

        # 构建 opener（含代理）
        handlers: List[Any] = [urllib.request.HTTPCookieProcessor(self.cookies)]
        if proxy:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy,
                "https": proxy,
            })
            handlers.insert(0, proxy_handler)
        self._opener = urllib.request.build_opener(*handlers)

    def _apply_fingerprint(self, headers: Dict[str, str]) -> Dict[str, str]:
        """如果启用指纹随机化，为每次请求注入变化的指纹 headers"""
        if not self.randomize_fingerprint:
            return headers
        fp = random_fingerprint_headers()
        # 不覆盖调用方显式设置的 headers
        for k, v in fp.items():
            if k not in headers:
                headers[k] = v
        return headers

    def _rotate_ua_if_needed(self, headers: Dict[str, str]) -> Dict[str, str]:
        """如果启用 UA 轮换且调用方没显式设 UA，则随机换一个"""
        if not self.rotate_ua:
            return headers
        if "User-Agent" in headers:
            return headers
        base_ua = self.headers.get("User-Agent", "")
        # 从 URL host 获取域名以使用最佳 UA
        # （在 _build_request 中无法预知 URL，这里用默认随机）
        headers["User-Agent"] = random_ua(mobile=self.mobile)
        return headers

    def _build_request(self, url: str, headers: Dict,
                       data: Optional[bytes],
                       method: Optional[str] = None) -> urllib.request.Request:
        merged = {**self.headers, **headers}

        # v4.0: 指纹随机化
        merged = self._apply_fingerprint(merged)

        # v4.0: UA 轮换 + 域名最佳 UA
        if self.rotate_ua and "User-Agent" not in headers:
            domain = _extract_domain(url)
            merged["User-Agent"] = get_best_ua_for_domain(domain, mobile=self.mobile)

        # v7.2: 显式 method 优先（修复空 body POST 被误判为 GET）
        if method is None:
            method = "GET" if data is None else "POST"
        return urllib.request.Request(url, data=data, headers=merged, method=method)

    def _do_open(self, request: urllib.request.Request,
                 timeout: int,
                 response_cls: type = StdlibResponse) -> StdlibResponse:
        """执行请求并返回 StdlibResponse"""
        try:
            raw = self._opener.open(request, timeout=timeout)
            code = raw.getcode()
            # urllib 返回的是 email.message.Message 或类似对象
            hdrs = dict(raw.headers) if hasattr(raw.headers, '__iter__') else {}
            data = raw.read()
            url = request.full_url
            raw.close()

            # 编码检测（v7.5: 传入 url 以启用域名编码记忆）
            encoding = self._detect_encoding(code, hdrs, data, url)
            return StdlibResponse(url, code, hdrs, data, encoding)

        except urllib.error.HTTPError as e:
            # 读取错误响应体（某些服务器在 4xx/5xx 时仍返回内容）
            code = e.code
            hdrs = dict(e.headers) if hasattr(e.headers, '__iter__') else {}
            data = b""
            try:
                if e.fp:
                    data = e.fp.read()
                    e.fp.close()
            except Exception:
                pass
            url = e.url or request.full_url
            encoding = self._detect_encoding(code, hdrs, data, url)
            return StdlibResponse(url, code, hdrs, data, encoding)

    def _detect_encoding(self, code: int, hdrs: Dict, data: bytes, url: str = "") -> str:
        """推断响应编码（v7.5 重写：BOM → 域名记忆 → Content-Type（严格解码验证）
        → HTML meta charset → chardet → 中文启发式）。
        修复：声明编码与正文不符时不再盲信（如声称 utf-8 实为 GBK），
        gb2312/gbk 统一归一化为 gb18030 避免超纲字符变替换符。"""
        if not data:
            return "utf-8"

        # 0. BOM 探测（最高优先）
        if data.startswith(b"\xef\xbb\xbf"):
            return "utf-8"
        if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
            return "utf-16"

        # 0.5 域名编码记忆（同一站点首次探测成功后直接复用）
        if url:
            hinted = get_domain_encoding(_extract_domain(url))
            if hinted:
                try:
                    data[:4096].decode(hinted)
                    return hinted
                except (UnicodeDecodeError, LookupError):
                    pass

        # 1. Content-Type header 的 charset=（归一化 + 严格解码验证）
        ct = hdrs.get("Content-Type", "") or hdrs.get("content-type", "")
        m = re.search(r'charset=([^\s;]+)', ct, re.IGNORECASE)
        declared = normalize_charset(m.group(1)) if m else None
        if declared:
            try:
                data.decode(declared)
                return declared
            except (UnicodeDecodeError, LookupError):
                log.debug(f"declared charset {declared} 无法解码正文，启发式纠错")

        # 2. HTML <meta charset> / http-equiv（字节级探测，避免先用错编码解析头部）
        meta_enc = _detect_meta_charset_bytes(data)
        if meta_enc:
            try:
                data[:4096].decode(meta_enc)
                return meta_enc
            except (UnicodeDecodeError, LookupError):
                pass

        # 3. 可选：chardet 高精度检测
        if HAS_CHARDET and len(data) > 20:
            try:
                detected = _chardet.detect(data[:16384])  # type: ignore[union-attr]
                if detected and detected.get("encoding") and detected.get("confidence", 0) > 0.7:
                    enc = normalize_charset(detected["encoding"])
                    if enc:
                        return enc
            except Exception:
                pass

        # 4. 中文编码启发式（中国金融网站主流编码）
        guess = looks_like_chinese_binary(data)
        if guess:
            return guess

        # 5. 最后兜底：latin-1 保证任何字节流都可解码（不再伪造 utf-8）
        return "latin-1"

    def get(self, url: str,
            headers: Optional[Dict[str, str]] = None,
            timeout: int = DEFAULT_TIMEOUT,
            **kwargs) -> StdlibResponse:
        """HTTP GET"""
        req = self._build_request(url, headers or {}, None)
        return self._do_open(req, timeout)

    def post(self, url: str,
             data: Optional[Any] = None,
             json_body: Optional[Dict] = None,
             headers: Optional[Dict[str, str]] = None,
             timeout: int = DEFAULT_TIMEOUT,
             **kwargs) -> StdlibResponse:
        """HTTP POST — data 或 json_body 二选一"""
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            h = {"Content-Type": "application/json; charset=utf-8"}
            if headers:
                h = {**headers, **h}
            headers = h
        elif data is not None:
            if isinstance(data, str):
                body_bytes = data.encode("utf-8")
            elif isinstance(data, dict):
                body_bytes = urllib.parse.urlencode(data).encode("utf-8")
            else:
                body_bytes = data
        else:
            body_bytes = None
        req = self._build_request(url, headers or {}, body_bytes, method="POST")
        return self._do_open(req, timeout)

    def request(self, method: str, url: str,
                headers: Optional[Dict[str, str]] = None,
                data: Optional[Any] = None,
                timeout: int = DEFAULT_TIMEOUT,
                **kwargs) -> StdlibResponse:
        """通用 request"""
        if method.upper() == "POST":
            return self.post(url, data=data, headers=headers, timeout=timeout, **kwargs)
        return self.get(url, headers=headers, timeout=timeout, **kwargs)


def _extract_meta_charset(html_head: str) -> Optional[str]:
    """从 HTML 头部提取 charset 声明"""
    # <meta charset="gbk">
    m = re.search(r'<meta[^>]+charset=["\']?\s*([^\s"\'/>]+)', html_head, re.IGNORECASE)
    if m:
        enc = m.group(1).strip().lower()
        if enc in ("gbk", "gb2312", "gb18030", "utf-8", "utf8", "big5"):
            if enc in ("gb2312", "gbk"):
                return "gb18030"
            return enc
    # <meta http-equiv="Content-Type" content="text/html; charset=gbk">
    m = re.search(r'http-equiv=["\']?Content-Type["\']?[^>]+charset=["\']?\s*([^\s"\'/>]+)',
                   html_head, re.IGNORECASE)
    if m:
        enc = m.group(1).strip().lower()
        if enc in ("gbk", "gb2312", "gb18030", "utf-8", "utf8", "big5"):
            if enc in ("gb2312", "gbk"):
                return "gb18030"
            return enc
    return None


# ─── v7.5 编码辅助 ──────────────────────────────────────

_CHARSET_ALIASES: Dict[str, str] = {
    "utf8": "utf-8", "utf-16le": "utf-16", "utf-16be": "utf-16",
    "gb2312": "gb18030", "gbk": "gb18030", "x-gbk": "gb18030",
    "chinese": "gb18030", "cp936": "gb18030",
    "iso-8859-1": "latin-1", "latin1": "latin-1", "iso8859-1": "latin-1",
    "windows-1252": "cp1252", "ascii": "utf-8", "us-ascii": "utf-8",
}


def normalize_charset(enc: Optional[str]) -> Optional[str]:
    """归一化 charset 声明：别名映射 + GB 系统一为 gb18030（超集，避免超纲字符变替换符），
    用 codecs 验证合法性。非法/无法识别返回 None。"""
    if not enc:
        return None
    e = str(enc).strip().lower().strip('"\'')
    e = _CHARSET_ALIASES.get(e, e)
    try:
        codecs.lookup(e)
    except (LookupError, TypeError):
        return None
    return e


# 域名编码记忆：首次探测成功后复用，避免同站反复误判
domain_encoding_hints: Dict[str, str] = {}
_enc_hint_lock = threading.Lock()


def remember_domain_encoding(domain: str, enc: str):
    """记录域名下成功解码的编码（仅记非平凡编码）"""
    if not domain or not enc or enc.lower() in ("ascii", "latin-1"):
        return
    with _enc_hint_lock:
        domain_encoding_hints[domain] = enc.lower()
        if len(domain_encoding_hints) > 300:
            for k in list(domain_encoding_hints.keys())[:150]:
                del domain_encoding_hints[k]


def get_domain_encoding(domain: str) -> Optional[str]:
    with _enc_hint_lock:
        return domain_encoding_hints.get(domain)


def _detect_meta_charset_bytes(data: bytes) -> Optional[str]:
    """字节级 meta charset 探测：依次用常见编码解码头部再提取，
    避免旧版先用 errors=replace 解码导致 GBK 页头部解析失真。"""
    if not data:
        return None
    head = data[:4096]
    for enc_try in ("utf-8", "gb18030", "latin-1"):
        try:
            head_text = head.decode(enc_try)
        except (UnicodeDecodeError, LookupError):
            continue
        meta_enc = _extract_meta_charset(head_text)
        if meta_enc:
            return normalize_charset(meta_enc) or meta_enc.lower()
    return None


def looks_like_chinese_binary(data: bytes) -> Optional[str]:
    """无 charset 声明时的启发式编码判定：
    utf-8 严格解码通过 → utf-8；失败则统计 GBK 双字节对比例，
    显著时判为 gb18030；均不满足返回 None。"""
    if not data:
        return None
    sample = data[:16384]
    if all(b < 0x80 for b in sample):
        return "utf-8"          # 纯 ASCII，任何超集等价
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    # GBK 双字节对统计（首字节 0x81-0xFE + 尾字节 0x40-0xFE）
    gbk_pairs = 0
    i = 0
    n = len(sample)
    while i < n - 1:
        b = sample[i]
        if 0x81 <= b <= 0xFE and 0x40 <= sample[i + 1] <= 0xFE:
            gbk_pairs += 1
            i += 2
        else:
            i += 1
    if gbk_pairs >= max(2, n // 64):
        return "gb18030"
    return None


# ─── 代理支持（v4.0） ──────────────────────────────────────────────────────────

_proxy_url: Optional[str] = None
_proxy_lock = threading.Lock()


def set_proxy(proxy_url: Optional[str]):
    """设置全局代理 URL（http://user:pass@host:port 或 socks5://host:port）

    Args:
        proxy_url: 代理 URL，传 None 清除代理设置
    """
    global _proxy_url
    with _proxy_lock:
        _proxy_url = proxy_url
    # 清除共享 session 以使用新代理重建
    clear_session_cache()
    if proxy_url:
        log.info(f"代理已设置: {proxy_url[:50]}...")
    else:
        log.info("代理已清除")


def get_proxy() -> Optional[str]:
    """获取当前全局代理 URL"""
    with _proxy_lock:
        return _proxy_url


def check_proxy(proxy_url: Optional[str] = None, test_url: str = "https://www.baidu.com",
                timeout: int = 10) -> Tuple[bool, str]:
    """检测代理是否可用

    Returns:
        (是否可用, 响应信息或错误描述)
    """
    proxy = proxy_url or _proxy_url
    if not proxy:
        return False, "未设置代理"

    try:
        proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(test_url, headers={"User-Agent": DEFAULT_UA})
        resp = opener.open(req, timeout=timeout)
        code = resp.getcode()
        resp.close()
        if 200 <= code < 400:
            return True, f"代理可用 (HTTP {code})"
        return False, f"代理返回 HTTP {code}"
    except Exception as e:
        return False, f"代理检测失败: {str(e)[:100]}"


# ─── SSL 验证管理（v5.0） ─────────────────────────────────────────────────────

_ssl_verify_enabled: bool = True
_ssl_lock = threading.Lock()


def disable_ssl_verify():
    """⚠️ 仅调试用 — 关闭 SSL 证书验证（生产环境请安装代理 CA 证书）

    调用此函数会记录 WARNING 日志（含调用栈），便于安全审计。
    推荐替代方案：
    1. 安装代理 CA 证书到系统信任库
    2. 使用 requests 的 verify 参数指定 PEM 文件路径
    """
    global _ssl_verify_enabled
    import traceback
    stack = "".join(traceback.format_stack()[-4:-1])
    log.warning(
        "SSL 证书验证已关闭（仅调试用，生产环境请安装代理 CA 证书）\n"
        f"调用栈:\n{stack}"
    )
    with _ssl_lock:
        _ssl_verify_enabled = False


def enable_ssl_verify():
    """重新启用 SSL 证书验证（默认状态）"""
    global _ssl_verify_enabled
    with _ssl_lock:
        _ssl_verify_enabled = True
    log.info("SSL 证书验证已启用")


def get_ssl_status() -> Dict[str, Any]:
    """查询当前 SSL 验证状态

    Returns:
        {"enabled": bool, "status": str}
    """
    with _ssl_lock:
        enabled = _ssl_verify_enabled
    return {
        "enabled": enabled,
        "status": "正常（证书验证开启）" if enabled else "⚠️ 已关闭（仅调试模式）",
    }


def is_ssl_verify_enabled() -> bool:
    """返回当前 SSL 验证是否启用"""
    with _ssl_lock:
        return _ssl_verify_enabled


def clear_session_cache():
    """清除共享 session 缓存（代理变更后需要重建 session）"""
    global _shared_session, _shared_mobile_session
    _shared_session = None
    _shared_mobile_session = None


# ─── 全局会话（v4.0 增强） ────────────────────────────────────────────────────

_shared_session: Optional[StdlibSession] = None
_shared_mobile_session: Optional[StdlibSession] = None


def get_session(headers: Optional[Dict[str, str]] = None,
                reuse: bool = True,
                mobile: bool = False,
                rotate_ua: bool = True,
                randomize_fingerprint: bool = True) -> StdlibSession:
    """获取或创建 HTTP 会话

    Args:
        headers: 额外的请求 headers
        reuse: 是否复用共享 session
        mobile: 是否使用移动端 UA
        rotate_ua: 是否启用 UA 轮换（每次请求随机选 UA）
        randomize_fingerprint: 是否启用请求指纹随机化

    v4.0: 新增 rotate_ua 和 randomize_fingerprint 参数，
          默认开启以增强反爬能力。
    """
    global _shared_session, _shared_mobile_session

    proxy = get_proxy()

    if mobile:
        if reuse and _shared_mobile_session is not None and headers is None and not proxy:
            return _shared_mobile_session
        session = StdlibSession(
            {**DEFAULT_HEADERS, "User-Agent": MOBILE_UA, **(headers or {})},
            proxy=proxy, rotate_ua=rotate_ua,
            randomize_fingerprint=randomize_fingerprint, mobile=True
        )
        if reuse and headers is None and not proxy:
            _shared_mobile_session = session
        return session

    if reuse and _shared_session is not None and headers is None and not proxy:
        return _shared_session

    session = StdlibSession(
        {**DEFAULT_HEADERS, **(headers or {})},
        proxy=proxy, rotate_ua=rotate_ua,
        randomize_fingerprint=randomize_fingerprint, mobile=False
    )
    if reuse and headers is None and not proxy:
        _shared_session = session
    return session


# ─── 自适应限流（v4.0） ──────────────────────────────────────────────────────────

class AdaptiveRateLimiter:
    """根据服务器响应动态调整域名限流延迟

    - HTTP 429 / 503 → 加大该域名延迟（指数增长，最大 120 秒）
    - 连续成功 → 逐步恢复至基础延迟
    - 触发限流后短暂暂停该域名所有请求
    """

    def __init__(self, base_delays: Optional[Dict[str, float]] = None):
        # 延迟引用 _DOMAIN_RATE_LIMITS（在模块底部定义），避免循环依赖
        defaults = base_delays if base_delays else _DOMAIN_RATE_LIMITS
        self._base_delays: Dict[str, float] = dict(defaults)
        self._current_delays: Dict[str, float] = {}
        self._consecutive_ok: Dict[str, int] = {}
        self._consecutive_fail: Dict[str, int] = {}
        self._blocked_until: Dict[str, float] = {}
        self._lock = threading.Lock()
        # 配置
        self.max_delay = 120.0          # 最大延迟（秒）
        self.fail_threshold = 3          # 连续失败几次触发熔断
        self.ok_recovery_threshold = 5   # 连续成功几次开始恢复
        self.recovery_factor = 0.8       # 恢复因子（每次乘 0.8）

    def _get_base_delay(self, domain: str) -> float:
        """获取域名基础延迟"""
        for d, v in self._base_delays.items():
            if d in domain and d != "default":
                return v
        return self._base_delays.get("default", RATE_LIMIT_DELAY)

    def get_delay(self, domain: str) -> float:
        """获取当前域名的限流延迟"""
        with self._lock:
            # 检查是否在熔断期
            blocked = self._blocked_until.get(domain, 0)
            if blocked > time.time():
                remaining = blocked - time.time()
                return max(remaining, 5.0)
            return self._current_delays.get(domain, self._get_base_delay(domain))

    def report_success(self, domain: str):
        """报告一次成功的请求"""
        with self._lock:
            self._consecutive_fail[domain] = 0
            ok_count = self._consecutive_ok.get(domain, 0) + 1
            self._consecutive_ok[domain] = ok_count
            # 连续成功 N 次后逐步恢复延迟
            if ok_count >= self.ok_recovery_threshold:
                current = self._current_delays.get(domain, self._get_base_delay(domain))
                base = self._get_base_delay(domain)
                if current > base:
                    new_delay = max(base, current * self.recovery_factor)
                    self._current_delays[domain] = new_delay
                    log.debug(f"域名 {domain} 延迟恢复: {current:.1f}s → {new_delay:.1f}s")

    def report_failure(self, domain: str, status_code: int = 0) -> bool:
        """报告一次失败的请求

        Returns:
            是否触发熔断（该域名应暂时停止请求）
        """
        with self._lock:
            self._consecutive_ok[domain] = 0
            fail_count = self._consecutive_fail.get(domain, 0) + 1
            self._consecutive_fail[domain] = fail_count

            # HTTP 429 (Rate Limited) 或 503 (Service Unavailable) → 加大延迟
            if status_code in (429, 503):
                current = self._current_delays.get(domain, self._get_base_delay(domain))
                new_delay = min(current * 2.5, self.max_delay)
                self._current_delays[domain] = new_delay
                log.warning(f"域名 {domain} HTTP {status_code}，延迟增至 {new_delay:.1f}s")

            # 连续失败 → 熔断
            if fail_count >= self.fail_threshold:
                block_seconds = min(10 * (2 ** (fail_count - self.fail_threshold)), 300)
                self._blocked_until[domain] = time.time() + block_seconds
                log.warning(f"域名 {domain} 连续失败 {fail_count} 次，熔断 {block_seconds:.0f}s")
                return True
            return False

    def is_blocked(self, domain: str) -> bool:
        """检查域名是否在熔断期"""
        with self._lock:
            blocked = self._blocked_until.get(domain, 0)
            if blocked > time.time():
                return True
            # 熔断过期，半开恢复
            if blocked > 0 and blocked <= time.time():
                self._blocked_until[domain] = 0
            return False

    def reset_domain(self, domain: str):
        """重置某个域名的限流状态"""
        with self._lock:
            self._current_delays.pop(domain, None)
            self._consecutive_ok.pop(domain, None)
            self._consecutive_fail.pop(domain, None)
            self._blocked_until.pop(domain, None)

    def get_stats(self) -> Dict[str, Any]:
        """获取限流统计信息"""
        with self._lock:
            return {
                "blocked_domains": [
                    d for d, t in self._blocked_until.items() if t > time.time()
                ],
                "delays": dict(self._current_delays),
                "consecutive_fails": dict(self._consecutive_fail),
            }


# ── 全局自适应限流器（延迟创建，在 _DOMAIN_RATE_LIMITS 之后） ──
_adaptive_limiter: Optional[AdaptiveRateLimiter] = None


def _get_adaptive_limiter() -> AdaptiveRateLimiter:
    """延迟获取全局自适应限流器（确保 _DOMAIN_RATE_LIMITS 已定义）"""
    global _adaptive_limiter
    if _adaptive_limiter is None:
        _adaptive_limiter = AdaptiveRateLimiter()
    return _adaptive_limiter


def get_adaptive_delay(domain: str) -> float:
    """便捷函数：获取域名自适应延迟"""
    return _get_adaptive_limiter().get_delay(domain)


def report_request_result(url: str, success: bool, status_code: int = 0):
    """便捷函数：报告单次请求结果给自适应限流器"""
    domain = _extract_domain(url)
    if success:
        _get_adaptive_limiter().report_success(domain)
    else:
        _get_adaptive_limiter().report_failure(domain, status_code)


# ─── LRU 缓存 ────────────────────────────────────────────────────────────────

class LRUCache:
    def __init__(self, max_size: int = 128, ttl: float = 3600):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def _key(self, url: str) -> str:
        return hashlib.md5(url.encode()).hexdigest()[:16]

    def get(self, url: str) -> Optional[StdlibResponse]:
        key = self._key(url)
        with self._lock:
            if key in self._cache:
                entry_time, resp = self._cache[key]
                if time.time() - entry_time < self.ttl:
                    self._cache.move_to_end(key)
                    return resp
                del self._cache[key]
        return None

    def set(self, url: str, resp: StdlibResponse):
        key = self._key(url)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            else:
                if len(self._cache) >= self.max_size:
                    self._cache.popitem(last=False)
                self._cache[key] = (time.time(), resp)

    def clear(self):
        with self._lock:
            self._cache.clear()

    def __len__(self):
        return len(self._cache)


_response_cache = LRUCache(max_size=256, ttl=1800)


# ─── 按域名限流 ──────────────────────────────────────────────────────────────

_DOMAIN_RATE_LIMITS: Dict[str, float] = {
    "eastmoney.com": 1.0,
    "10jqka.com.cn": 2.0,
    "sina.com.cn": 1.5,
    "163.com": 1.5,
    "xueqiu.com": 3.0,
    "cls.cn": 2.0,
    "jisilu.cn": 3.0,
    "wallstreetcn.com": 2.0,
    "sse.com.cn": 3.0,
    "szse.cn": 3.0,
    # v6.0.0 新增媒体源
    "hexun.com": 2.0,
    "jrj.com.cn": 2.0,
    "ifeng.com": 1.5,
    "fx678.com": 2.0,
    "cngold.org": 2.0,
    "investing.com": 2.0,
    "zhitongcaijing.com": 2.0,
    "jiemian.com": 1.5,
    "lanjinger.com": 2.0,
    "futunn.com": 2.0,
    "aastocks.com": 2.0,
    "smm.cn": 2.0,
    "default": RATE_LIMIT_DELAY,
}

_domain_last_request: Dict[str, float] = {}
_domain_lock = threading.Lock()


def _extract_domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return "unknown"


def rate_limit(delay: float = RATE_LIMIT_DELAY, url: str = ""):
    """按域名限流（v4.0 增强：合并自适应限流器延迟；v7.2: sleep 移出锁外）"""
    global _domain_last_request
    wait_time = 0.0
    block_wait = 0.0
    blocked_domain = ""
    with _domain_lock:
        now = time.time()
        if url:
            domain = _extract_domain(url)
            # v4.0: 优先使用自适应限流延迟
            limiter = _get_adaptive_limiter()
            adaptive_delay = limiter.get_delay(domain)
            # 查找匹配域名配置（取自适应与静态配置的最大值）
            domain_delay = adaptive_delay
            for d, v in _DOMAIN_RATE_LIMITS.items():
                if d in domain and d != "default":
                    domain_delay = max(adaptive_delay, v)
                    break
            else:
                domain_delay = max(adaptive_delay, _DOMAIN_RATE_LIMITS.get("default", RATE_LIMIT_DELAY))

            # 检查是否熔断（v7.2: 只计算等待时长，sleep 在锁外执行，
            # 避免一个域名熔断阻塞所有域名的限流判断）
            if limiter.is_blocked(domain):
                block_wait = max(domain_delay, 5.0)
                blocked_domain = domain
            else:
                last_time = _domain_last_request.get(domain, 0)
                elapsed = now - last_time
                if elapsed < domain_delay:
                    wait_time = domain_delay - elapsed
                _domain_last_request[domain] = now + wait_time
                if len(_domain_last_request) > 100:
                    expired = [d for d, t in _domain_last_request.items() if now - t > 3600]
                    for d in expired:
                        del _domain_last_request[d]
    if block_wait > 0:
        log.warning(f"域名 {blocked_domain} 已熔断，等待 {block_wait:.1f}s")
        time.sleep(block_wait)
        # 不更新 last_request，让下次请求也等待
        return
    if wait_time > 0:
        time.sleep(wait_time)


class RatelimitedSession:
    """v5.0.0: requests.Session 的自适应限流代理。

    包装 requests.Session，使 self.session.get(...) 自动按域名限流 + 熔断，
    供 company_report / research_report / news 等以 requests 直连的 scraper 使用，
    使其与 http_utils 的限流策略保持一致（此前 requests 直连完全绕过 rate_limit）。
    属性访问透明代理到原 Session（如 headers.update / close）。
    """

    def __init__(self, session):
        self._session = session

    def get(self, url, **kwargs):
        rate_limit(url=url)
        return self._session.get(url, **kwargs)

    def post(self, url, **kwargs):
        rate_limit(url=url)
        return self._session.post(url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return self._session.close()


# ─── 重试抖动 ────────────────────────────────────────────────────────────────

def _jitter(base: float, factor: float = JITTER_FACTOR) -> float:
    return base * (1 + random.uniform(-factor, factor))


def _apply_retry_strategy(strategy: RetryStrategy, headers: Dict[str, str],
                          session: Optional[StdlibSession], mobile: bool,
                          url: str) -> Tuple[Dict[str, str], bool]:
    """对单次重试应用指定策略，返回 (新headers, 是否切换为移动端session)"""
    h = {**headers}
    new_mobile = mobile

    if strategy == RetryStrategy.UA_ROTATE:
        h["User-Agent"] = random_ua(mobile=mobile)
    elif strategy == RetryStrategy.MOBILE_UA:
        h["User-Agent"] = random_ua(mobile=True)
        new_mobile = True
    elif strategy == RetryStrategy.FINGERPRINT:
        fp = random_fingerprint_headers(h.get("User-Agent"))
        for k, v in fp.items():
            if k not in h:
                h[k] = v
    elif strategy == RetryStrategy.ADD_AJAX_HEADER:
        h["X-Requested-With"] = "XMLHttpRequest"
    elif strategy == RetryStrategy.REFERRER_CHANGE:
        # 尝试设为同域根路径
        try:
            parsed = urllib.parse.urlparse(url)
            h["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        except Exception:
            h["Referer"] = "https://www.google.com/"
    elif strategy == RetryStrategy.DELAY_INCREASE:
        # 增加静态延迟，在调用方处理
        pass
    elif strategy == RetryStrategy.NO_CACHE:
        # 在调用方处理
        pass

    return h, new_mobile


# ─── v7.2 重试/软拦截辅助 ────────────────────────────────────────────

def _header_ci(headers: Dict[str, str], name: str) -> str:
    """大小写不敏感取响应头"""
    if not headers:
        return ""
    lname = name.lower()
    for k, v in headers.items():
        if k.lower() == lname:
            return v or ""
    return ""


def _retry_after_seconds(resp: StdlibResponse, cap: float = 60.0) -> float:
    """解析 Retry-After 响应头（仅支持秒数格式），限幅 cap"""
    ra = _header_ci(resp.headers, "Retry-After").strip()
    if not ra:
        return 0.0
    try:
        return max(0.0, min(float(int(ra)), cap))
    except (ValueError, TypeError):
        return 0.0


def _is_ssl_error(e: Exception) -> bool:
    """v7.5: 判断异常是否为 SSL 证书/握手类错误（银行等站点证书链不全常见）"""
    reason = getattr(e, "reason", None)
    if isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError):
        return True
    msg = f"{type(e).__name__}: {e}".lower()
    if not isinstance(e, (urllib.error.URLError, OSError, ConnectionError)):
        return False
    return any(k in msg for k in ("certificate", "ssl", "handshake", "tlsv1"))


def _scheme_downgrade(url: str) -> Optional[str]:
    """v7.5: https → http 协议降级（仅证书失败时兜底，返回新 URL 或 None）"""
    if url.startswith("https://"):
        return "http://" + url[len("https://"):]
    return None


def _is_soft_blocked(resp: StdlibResponse):
    """v7.2: 检测伪装成 200 的软拦截页面。

    保守策略：仅对短小（<8KB）的 HTML 响应做文本特征检测，
    避免误伤 JSON API 与正常长文章页。返回 BlockSignal 或 None。
    """
    if _detect_block is None or resp.code >= 300 or len(resp.content) > 8192:
        return None
    ct = _header_ci(resp.headers, "Content-Type").lower()
    body_start = resp.content[:64].lstrip().lower()
    if "html" not in ct and not body_start.startswith(b"<"):
        return None
    try:
        return _detect_block(resp.code, resp.headers, resp.text[:2000])
    except Exception:
        return None


def _prepare_next_attempt(strategies: List[RetryStrategy], attempt: int,
                          merged: Dict[str, str], s: Any, current_mobile: bool,
                          url: str, wait: float, use_cache: bool
                          ) -> Tuple[Dict[str, str], Any, bool, float, bool]:
    """对第 attempt 次失败应用重试策略。

    返回 (新headers, session, 是否移动端, 调整后wait, 调整后use_cache)。
    """
    if attempt >= len(strategies):
        return merged, s, current_mobile, wait, use_cache
    strat = strategies[attempt]
    if strat == RetryStrategy.DELAY_INCREASE:
        wait *= 2
    elif strat == RetryStrategy.NO_CACHE:
        use_cache = False
    merged, current_mobile = _apply_retry_strategy(strat, merged, s, current_mobile, url)
    if current_mobile and not getattr(s, 'mobile', False):
        s = get_session(mobile=True, rotate_ua=True, randomize_fingerprint=True)
    return merged, s, current_mobile, wait, use_cache


# ─── HTTP GET（v4.0: 多策略重试 + 自适应限流） ──────────────────────────────────

def http_get(url: str,
             headers: Optional[Dict[str, str]] = None,
             timeout: int = DEFAULT_TIMEOUT,
             retries: int = MAX_RETRIES,
             rate_limit_delay: float = 0,
             session: Optional[StdlibSession] = None,
             use_cache: bool = True,
             retry_strategies: Optional[List[RetryStrategy]] = None,
             detect_soft_block: bool = True,
             scheme_fallback: bool = True,
             retry_empty: bool = True,
             **kwargs) -> Optional[StdlibResponse]:
    """HTTP GET 请求（v4.0 多策略重试 + 自适应限流；v7.2 软拦截检测；
    v7.5 SSL 协议降级 + 空响应重试）

    Args:
        url: 目标 URL
        headers: 额外 headers
        timeout: 超时秒数
        retries: 最大重试次数
        rate_limit_delay: 最低限流延迟
        session: 复用已有 session
        use_cache: 是否使用 LRU 缓存
        retry_strategies: 重试策略序列，默认使用 DEFAULT_RETRY_STRATEGIES
        detect_soft_block: v7.2 是否检测 200 软拦截页面（短小反爬 HTML）
        scheme_fallback: v7.5 SSL 证书失败时自动 https→http 降级重试一次（仅公开数据）
        retry_empty: v7.5 200 空响应自动重试（部分站点首包空体）

    Returns:
        StdlibResponse 或 None
    """
    # 缓存检查
    if use_cache:
        cached = _response_cache.get(url)
        if cached is not None:
            log.debug(f"Cache hit: {url[:80]}")
            return cached

    domain = _extract_domain(url)

    # 自适应限流 + 熔断检查
    rate_limit(rate_limit_delay, url)

    s = session or get_session()
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    current_mobile = False

    # 策略列表
    strategies = retry_strategies if retry_strategies is not None else DEFAULT_RETRY_STRATEGIES

    downgraded = False      # v7.5: SSL 协议降级只做一次
    for attempt in range(retries):
        try:
            resp = s.get(url, headers=merged, timeout=timeout, **kwargs)

            if resp.code >= 400:
                # 报告失败给自适应限流器
                report_request_result(url, False, resp.code)

                wait = _jitter(RETRY_BACKOFF ** (attempt + 1))
                # v7.2: 遵从服务端 Retry-After 声明（429/503）
                if resp.code in (429, 503):
                    wait = max(wait, _retry_after_seconds(resp))

                # v7.2: 应用重试策略（统一辅助函数）
                merged, s, current_mobile, wait, use_cache = _prepare_next_attempt(
                    strategies, attempt, merged, s, current_mobile, url, wait, use_cache)

                log.warning(f"HTTP {resp.code} on {url[:80]}, retry {attempt+1}/{retries} "
                           f"in {wait:.1f}s")
                time.sleep(wait)
                continue

            # v7.5: 200 空响应自动重试（部分站点首包空体，二次请求即正常）
            if retry_empty and resp.code < 300 and not resp.content \
                    and attempt < retries - 1:
                log.warning(f"Empty body on {url[:80]}, retry {attempt+1}/{retries}")
                time.sleep(_jitter(1.0))
                continue

            # v7.2: 软拦截检测（伪装成 200 的反爬页面视为失败并重试）
            if detect_soft_block:
                sig = _is_soft_blocked(resp)
                if sig is not None:
                    report_request_result(url, False, 429)
                    wait = _jitter(RETRY_BACKOFF ** (attempt + 1))
                    merged, s, current_mobile, wait, use_cache = _prepare_next_attempt(
                        strategies, attempt, merged, s, current_mobile, url, wait, use_cache)
                    log.warning(f"Soft-block on {url[:80]} ({sig.reason}), "
                               f"retry {attempt+1}/{retries} in {wait:.1f}s")
                    time.sleep(wait)
                    continue

            # 成功：缓存、记录成功 UA、报告成功
            if use_cache:
                _response_cache.set(url, resp)
            report_request_result(url, True)
            # 记录成功 UA 供后续使用
            ua = merged.get("User-Agent", "")
            if ua:
                record_successful_ua(domain, ua)
            # v7.5: 域名编码记忆（下次同域请求直接复用）
            remember_domain_encoding(domain, getattr(resp, "_encoding", ""))

            return resp

        except Exception as e:
            # v7.5: SSL 证书失败 → 协议降级重试一次（公开数据场景，警告日志留痕）
            if scheme_fallback and not downgraded and _is_ssl_error(e):
                alt = _scheme_downgrade(url)
                if alt:
                    log.warning(f"SSL error on {url[:80]} ({e})，降级 http 重试")
                    url = alt
                    domain = _extract_domain(url)
                    downgraded = True
                    continue
            report_request_result(url, False)
            wait = _jitter(RETRY_BACKOFF ** (attempt + 1))
            merged, s, current_mobile, wait, use_cache = _prepare_next_attempt(
                strategies, attempt, merged, s, current_mobile, url, wait, use_cache)

            log.warning(f"Request failed on {url[:80]}: {e}, retry {attempt+1}/{retries} "
                       f"in {wait:.1f}s")
            time.sleep(wait)

    log.error(f"All {retries} retries exhausted for {url[:80]}")
    return None


def http_get_json(url: str,
                  timeout: int = DEFAULT_TIMEOUT,
                  session: Optional[StdlibSession] = None,
                  retry_strategies: Optional[List[RetryStrategy]] = None,
                  **kwargs) -> Optional[Dict]:
    resp = http_get(url, timeout=timeout, session=session,
                    headers={"Accept": "application/json, */*"},
                    retry_strategies=retry_strategies, **kwargs)
    if resp is not None:
        try:
            return resp.json()
        except Exception as e:
            log.warning(f"JSON parse failed for {url[:80]}: {e}")
    return None


# ─── HTTP POST（v4.0: 多策略重试 + 自适应限流） ──────────────────────────────────

def http_post(url: str,
              data: Optional[Any] = None,
              json_body: Optional[Dict] = None,
              headers: Optional[Dict[str, str]] = None,
              timeout: int = DEFAULT_TIMEOUT,
              retries: int = MAX_RETRIES,
              rate_limit_delay: float = 0,
              session: Optional[StdlibSession] = None,
              use_cache: bool = False,
              retry_strategies: Optional[List[RetryStrategy]] = None,
              scheme_fallback: bool = True,
              **kwargs) -> Optional[StdlibResponse]:
    """HTTP POST 请求（v4.0 多策略重试 + 自适应限流）

    参数与 http_get 一致，额外支持 data/json_body。
    """
    cache_key = url if use_cache else None
    domain = _extract_domain(url)

    rate_limit(rate_limit_delay, url)

    s = session or get_session()
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    current_mobile = False
    strategies = retry_strategies if retry_strategies is not None else DEFAULT_RETRY_STRATEGIES

    downgraded = False      # v7.5: SSL 协议降级只做一次
    for attempt in range(retries):
        try:
            resp = s.post(url, data=data, json_body=json_body,
                          headers=merged, timeout=timeout, **kwargs)

            if resp.code >= 400:
                report_request_result(url, False, resp.code)
                wait = _jitter(RETRY_BACKOFF ** (attempt + 1))
                # v7.2: 遵从服务端 Retry-After 声明（429/503）
                if resp.code in (429, 503):
                    wait = max(wait, _retry_after_seconds(resp))

                merged, s, current_mobile, wait, use_cache = _prepare_next_attempt(
                    strategies, attempt, merged, s, current_mobile, url, wait, use_cache)

                log.warning(f"HTTP {resp.code} on POST {url[:80]}, retry {attempt+1}/{retries} "
                           f"in {wait:.1f}s")
                time.sleep(wait)
                continue

            if use_cache and cache_key:
                _response_cache.set(cache_key, resp)
            report_request_result(url, True)
            ua = merged.get("User-Agent", "")
            if ua:
                record_successful_ua(domain, ua)
            return resp

        except Exception as e:
            # v7.5: SSL 证书失败 → 协议降级重试一次（与 http_get 一致）
            if scheme_fallback and not downgraded and _is_ssl_error(e):
                alt = _scheme_downgrade(url)
                if alt:
                    log.warning(f"SSL error on POST {url[:80]} ({e})，降级 http 重试")
                    url = alt
                    domain = _extract_domain(url)
                    downgraded = True
                    continue
            report_request_result(url, False)
            wait = _jitter(RETRY_BACKOFF ** (attempt + 1))
            merged, s, current_mobile, wait, use_cache = _prepare_next_attempt(
                strategies, attempt, merged, s, current_mobile, url, wait, use_cache)

            log.warning(f"POST failed on {url[:80]}: {e}, retry {attempt+1}/{retries} "
                       f"in {wait:.1f}s")
            time.sleep(wait)

    log.error(f"All {retries} retries exhausted for POST {url[:80]}")
    return None


# ─── 便捷函数 ───────────────────────────────────────────────────────────────

def fetch_text(url: str,
               headers: Optional[Dict[str, str]] = None,
               timeout: int = DEFAULT_TIMEOUT,
               retries: int = MAX_RETRIES,
               use_cache: bool = True,
               **kwargs) -> Optional[str]:
    resp = http_get(url, headers=headers, timeout=timeout,
                    retries=retries, use_cache=use_cache, **kwargs)
    return resp.text if resp is not None else None


def clear_cache() -> int:
    _response_cache.clear()
    return len(_response_cache)


# ─── 文件下载 ────────────────────────────────────────────────────────────────

def download_file(url: str,
                  save_dir: str = ".",
                  filename: Optional[str] = None,
                  headers: Optional[Dict[str, str]] = None,
                  timeout: int = DOWNLOAD_TIMEOUT,
                  session: Optional[StdlibSession] = None,
                  auto_referer: bool = True,
                  transcode_text: bool = True,
                  verify_content: bool = True,
                  resume: bool = True,
                  overwrite: bool = False,
                  expect_sha256: str = "",
                  min_bytes: int = 0,
                  **kwargs) -> Optional[str]:
    """下载文件（v7.5 加固 + v7.7 续传/校验）

    相比旧版：
    - 走 http_get 重试链（多策略重试/限流/软拦截检测，此前裸 session 无重试）
    - 自动注入同源 Referer（部分金融站点校验 Referer，缺失直接拒载）
    - GBK Content-Disposition 中文文件名乱码修复（v7.5）
    - 反爬页校验：期望下载文档却收到 HTML 壳页时不落盘（v7.5）
    - 文本文件自动转码为 UTF-8，修复 GB2312/GBK 源页下载后打开乱码（v7.5）

    v7.7 新增：
    - 已存在跳过：目标文件非空且非 overwrite → 直接返回（批量重跑省时）
    - 断点续传：文件名已知且存在 .part 残文件时附 Range 头，
      服务端支持则 206 只拉剩余部分；不支持则自动全量重下（仅缓存未命中时生效）
    - 完整性：expect_sha256 校验（不匹配则删文件返回 None）；min_bytes 拒收过小文件
    - .part 原子落盘：先写临时文件再 os.replace，失败不残留半成品；下载不进 LRU 缓存
    """
    merged = {**DEFAULT_HEADERS, **(headers or {})}
    # v7.5: 同源 Referer 注入（不少公告/研报下载接口校验）
    if auto_referer and not any(k.lower() == "referer" for k in merged):
        try:
            p = urllib.parse.urlparse(url)
            merged["Referer"] = f"{p.scheme}://{p.netloc}/"
        except Exception:
            pass

    # v7.7 已存在跳过（仅文件名已知时可用）
    pre_path = None
    if filename:
        pre_path = os.path.join(save_dir, sanitize_filename(filename))
        if not overwrite and os.path.isfile(pre_path) \
                and os.path.getsize(pre_path) > 0:
            log.info(f"Download skip existing: {pre_path}")
            return pre_path

    # v7.7 断点续传：存在 .part 残文件 → 附 Range 头只拉剩余（需服务端支持 206）
    part_path = (pre_path + ".part") if pre_path else None
    part_size = 0
    if resume and part_path and os.path.isfile(part_path):
        part_size = os.path.getsize(part_path)
        if part_size > 0:
            merged["Range"] = f"bytes={part_size}-"

    kwargs.setdefault("use_cache", False)   # v7.7: 下载不走 LRU 缓存（大体积且需新鲜）
    resp = http_get(url, headers=merged, timeout=timeout,
                    session=session, **kwargs)
    # v7.2: 错误响应不落盘（此前 404 页面会被当文件保存）
    if resp is None or resp.code >= 400:
        code = resp.code if resp is not None else 0
        log.error(f"Download got HTTP {code} from {url[:80]}, skip saving")
        return None
    if not resp.content:
        log.error(f"Download got empty body from {url[:80]}, skip saving")
        return None

    # v7.7: 206 = 服务端确认续传；200 = 不支持 Range，残文件作废重下
    resumed = part_size > 0 and resp.code == 206
    if part_size > 0 and not resumed:
        part_size = 0

    # 解析文件名（含 GBK 乱码修复）
    if not filename:
        filename = _extract_filename(resp, url)
    filename = sanitize_filename(filename)
    if not filename or filename == "unnamed":
        filename = f"download_{int(time.time())}"
    data = resp.content
    # v7.5: 反爬页校验 — 期望二进制文档却收到 HTML，多为反爬/登录壳页，不落盘
    if verify_content and _looks_html_magic(data):
        ext = os.path.splitext(filename)[1].lower()
        if ext in _BINARY_DOC_EXTS:
            log.warning(f"Download {url[:80]} 返回 HTML（疑似反爬页），不保存为 {ext}")
            return None
    # v7.5: 文本文件自动转码 UTF-8（修复下载乱码）
    if transcode_text and _is_text_download(filename, resp):
        data = resp.text.encode("utf-8")
        if not filename.lower().endswith((".html", ".htm", ".txt", ".csv",
                                          ".json", ".xml", ".js")):
            filename += ".txt"
    os.makedirs(save_dir, exist_ok=True)
    save_path = os.path.join(save_dir, filename)
    # v7.7 .part 原子落盘：续传追加 / 全量先写临时再 replace
    if resumed and part_path and \
            os.path.abspath(part_path) == os.path.abspath(save_path + ".part"):
        with open(part_path, "ab") as f:
            f.write(data)
        os.replace(part_path, save_path)
    else:
        if part_path and os.path.isfile(part_path) and \
                os.path.abspath(part_path) != os.path.abspath(save_path + ".part"):
            try:            # 最终文件名由服务端决定且与预设不一致 → 清旧残文件
                os.remove(part_path)
            except OSError:
                pass
        tmp_path = save_path + ".part"
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, save_path)
    # v7.7 完整性校验
    if expect_sha256:
        actual = file_sha256(save_path)
        if actual != expect_sha256.strip().lower():
            log.error(f"Download sha256 mismatch: {url[:80]} "
                      f"expect {expect_sha256[:12]}… got {actual[:12]}…")
            try:
                os.remove(save_path)
            except OSError:
                pass
            return None
    if min_bytes and os.path.getsize(save_path) < min_bytes:
        log.error(f"Download too small (<{min_bytes}B): {url[:80]}")
        try:
            os.remove(save_path)
        except OSError:
            pass
        return None
    log.info(f"Downloaded: {save_path}")
    return save_path


_BINARY_DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx",
                    ".ppt", ".pptx", ".zip", ".rar", ".7z"}


def _looks_html_magic(data: bytes) -> bool:
    """魔数判断响应体是否为 HTML（而非声明的文档类型）"""
    head = (data or b"")[:64].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html")


def _is_text_download(filename: str, resp: StdlibResponse) -> bool:
    """判断下载目标是否为文本类文件（可安全转码为 UTF-8）"""
    ct = _header_ci(resp.headers, "Content-Type").lower()
    if ct.startswith(("text/", "application/json", "application/xml")):
        return True
    return filename.lower().endswith((".html", ".htm", ".txt", ".csv"))


def _fix_mojibake_filename(name: str) -> str:
    """v7.5: 修复 Content-Disposition 中文文件名乱码。
    urllib 按 latin-1 解码响应头，中文文件名（实为 GBK 字节）会呈现乱码；
    回环 latin-1 编码后用 gb18030 重解，若结果含 CJK 则采用。"""
    if not name or name.isascii():
        return name
    try:
        fixed = name.encode("latin-1").decode("gb18030")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    if any('\u4e00' <= ch <= '\u9fff' for ch in fixed):
        return fixed
    return name


def _extract_filename(resp: StdlibResponse, url: str) -> str:
    cd = resp.headers.get("Content-Disposition", "") or _header_ci(
        resp.headers, "Content-Disposition")
    if cd:
        m = re.search(r"filename\*=UTF-8''(.+?)(?:;|$)", cd, re.IGNORECASE)
        if m:
            return _fix_mojibake_filename(urllib.parse.unquote(m.group(1).strip()))
        m = re.search(r'filename=["\']?([^"\';]+)', cd)
        if m:
            return _fix_mojibake_filename(m.group(1).strip())
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(path)
    if name and '.' in name:
        return _fix_mojibake_filename(urllib.parse.unquote(name))
    return f"download_{int(time.time())}"


def sanitize_filename(name: str, max_len: int = 100) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > max_len:
        name = name[:max_len]
    # If the name is empty or only underscores after sanitization, use a default
    if not name or not name.strip('_'):
        return "unnamed"
    return name


def file_sha256(path: str) -> str:
    """v7.7: 文件 sha256（分块读取，支持大文件）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def batch_download(urls: List[str], save_dir: str = ".",
                   concurrency: int = 3,
                   session: Optional[StdlibSession] = None,
                   progress: Optional[Callable[[int, int, str, Optional[str]], None]] = None,
                   **dl_kwargs) -> Dict[str, Any]:
    """v7.7 并发批量下载（单个失败不阻断其余）。

    Args:
        urls: 下载链接列表
        save_dir: 保存目录（共用）
        concurrency: 并发数（建议 ≤4，防风控）
        session: 复用会话（共享 Cookie/指纹）
        progress: 进度回调 (done, total, url, path_or_None)
        **dl_kwargs: 透传 download_file（resume/overwrite/expect_sha256 等）

    Returns:
        {"total", "ok", "failed": [url...], "files": {url: path/None}, "elapsed"}
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    start = time.monotonic()
    files: Dict[str, Optional[str]] = {}
    done = 0
    dl_kwargs.pop("filename", None)     # 各文件名自动解析，不接受统一指定
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 8))) as pool:
        futures = {pool.submit(download_file, u, save_dir=save_dir,
                               session=session, **dl_kwargs): u for u in urls}
        for fut in as_completed(futures):
            u = futures[fut]
            try:
                files[u] = fut.result()
            except Exception as e:
                log.error(f"batch_download {u[:80]} error: {e}")
                files[u] = None
            done += 1
            if progress:
                try:
                    progress(done, len(urls), u, files[u])
                except Exception:
                    pass
    ok_paths = [p for p in files.values() if p]
    return {"total": len(urls), "ok": len(ok_paths),
            "failed": [u for u, p in files.items() if not p],
            "files": files,
            "elapsed": round(time.monotonic() - start, 2)}


# ─── 错误诊断 (v4.3.1 新增，针对 R 维度优化) ──────────────────────────────────

# 错误码 → 用户可读解释 + 建议（与 TROUBLESHOOTING.md §8 对齐）
_HTTP_DIAGNOSTIC_TABLE = {
    400: ("请求格式错误", "检查 URL 参数和请求头"),
    401: ("需要鉴权", "当前为公开接口无需登录；如错误持续，可能源站策略变更"),
    403: ("访问被拒（IP/UA/反爬触发）", "切换代理 IP 或降低频率，详见 TROUBLESHOOTING.md §3.1"),
    404: ("资源不存在", "检查 URL 是否拼写正确，或更新到机构列表最新版"),
    418: ("被反爬系统识别", "启用 UA 轮换 + 降低频率，详见 TROUBLESHOOTING.md §3.4"),
    429: ("请求过于频繁", "降低并发到 max_workers=3 或加长 request_delay"),
    500: ("源站内部错误", "稍后重试，源站可能临时异常"),
    502: ("网关错误（CDN 故障）", "切换代理或等待 5-15 分钟"),
    503: ("服务暂不可用", "稍后重试；可能是源站维护中"),
    521: ("CDN Web 服务器宕机", "切换代理或等待"),
    522: ("CDN 连接超时", "切换代理或降低并发"),
    523: ("CDN 不可达", "源站 CDN 故障，等待 1-30 分钟后重试"),
}


def get_diagnostic(code_or_exc, url: str = "") -> dict:
    """根据 HTTP 状态码或异常返回结构化诊断信息。

    用于标准化网络错误的展示，方便用户在 [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) 中查询。

    Args:
        code_or_exc: HTTP 状态码（int）/ 异常对象（urllib.error.HTTPError, ConnectionError 等）
        url: 触发该错误的 URL（用于日志）

    Returns:
        dict: {
            "code": int 或 None,
            "title": str,         # 简短说明
            "suggestion": str,    # 可执行建议
            "doc_link": str,      # TROUBLESHOOTING 锚点
            "url": str,           # 触发 URL
        }

    Examples:
        >>> get_diagnostic(403, "https://example.com")
        {'code': 403, 'title': '访问被拒（IP/UA/反爬触发）',
         'suggestion': '切换代理 IP 或降低频率，详见 TROUBLESHOOTING.md §3.1',
         'doc_link': '#31-ip-被封--一直-403',
         'url': 'https://example.com'}

        >>> try:
        ...     urllib.request.urlopen(url)
        ... except urllib.error.HTTPError as e:
        ...     print(get_diagnostic(e, url))
    """
    # 提取 code（兼容多种输入）
    if isinstance(code_or_exc, int):
        code = code_or_exc
    elif hasattr(code_or_exc, "code"):
        code = int(code_or_exc.code)
    elif hasattr(code_or_exc, "status_code"):
        code = int(code_or_exc.status_code)
    else:
        code = None

    if code is not None and code in _HTTP_DIAGNOSTIC_TABLE:
        title, suggestion = _HTTP_DIAGNOSTIC_TABLE[code]
        # 根据 code 选择对应的 §X.Y 锚点
        anchors = {
            400: "#81-错误码速查表",   # 占位，实际查看速查表
            401: "#81-错误码速查表",
            403: "#31-ip-被封--一直-403",
            404: "#81-错误码速查表",
            418: "#34-user-agent-失效--抓到的是请升级浏览器",
            429: "#81-错误码速查表",
            500: "#82-反复-504-502-网关错误",
            502: "#82-反复-504-502-网关错误",
            503: "#82-反复-504-502-网关错误",
            521: "#82-反复-504-502-网关错误",
            522: "#82-反复-504-502-网关错误",
            523: "#82-反复-504-502-网关错误",
        }
        doc_link = anchors.get(code, "#81-错误码速查表")
    else:
        title = "未知错误"
        suggestion = "查看完整堆栈后查阅 TROUBLESHOOTING.md 或提 issue"
        doc_link = "#9-如何提-issue"

    return {
        "code": code,
        "title": title,
        "suggestion": suggestion,
        "doc_link": doc_link,
        "url": url,
    }


def format_diagnostic(code_or_exc, url: str = "") -> str:
    """便捷函数：把 get_diagnostic() 格式化成可粘贴的多行字符串。"""
    d = get_diagnostic(code_or_exc, url)
    lines = []
    if d["code"] is not None:
        lines.append(f"❌ HTTP {d['code']}: {d['title']}")
    else:
        lines.append(f"❌ {d['title']}")
    if d["url"]:
        lines.append(f"   URL: {d['url']}")
    lines.append(f"   建议: {d['suggestion']}")
    lines.append(f"   文档: TROUBLESHOOTING.md{d['doc_link']}")
    return "\n".join(lines)

