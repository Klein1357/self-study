"""
阶段 3 · 3.4 并发控制：信号量、限速、任务队列
======================================================
对应网页章节：#s3-4

本脚本实测四件事：
  1. 不限并发会怎样（同时打出去 50 个请求）
  2. Semaphore 如何把并发压到指定值
  3. 令牌桶限速器：把"速率"控制在每秒 N 个
  4. 生产者-消费者队列：边生产 URL 边消费，内存可控

运行：python3 33_rate_control.py
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

# --- [Windows UTF-8 输出适配] ---
# Windows 控制台默认 GBK（cp936），而本课会输出 ✓ ✗ ⚠ ▸ ✅ 等非 ASCII 符号，
# 不处理会在打印时抛 UnicodeEncodeError 直接崩溃。这里统一切到 UTF-8，
# 编码不了就降级替换，保证在中文 Windows 上也能完整跑完。
import sys as _dsh_sys

for _stream in (_dsh_sys.stdout, _dsh_sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass  # 老解释器或已被重定向/包装的流不支持重配
del _stream, _dsh_sys


T0 = time.perf_counter()


def ts() -> str:
    """相对时间戳。"""
    return f"[{time.perf_counter() - T0:6.2f}s]"


# ============================================================
# 通用：带并发数统计的请求模拟
# ============================================================
@dataclass
class ConcurrencyTracker:
    """统计运行过程中的瞬时并发峰值。"""

    current: int = 0
    peak: int = 0
    completed: int = 0
    timeline: list[tuple[float, int]] = field(default_factory=list)

    def enter(self) -> None:
        """进入临界区。"""
        self.current += 1
        self.peak = max(self.peak, self.current)
        self.timeline.append((time.perf_counter() - T0, self.current))

    def exit(self) -> None:
        """离开临界区。"""
        self.current -= 1
        self.completed += 1


async def fake_request(url: str, delay: float = 0.4) -> str:
    """模拟一次网络请求。"""
    await asyncio.sleep(delay)
    return url


# ============================================================
# 实验 1：不限并发
# ============================================================
async def unlimited_spider(urls: list[str], tracker: ConcurrencyTracker) -> list[str]:
    """
    不加任何限制地并发（危险示范）。

    Args:
        urls: URL 列表。
        tracker: 并发统计器。

    Returns:
        结果列表。
    """

    async def one(u: str) -> str:
        tracker.enter()
        try:
            return await fake_request(u)
        finally:
            tracker.exit()      # 用 finally 保证异常时也能释放计数

    t = time.perf_counter()
    results = await asyncio.gather(*(one(u) for u in urls))
    print(f"{ts()}   不限并发：{len(urls)} 个请求，耗时 {time.perf_counter() - t:.2f}s，"
          f"瞬间并发峰值 {tracker.peak}")
    return list(results)


# ============================================================
# 实验 2：Semaphore 限制并发数
# ============================================================
async def semaphore_spider(
    urls: list[str],
    tracker: ConcurrencyTracker,
    limit: int = 5,
) -> list[str]:
    """
    用 Semaphore 限制同时执行的协程数。

    Args:
        urls: URL 列表。
        tracker: 并发统计器。
        limit: 最大并发数。

    Returns:
        结果列表。
    """
    sem = asyncio.Semaphore(limit)

    async def one(u: str) -> str:
        async with sem:             # 拿不到令牌就在这里排队，不占用任何资源
            tracker.enter()
            try:
                return await fake_request(u)
            finally:
                tracker.exit()

    t = time.perf_counter()
    results = await asyncio.gather(*(one(u) for u in urls))
    print(f"{ts()}   Semaphore({limit})：{len(urls)} 个请求，耗时 {time.perf_counter() - t:.2f}s，"
          f"并发峰值 {tracker.peak}")
    return list(results)


# ============================================================
# 实验 3：令牌桶限速（控制速率，而非并发数）
# ============================================================
class TokenBucket:
    """
    令牌桶限速器。

    与 Semaphore 的本质区别：
      · Semaphore 控制"同时有几个在跑"（并发数）
      · TokenBucket 控制"每秒放行几个"（速率）
    真实爬虫往往两者都要：并发 10 + 每秒 5 个。

    Attributes:
        rate: 每秒补充的令牌数。
        capacity: 桶容量（允许的突发量）。
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """获取一个令牌，不够就等到够。"""
        async with self._lock:
            while True:
                now = time.monotonic()
                # 按流逝时间补充令牌
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
                await asyncio.sleep(wait)


async def rate_limited_spider(urls: list[str], rate: float = 5.0) -> list[str]:
    """
    令牌桶限速采集。

    Args:
        urls: URL 列表。
        rate: 每秒允许的请求数。

    Returns:
        结果列表。
    """
    bucket = TokenBucket(rate=rate)
    stamps: list[float] = []

    async def one(u: str) -> str:
        await bucket.acquire()      # ← 限速发生在"发请求之前"
        stamps.append(time.perf_counter() - T0)
        return await fake_request(u, 0.2)

    t = time.perf_counter()
    results = await asyncio.gather(*(one(u) for u in urls))
    elapsed = time.perf_counter() - t

    print(f"{ts()}   TokenBucket({rate}/s)：{len(urls)} 个请求，耗时 {elapsed:.2f}s")
    # 计算实际速率（跳过前 capacity 个突发）
    if len(stamps) > int(rate) + 1:
        tail = stamps[int(rate) + 1:]
        span = tail[-1] - tail[0]
        actual = (len(tail) - 1) / span if span > 0 else 0
        print(f"           突发阶段过后实测速率：{actual:.2f}/s（目标 {rate}/s）")
    return list(results)


# ============================================================
# 实验 4：生产者-消费者队列
# ============================================================
async def queue_spider(n_pages: int = 5, per_page: int = 10) -> dict[str, int]:
    """
    用 asyncio.Queue 实现生产者-消费者模式。

    场景：先爬列表页拿到详情页链接（生产者），
    再并发爬详情页（消费者）。队列让两件事解耦，
    详情页不必等列表页全部爬完才开始。

    Args:
        n_pages: 列表页数量。
        per_page: 每页链接数。

    Returns:
        统计信息。
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=20)
    stats = {"produced": 0, "consumed": 0, "max_qsize": 0}
    qsizes: list[int] = []

    async def producer() -> None:
        """生产详情页 URL。"""
        for p in range(1, n_pages + 1):
            await asyncio.sleep(0.3)        # 模拟爬列表页
            for i in range(per_page):
                await queue.put(f"detail/p{p}/item{i}")
                stats["produced"] += 1
            qsizes.append(queue.qsize())
            print(f"{ts()}   生产：列表页 {p} → 已投递 {stats['produced']} 个详情链接"
                  f"（队列积压 {queue.qsize()}）")
        # 投递 N 个哨兵通知消费者收工
        for _ in range(3):
            await queue.put(None)

    async def worker(wid: int) -> None:
        """消费详情页 URL。"""
        while True:
            url = await queue.get()
            stats["max_qsize"] = max(stats["max_qsize"], queue.qsize())
            try:
                if url is None:
                    return              # 收到哨兵，正常退出
                await fake_request(url, 0.25)
                stats["consumed"] += 1
            finally:
                queue.task_done()

    t = time.perf_counter()
    await asyncio.gather(producer(), *(worker(i) for i in range(3)))
    elapsed = time.perf_counter() - t

    print(f"{ts()}   消费完成：{stats['consumed']} 个详情页，耗时 {elapsed:.2f}s")
    return {"produced": stats["produced"], "consumed": stats["consumed"],
            "max_qsize": stats["max_qsize"], "elapsed": int(elapsed * 100)}


# ============================================================
# 实验 5：Semaphore + 限速 组合
# ============================================================
async def combined_control(urls: list[str], concurrency: int = 8, rate: float = 6.0) -> None:
    """
    真实生产配置：并发上限 + 速率上限双保险。

    Args:
        urls: URL 列表。
        concurrency: 最大并发。
        rate: 每秒最大请求数。
    """
    sem = asyncio.Semaphore(concurrency)
    bucket = TokenBucket(rate=rate, capacity=rate)
    tracker = ConcurrencyTracker()

    async def one(u: str) -> str:
        await bucket.acquire()
        async with sem:
            tracker.enter()
            try:
                return await fake_request(u, 0.5)
            finally:
                tracker.exit()

    t = time.perf_counter()
    await asyncio.gather(*(one(u) for u in urls))
    elapsed = time.perf_counter() - t
    print(f"{ts()}   组合控制（并发≤{concurrency}, 速率≤{rate}/s）："
          f"{len(urls)} 个请求，耗时 {elapsed:.2f}s")
    print(f"           并发峰值 {tracker.peak}（未超上限 ✓）")
    print(f"           平均速率 {len(urls) / elapsed:.2f}/s")


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    """运行全部并发控制实验。"""
    urls = [f"https://example.com/item/{i}" for i in range(50)]

    print("=" * 68)
    print("3.4 并发控制实验")
    print("=" * 68)

    print("\n【实验 1&2】Semaphore：把并发压下来")
    print("-" * 68)
    t_all = ConcurrencyTracker()
    await unlimited_spider(urls, t_all)
    print(f"   ⚠️ 峰值 {t_all.peak} —— 真实环境会被判定为 CC 攻击，直接封 IP\n")

    results = {}
    for limit in (2, 5, 10):
        tk = ConcurrencyTracker()
        await semaphore_spider(urls, tk, limit)
        results[limit] = tk.peak

    print("\n   规律：并发越小越慢，但越安全。")
    print("   选多少？看对方的容忍度 —— 保守从 5 开始，观察响应时间再往上调。")

    print("\n【实验 3】令牌桶：控制速率")
    print("-" * 68)
    for rate in (3.0, 10.0):
        await rate_limited_spider(urls[:20], rate=rate)
        print()

    print("【实验 4】生产者-消费者队列")
    print("-" * 68)
    qs = await queue_spider()
    print(f"   队列最大积压 {qs['max_qsize']}（maxsize=20 时自动反压生产者）")
    print("   价值：列表页和详情页并行推进，总耗时 ≈ max(两者) 而不是两者之和。")

    print("\n【实验 5】组合控制（生产推荐配置）")
    print("-" * 68)
    await combined_control(urls, concurrency=8, rate=6.0)

    print("\n" + "=" * 68)
    print("选型速查")
    print("=" * 68)
    print("  场景                          用什么")
    print("  ────────────────────────────  ──────────────────")
    print("  只要别把对方打挂              Semaphore(N)")
    print("  有明确 QPS 要求（如 5次/秒）  TokenBucket")
    print("  要压测自己人的接口            Semaphore(N) 大 N")
    print("  URL 要动态生成 / 海量          asyncio.Queue")
    print("  生产环境                      两者组合 + 自适应退避")
    print("\n  记住：并发控制的本质不是『技术』，是『礼仪』。")
    print("       你希望别人怎么爬你的网站，就怎么爬别人的。")


if __name__ == "__main__":
    asyncio.run(main())
