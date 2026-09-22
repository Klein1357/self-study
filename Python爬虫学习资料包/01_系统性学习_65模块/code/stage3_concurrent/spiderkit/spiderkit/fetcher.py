"""
spiderkit 抓取器模块
====================
对应网页章节：#s3-3 #s3-4 #s3-5

职责：
  · 统一的异步 HTTP 客户端（连接池 + 超时）
  · 并发控制（Semaphore + 令牌桶限速）
  · 重试（指数退避 + 抖动 + 错误分类）
  · 熔断（连续失败快速失败）

不负责：解析、存储。保持单一职责。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import httpx

from .config import Settings

log = logging.getLogger("spiderkit.fetcher")


# ============================================================
# 错误分类（重试策略的唯一依据）
# ============================================================
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})
RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.PoolTimeout,
)


def is_retryable(exc: BaseException) -> bool:
    """
    判断异常是否值得重试。

    Args:
        exc: 捕获到的异常。

    Returns:
        True 表示应重试。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, RETRYABLE_EXC)


def backoff_delay(attempt: int, base: float = 0.5, cap: float = 30.0) -> float:
    """
    指数退避 + 全抖动。

    Args:
        attempt: 第几次（从 1 开始）。
        base: 基础间隔秒数。
        cap: 上限秒数。

    Returns:
        应等待的秒数。
    """
    ceiling = min(cap, base * 2 ** (attempt - 1))
    return random.uniform(0, ceiling)


# ============================================================
# 令牌桶限速
# ============================================================
class TokenBucket:
    """
    异步令牌桶限速器。

    Attributes:
        rate: 每秒放行令牌数。
        capacity: 桶容量（允许的突发量）。
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """获取一个令牌，不足则等待。"""
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._last) * self.rate,
                )
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self.rate)


# ============================================================
# 熔断器
# ============================================================
class CircuitBreaker:
    """
    熔断器：连续失败达阈值即跳闸，冷却后半开试探。

    Attributes:
        threshold: 触发熔断的连续失败次数。
        cooldown: 冷却秒数。
    """

    def __init__(self, threshold: int = 10, cooldown: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self.failures = 0
        self.opened_at = 0.0
        self.is_open = False
        self.rejected = 0

    def allow(self) -> bool:
        """
        是否允许发起请求。

        Returns:
            True 表示放行。
        """
        if not self.is_open:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            log.warning("熔断冷却结束，恢复放行（半开试探）")
            self.is_open = False
            self.failures = 0
            return True
        self.rejected += 1
        return False

    def record_success(self) -> None:
        """记录一次成功。"""
        self.failures = 0

    def record_failure(self) -> None:
        """记录一次失败，达阈值则跳闸。"""
        self.failures += 1
        if self.failures >= self.threshold and not self.is_open:
            self.is_open = True
            self.opened_at = time.monotonic()
            log.error("连续失败 %d 次 → 熔断 %.0f 秒", self.failures, self.cooldown)


# ============================================================
# 统计
# ============================================================
@dataclass
class FetchStats:
    """抓取统计。"""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    retried: int = 0
    retry_wait: float = 0.0
    bytes_in: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        """已耗时（秒）。"""
        return time.monotonic() - self.started_at

    @property
    def success_rate(self) -> float:
        """成功率。"""
        return self.succeeded / self.total * 100 if self.total else 0.0

    def report(self) -> str:
        """生成统计报告。"""
        return (
            f"请求 {self.total} | 成功 {self.succeeded} | 失败 {self.failed} "
            f"| 成功率 {self.success_rate:.1f}% | 重试 {self.retried} 次"
            f"（等待 {self.retry_wait:.1f}s）"
            f"| 流量 {self.bytes_in / 1024:.1f} KB | 耗时 {self.elapsed:.1f}s"
        )


# ============================================================
# 抓取器
# ============================================================
class Fetcher:
    """
    异步 HTTP 抓取器，封装并发控制 + 限速 + 重试 + 熔断。

    用法：
        async with Fetcher(settings) as f:
            html = await f.get(url)
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sem = asyncio.Semaphore(settings.concurrency)
        self.bucket = TokenBucket(settings.rate_limit)
        self.breaker = CircuitBreaker()
        self.stats = FetchStats()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Fetcher:
        """创建客户端。"""
        limits = httpx.Limits(
            max_connections=self.settings.concurrency * 2,
            max_keepalive_connections=self.settings.concurrency,
        )
        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=httpx.Timeout(
                self.settings.timeout_read,
                connect=self.settings.timeout_connect,
            ),
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
            proxy=self.settings.proxy,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        """关闭客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get(self, url: str) -> str | None:
        """
        抓取一个 URL（含重试与熔断）。

        Args:
            url: 目标地址。

        Returns:
            页面 HTML；彻底失败时返回 None。
        """
        self.stats.total += 1

        if not self.breaker.allow():
            self.stats.failed += 1
            log.debug("熔断中，跳过 %s", url)
            return None

        await self.bucket.acquire()          # 限速：在真正发包前

        async with self.sem:                 # 并发上限
            for attempt in range(1, self.settings.max_retries + 2):
                try:
                    assert self._client is not None
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                    self.stats.succeeded += 1
                    self.stats.bytes_in += len(resp.content)
                    self.breaker.record_success()
                    if attempt > 1:
                        self.stats.retried += 1
                        log.debug("第 %d 次尝试成功：%s", attempt, url)
                    return resp.text
                except Exception as e:                  # noqa: BLE001
                    last = attempt > self.settings.max_retries
                    if not is_retryable(e) or last:
                        self.stats.failed += 1
                        self.breaker.record_failure()
                        log.warning("抓取失败（%s）：%s — %s",
                                    "不可重试" if not is_retryable(e) else "重试耗尽",
                                    url, type(e).__name__)
                        return None
                    delay = backoff_delay(attempt, self.settings.retry_base)
                    self.stats.retry_wait += delay
                    log.debug("第 %d 次失败（%s），%.2fs 后重试：%s",
                              attempt, type(e).__name__, delay, url)
                    await asyncio.sleep(delay)
        return None
