# -*- coding: utf-8 -*-
"""
爬取调度器模块 v10.0 (crawler_dispatcher.py)

在 v9 6 级降级链基础上引入智能调度中心，解决：
1. **多 scraper 接口不统一**：BaseScraper 协议 + 自动发现/注册
2. **源切换不智能**：CrawlerDispatcher 按健康度自动选择最优源
3. **不支持增量**：IncrementalFetcher 基于 lastmod/ETag/hash 跳过未变更
4. **中断后无法续**：ResumeState 持久化调度状态，中断后自动恢复

设计原则：
- 零 pip 依赖即可用
- 与现有 scraper 完全共存（不强制改造）
- 线程安全；持久化数据尽量小
- 与反爬管道 (AntiBlockPipeline) 配合使用

向后兼容：所有类独立，缺失依赖时自动降级。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Type
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# 反爬建议延迟的封顶值：调度器同步执行，绝不能让一次预算惩罚挂住整批任务
MAX_ANTI_BLOCK_DELAY = 20.0


# ============================================================================
# 1. BaseScraper 统一接口协议
# ============================================================================

class ScrapeStatus(Enum):
    """爬取状态枚举。"""
    SUCCESS = auto()
    PARTIAL = auto()       # 部分成功（部分数据缺失）
    FAILED = auto()
    BLOCKED = auto()       # 被反爬拦截
    RATE_LIMITED = auto()  # 触发限流
    TIMEOUT = auto()
    NOT_MODIFIED = auto()  # 增量爬取：未变更


@dataclass
class ScrapeResult:
    """单次爬取结果。"""
    url: str
    status: ScrapeStatus
    data: Any = None
    error: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    source: str = ""           # 数据源标识
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    content_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.name
        return d


class BaseScraper(ABC):
    """爬虫基类协议。

    v10 新增。所有站点专属 scraper 推荐继承本类；老 scraper 不继承也能
    通过 register_external() 包装接入。
    """

    name: str = "base"               # 数据源标识
    domain: str = ""                 # 主域
    priority: int = 100              # 默认优先级（数字越小越优先）
    requires_browser: bool = False   # 是否需要浏览器渲染
    supports_incremental: bool = True

    def __init__(self, **kwargs: Any) -> None:
        self.config: Dict[str, Any] = dict(kwargs)
        self._lock = threading.Lock()

    @abstractmethod
    def fetch(self, url: str, **kwargs: Any) -> ScrapeResult:
        """拉取单个 URL。子类必须实现。"""
        raise NotImplementedError

    def fetch_batch(self, urls: Iterable[str], **kwargs: Any) -> List[ScrapeResult]:
        """批量拉取，默认顺序执行。子类可覆盖为并发。"""
        return [self.fetch(u, **kwargs) for u in urls]

    def health_check(self) -> Dict[str, Any]:
        """健康检查。返回 {"available": bool, "latency_ms": float, "note": str}"""
        return {"available": True, "latency_ms": 0.0, "note": "default"}

    @property
    def id(self) -> str:
        return f"{self.__class__.__module__}.{self.__class__.__name__}"


class ExternalScraperAdapter(BaseScraper):
    """把已有的类（非 BaseScraper 子类）包装成 BaseScraper。

    例如 wrap(FinancialPageScraper(...)) 即可接入调度器。
    """

    def __init__(self, name: str, fetch_fn: Callable[..., Any],
                 domain: str = "", priority: int = 100,
                 **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.name = name
        self.domain = domain
        self.priority = priority
        self._fetch_fn = fetch_fn

    def fetch(self, url: str, **kwargs: Any) -> ScrapeResult:
        t0 = time.time()
        started = datetime.now().isoformat(timespec="seconds")
        try:
            data = self._fetch_fn(url, **kwargs)
            elapsed = (time.time() - t0) * 1000
            return ScrapeResult(
                url=url, status=ScrapeStatus.SUCCESS,
                data=data, started_at=started,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                duration_ms=elapsed, source=self.name,
            )
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.time() - t0) * 1000
            return ScrapeResult(
                url=url, status=ScrapeStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                started_at=started,
                finished_at=datetime.now().isoformat(timespec="seconds"),
                duration_ms=elapsed, source=self.name,
            )


# ============================================================================
# 2. 爬虫注册中心
# ============================================================================

class ScraperRegistry:
    """爬虫注册中心：按 domain / name 索引。

    线程安全。支持运行时注册新 scraper。
    """

    def __init__(self) -> None:
        self._by_name: Dict[str, BaseScraper] = {}
        self._by_domain: Dict[str, List[BaseScraper]] = {}
        self._lock = threading.RLock()

    def register(self, scraper: BaseScraper) -> None:
        with self._lock:
            self._by_name[scraper.name] = scraper
            if scraper.domain:
                self._by_domain.setdefault(scraper.domain, []).append(scraper)
                self._by_domain[scraper.domain].sort(key=lambda s: s.priority)

    def register_external(self, name: str, fetch_fn: Callable[..., Any],
                          domain: str = "", priority: int = 100,
                          **kwargs: Any) -> ExternalScraperAdapter:
        adapter = ExternalScraperAdapter(
            name=name, fetch_fn=fetch_fn,
            domain=domain, priority=priority, **kwargs,
        )
        self.register(adapter)
        return adapter

    def get(self, name: str) -> Optional[BaseScraper]:
        with self._lock:
            return self._by_name.get(name)

    def for_domain(self, domain: str) -> List[BaseScraper]:
        with self._lock:
            return list(self._by_domain.get(domain, []))

    def all_names(self) -> List[str]:
        with self._lock:
            return sorted(self._by_name.keys())

    def unregister(self, name: str) -> bool:
        with self._lock:
            sc = self._by_name.pop(name, None)
            if sc and sc.domain:
                lst = self._by_domain.get(sc.domain, [])
                self._by_domain[sc.domain] = [s for s in lst if s.name != name]
            return sc is not None

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_scrapers": len(self._by_name),
                "total_domains": len(self._by_domain),
                "scrapers": sorted(self._by_name.keys()),
            }


# 全局默认注册中心
_DEFAULT_REGISTRY = ScraperRegistry()


def get_default_registry() -> ScraperRegistry:
    return _DEFAULT_REGISTRY


def register(scraper: BaseScraper) -> None:
    """便捷函数：注册到全局注册中心。"""
    _DEFAULT_REGISTRY.register(scraper)


# ============================================================================
# 3. 数据源健康追踪
# ============================================================================

@dataclass
class SourceHealthRecord:
    """单数据源健康记录。"""
    name: str
    total_calls: int = 0
    success_calls: int = 0
    failure_calls: int = 0
    blocked_calls: int = 0
    consecutive_failures: int = 0
    last_success: Optional[str] = None
    last_failure: Optional[str] = None
    total_latency_ms: float = 0.0
    cooldown_until: Optional[str] = None  # ISO 时间
    domain: str = ""

    @property
    def success_rate(self) -> float:
        return self.success_calls / self.total_calls if self.total_calls else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.success_calls if self.success_calls else 0.0

    def is_available(self, now: Optional[datetime] = None) -> bool:
        if not self.cooldown_until:
            return True
        now = now or datetime.now()
        try:
            return datetime.fromisoformat(self.cooldown_until) <= now
        except Exception:  # noqa: BLE001
            return True

    def score(self) -> float:
        """综合评分（越大越优）：成功率×100 - 平均延迟(ms)×0.1 - 连续失败×20。"""
        if not self.is_available():
            return -1e9
        return self.success_rate * 100 - self.avg_latency_ms * 0.1 - self.consecutive_failures * 20


class HealthTracker:
    """数据源健康追踪（持久化到 JSON 文件）。"""

    def __init__(self, persist_path: Optional[str] = None) -> None:
        self._records: Dict[str, SourceHealthRecord] = {}
        self._lock = threading.RLock()
        self._persist_path = Path(persist_path) if persist_path else None
        if self._persist_path and self._persist_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for name, rec in data.items():
                self._records[name] = SourceHealthRecord(**rec)
        except Exception as exc:  # noqa: BLE001
            log.warning("HealthTracker load failed: %s", exc)

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {n: asdict(r) for n, r in self._records.items()}
            self._persist_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("HealthTracker save failed: %s", exc)

    def record(self, name: str, status: ScrapeStatus, latency_ms: float = 0.0,
               domain: str = "", cooldown_seconds: int = 30) -> None:
        with self._lock:
            rec = self._records.setdefault(
                name, SourceHealthRecord(name=name, domain=domain),
            )
            rec.total_calls += 1
            rec.domain = rec.domain or domain
            now = datetime.now()
            if status == ScrapeStatus.SUCCESS:
                rec.success_calls += 1
                rec.consecutive_failures = 0
                rec.last_success = now.isoformat(timespec="seconds")
                rec.total_latency_ms += latency_ms
                rec.cooldown_until = None
            elif status == ScrapeStatus.BLOCKED:
                rec.blocked_calls += 1
                rec.failure_calls += 1
                rec.consecutive_failures += 1
                rec.last_failure = now.isoformat(timespec="seconds")
                rec.cooldown_until = (
                    now + timedelta(seconds=cooldown_seconds * rec.consecutive_failures)
                ).isoformat(timespec="seconds")
            else:
                rec.failure_calls += 1
                rec.consecutive_failures += 1
                rec.last_failure = now.isoformat(timespec="seconds")
                if rec.consecutive_failures >= 3:
                    rec.cooldown_until = (
                        now + timedelta(seconds=cooldown_seconds)
                    ).isoformat(timespec="seconds")
            self._save()

    def get(self, name: str) -> Optional[SourceHealthRecord]:
        with self._lock:
            return self._records.get(name)

    def best(self, candidates: Iterable[str]) -> Optional[str]:
        with self._lock:
            best_name: Optional[str] = None
            best_score = -1e18
            for name in candidates:
                rec = self._records.get(name)
                if not rec:
                    return name  # 没记录 → 无成本 → 直接选
                score = rec.score()
                if score > best_score:
                    best_score = score
                    best_name = name
            return best_name

    def reset(self, name: str) -> None:
        with self._lock:
            self._records.pop(name, None)
            self._save()

    def all_records(self) -> List[SourceHealthRecord]:
        with self._lock:
            return list(self._records.values())

    def report(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sources": len(self._records),
                "details": [
                    {
                        "name": r.name,
                        "domain": r.domain,
                        "success_rate": round(r.success_rate, 3),
                        "avg_latency_ms": round(r.avg_latency_ms, 1),
                        "consecutive_failures": r.consecutive_failures,
                        "cooldown_until": r.cooldown_until,
                        "available": r.is_available(),
                        "score": round(r.score(), 2),
                    }
                    for r in self._records.values()
                ],
            }


# ============================================================================
# 4. 增量爬取
# ============================================================================

@dataclass
class IncrementalIndex:
    """URL → 元信息的索引（用于增量判断）。"""
    state: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def should_skip(self, url: str, etag: Optional[str], last_modified: Optional[str],
                    content_hash: Optional[str]) -> bool:
        old = self.state.get(url)
        if not old:
            return False
        if etag and old.get("etag") == etag:
            return True
        if last_modified and old.get("last_modified") == last_modified:
            return True
        if content_hash and old.get("content_hash") == content_hash:
            return True
        return False

    def update(self, url: str, etag: Optional[str] = None,
               last_modified: Optional[str] = None,
               content_hash: Optional[str] = None,
               status: str = "success") -> None:
        self.state[url] = {
            "etag": etag,
            "last_modified": last_modified,
            "content_hash": content_hash,
            "status": status,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def to_json(self) -> str:
        return json.dumps(self.state, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "IncrementalIndex":
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            data = {}
        return cls(state=data)


class IncrementalFetcher:
    """增量爬取器。包装 BaseScraper，自动跳过未变更内容。

    索引持久化：内存 + 可选 JSON 文件。
    """

    def __init__(self, scraper: BaseScraper,
                 persist_path: Optional[str] = None) -> None:
        self.scraper = scraper
        self._path = Path(persist_path) if persist_path else None
        self.index = self._load()

    def _load(self) -> IncrementalIndex:
        if not self._path or not self._path.exists():
            return IncrementalIndex()
        try:
            return IncrementalIndex.from_json(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return IncrementalIndex()

    def _save(self) -> None:
        if not self._path:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(self.index.to_json(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("IncrementalFetcher save failed: %s", exc)

    @staticmethod
    def compute_hash(data: Any) -> str:
        s = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def fetch(self, url: str, **kwargs: Any) -> ScrapeResult:
        result = self.scraper.fetch(url, **kwargs)
        if result.status == ScrapeStatus.SUCCESS:
            h = self.compute_hash(result.data)
            if self.index.should_skip(url, result.etag, result.last_modified, h):
                result.status = ScrapeStatus.NOT_MODIFIED
            else:
                self.index.update(url, result.etag, result.last_modified, h)
                self._save()
        return result


# ============================================================================
# 5. 断点续爬状态
# ============================================================================

@dataclass
class ResumeTask:
    """单个待恢复任务。"""
    task_id: str
    url: str
    scraper_name: str
    kwargs: Dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    last_error: Optional[str] = None
    enqueued_at: str = ""
    status: str = "pending"  # pending / running / done / failed


@dataclass
class ResumeState:
    """持久化调度状态。"""
    tasks: List[ResumeTask] = field(default_factory=list)
    completed: List[str] = field(default_factory=list)
    cursor: int = 0

    def pending_tasks(self) -> List[ResumeTask]:
        return [t for t in self.tasks if t.status == "pending"]

    def mark_done(self, task_id: str) -> None:
        if task_id not in self.completed:
            self.completed.append(task_id)
        for t in self.tasks:
            if t.task_id == task_id:
                t.status = "done"
                break

    def to_json(self) -> str:
        return json.dumps({
            "tasks": [asdict(t) for t in self.tasks],
            "completed": self.completed,
            "cursor": self.cursor,
        }, ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "ResumeState":
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return cls()
        tasks = [ResumeTask(**t) for t in data.get("tasks", [])]
        return cls(
            tasks=tasks,
            completed=data.get("completed", []),
            cursor=data.get("cursor", 0),
        )


class ResumeManager:
    """断点续爬管理器。"""

    def __init__(self, persist_path: str) -> None:
        self._path = Path(persist_path)
        self.state = self._load()
        self._lock = threading.RLock()

    def _load(self) -> ResumeState:
        if not self._path.exists():
            return ResumeState()
        try:
            return ResumeState.from_json(self._path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return ResumeState()

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(self.state.to_json(), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("ResumeManager save failed: %s", exc)

    def enqueue(self, urls: Iterable[str], scraper_name: str = "default",
                **kwargs: Any) -> List[str]:
        ids: List[str] = []
        with self._lock:
            now = datetime.now().isoformat(timespec="seconds")
            for url in urls:
                tid = uuid.uuid4().hex[:12]
                self.state.tasks.append(ResumeTask(
                    task_id=tid, url=url, scraper_name=scraper_name,
                    kwargs=kwargs, enqueued_at=now,
                ))
                ids.append(tid)
            self._save()
        return ids

    def next_task(self) -> Optional[ResumeTask]:
        with self._lock:
            for t in self.state.tasks:
                if t.status == "pending":
                    t.status = "running"
                    t.attempts += 1
                    self._save()
                    return t
        return None

    def complete(self, task_id: str) -> None:
        with self._lock:
            self.state.mark_done(task_id)
            self._save()

    def fail(self, task_id: str, error: str, max_attempts: int = 3) -> bool:
        """失败任务回到 pending 或标记 failed（达到 max_attempts）。"""
        with self._lock:
            for t in self.state.tasks:
                if t.task_id == task_id:
                    t.last_error = error
                    if t.attempts >= max_attempts:
                        t.status = "failed"
                    else:
                        t.status = "pending"
                    self._save()
                    return t.status == "failed"
        return False

    def progress(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self.state.tasks)
            done = sum(1 for t in self.state.tasks if t.status == "done")
            failed = sum(1 for t in self.state.tasks if t.status == "failed")
            pending = sum(1 for t in self.state.tasks if t.status == "pending")
            return {
                "total": total,
                "done": done,
                "failed": failed,
                "pending": pending,
                "progress_pct": round(done / total * 100, 1) if total else 0.0,
            }

    def reset(self) -> None:
        with self._lock:
            self.state = ResumeState()
            self._save()


# ============================================================================
# 6. CrawlerDispatcher 智能调度器
# ============================================================================

class DispatchStrategy(Enum):
    """调度策略。"""
    BEST_SCORE = auto()      # 按健康评分
    PRIORITY = auto()        # 按优先级
    ROUND_ROBIN = auto()     # 轮询
    RANDOM = auto()          # 随机


@dataclass
class DispatchConfig:
    """调度器配置。"""
    strategy: DispatchStrategy = DispatchStrategy.BEST_SCORE
    cooldown_seconds: int = 30
    max_attempts: int = 3
    enable_incremental: bool = True
    state_dir: str = "data/crawl_state"
    enable_health_persist: bool = True
    # 反爬预算耗尽时是否直接拒发请求（默认只延后，不改变既有调用方行为）
    reject_over_budget: bool = False


class CrawlerDispatcher:
    """智能爬取调度器。

    用法:
        reg = ScraperRegistry()
        reg.register(MyScraperA())
        reg.register(MyScraperB())
        dispatcher = CrawlerDispatcher(reg)
        result = dispatcher.dispatch("https://example.com/news", "scraper_a")
    """

    def __init__(self, registry: Optional[ScraperRegistry] = None,
                 config: Optional[DispatchConfig] = None,
                 anti_block: Optional[Any] = None) -> None:
        self.registry = registry or get_default_registry()
        self.config = config or DispatchConfig()
        self._rr_cursor = 0
        self._rr_lock = threading.Lock()
        Path(self.config.state_dir).mkdir(parents=True, exist_ok=True)
        self.health = HealthTracker(
            persist_path=(
                f"{self.config.state_dir}/health.json"
                if self.config.enable_health_persist else None
            )
        )
        self.resume = ResumeManager(
            persist_path=f"{self.config.state_dir}/resume.json",
        )
        self.anti_block = anti_block  # 可选：AntiBlockPipeline
        # incremental_fetcher 缓存
        self._inc_cache: Dict[str, IncrementalFetcher] = {}

    # ---------- 调度核心 ----------

    def _select(self, candidates: List[BaseScraper]) -> Optional[BaseScraper]:
        if not candidates:
            return None
        if self.config.strategy == DispatchStrategy.PRIORITY:
            return sorted(candidates, key=lambda s: s.priority)[0]
        if self.config.strategy == DispatchStrategy.ROUND_ROBIN:
            with self._rr_lock:
                self._rr_cursor = (self._rr_cursor + 1) % len(candidates)
                return candidates[self._rr_cursor]
        if self.config.strategy == DispatchStrategy.RANDOM:
            import random
            return random.choice(candidates)
        # BEST_SCORE
        names = [s.name for s in candidates]
        best_name = self.health.best(names)
        if not best_name:
            return candidates[0]
        for s in candidates:
            if s.name == best_name:
                return s
        return candidates[0]

    def _resolve_scraper(self, name: Optional[str], url: str) -> Optional[BaseScraper]:
        if name:
            return self.registry.get(name)
        domain = urlparse(url).netloc
        candidates = self.registry.for_domain(domain)
        return self._select(candidates) if candidates else None

    def _get_incremental(self, scraper: BaseScraper) -> Optional[IncrementalFetcher]:
        if not self.config.enable_incremental or not scraper.supports_incremental:
            return None
        if scraper.name in self._inc_cache:
            return self._inc_cache[scraper.name]
        fetcher = IncrementalFetcher(
            scraper,
            persist_path=f"{self.config.state_dir}/incremental_{scraper.name}.json",
        )
        self._inc_cache[scraper.name] = fetcher
        return fetcher

    def dispatch(self, url: str, scraper_name: Optional[str] = None,
                 **kwargs: Any) -> ScrapeResult:
        """单 URL 调度：选源 → 增量判断 → 抓取 → 记录健康。"""
        scraper = self._resolve_scraper(scraper_name, url)
        if not scraper:
            return ScrapeResult(
                url=url, status=ScrapeStatus.FAILED,
                error="no scraper available",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                source=scraper_name or "auto",
            )

        # 反爬包裹：AntiBlockPipeline 提供的是 before_request/after_request，
        # 老的 wrap_request 分支从未命中过（方法不存在），等于调度完全没走反爬。
        decision: Optional[Dict[str, Any]] = None
        if self.anti_block is not None and hasattr(self.anti_block, "before_request"):
            try:
                decision = self.anti_block.before_request(url)
            except Exception as exc:  # noqa: BLE001
                log.debug("anti_block before_request failed: %s", exc)
                decision = None
        if decision:
            delay = float(decision.get("delay") or 0.0)
            if decision.get("budget_ok") is False and self.config.reject_over_budget:
                return ScrapeResult(
                    url=url, status=ScrapeStatus.RATE_LIMITED, source=scraper.name,
                    error="over_budget（反爬预算耗尽，已拒绝请求）",
                    started_at=datetime.now().isoformat(timespec="seconds"),
                    finished_at=datetime.now().isoformat(timespec="seconds"),
                )
            if delay > 0:
                time.sleep(min(delay, MAX_ANTI_BLOCK_DELAY))

        started_ms = time.monotonic()
        result: Optional[ScrapeResult] = None
        error: Optional[BaseException] = None
        try:
            inc = self._get_incremental(scraper)
            result = inc.fetch(url, **kwargs) if inc else scraper.fetch(url, **kwargs)
            self.health.record(
                scraper.name, result.status, result.duration_ms,
                domain=scraper.domain,
                cooldown_seconds=self.config.cooldown_seconds,
            )
            return result
        except Exception as exc:  # noqa: BLE001
            error = exc
            return ScrapeResult(
                url=url, status=ScrapeStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                source=scraper.name,
            )
        finally:
            if decision is not None and hasattr(self.anti_block, "after_request"):
                elapsed_ms = (time.monotonic() - started_ms) * 1000.0
                fb_response: Any = result
                fb_error: Optional[BaseException] = error
                if result is not None and result.status in (
                        ScrapeStatus.FAILED, ScrapeStatus.BLOCKED):
                    fb_error = error or RuntimeError(f"{result.status.name}: {result.error}")
                try:
                    self.anti_block.after_request(
                        decision, fb_response, elapsed_ms=elapsed_ms, error=fb_error)
                except Exception as exc:  # noqa: BLE001
                    log.debug("anti_block after_request failed: %s", exc)

    def dispatch_batch(self, urls: Iterable[str], scraper_name: Optional[str] = None,
                       **kwargs: Any) -> List[ScrapeResult]:
        """批量调度。"""
        return [self.dispatch(u, scraper_name, **kwargs) for u in urls]

    # ---------- 断点续爬 ----------

    def run_resume(self, scraper_name: Optional[str] = None,
                   max_tasks: int = 1000) -> Dict[str, Any]:
        """从持久化状态恢复并执行 pending 任务。"""
        processed = 0
        succeeded = 0
        failed = 0
        while processed < max_tasks:
            task = self.resume.next_task()
            if not task:
                break
            result = self.dispatch(task.url, task.scraper_name, **task.kwargs)
            if result.status == ScrapeStatus.SUCCESS:
                self.resume.complete(task.task_id)
                succeeded += 1
            else:
                self.resume.fail(
                    task.task_id,
                    result.error or result.status.name,
                    max_attempts=self.config.max_attempts,
                )
                failed += 1
            processed += 1
        return {
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "progress": self.resume.progress(),
        }

    def queue_resume(self, urls: Iterable[str], scraper_name: str = "default",
                     **kwargs: Any) -> List[str]:
        """把 URL 入队持久化任务列表（可后续调 run_resume）。"""
        return self.resume.enqueue(urls, scraper_name, **kwargs)

    # ---------- 统计 ----------

    def report(self) -> Dict[str, Any]:
        return {
            "registry": self.registry.stats(),
            "health": self.health.report(),
            "resume": self.resume.progress(),
            "strategy": self.config.strategy.name,
        }


# ============================================================================
# 7. 默认接入 v9 现有 scraper（外部包装）
# ============================================================================

def register_default_scrapers(registry: Optional[ScraperRegistry] = None) -> int:
    """把 v9 已有 scraper 通过 ExternalScraperAdapter 接入调度器。

    返回注册的 scraper 数。失败/缺失依赖时不报错（try/except 吞掉）。
    """
    reg = registry or get_default_registry()
    count = 0

    def _safe_register(name: str, fetch_fn: Callable[..., Any],
                       domain: str = "", priority: int = 100) -> bool:
        try:
            reg.register_external(name=name, fetch_fn=fetch_fn,
                                  domain=domain, priority=priority)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("register %s failed: %s", name, exc)
            return False

    try:
        from scripts.scraper import FinancialPageScraper
        sc = FinancialPageScraper()
        if _safe_register("financial_page",
                          lambda url, **kw: sc.fetch(url, **kw),
                          domain="", priority=10):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("FinancialPageScraper not available: %s", exc)

    try:
        from scripts.eastmoney_scraper import get_eastmoney_data
        if _safe_register(
            "eastmoney_api",
            lambda symbol, data_type="quote", **kw: get_eastmoney_data(symbol, data_type, **kw),
            domain="eastmoney.com", priority=20,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("eastmoney_api not available: %s", exc)

    try:
        from scripts.sina_scraper import get_realtime_quote
        if _safe_register(
            "sina_hq",
            lambda symbol, **kw: get_realtime_quote(symbol, **kw),
            domain="hq.sinajs.cn", priority=30,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("sina_hq not available: %s", exc)

    try:
        from scripts.cninfo_scraper import search_cninfo_announcements
        if _safe_register(
            "cninfo",
            lambda keyword, **kw: search_cninfo_announcements(keyword, **kw),
            domain="cninfo.com.cn", priority=20,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("cninfo not available: %s", exc)

    try:
        from scripts.cls_scraper import get_telegraph
        if _safe_register(
            "cls_telegraph",
            lambda **kw: get_telegraph(**kw),
            domain="cls.cn", priority=40,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("cls_telegraph not available: %s", exc)

    try:
        from scripts.jisilu_scraper import get_convertible_bonds
        if _safe_register(
            "jisilu",
            lambda **kw: get_convertible_bonds(**kw),
            domain="jisilu.cn", priority=40,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("jisilu not available: %s", exc)

    try:
        from scripts.wallstreetcn_scraper import get_live_news
        if _safe_register(
            "wallstreetcn",
            lambda **kw: get_live_news(**kw),
            domain="wallstreetcn.com", priority=40,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("wallstreetcn not available: %s", exc)

    try:
        from scripts.financial_data_mcp import FinancialDataRouter
        router = FinancialDataRouter()
        if _safe_register(
            "financial_data_router",
            lambda query_type, **kw: router.query(query_type, **kw),
            domain="", priority=5,
        ):
            count += 1
    except Exception as exc:  # noqa: BLE001
        log.debug("financial_data_router not available: %s", exc)

    return count


# ============================================================================
# 8. 便捷入口（CLI 友好）
# ============================================================================

def quick_dispatch(url: str, scraper_name: Optional[str] = None,
                   **kwargs: Any) -> ScrapeResult:
    """一键调度：自动注册默认 scraper + 调度单个 URL。"""
    if not _DEFAULT_REGISTRY.all_names():
        register_default_scrapers()
    dispatcher = CrawlerDispatcher(_DEFAULT_REGISTRY)
    return dispatcher.dispatch(url, scraper_name, **kwargs)


def quick_report() -> Dict[str, Any]:
    """一键报告：调度器当前状态。"""
    if not _DEFAULT_REGISTRY.all_names():
        register_default_scrapers()
    dispatcher = CrawlerDispatcher(_DEFAULT_REGISTRY)
    return dispatcher.report()


# ============================================================================
# 9. 公共导出
# ============================================================================

__all__ = [
    "ScrapeStatus",
    "ScrapeResult",
    "BaseScraper",
    "ExternalScraperAdapter",
    "ScraperRegistry",
    "get_default_registry",
    "register",
    "SourceHealthRecord",
    "HealthTracker",
    "IncrementalIndex",
    "IncrementalFetcher",
    "ResumeTask",
    "ResumeState",
    "ResumeManager",
    "DispatchStrategy",
    "DispatchConfig",
    "CrawlerDispatcher",
    "register_default_scrapers",
    "quick_dispatch",
    "quick_report",
]