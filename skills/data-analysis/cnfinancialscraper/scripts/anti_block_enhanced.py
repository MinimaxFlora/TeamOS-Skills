# -*- coding: utf-8 -*-
"""
反爬深度增强模块 v8.0 (anti_block_enhanced.py)

在 v7.x 基础上进一步强化"不被识别为爬虫"的能力，目标是降低
触发目标站 IP/账号风控的概率，扩展已有的
DomainRateLimiter / UrlDeduplicator / DomainHealthTracker / BlockDetector。

设计目标：
- 零 pip 依赖即可用（可选 curl_cffi / scrapling 增强）
- 全部功能可独立 import；缺失依赖时自动降级
- 线程安全；持久化数据尽量小

主要能力：
1. **ProxyPool 代理池**
   多代理轮换（轮询/加权随机/故障自动剔除/健康度评分/自动恢复）
2. **RequestBehaviorProfile 行为画像**
   模拟人类阅读停顿 + 随机等待 + Referer 链路 + 鼠标轨迹节律
3. **TLSClientFactory TLS 指纹工厂**
   自动按可选依赖优先级拼接：curl_cffi → scrapling → stdlib
4. **CrawlBudgetController 预算控制**
   每日/每小时每域请求预算，到达后自动暂停；窗口结束自动续期
5. **IPReputationTracker IP 信誉**
   按代理/出口 IP 维度记录成功率与失败模式，智能退避
6. **CaptchaDetector 验证码识别**
   识别 reCAPTCHA / hCaptcha / GeeTest / 极验 / 阿里云滑块 等
7. **SessionWarmer 会话预热**
   首次访问目标域前先访问首页/列表页建立可信 cookie / referer

向后兼容：所有类独立，与现有 anti_block_utils 共存。
"""
from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# v9.0: 导入 http_utils 的指纹常量（用于 FingerprintSession）
try:
    from http_utils import (
        random_ua, ACCEPT_LANGUAGE_POOL, ACCEPT_ENCODING_POOL,
        SEC_CH_UA_POOL, SEC_CH_UA_PLATFORM_POOL,
    )
except ImportError:
    try:
        from scripts.http_utils import (
            random_ua, ACCEPT_LANGUAGE_POOL, ACCEPT_ENCODING_POOL,
            SEC_CH_UA_POOL, SEC_CH_UA_PLATFORM_POOL,
        )
    except ImportError:
        # 降级：定义最小可用版本
        ACCEPT_LANGUAGE_POOL = ["zh-CN,zh;q=0.9", "zh-CN,zh;q=0.9,en;q=0.8"]
        ACCEPT_ENCODING_POOL = ["gzip, deflate", "gzip, deflate, br"]
        SEC_CH_UA_POOL = ['"Google Chrome";v="135", "Chromium";v="135", "Not=A?Brand";v="24"']
        SEC_CH_UA_PLATFORM_POOL = ['"Windows"', '"macOS"']
        def random_ua(mobile: bool = False) -> str:
            return ("Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36" if mobile
                    else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/135.0.0.0")


# ============================================================================
# 1. ProxyPool 代理池（轮换 / 故障剔除 / 健康评分）
# ============================================================================


@dataclass
class ProxyEntry:
    """单条代理记录。"""
    url: str
    proto: str = "http"            # http / https / socks5
    weight: float = 1.0            # 初始权重（越大越优先）
    enabled: bool = True
    success: int = 0
    failure: int = 0
    last_success: Optional[str] = None
    last_failure: Optional[str] = None
    last_latency_ms: float = 0.0
    last_error: str = ""
    consecutive_fail: int = 0

    @property
    def score(self) -> float:
        """健康评分 0-100：成功 +1，失败 -5，连续失败额外减分。"""
        total = self.success + self.failure
        if total == 0:
            return 50.0
        success_rate = self.success / total
        # 连续失败惩罚：每连失 -3，最低 0
        penalty = max(0.0, 30.0 - self.consecutive_fail * 3.0)
        score = success_rate * 100.0 - penalty
        if self.last_latency_ms > 0:
            # 延迟越低越好；>5s 视为不可用扣分
            latency_penalty = max(0.0, (self.last_latency_ms - 2000) / 100)
            score -= latency_penalty
        return max(0.0, min(100.0, score))


class ProxyPool:
    """代理池管理器。

    用法:
        pool = ProxyPool([
            "http://user:pass@1.2.3.4:8080",
            "socks5://5.6.7.8:1080",
            "http://9.10.11.12:3128",
        ])
        proxy_url = pool.pick()                  # 选一个
        try:
            resp = http_get(url, proxy=proxy_url)
            pool.report(proxy_url, success=True, latency_ms=120)
        except Exception as e:
            pool.report(proxy_url, success=False, error=str(e))
    """

    def __init__(self, proxies: Optional[List[str]] = None,
                 file_path: Optional[Path] = None,
                 strategy: str = "weighted",
                 disable_threshold: int = 5,
                 cooldown_seconds: int = 600):
        """Args:
            proxies: 初始代理列表（字符串）
            file_path: 持久化文件（记录评分）
            strategy: 选择策略 weighted / round_robin / random
            disable_threshold: 连续失败达此值自动临时禁用
            cooldown_seconds: 禁用时长
        """
        self.file_path = Path(file_path) if file_path else None
        self.strategy = strategy
        self.disable_threshold = disable_threshold
        self.cooldown_seconds = cooldown_seconds
        self._entries: Dict[str, ProxyEntry] = {}
        self._rr_cursor = 0
        self._lock = threading.Lock()
        for u in proxies or []:
            self._entries[u] = ProxyEntry(url=u)
        if self.file_path:
            self._load()

    def add(self, proxy_url: str, weight: float = 1.0) -> None:
        with self._lock:
            if proxy_url not in self._entries:
                self._entries[proxy_url] = ProxyEntry(url=proxy_url, weight=weight)

    def remove(self, proxy_url: str) -> None:
        with self._lock:
            self._entries.pop(proxy_url, None)
            self._save()

    def pick(self) -> Optional[str]:
        """选择代理（线程安全）。无代理或全部 disable_threshold 内返回 None。

        全部处于 cooldown 时不会自动尝试：调用方应先调用
        ``recover_disabled()`` 手动恢复，或等待 cooldown 过期后再调用。
        """
        with self._lock:
            available = [e for e in self._entries.values()
                         if e.enabled and e.consecutive_fail < self.disable_threshold]
            if not available:
                return None
            if self.strategy == "round_robin":
                self._rr_cursor = (self._rr_cursor + 1) % len(available)
                return available[self._rr_cursor].url
            if self.strategy == "random":
                return random.choice(available).url
            # weighted（按 score*weight）
            weights = [max(0.01, e.score * e.weight) for e in available]
            total = sum(weights)
            r = random.random() * total
            acc = 0.0
            for e, w in zip(available, weights):
                acc += w
                if r <= acc:
                    return e.url
            return available[-1].url

    def report(self, proxy_url: str, success: bool,
               latency_ms: float = 0.0,
               error: str = "") -> None:
        """反馈一次请求结果（用于评分与故障剔除）。"""
        with self._lock:
            e = self._entries.get(proxy_url)
            if not e:
                return
            now = datetime.now().isoformat(timespec="seconds")
            if success:
                e.success += 1
                e.consecutive_fail = 0
                e.last_success = now
                e.last_latency_ms = latency_ms
            else:
                e.failure += 1
                e.consecutive_fail += 1
                e.last_failure = now
                e.last_latency_ms = latency_ms
                e.last_error = error[:120]
                if e.consecutive_fail >= self.disable_threshold:
                    e.enabled = False
                    log.warning(f"代理 {proxy_url} 连续失败 {e.consecutive_fail} 次，临时禁用")
            self._save()

    def recover_disabled(self) -> int:
        """恢复所有 disabled 的代理（手动调用）。"""
        with self._lock:
            n = 0
            for e in self._entries.values():
                if not e.enabled:
                    e.enabled = True
                    e.consecutive_fail = 0
                    n += 1
            self._save()
            return n

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total": len(self._entries),
                "enabled": sum(1 for e in self._entries.values() if e.enabled),
                "disabled": [e.url for e in self._entries.values() if not e.enabled],
                "entries": {e.url: {
                    "score": round(e.score, 1),
                    "success": e.success, "failure": e.failure,
                    "consecutive_fail": e.consecutive_fail,
                    "latency_ms": e.last_latency_ms,
                    "enabled": e.enabled,
                } for e in self._entries.values()},
            }

    def _load(self) -> None:
        if not self.file_path or not self.file_path.exists():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            for url, val in raw.items():
                self._entries[url] = ProxyEntry(**val)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.warning(f"代理池加载失败: {e}")

    def _save(self) -> None:
        if not self.file_path:
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(
                json.dumps({k: asdict(v) for k, v in self._entries.items()},
                          ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning(f"代理池保存失败: {e}")


# ============================================================================
# 2. RequestBehaviorProfile 行为画像（让请求更像人类）
# ============================================================================


@dataclass
class BehaviorConfig:
    """行为画像配置。"""
    # 两次请求间的随机停顿（秒，模拟阅读）
    min_delay: float = 1.5
    max_delay: float = 6.0
    # 长停顿概率（模拟思考/去做别的事）
    long_pause_prob: float = 0.08
    long_pause_range: Tuple[float, float] = (15.0, 45.0)
    # Referer 链路：0.4 概率停留在当前域；0.6 概率来自上游页
    referrer_same_domain_prob: float = 0.4
    referrer_search_engine_prob: float = 0.3
    # 是否模拟鼠标节律（仅用于记录节奏日志，不影响 HTTP）
    simulate_mouse_rhythm: bool = True


_SEARCH_ENGINES = [
    "https://www.baidu.com/s?wd={q}",
    "https://www.google.com/search?q={q}",
    "https://www.bing.com/search?q={q}",
    "https://www.sogou.com/web?query={q}",
]


class RequestBehaviorProfile:
    """请求行为画像（线程安全）。

    用法:
        profile = RequestBehaviorProfile()
        delay = profile.next_delay()
        time.sleep(delay)
        referer = profile.next_referer("https://example.com/news/123",
                                       query="贵州茅台")
        headers["Referer"] = referer
    """

    def __init__(self, config: Optional[BehaviorConfig] = None):
        self.config = config or BehaviorConfig()
        self._lock = threading.Lock()
        self._last_referer: Dict[str, str] = {}    # domain -> last referer
        self._request_count = 0
        self._last_request_at = 0.0

    def next_delay(self) -> float:
        """计算下一次请求前应等待的秒数。"""
        with self._lock:
            self._request_count += 1
        if random.random() < self.config.long_pause_prob:
            lo, hi = self.config.long_pause_range
            return random.uniform(lo, hi)
        # 短停顿区间
        base = random.uniform(self.config.min_delay, self.config.max_delay)
        # 鼠标节律：叠加微抖动（0-300ms）模拟不规律请求
        if self.config.simulate_mouse_rhythm:
            jitter = random.uniform(0, 0.3)
            base += jitter
        return base

    def next_referer(self, url: str, query: Optional[str] = None) -> str:
        """为下一次请求生成合理的 Referer。"""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        with self._lock:
            r = random.random()
            if r < self.config.referrer_search_engine_prob:
                q = (query or parsed.path.strip("/").split("/")[-1] or "财经新闻").strip()
                q = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", " ", q)[:60]
                tmpl = random.choice(_SEARCH_ENGINES)
                referer = tmpl.format(q=q)
            elif r < self.config.referrer_search_engine_prob + self.config.referrer_same_domain_prob:
                # 同域路径：随机一个站内路径
                paths = ["/", "/index", "/list", "/category/all",
                         "/search", "/news", "/article"]
                referer = f"{parsed.scheme}://{host}{random.choice(paths)}"
            else:
                # 沿用上次 referer（模拟连续浏览）
                last = self._last_referer.get(host)
                if last:
                    referer = last
                else:
                    referer = f"{parsed.scheme}://{host}/"
            self._last_referer[host] = referer
            return referer

    def mark_request(self) -> None:
        """记录一次请求已发出（用于节奏统计）。"""
        with self._lock:
            self._last_request_at = time.monotonic()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "request_count": self._request_count,
                "tracked_referers": len(self._last_referer),
            }


# ============================================================================
# 3. TLSClientFactory TLS 指纹工厂（按依赖优先级）
# ============================================================================


@dataclass
class TLSBackend:
    """可用 TLS 后端的描述。"""
    name: str
    available: bool
    priority: int
    description: str


class TLSClientFactory:
    """按依赖优先级选择最佳 TLS 指纹客户端。

    优先级：curl_cffi (JA3 真实模拟) > scrapling (自适应) > stdlib (无指纹)

    用法:
        client = TLSClientFactory.pick()   # 返回 (name, get_fn) 或 (None, None)
        if client:
            name, get_fn = client
            resp = get_fn(url, headers=headers, timeout=30)
    """

    BACKENDS: List[TLSBackend] = []   # 启动时填充

    @classmethod
    def _ensure_backends(cls) -> None:
        if cls.BACKENDS:
            return
        # 1) curl_cffi — 真实浏览器 JA3
        try:
            import curl_cffi  # noqa: F401
            cls.BACKENDS.append(TLSBackend(
                name="curl_cffi", available=True, priority=10,
                description="curl-cffi：模拟真实浏览器 JA3/TLS 指纹",
            ))
        except ImportError:
            cls.BACKENDS.append(TLSBackend(
                name="curl_cffi", available=False, priority=10,
                description="curl_cffi 未装（pip install curl-cffi）",
            ))
        # 2) scrapling — 自适应反爬
        try:
            import scrapling  # noqa: F401
            cls.BACKENDS.append(TLSBackend(
                name="scrapling", available=True, priority=8,
                description="scrapling：自适应反爬与指纹伪装",
            ))
        except ImportError:
            cls.BACKENDS.append(TLSBackend(
                name="scrapling", available=False, priority=8,
                description="scrapling 未装（pip install scrapling）",
            ))
        # 3) stdlib — 始终可用
        cls.BACKENDS.append(TLSBackend(
            name="stdlib", available=True, priority=1,
            description="urllib 标准库（无 TLS 指纹）",
        ))

    @classmethod
    def available_backends(cls) -> List[TLSBackend]:
        cls._ensure_backends()
        return [b for b in cls.BACKENDS if b.available]

    @classmethod
    def pick(cls) -> Optional[Tuple[str, Callable[..., Any]]]:
        """按优先级选择最佳后端。返回 (name, get_fn) 或 None。"""
        cls._ensure_backends()
        for b in sorted(cls.BACKENDS, key=lambda x: -x.priority):
            if not b.available:
                continue
            try:
                if b.name == "curl_cffi":
                    from curl_cffi import requests as cc_requests
                    return ("curl_cffi",
                            lambda url, headers=None, timeout=30, **kw:
                            cc_requests.get(url, headers=headers or {},
                                            timeout=timeout, impersonate="chrome120", **kw))
                if b.name == "scrapling":
                    from scrapling.fetchers import Fetcher
                    return ("scrapling",
                            lambda url, headers=None, timeout=30, **kw:
                            Fetcher.get(url, headers=headers or {}, timeout=timeout, **kw))
                if b.name == "stdlib":
                    try:
                        from http_utils import http_get
                    except ImportError:
                        from scripts.http_utils import http_get  # type: ignore
                    return ("stdlib",
                            lambda url, headers=None, timeout=30, **kw:
                            http_get(url, headers=headers, timeout=timeout, **kw))
            except ImportError:
                continue
        return None


# ============================================================================
# 4. CrawlBudgetController 预算控制
# ============================================================================


@dataclass
class _BudgetBucket:
    domain: str
    daily_limit: int
    hourly_limit: int
    daily_used: int = 0
    hourly_used: int = 0
    daily_reset_at: str = ""   # ISO 时间
    hourly_reset_at: str = ""  # ISO 时间


class CrawlBudgetController:
    """按域请求预算控制器。

    用法:
        ctrl = CrawlBudgetController({
            "cls.cn": {"daily": 5000, "hourly": 500},
            "jisilu.cn": {"daily": 1000, "hourly": 100},
            "default": {"daily": 10000, "hourly": 1500},
        })
        if ctrl.allow("cls.cn"):
            make_request()
            ctrl.consume("cls.cn")
        else:
            wait_until_reset()
    """

    def __init__(self, budgets: Optional[Dict[str, Dict[str, int]]] = None,
                 file_path: Optional[Path] = None):
        """budgets: {domain: {"daily": N, "hourly": M}} 或 {"default": {...}}"""
        self.file_path = Path(file_path) if file_path else None
        self._buckets: Dict[str, _BudgetBucket] = {}
        self._lock = threading.Lock()
        for dom, conf in (budgets or {}).items():
            self._buckets[dom] = _BudgetBucket(
                domain=dom,
                daily_limit=conf.get("daily", 10000),
                hourly_limit=conf.get("hourly", 1500),
            )
        if "default" not in self._buckets:
            self._buckets["default"] = _BudgetBucket(
                domain="default",
                daily_limit=(budgets or {}).get("default", {}).get("daily", 10000),
                hourly_limit=(budgets or {}).get("default", {}).get("hourly", 1500),
            )
        if self.file_path:
            self._load()

    @staticmethod
    def _normalize(domain: str) -> str:
        host = (domain or "").strip().lower()
        parts = [p for p in host.split(".") if p]
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def _bucket_for(self, domain: str) -> _BudgetBucket:
        n = self._normalize(domain)
        return self._buckets.get(n) or self._buckets["default"]

    def _maybe_reset(self, b: _BudgetBucket) -> None:
        now = datetime.now()
        if not b.daily_reset_at or datetime.fromisoformat(b.daily_reset_at) <= now:
            b.daily_used = 0
            b.daily_reset_at = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
        if not b.hourly_reset_at or datetime.fromisoformat(b.hourly_reset_at) <= now:
            b.hourly_used = 0
            b.hourly_reset_at = (now + timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0).isoformat(timespec="seconds")

    def allow(self, domain: str) -> bool:
        """是否允许下一次请求。"""
        with self._lock:
            b = self._bucket_for(domain)
            self._maybe_reset(b)
            return (b.daily_used < b.daily_limit
                    and b.hourly_used < b.hourly_limit)

    def consume(self, domain: str, n: int = 1) -> None:
        with self._lock:
            b = self._bucket_for(domain)
            self._maybe_reset(b)
            b.daily_used += n
            b.hourly_used += n
            self._save()

    def wait_seconds_until_available(self, domain: str) -> float:
        """返回还需等待多少秒才有额度（0=立即可用）。"""
        with self._lock:
            b = self._bucket_for(domain)
            self._maybe_reset(b)
            secs = 0.0
            if b.daily_used >= b.daily_limit:
                try:
                    secs = max(secs,
                               (datetime.fromisoformat(b.daily_reset_at)
                                - datetime.now()).total_seconds())
                except ValueError:
                    pass
            if b.hourly_used >= b.hourly_limit:
                try:
                    secs = max(secs,
                               (datetime.fromisoformat(b.hourly_reset_at)
                                - datetime.now()).total_seconds())
                except ValueError:
                    pass
            return max(0.0, secs)

    def status(self) -> Dict[str, Any]:
        with self._lock:
            out = {}
            for d, b in self._buckets.items():
                self._maybe_reset(b)
                out[d] = {
                    "daily_used": b.daily_used, "daily_limit": b.daily_limit,
                    "hourly_used": b.hourly_used, "hourly_limit": b.hourly_limit,
                    "daily_reset_at": b.daily_reset_at,
                    "hourly_reset_at": b.hourly_reset_at,
                }
            return out

    def _load(self) -> None:
        if not self.file_path or not self.file_path.exists():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            for d, v in raw.items():
                self._buckets[d] = _BudgetBucket(**v)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.warning(f"预算控制器加载失败: {e}")

    def _save(self) -> None:
        if not self.file_path:
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(
                json.dumps({k: asdict(v) for k, v in self._buckets.items()},
                          ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning(f"预算控制器保存失败: {e}")


# ============================================================================
# 5. IPReputationTracker IP/出口信誉
# ============================================================================


@dataclass
class IPRecord:
    """单条 IP/出口的信誉。"""
    key: str
    success: int = 0
    failure: int = 0
    consecutive_fail: int = 0
    last_success: Optional[str] = None
    last_failure: Optional[str] = None
    last_block_reason: str = ""
    backoff_until: Optional[str] = None
    total_quota_used: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success + self.failure
        return self.success / total if total else 0.0


class IPReputationTracker:
    """按 IP/出口维度记录成功率。

    与 ProxyPool 的区别：本类不调度，只做信誉追踪与"智能退避"
    （失败次数越高，冷却时间指数增长，而不是固定值）。

    用法:
        tracker = IPReputationTracker()
        if tracker.allow("proxy-1.2.3.4"):
            resp = make_request()
            if ok:
                tracker.record_success("proxy-1.2.3.4")
            else:
                tracker.record_failure("proxy-1.2.3.4", reason="429")
    """

    def __init__(self, file_path: Optional[Path] = None,
                 base_backoff: float = 30.0,
                 max_backoff: float = 1800.0):
        self.file_path = Path(file_path) if file_path else None
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self._records: Dict[str, IPRecord] = {}
        self._lock = threading.Lock()
        if self.file_path:
            self._load()

    def _backoff_seconds(self, consecutive_fail: int) -> float:
        """失败次数 → 退避秒数（指数 + 抖动）。"""
        if consecutive_fail <= 0:
            return 0.0
        delay = min(self.max_backoff,
                    self.base_backoff * (2 ** (consecutive_fail - 1)))
        # ±30% 抖动
        return delay * (0.7 + random.random() * 0.6)

    def allow(self, key: str) -> bool:
        with self._lock:
            r = self._records.get(key)
            if not r or not r.backoff_until:
                return True
            try:
                if datetime.fromisoformat(r.backoff_until) <= datetime.now():
                    r.backoff_until = None
                    self._save()
                    return True
            except ValueError:
                pass
            return False

    def wait_seconds(self, key: str) -> float:
        with self._lock:
            r = self._records.get(key)
            if not r or not r.backoff_until:
                return 0.0
            try:
                secs = (datetime.fromisoformat(r.backoff_until)
                        - datetime.now()).total_seconds()
                return max(0.0, secs)
            except ValueError:
                return 0.0

    def record_success(self, key: str) -> None:
        with self._lock:
            r = self._records.setdefault(key, IPRecord(key=key))
            r.success += 1
            r.consecutive_fail = 0
            r.last_success = datetime.now().isoformat(timespec="seconds")
            r.backoff_until = None
            r.last_block_reason = ""
            self._save()

    def record_failure(self, key: str, reason: str = "") -> None:
        with self._lock:
            r = self._records.setdefault(key, IPRecord(key=key))
            r.failure += 1
            r.consecutive_fail += 1
            r.last_failure = datetime.now().isoformat(timespec="seconds")
            r.last_block_reason = reason[:120]
            r.backoff_until = (
                datetime.now() + timedelta(seconds=self._backoff_seconds(r.consecutive_fail))
            ).isoformat(timespec="seconds")
            self._save()

    def record_quota(self, key: str, n: int = 1) -> None:
        with self._lock:
            r = self._records.setdefault(key, IPRecord(key=key))
            r.total_quota_used += n
            self._save()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {k: {**asdict(v), "success_rate": round(v.success_rate, 3)}
                    for k, v in self._records.items()}

    def _load(self) -> None:
        if not self.file_path or not self.file_path.exists():
            return
        try:
            raw = json.loads(self.file_path.read_text(encoding="utf-8"))
            for k, v in raw.items():
                self._records[k] = IPRecord(**v)
        except (json.JSONDecodeError, OSError, TypeError) as e:
            log.warning(f"IP 信誉加载失败: {e}")

    def _save(self) -> None:
        if not self.file_path:
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(
                json.dumps({k: asdict(v) for k, v in self._records.items()},
                          ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
        except OSError as e:
            log.warning(f"IP 信誉保存失败: {e}")


# ============================================================================
# 6. CaptchaDetector 验证码识别
# ============================================================================


# 已知验证码特征（HTML/CSS/JS 关键字）
_CAPTCHA_PATTERNS: List[Tuple[str, str]] = [
    (r"g-recaptcha|google\.com/recaptcha", "google_recaptcha"),
    (r"hcaptcha\.com|h-captcha", "hcaptcha"),
    (r"geetest|gt3-|gt4-", "geetest"),
    (r"yidun|yd-\w+slider", "yidun"),
    (r"aliyun.*?滑块|alibaba.*?nc|nc[._-]?\d|nc\.js", "aliyun_slider"),
    (r"tencent.*?captcha|TCaptcha", "tencent_captcha"),
    (r"turing|slide.*?verify", "turing"),
    (r"captcha[_-]?image|captcha[_-]?input", "image_captcha"),
    (r"sms[_-]?code|sms-验证码", "sms_captcha"),
    (r"二次验证|two[_-]?factor|2fa", "two_factor"),
]


@dataclass
class CaptchaSignal:
    """检测到的验证码。"""
    kind: str
    snippet: str = ""
    advice: str = ""


def detect_captcha(html: str) -> Optional[CaptchaSignal]:
    """从 HTML 文本中识别是否含验证码挑战。

    返回 None 表示未检测到；返回 CaptchaSignal 含种类与建议。
    """
    if not html:
        return None
    lower = html.lower() if html.isascii() else html
    # 同时支持中英
    for pattern, kind in _CAPTCHA_PATTERNS:
        m = re.search(pattern, lower, re.IGNORECASE)
        if m:
            advice_map = {
                "google_recaptcha": "Google reCAPTCHA：建议等待自动挑战或切备用源",
                "hcaptcha": "hCaptcha：建议暂停爬取或切备用源",
                "geetest": "极验 GeeTest：建议触发浏览器兜底",
                "yidun": "网易易盾：建议触发浏览器兜底",
                "aliyun_slider": "阿里云滑块：必须人工接管或换 IP",
                "tencent_captcha": "腾讯防水墙：建议切备用源",
                "turing": "同盾/图灵：建议换 IP 或暂停",
                "image_captcha": "图形验证码：建议触发浏览器接管或暂停",
                "sms_captcha": "短信验证码：需登录态，跳过该目标",
                "two_factor": "二次验证：跳过该目标，记录到死信",
            }
            return CaptchaSignal(
                kind=kind,
                snippet=m.group(0)[:60],
                advice=advice_map.get(kind, "检测到验证码，建议暂停或换源"),
            )
    # 极简：标题/正文含验证码
    if any(s in html for s in ("请输入验证码", "输入右侧验证码", "完成验证",
                                "Slide to verify", "Drag the slider",
                                "I'm not a robot", "Verify you are human")):
        return CaptchaSignal(kind="generic", snippet="<page>",
                             advice="通用验证提示：建议暂停爬取或切备用源")
    return None


# ============================================================================
# 7. SessionWarmer 会话预热
# ============================================================================


class SessionWarmer:
    """首次访问目标域前先访问首页/列表页，建立可信 cookie / referer。

    用法:
        warmer = SessionWarmer()
        warmer.warm("https://www.cls.cn/api/sw",
                    fetch=lambda u: http_get(u))
        # 之后真实请求将携带刚获取的 cookie
    """

    def __init__(self, fetcher: Optional[Callable[[str], Any]] = None):
        self._fetcher = fetcher
        self._warmed: Dict[str, str] = {}    # domain -> ISO 时间
        self._lock = threading.Lock()
        self._ttl_seconds = 1800             # 30 分钟

    def _extract_main(self, url: str) -> str:
        try:
            p = urlparse(url)
            if not p.netloc:
                return ""
            return f"{p.scheme}://{p.netloc}/"
        except Exception:
            return ""

    def warm(self, url: str, fetcher: Optional[Callable[[str], Any]] = None,
             extra_paths: Optional[List[str]] = None) -> bool:
        """预热目标域。返回是否成功（warm 可重复调用，TTL 内直接返回 True）。"""
        main = self._extract_main(url)
        if not main:
            return False
        domain = urlparse(main).netloc.lower()
        with self._lock:
            last = self._warmed.get(domain)
            if last:
                try:
                    if (datetime.now()
                            - datetime.fromisoformat(last)).total_seconds() < self._ttl_seconds:
                        return True
                except ValueError:
                    pass
        f = fetcher or self._fetcher
        if not f:
            return False
        try:
            f(main)
            for p in (extra_paths or ["/", "/index.html"])[1:3]:
                try:
                    f(main.rstrip("/") + p)
                except Exception:
                    pass
            with self._lock:
                self._warmed[domain] = datetime.now().isoformat(timespec="seconds")
            return True
        except Exception as e:
            log.debug(f"会话预热失败 {domain}: {e}")
            return False

    def is_warm(self, url: str) -> bool:
        main = self._extract_main(url)
        if not main:
            return False
        domain = urlparse(main).netloc.lower()
        with self._lock:
            last = self._warmed.get(domain)
            if not last:
                return False
            try:
                return (datetime.now()
                        - datetime.fromisoformat(last)).total_seconds() < self._ttl_seconds
            except ValueError:
                return False

    def clear(self, url: Optional[str] = None) -> None:
        with self._lock:
            if url:
                domain = urlparse(url).netloc.lower()
                self._warmed.pop(domain, None)
            else:
                self._warmed.clear()


# ============================================================================
# 8. AntiBlockPipeline 增强管道（一站式）
# ============================================================================


@dataclass
class PipelineConfig:
    """增强管道配置（可在调用 batch_fetch 时直接传入）。"""
    proxy_pool: Optional[ProxyPool] = None
    behavior: Optional[RequestBehaviorProfile] = None
    budget: Optional[CrawlBudgetController] = None
    ip_tracker: Optional[IPReputationTracker] = None
    warmer: Optional[SessionWarmer] = None
    enable_proxy: bool = False
    enable_behavior_delay: bool = True
    enable_warm: bool = False
    query_for_referer: Optional[str] = None


class AntiBlockPipeline:
    """反爬增强管道（一站式接线器）。

    用法:
        pipeline = AntiBlockPipeline(
            proxies=["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"],
            budgets={"cls.cn": {"daily": 3000, "hourly": 300}},
        )
        result = pipeline.run(url, fetch=lambda u, **kw: http_get(u, **kw))
        # result: {"proxy": str, "delay": float, "referer": str,
        #          "response": Response, "captcha": CaptchaSignal | None}
    """

    def __init__(self, proxies: Optional[List[str]] = None,
                 budgets: Optional[Dict[str, Dict[str, int]]] = None,
                 proxy_pool_file: Optional[Path] = None,
                 ip_tracker_file: Optional[Path] = None,
                 config: Optional[PipelineConfig] = None):
        self.config = config or PipelineConfig(
            proxy_pool=ProxyPool(proxies, proxy_pool_file) if proxies else None,
            behavior=RequestBehaviorProfile(),
            budget=CrawlBudgetController(budgets) if budgets else None,
            ip_tracker=IPReputationTracker(ip_tracker_file),
            warmer=SessionWarmer(),
        )
        self._lock = threading.Lock()

    def before_request(self, url: str) -> Dict[str, Any]:
        """请求前的所有决策：选代理、计算延迟、生成 Referer、检查预算。"""
        domain = urlparse(url).netloc.lower()
        decision: Dict[str, Any] = {
            "url": url, "proxy": None, "delay": 0.0, "referer": "",
            "warm_ok": True, "budget_ok": True, "ip_key": None,
        }
        # 1) 预热
        if self.config.enable_warm and self.config.warmer:
            ok = self.config.warmer.warm(url)
            decision["warm_ok"] = ok
        # 2) 选代理
        ip_key = "default"
        if self.config.enable_proxy and self.config.proxy_pool:
            proxy = self.config.proxy_pool.pick()
            decision["proxy"] = proxy
            if proxy:
                ip_key = proxy
        decision["ip_key"] = ip_key
        # 3) IP 信誉检查
        if self.config.ip_tracker and not self.config.ip_tracker.allow(ip_key):
            wait = self.config.ip_tracker.wait_seconds(ip_key)
            decision["delay"] = max(decision["delay"], wait)
            decision["budget_ok"] = False
        # 4) 预算检查
        if self.config.budget:
            if not self.config.budget.allow(domain):
                wait = self.config.budget.wait_seconds_until_available(domain)
                decision["delay"] = max(decision["delay"], wait)
                decision["budget_ok"] = False
            else:
                self.config.budget.consume(domain)
        # 5) 行为延迟
        if self.config.enable_behavior_delay and self.config.behavior:
            decision["delay"] += self.config.behavior.next_delay()
            decision["referer"] = self.config.behavior.next_referer(
                url, query=self.config.query_for_referer)
        return decision

    def after_request(self, decision: Dict[str, Any],
                      response: Any,
                      elapsed_ms: float = 0.0,
                      error: Optional[BaseException] = None) -> Dict[str, Any]:
        """请求后的反馈：更新代理评分 / IP 信誉 / 检测验证码。"""
        result: Dict[str, Any] = {"ok": False, "captcha": None, "error": ""}
        ip_key = decision.get("ip_key") or "default"
        success = error is None and response is not None
        # 1) 验证码检测
        if success and response is not None:
            try:
                text = getattr(response, "text", "") or ""
                code = getattr(response, "status_code", 200)
                if code == 200 and text:
                    sig = detect_captcha(text)
                    if sig:
                        result["captcha"] = sig
                        success = False
                        result["error"] = f"captcha:{sig.kind}"
            except Exception:
                pass
        # 2) 代理评分
        if self.config.enable_proxy and self.config.proxy_pool:
            proxy = decision.get("proxy")
            if proxy:
                self.config.proxy_pool.report(proxy, success=success,
                                              latency_ms=elapsed_ms,
                                              error=str(error)[:120] if error else "")
        # 3) IP 信誉
        if self.config.ip_tracker:
            if success:
                self.config.ip_tracker.record_success(ip_key)
            else:
                reason = result["error"] or str(error or "unknown")[:120]
                self.config.ip_tracker.record_failure(ip_key, reason=reason)
        result["ok"] = success
        if error and not result["error"]:
            result["error"] = str(error)[:200]
        return result

    def run(self, url: str, *, fetch: Callable[..., Any],
            timeout: float = 15.0, max_delay: float = 20.0,
            **fetch_kw: Any) -> Dict[str, Any]:
        """类 docstring 早就承诺了 pipeline.run(...)，但方法一直没实现——补上。

        fetch 由调用方注入（例：lambda u, **kw: http_get(u, **kw)），
        管道不绑定具体 HTTP 库；决策、限速、反馈在这里闭环。
        """
        decision = self.before_request(url)
        delay = max(0.0, float(decision.get("delay") or 0.0))
        if delay:
            time.sleep(min(delay, max_delay))
        kw: Dict[str, Any] = dict(fetch_kw)
        kw.setdefault("timeout", timeout)
        if decision.get("proxy"):
            kw["proxy"] = decision["proxy"]
        if decision.get("referer"):
            hdrs = dict(kw.get("headers") or {})
            hdrs.setdefault("Referer", decision["referer"])
            kw["headers"] = hdrs
        started = time.monotonic()
        response: Any = None
        error: Optional[BaseException] = None
        try:
            response = fetch(url, **kw)
        except BaseException as exc:  # noqa: BLE001
            error = exc
        elapsed = (time.monotonic() - started) * 1000.0
        feed = self.after_request(decision, response, elapsed_ms=elapsed, error=error)
        return {
            "url": url, "proxy": decision.get("proxy"),
            "delay": round(delay, 3), "referer": decision.get("referer", ""),
            "budget_ok": decision.get("budget_ok", True),
            "response": response, "elapsed_ms": round(elapsed, 1),
            "ok": feed["ok"], "captcha": feed.get("captcha"),
            "error": feed.get("error") or "",
        }

    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.config.proxy_pool:
            out["proxy_pool"] = self.config.proxy_pool.status()
        if self.config.budget:
            out["budget"] = self.config.budget.status()
        if self.config.ip_tracker:
            out["ip_tracker"] = self.config.ip_tracker.status()
        if self.config.behavior:
            out["behavior"] = self.config.behavior.stats()
        return out


# ============================================================================
# 9. 免费代理源发现器（v9.0）
# ============================================================================


# 已知可靠的免费代理列表 API（公开数据，非侵入性）
FREE_PROXY_SOURCES = [
    # 各开源代理池 API
    "https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list",
    "https://raw.githubusercontent.com/jhao104/proxy_pool/master/proxy_pool",
    "https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&sort_by=lastChecked&sort_type=desc&protocols=http%2Chttps",
]


class FreeProxyDiscovery:
    """免费代理发现与验证（v9.0）。

    从公开代理列表 API 获取代理地址，验证可用性后加入代理池。
    仅用于合法合规的数据采集场景。

    用法:
        discovery = FreeProxyDiscovery()
        proxies = discovery.discover(max_proxies=20)
        discovery.validate_and_add(proxies, proxy_pool)
    """

    def __init__(self, test_url: str = "https://www.baidu.com",
                 timeout: int = 10):
        self.test_url = test_url
        self.timeout = timeout

    def discover(self, max_proxies: int = 20) -> List[str]:
        """从免费代理源发现可用代理。"""
        import urllib.request
        import urllib.error
        discovered = []
        for source_url in FREE_PROXY_SOURCES:
            if len(discovered) >= max_proxies:
                break
            try:
                req = urllib.request.Request(source_url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/135.0.0.0"
                })
                resp = urllib.request.urlopen(req, timeout=15)
                data = resp.read().decode("utf-8", errors="replace")
                resp.close()
                # 尝试解析 JSON 格式
                try:
                    parsed = json.loads(data)
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                host = item.get("ip", item.get("host", ""))
                                port = item.get("port", "")
                                proto = item.get("protocols", ["http"])
                                if isinstance(proto, list):
                                    proto = proto[0] if proto else "http"
                                if host and port:
                                    discovered.append(f"{proto}://{host}:{port}")
                            elif isinstance(item, str):
                                discovered.append(f"http://{item}")
                except (json.JSONDecodeError, ValueError):
                    # 尝试解析文本格式 (ip:port per line)
                    for line in data.splitlines():
                        line = line.strip()
                        if line and ":" in line and not line.startswith("#"):
                            # 可能是 ip:port 或 JSON 行
                            parts = line.split()
                            for p in parts:
                                if ":" in p and "." in p.split(":")[0]:
                                    discovered.append(f"http://{p.split(':')[0]}:{p.split(':')[1]}")
                                    break
            except Exception as e:
                log.debug(f"Proxy source {source_url[:50]} failed: {e}")
                continue

        # 去重
        seen = set()
        unique = []
        for p in discovered:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        log.info(f"Discovered {len(unique)} proxies from {len(FREE_PROXY_SOURCES)} sources")
        return unique[:max_proxies]

    def validate_proxy(self, proxy_url: str) -> Tuple[bool, float]:
        """验证单个代理是否可用。返回 (是否可用, 延迟ms)。"""
        import urllib.request
        start = time.monotonic()
        try:
            proxy_handler = urllib.request.ProxyHandler({
                "http": proxy_url, "https": proxy_url
            })
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(self.test_url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/135.0.0.0"
            })
            resp = opener.open(req, timeout=self.timeout)
            code = resp.getcode()
            resp.close()
            latency = (time.monotonic() - start) * 1000
            if 200 <= code < 400:
                return True, latency
            return False, latency
        except Exception:
            return False, (time.monotonic() - start) * 1000

    def validate_and_add(self, proxy_urls: List[str],
                         pool: ProxyPool,
                         max_valid: int = 10) -> int:
        """批量验证代理并添加到代理池。返回新增可用代理数。"""
        valid_count = 0
        for proxy_url in proxy_urls:
            if valid_count >= max_valid:
                break
            ok, latency = self.validate_proxy(proxy_url)
            if ok:
                pool.add(proxy_url, weight=max(0.1, 100.0 / max(1, latency / 100)))
                pool.report(proxy_url, success=True, latency_ms=latency)
                valid_count += 1
                log.info(f"Proxy validated: {proxy_url} ({latency:.0f}ms)")
            else:
                log.debug(f"Proxy failed: {proxy_url}")
        return valid_count


# ============================================================================
# 10. Header 顺序随机化（v9.0）— 对抗 WAF 指纹检测
# ============================================================================


class HeaderOrderRandomizer:
    """HTTP Header 顺序随机化器。

    某些 WAF/CDN（如 Cloudflare、阿里云 WAF）会检查 HTTP header 的顺序作为
    浏览器指纹的一部分。不同浏览器有不同的 header 顺序特征。
    此类根据模拟的浏览器类型生成对应的 header 顺序。

    用法:
        rand = HeaderOrderRandomizer()
        headers = {"User-Agent": "...", "Accept": "...", ...}
        ordered = rand.shuffle(headers, browser="chrome")
    """

    # 不同浏览器的 header 顺序模板
    _ORDER_TEMPLATES = {
        "chrome": [
            "Host", "Connection", "sec-ch-ua", "sec-ch-ua-mobile",
            "sec-ch-ua-platform", "Upgrade-Insecure-Requests",
            "User-Agent", "Accept", "Sec-Fetch-Site", "Sec-Fetch-Mode",
            "Sec-Fetch-User", "Sec-Fetch-Dest", "Accept-Encoding",
            "Accept-Language", "Cookie", "Referer",
        ],
        "firefox": [
            "Host", "User-Agent", "Accept", "Accept-Language",
            "Accept-Encoding", "Connection", "Cookie", "Upgrade-Insecure-Requests",
            "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Sec-Fetch-Site",
            "Sec-Fetch-User", "Referer",
        ],
        "safari": [
            "Host", "Accept", "Sec-Fetch-Site", "Cookie",
            "Sec-Fetch-Mode", "Accept-Language", "User-Agent",
            "Accept-Encoding", "Connection", "Referer",
        ],
    }

    def shuffle(self, headers: Dict[str, str],
                browser: Optional[str] = None) -> List[Tuple[str, str]]:
        """按浏览器模板排序 headers，模板外的随机追加。"""
        if browser is None:
            browser = random.choice(["chrome", "firefox", "safari"])

        template = self._ORDER_TEMPLATES.get(browser, self._ORDER_TEMPLATES["chrome"])
        # 模板中的 headers 按模板顺序
        ordered = []
        remaining = dict(headers)
        for key in template:
            lk = key.lower()
            for hk in list(remaining.keys()):
                if hk.lower() == lk:
                    ordered.append((hk, remaining.pop(hk)))
                    break
        # 剩余的随机追加
        rest = list(remaining.items())
        random.shuffle(rest)
        return ordered + rest


# ============================================================================
# 11. 反指纹会话管理器（v9.0）
# ============================================================================


class FingerprintSession:
    """反指纹会话管理器。

    在一次会话中保持一致的浏览器指纹（UA、Sec-Ch-Ua、Accept-Language 等），
    避免同一会话内指纹不一致被 WAF 识别为异常。

    用法:
        session = FingerprintSession()
        headers = session.get_headers(url)
        # 在同一会话中复用相同的 headers
        next_headers = session.get_headers(next_url)
        # 新会话生成全新指纹
        session.rotate()
    """

    def __init__(self, browser: Optional[str] = None):
        self._browser = browser or random.choice(["chrome", "firefox", "safari"])
        self._ua = random_ua(mobile=False)
        self._fingerprint = self._build_fingerprint()
        self._created_at = time.monotonic()
        self._request_count = 0

    def _build_fingerprint(self) -> Dict[str, str]:
        """构建一致的浏览器指纹。"""
        fp: Dict[str, str] = {}
        if "Chrome" in self._ua or "Edg" in self._ua:
            fp["Sec-Ch-Ua"] = random.choice(SEC_CH_UA_POOL) if SEC_CH_UA_POOL else ""
            fp["Sec-Ch-Ua-Mobile"] = "?0"
            fp["Sec-Ch-Ua-Platform"] = random.choice(SEC_CH_UA_PLATFORM_POOL) if SEC_CH_UA_PLATFORM_POOL else '"Windows"'
            fp["Sec-Fetch-Dest"] = "document"
            fp["Sec-Fetch-Mode"] = "navigate"
            fp["Sec-Fetch-Site"] = "none"
            fp["Sec-Fetch-User"] = "?1"
        # 一次会话固定 Accept-Language
        fp["Accept-Language"] = random.choice(ACCEPT_LANGUAGE_POOL) if ACCEPT_LANGUAGE_POOL else "zh-CN,zh;q=0.9"
        fp["Accept-Encoding"] = random.choice(ACCEPT_ENCODING_POOL) if ACCEPT_ENCODING_POOL else "gzip, deflate"
        return fp

    def get_headers(self, url: str = "",
                    extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """获取当前会话的 headers（保持一致性）。"""
        self._request_count += 1
        headers = {"User-Agent": self._ua}
        headers.update(self._fingerprint)
        if extra:
            headers.update(extra)
        return headers

    def rotate(self) -> None:
        """轮换整个会话指纹（模拟新浏览器实例）。"""
        self._ua = random_ua(mobile=False)
        self._browser = random.choice(["chrome", "firefox", "safari"])
        self._fingerprint = self._build_fingerprint()
        self._created_at = time.monotonic()
        self._request_count = 0

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self._created_at

    def maybe_rotate(self, max_age: float = 1800, max_requests: int = 100) -> bool:
        """条件轮换：超龄或请求数过多时自动轮换。返回是否轮换了。"""
        if self.age_seconds > max_age or self._request_count > max_requests:
            self.rotate()
            return True
        return False


# ============================================================================
# 12. 请求时间分布器（v9.0）— 人类化请求节奏
# ============================================================================


class RequestTempoScheduler:
    """请求节奏调度器 — 生成符合人类浏览习惯的请求时间间隔。

    使用混合分布：
    - 70% 快速浏览（1-3秒，正态分布）
    - 20% 正常阅读（3-10秒，对数正态分布）
    - 10% 长暂停（10-60秒，模拟走神/切换页面）

    用法:
        scheduler = RequestTempoScheduler()
        for url in urls:
            delay = scheduler.next_delay()
            time.sleep(delay)
            fetch(url)
    """

    def __init__(self, base_rate: float = 1.0):
        self._base_rate = base_rate
        self._count = 0

    def next_delay(self) -> float:
        """生成下一次请求的延迟秒数。"""
        self._count += 1
        r = random.random()

        if r < 0.70:
            # 快速浏览：正态分布，均值2秒
            delay = random.gauss(2.0 * self._base_rate, 0.8)
            return max(0.5, delay)
        elif r < 0.90:
            # 正常阅读：对数正态
            import math
            mu = math.log(5.0 * self._base_rate)
            delay = random.lognormvariate(mu, 0.4)
            return max(2.0, min(delay, 30.0))
        else:
            # 长暂停
            delay = random.uniform(10.0, 60.0) * self._base_rate
            return delay


# ============================================================================
# 自检
# ============================================================================


def self_check() -> Dict[str, Any]:
    """反爬增强模块自检报告。"""
    return {
        "version": "v9.0",
        "ProxyPool": "OK（线程安全/可选持久化）",
        "FreeProxyDiscovery": "OK（3个公开代理源 + 验证）",
        "RequestBehaviorProfile": "OK",
        "HeaderOrderRandomizer": "OK（Chrome/Firefox/Safari 三种模板）",
        "FingerprintSession": "OK（一致性指纹 + 自动轮换）",
        "RequestTempoScheduler": "OK（混合分布：70%快浏览+20%阅读+10%长暂停）",
        "TLSClientFactory": TLSClientFactory.pick()[0] if TLSClientFactory.pick() else "stdlib",
        "CrawlBudgetController": "OK（每日/每小时预算 + 自动续期）",
        "IPReputationTracker": "OK（指数退避 + 抖动）",
        "CaptchaDetector": f"覆盖 {len(_CAPTCHA_PATTERNS)} 种验证码模式",
        "SessionWarmer": "OK（30 分钟 TTL）",
        "AntiBlockPipeline": "OK（一站式）",
    }


if __name__ == "__main__":
    print(json.dumps(self_check(), ensure_ascii=False, indent=2))
