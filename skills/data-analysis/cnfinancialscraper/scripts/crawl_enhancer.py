# -*- coding: utf-8 -*-
"""
爬取增强引擎 v7.7.0 (crawl_enhancer.py)

在既有基础设施（http_utils / anti_block_utils / browser_pro）之上提供
智能多级爬取管道与批量并发爬取：

  1. SmartFetcher —— 三级降级抓取：
     API 直连（api_registry 挖掘到的接口）→ HTTP → 浏览器兜底
     并对结果做失败分类：ok / soft_blocked / empty / timeout / error
  2. AdaptiveCrawlPolicy —— v7.7 自适应策略：
     失败早停（风控止损）/ 连续失败并发降级 / 全域沦陷提前终止 / 耗时统计
  3. batch_smart_crawl —— 线程池并发批量爬取：
     域名限速 + URL 去重（可持久化）+ 域名健康度跟踪 + 成功率统计

用法：
    from scripts.crawl_enhancer import SmartFetcher, batch_smart_crawl
    fr = SmartFetcher().fetch("https://example.com/news")
    print(fr.status, fr.text_len, fr.source)

    stats = batch_smart_crawl(urls, concurrency=4,
                              dedup_file="data/seen_urls.json")
    print(stats["stats"])
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

try:
    from .http_utils import http_get
    from .anti_block_utils import (DomainRateLimiter, DomainHealthTracker,
                                   UrlDeduplicator, detect_block)
except ImportError:  # 脚本直跑
    from http_utils import http_get  # type: ignore
    from anti_block_utils import (DomainRateLimiter, DomainHealthTracker,  # type: ignore
                                  UrlDeduplicator, detect_block)

# ── 状态分类 ───────────────────────────────────────────────────
STATUS_OK = "ok"
STATUS_SOFT_BLOCKED = "soft_blocked"   # 200 但内容是反爬页/验证码页
STATUS_EMPTY = "empty"                 # 内容为空或过短
STATUS_TIMEOUT = "timeout"
STATUS_ERROR = "error"

_OK_STATUSES = {STATUS_OK}

# v7.7 自适应策略默认值（均可经 batch_smart_crawl 参数覆盖）
EARLY_STOP_MIN_SAMPLES = 8      # 早停判定最少样本数（避免冷启动误判）
EARLY_STOP_FAIL_RATE = 0.85     # 失败率达到此值且无成功 → 提前终止剩余任务


@dataclass
class FetchResult:
    """单次抓取的标准结果。"""
    url: str
    status: str = STATUS_ERROR
    html: str = ""
    text_len: int = 0
    source: str = ""                    # api / http / browser
    elapsed: float = 0.0
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("html", None)             # 大字段不进统计
        return d


def classify_response(html: Optional[str], min_text_len: int = 200) -> str:
    """把一次 HTTP/浏览器响应分类为抓取状态（纯函数，可单测）。"""
    if html is None:
        return STATUS_ERROR
    text = html.strip()
    if not text:
        return STATUS_EMPTY
    block = detect_block(200, None, text[:500])
    if block:
        return STATUS_SOFT_BLOCKED
    if len(text) < min_text_len:
        # 短页面：可能是反爬壳页，也可能确实是小接口响应
        if _looks_like_block_shell(text):
            return STATUS_SOFT_BLOCKED
        if text.lstrip()[:1] in ("{", "["):
            return STATUS_OK            # JSON 短响应也算有效
        return STATUS_EMPTY
    return STATUS_OK


_BLOCK_SHELL_RE = re.compile(
    r"(验证码|安全验证|访问被拒|请勿频繁访问|人机验证|滑动验证|"
    r"captcha|verify|access denied|forbidden)", re.IGNORECASE)


def _looks_like_block_shell(text: str) -> bool:
    return bool(_BLOCK_SHELL_RE.search(text))


class SmartFetcher:
    """多级降级抓取器：API 直连 → HTTP → 浏览器。

    所有外部依赖（http_fetcher / browser_fetcher / api_lookup）均可注入，
    便于离线测试；默认实现延迟绑定到项目内模块。
    """

    def __init__(self,
                 http_fetcher: Optional[Callable[..., Any]] = None,
                 browser_fetcher: Optional[Callable[[str], Optional[str]]] = None,
                 api_lookup: Optional[Callable[[str], Optional[Dict]]] = None,
                 min_text_len: int = 200):
        self.http_fetcher = http_fetcher or self._default_http_fetch
        self.browser_fetcher = browser_fetcher
        self.api_lookup = api_lookup or self._default_api_lookup
        self.min_text_len = min_text_len

    # ── 默认实现 ──────────────────────────────────────────────
    @staticmethod
    def _default_http_fetch(url: str, timeout: int = 20):
        return http_get(url, timeout=timeout)

    @staticmethod
    def _default_api_lookup(url: str) -> Optional[Dict[str, Any]]:
        """在已挖掘的 API 注册表中找同域接口（懒加载避免循环依赖）。"""
        try:
            from .browser_pro import ApiRegistry
        except ImportError:
            try:
                from browser_pro import ApiRegistry  # type: ignore
            except ImportError:
                return None
        try:
            domain = urlparse(url).netloc.lower()
            if not domain:
                return None
            hits = ApiRegistry().search(domain=domain)
            return hits[0] if hits else None
        except Exception:
            return None

    @staticmethod
    def _default_browser_fetch(url: str) -> Optional[str]:
        try:
            try:
                from .browser_scraper import browser_fetch
            except ImportError:
                from browser_scraper import browser_fetch  # type: ignore
            return browser_fetch(url)
        except Exception:
            return None

    def _http_fetch(self, url: str):
        """单次 HTTP 抓取并分类（返回 (状态, 正文, 错误信息)）。"""
        try:
            resp = self.http_fetcher(url)
            if resp is None:
                return STATUS_ERROR, "", "http_get returned None"
            html = getattr(resp, "text", "") or ""
            return classify_response(html, self.min_text_len), html, ""
        except TimeoutError as e:
            return STATUS_TIMEOUT, "", str(e)
        except Exception as e:
            msg = str(e)
            if "timed out" in msg.lower() or "timeout" in msg.lower():
                return STATUS_TIMEOUT, "", msg
            return STATUS_ERROR, "", msg

    # ── 主流程 ────────────────────────────────────────────────
    def fetch(self, url: str, use_api: bool = True,
              use_browser: bool = True) -> FetchResult:
        start = time.monotonic()
        result = FetchResult(url=url)

        # 1) API 直连（挖掘到的接口优先）
        if use_api:
            api = None
            try:
                api = self.api_lookup(url)
            except Exception:
                api = None
            if api and api.get("url"):
                try:
                    resp = self.http_fetcher(api["url"], timeout=15)
                    html = getattr(resp, "text", None) or ""
                    status = classify_response(html, min_text_len=50)
                    if status == STATUS_OK:
                        result.status = STATUS_OK
                        result.html = html
                        result.text_len = len(html)
                        result.source = "api"
                        result.extra = {"api_url": api["url"]}
                        result.elapsed = time.monotonic() - start
                        return result
                except Exception:
                    pass

        # 2) HTTP 抓取
        status, html, err = self._http_fetch(url)
        if status == STATUS_OK:
            result.status, result.html, result.source = STATUS_OK, html, "http"
            result.text_len = len(html)
            result.elapsed = time.monotonic() - start
            return result

        # 3) 浏览器兜底（仅对"拿不到/被拦"做兜底，超时不重试浏览器避免叠加等待）
        if use_browser and status in (STATUS_SOFT_BLOCKED, STATUS_EMPTY,
                                      STATUS_ERROR):
            try:
                bhtml = (self.browser_fetcher or self._default_browser_fetch)(url)
                bstat = classify_response(bhtml, self.min_text_len)
                if bstat == STATUS_OK:
                    result.status, result.html, result.source = (
                        STATUS_OK, bhtml, "browser")
                    result.text_len = len(bhtml)
                    result.elapsed = time.monotonic() - start
                    return result
            except Exception as e:
                err = err or f"browser: {e}"

        result.status, result.error = status, err
        result.elapsed = time.monotonic() - start
        return result


class AdaptiveCrawlPolicy:
    """v7.7 自适应爬取策略（线程安全）。

    - 早停：累计 ≥min_samples 且成功率 0 → 判定整批被风控，终止剩余任务；
      后续若恢复成功则自动解除（如限速窗口过期）。
    - 并发降级：连续 ≥downgrade_after 次失败 → 建议降并发（由调用方执行）。
    - 全域沦陷：所有出现过的域名都被健康跟踪器禁用 → 提前终止。
    """

    def __init__(self, min_samples: int = EARLY_STOP_MIN_SAMPLES,
                 fail_rate: float = EARLY_STOP_FAIL_RATE,
                 downgrade_after: int = 4):
        self.min_samples = max(1, min_samples)
        self.fail_rate = fail_rate
        self.downgrade_after = downgrade_after
        self._lock = threading.Lock()
        self.ok = 0
        self.fail = 0
        self.consecutive_fail = 0
        self.downgrades = 0
        self.early_stopped = False
        self.stop_reason = ""

    def record(self, fr: FetchResult, seen_domains: set) -> None:
        with self._lock:
            if fr.ok:
                self.ok += 1
                self.consecutive_fail = 0
                self.early_stopped = False      # 恢复成功 → 解除早停
                self.stop_reason = ""
            else:
                self.fail += 1
                self.consecutive_fail += 1
                total = self.ok + self.fail
                if (total >= self.min_samples and self.ok == 0):
                    self.early_stopped = True
                    self.stop_reason = "all_failed"

    def should_stop(self, health=None, seen_domains=None) -> bool:
        with self._lock:
            if self.early_stopped:
                return True
        if health is not None and seen_domains:
            try:
                if all(not health.is_available(d) for d in seen_domains):
                    with self._lock:
                        self.early_stopped = True
                        self.stop_reason = "domains_disabled"
                    return True
            except Exception:
                pass
        return False

    def should_downgrade(self) -> bool:
        with self._lock:
            if self.consecutive_fail >= self.downgrade_after:
                self.consecutive_fail = 0
                self.downgrades += 1
                return True
            return False

    @staticmethod
    def degraded_concurrency(current: int) -> int:
        return max(1, current // 2)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {"ok": self.ok, "fail": self.fail,
                    "downgrades": self.downgrades,
                    "early_stopped": self.early_stopped,
                    "stop_reason": self.stop_reason}


def batch_smart_crawl(urls: List[str],
                      concurrency: int = 4,
                      dedup_file: Optional[str] = None,
                      rate_map: Optional[Dict[str, float]] = None,
                      use_browser: bool = False,
                      fetcher: Optional[SmartFetcher] = None,
                      progress: Optional[Callable[[int, int, str], None]] = None,
                      adaptive: bool = True,
                      early_stop_min_samples: int = EARLY_STOP_MIN_SAMPLES,
                      early_stop_fail_rate: float = EARLY_STOP_FAIL_RATE,
                      ) -> Dict[str, Any]:
    """并发批量智能爬取。

    Args:
        urls: 待爬 URL 列表
        concurrency: 并发数（建议 ≤5，避免触发风控）
        dedup_file: 可选，URL 去重持久化文件（跨进程）
        rate_map: 域名限速表，如 {"cls.cn": 2.0, "default": 1.0}
        use_browser: 是否允许浏览器兜底（默认关，批量场景控制资源）
        fetcher: 可注入自定义 SmartFetcher（测试用）
        progress: 进度回调 (done, total, url)
        adaptive: v7.7 自适应策略（失败早停/并发降级/全域沦陷终止）
        early_stop_min_samples / early_stop_fail_rate: 早停参数（保留兼容）

    Returns:
        {"results": [FetchResult...], "stats": {...}}
    """
    fetcher = fetcher or SmartFetcher()
    limiter = DomainRateLimiter(rate_map or {"default": 1.0})
    health = DomainHealthTracker()
    dedup = UrlDeduplicator(dedup_file) if dedup_file else None
    policy = AdaptiveCrawlPolicy(min_samples=early_stop_min_samples,
                                 fail_rate=early_stop_fail_rate) if adaptive else None

    targets: List[str] = []
    skipped_dup = 0
    for u in urls:
        if dedup and not dedup.is_new(u):
            skipped_dup += 1
            continue
        targets.append(u)

    results: List[FetchResult] = []
    done = 0
    skipped_stopped = 0
    seen_domains: set = set()
    wall_start = time.monotonic()

    def _work(url: str) -> FetchResult:
        # v7.7 死域快速跳过：域名已被健康跟踪器禁用时不再消耗请求与限速等待
        domain = urlparse(url).netloc.lower()
        if not health.is_available(domain):
            return FetchResult(url=url, status=STATUS_ERROR,
                               error="domain disabled by health tracker")
        limiter.wait(url)
        fr = fetcher.fetch(url, use_browser=use_browser)
        if fr.ok:
            health.record_success(domain)
        else:
            health.record_failure(domain)
        return fr

    # v7.7 分批提交：每批 = 当前并发数，批间检查早停/降级，避免一次性提交全部
    pool_size = max(1, min(concurrency, 8))
    queue = list(targets)
    with ThreadPoolExecutor(max_workers=pool_size) as pool:
        while queue:
            if policy and policy.should_stop(health, seen_domains):
                skipped_stopped += len(queue)
                break
            batch, queue = queue[:pool_size], queue[pool_size:]
            futures = {pool.submit(_work, u): u for u in batch}
            for fut in as_completed(futures):
                try:
                    fr = fut.result()
                except Exception as e:
                    fr = FetchResult(url=futures[fut], status=STATUS_ERROR,
                                     error=str(e))
                seen_domains.add(urlparse(fr.url).netloc.lower())
                results.append(fr)
                done += 1
                if policy:
                    policy.record(fr, seen_domains)
                    if policy.should_downgrade() and pool_size > 1:
                        pool_size = AdaptiveCrawlPolicy.degraded_concurrency(pool_size)
                        pool._max_workers = pool_size   # 动态收紧在飞上限（在飞任务不中断）
                if progress:
                    try:
                        progress(done, len(targets), fr.url)
                    except Exception:
                        pass

    by_status: Dict[str, int] = {}
    by_domain: Dict[str, Dict[str, int]] = {}
    for fr in results:
        by_status[fr.status] = by_status.get(fr.status, 0) + 1
        dom = urlparse(fr.url).netloc.lower()
        d = by_domain.setdefault(dom, {"ok": 0, "fail": 0})
        d["ok" if fr.ok else "fail"] += 1

    ok_n = by_status.get(STATUS_OK, 0)
    stats = {
        "total": len(urls),
        "fetched": len(results),
        "skipped_dup": skipped_dup,
        "skipped_stopped": skipped_stopped,      # v7.7 早停未执行数
        "ok": ok_n,
        "success_rate": round(ok_n / len(results), 3) if results else 0.0,
        "elapsed": round(time.monotonic() - wall_start, 2),   # v7.7 耗时统计
        "avg_elapsed": round(sum(r.elapsed for r in results) / len(results), 3)
        if results else 0.0,
        "by_status": by_status,
        "by_domain": by_domain,
        "adaptive": policy.snapshot() if policy else None,
    }
    return {"results": results, "stats": stats}


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python scripts/crawl_enhancer.py <url> [url2 ...]")
        sys.exit(1)
    out = batch_smart_crawl(sys.argv[1:], concurrency=2)
    for fr in out["results"]:
        print(f"[{fr.status:<12}] {fr.source or '-':<8} "
              f"{fr.text_len:>6}B  {fr.url}")
    print(json.dumps(out["stats"], ensure_ascii=False, indent=1))
