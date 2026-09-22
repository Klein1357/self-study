"""
阶段 3 · 3.5 重试与容错
======================================================
对应网页章节：#s3-5

本脚本实测：
  1. 哪些错误该重试、哪些绝不该重试（错误分类表）
  2. 指数退避 + 抖动：为什么必须加随机抖动
  3. 用 tenacity 优雅地写重试（装饰器风格）
  4. 熔断器：连续失败到阈值就"跳闸"，避免无谓请求
  5. 降级策略：重试都失败之后怎么办

运行：python3 34_retry_resilience.py
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

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



# ============================================================
# 一、错误分类：决定"要不要重试"的唯一依据
# ============================================================
class Decision(str, Enum):
    """重试决策。"""

    RETRY = "重试"
    FAIL = "直接失败"
    ABORT = "终止整个任务"


@dataclass(frozen=True)
class ErrorRule:
    """一条错误处理规则。"""

    match: str
    decision: Decision
    reason: str


# 这是爬虫工程里最重要的一张表 —— 无脑重试是新手最大的性能浪费来源
ERROR_RULES: list[ErrorRule] = [
    ErrorRule("ConnectTimeout / ReadTimeout", Decision.RETRY, "网络抖动，大概率下次能成"),
    ErrorRule("ConnectionResetError", Decision.RETRY, "连接被掐，重连即可"),
    ErrorRule("503 Service Unavailable", Decision.RETRY, "服务端限流，退避后应恢复"),
    ErrorRule("502 / 504", Decision.RETRY, "网关问题，通常瞬时"),
    ErrorRule("429 Too Many Requests", Decision.RETRY, "被限速，必须退避（看 Retry-After）"),
    ErrorRule("500 Internal Server Error", Decision.RETRY, "服务端偶发异常，可试 1~2 次"),
    ErrorRule("404 Not Found", Decision.FAIL, "资源不存在，重试一万次也还是没有"),
    ErrorRule("403 Forbidden", Decision.ABORT, "被封了，重试只会加深封禁"),
    ErrorRule("401 Unauthorized", Decision.ABORT, "Cookie 失效，需重新登录"),
    ErrorRule("400 Bad Request", Decision.FAIL, "参数写错了，重试是白费"),
]

# 仅这些异常/状态码才值得重试
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
RETRYABLE_EXC = (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError)


def print_error_table() -> None:
    """打印错误分类表。"""
    print("=" * 78)
    print("【表 1】错误分类表 —— 重试策略的唯一依据")
    print("=" * 78)
    print(f"  {'错误':<34}{'决策':<10}原因")
    print("  " + "-" * 74)
    for r in ERROR_RULES:
        mark = "✓" if r.decision is Decision.RETRY else "✗"
        print(f"  {r.match:<34}{r.decision.value:<10}{mark} {r.reason}")


def should_retry(exc: Exception) -> bool:
    """
    判断一个异常是否值得重试。

    Args:
        exc: 捕获到的异常。

    Returns:
        True 表示应重试。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_STATUS
    return isinstance(exc, RETRYABLE_EXC)


# ============================================================
# 二、退避策略对比：固定 / 指数 / 指数+抖动
# ============================================================
def backoff_fixed(attempt: int, base: float = 1.0) -> float:
    """固定间隔。"""
    return base


def backoff_exponential(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """纯指数退避（有问题，见下文）。"""
    return min(cap, base * 2 ** (attempt - 1))


def backoff_jitter(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """
    指数退避 + 全抖动（AWS 推荐）。

    抖动的作用：如果有 1000 个爬虫同时被限流，纯指数退避会让它们
    在同一时刻集体重试（惊群效应），把服务器再打挂一次。
    加上随机抖动，重试时间就散开了。

    Args:
        attempt: 第几次重试（从 1 开始）。
        base: 基础间隔。
        cap: 最大间隔。

    Returns:
        本次应等待的秒数。
    """
    exp = min(cap, base * 2 ** (attempt - 1))
    return random.uniform(0, exp)         # 全抖动：0 ~ exp 之间随机


def compare_backoff() -> None:
    """对比三种退避策略的时间序列。"""
    print("\n" + "=" * 78)
    print("【表 2】三种退避策略对比（假设第 1 次就失败，共重试 5 次）")
    print("=" * 78)

    print(f"  {'次数':<6}{'固定':<10}{'纯指数':<12}{'指数+抖动（推荐）'}")
    print("  " + "-" * 74)
    for i in range(1, 6):
        f = backoff_fixed(i)
        e = backoff_exponential(i)
        j = backoff_jitter(i)
        print(f"  {i:<6}{f:<10.2f}{e:<12.2f}{j:.2f}")
    print("\n  纯指数的问题：假设同一时刻有 500 个客户端都被限流，")
    print("  它们会在第 1、2、4、8 秒同时苏醒，形成新的流量尖峰。")
    print("  加抖动后，重试时刻被打散成均匀分布，压力平摊。")

    # 用数据说话：模拟 200 个客户端第 4 次重试的时刻
    print("\n  模拟 200 个客户端在第 4 次重试的时刻分布（基础间隔 8 秒）：")
    pure = [8.0] * 200
    jit = [backoff_jitter(4, cap=60) for _ in range(200)]
    print(f"    纯指数：min={min(pure):.2f}  max={max(pure):.2f}  "
          f"同一秒内重试 {len(pure)} 个（100% 撞车）")
    buckets: dict[int, int] = {}
    for j in jit:
        buckets[int(j)] = buckets.get(int(j), 0) + 1
    spread = len(buckets)
    print(f"    加抖动：min={min(jit):.2f}  max={max(jit):.2f}  "
          f"分散在 {spread} 个不同秒内，单秒最多 {max(buckets.values())} 个")
    print(f"    → 峰值压力从 {len(pure)} 降到 {max(buckets.values())}，"
          f"降低 {len(pure) / max(buckets.values()):.0f} 倍")


# ============================================================
# 三、模拟一个"不稳定"的服务器
# ============================================================
@dataclass
class FlakyServer:
    """
    前 N 次返回 503，之后成功 —— 用于验证重试逻辑。

    Attributes:
        fail_times: 前多少次请求失败。
        calls: 已调用次数。
        always_fail: 是否永久失败（测熔断）。
    """

    fail_times: int = 2
    always_fail: bool = False
    calls: int = 0
    history: list[str] = field(default_factory=list)

    async def get(self, url: str, delay: float = 0.02) -> str:
        """
        模拟一次请求。

        Args:
            url: 请求地址。
            delay: 模拟网络延迟。

        Raises:
            httpx.HTTPStatusError: 当响应为 503 时。

        Returns:
            响应正文。
        """
        self.calls += 1
        await asyncio.sleep(delay)
        if self.always_fail or self.calls <= self.fail_times:
            self.history.append("503")
            raise httpx.HTTPStatusError(
                "503 Service Unavailable",
                request=httpx.Request("GET", url),
                response=httpx.Response(503),
            )
        self.history.append("200")
        return f"OK from {url}"


# ============================================================
# 四、手写重试 vs tenacity
# ============================================================
async def manual_retry(
    server: FlakyServer,
    url: str,
    max_attempts: int = 4,
    base: float = 0.1,
) -> tuple[str | None, list[float]]:
    """
    手写重试循环 —— 理解原理用，生产环境建议用 tenacity。

    Args:
        server: 模拟服务器。
        url: 请求地址。
        max_attempts: 最大尝试次数。
        base: 退避基础值。

    Returns:
        (结果或 None, 每次等待时长列表)
    """
    waits: list[float] = []
    for attempt in range(1, max_attempts + 1):
        try:
            result = await server.get(url)
            return result, waits
        except Exception as e:                      # noqa: BLE001
            if not should_retry(e):
                print(f"      第 {attempt} 次失败，且不可重试 → 放弃")
                return None, waits
            if attempt == max_attempts:
                print(f"      第 {attempt} 次仍失败 → 用尽重试次数，放弃")
                return None, waits
            wait = backoff_jitter(attempt, base=base, cap=2.0)
            waits.append(wait)
            print(f"      第 {attempt} 次失败（503），{wait:.3f}s 后重试")
            await asyncio.sleep(wait)
    return None, waits


async def tenacity_retry(server: FlakyServer, url: str, max_attempts: int = 4) -> str | None:
    """
    用 tenacity 实现同样的逻辑（生产推荐）。

    tenacity 的价值：
      · 声明式，逻辑和重试策略分离
      · wait_exponential_jitter 内置抖动
      · 支持 before_sleep 回调打日志
      · 支持 reraise / retry_error_callback 精细控制

    Args:
        server: 模拟服务器。
        url: 请求地址。
        max_attempts: 最大尝试次数。

    Returns:
        结果或 None。
    """
    waits: list[float] = []

    def log_retry(retry_state) -> None:
        """重试前打日志。"""
        exc = retry_state.outcome.exception()
        print(f"      第 {retry_state.attempt_number} 次失败"
              f"（{type(exc).__name__}），准备重试…")

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential_jitter(initial=0.1, max=2.0, jitter=0.1),
            retry=retry_if_exception_type(RETRYABLE_EXC + (httpx.HTTPStatusError,)),
            before_sleep=log_retry,
            reraise=True,
        ):
            with attempt:
                return await server.get(url)
    except (RetryError, httpx.HTTPStatusError):
        return None
    return None


# ============================================================
# 五、熔断器
# ============================================================
class CircuitState(str, Enum):
    """熔断器状态。"""

    CLOSED = "闭合（正常放行）"
    OPEN = "断开（直接拒绝）"
    HALF_OPEN = "半开（试探放行）"


class CircuitBreaker:
    """
    熔断器：连续失败达阈值就跳闸，避免持续无效请求。

    为什么需要它：
      如果对方站点已经挂了，无脑重试只会让你花 10 分钟拿到 0 条数据。
      熔断器让你在第 5 次失败时立刻停下，5 秒后放 1 个请求试探，
      成功就恢复，失败就继续熔断。这既省时间，也少打扰对方。

    Attributes:
        threshold: 连续失败多少次触发断开。
        recovery: 断开后多少秒进入半开试探。
    """

    def __init__(self, threshold: int = 5, recovery: float = 3.0) -> None:
        self.threshold = threshold
        self.recovery = recovery
        self.failures = 0
        self.state = CircuitState.CLOSED
        self.opened_at = 0.0
        self.rejected = 0
        self.passed = 0

    async def call(self, fn: Callable[[], Awaitable[str]]) -> str | None:
        """
        通过熔断器调用函数。

        Args:
            fn: 无参异步函数。

        Returns:
            成功结果，或 None（被熔断 / 调用失败）。
        """
        # 断开状态下检查是否该进入半开
        if self.state is CircuitState.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery:
                self.state = CircuitState.HALF_OPEN
                print(f"      [熔断器] {self.recovery}s 已过 → 进入半开，试探 1 次")
            else:
                self.rejected += 1
                return None                    # 快速失败，不发请求
        elif self.state is CircuitState.HALF_OPEN:
            self.rejected += 1
            return None                        # 半开只放一个，其余拒绝

        try:
            result = await fn()
            # 成功：重置
            if self.state is CircuitState.HALF_OPEN:
                print("      [熔断器] 试探成功 → 恢复闭合")
            self.failures = 0
            self.state = CircuitState.CLOSED
            self.passed += 1
            return result
        except Exception:                      # noqa: BLE001
            self.failures += 1
            if self.state is CircuitState.HALF_OPEN or self.failures >= self.threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                print(f"      [熔断器] 连续失败 {self.failures} 次 → 跳闸！"
                      f"后续 {self.recovery}s 内请求直接拒绝")
            raise


async def demo_circuit_breaker() -> None:
    """演示熔断器在持续故障下的效果。"""
    print("\n" + "=" * 78)
    print("【实验 3】熔断器：持续故障下的快速失败")
    print("=" * 78)

    server = FlakyServer(always_fail=True)
    cb = CircuitBreaker(threshold=3, recovery=2.0)

    print("  配置：连续 3 次失败熔断，2 秒后半开试探")
    print("  目标：连续发起 12 次请求（服务器一直返回 503）\n")

    t0 = time.perf_counter()
    for i in range(1, 13):
        try:
            await cb.call(lambda: server.get(f"https://x.com/{i}"))
            print(f"    第 {i:>2} 次：成功")
        except Exception:                      # noqa: BLE001
            print(f"    第 {i:>2} 次：失败")
        await asyncio.sleep(0.05)

    elapsed = time.perf_counter() - t0
    print(f"\n  实际发出的请求数：{server.calls}（总尝试 12 次）")
    print(f"  被熔断器拦下的    ：{cb.rejected} 次（未产生任何网络请求）")
    print(f"  状态              ：{cb.state.value}")
    print(f"  耗时              ：{elapsed:.2f}s")
    print(f"  → 节省了 {cb.rejected} 次无效请求。若没有熔断，12 次全都得等超时。")


# ============================================================
# 六、完整容错链条
# ============================================================
@dataclass
class FetchStats:
    """采集统计。"""

    success: int = 0
    retried: int = 0
    failed: int = 0
    total_wait: float = 0.0


async def resilient_fetch(
    url: str,
    server: FlakyServer,
    stats: FetchStats,
    max_attempts: int = 3,
) -> str | None:
    """
    生产级容错请求：重试 + 退避 + 抖动 + 统计。

    Args:
        url: 请求地址。
        server: 模拟服务器。
        stats: 统计对象（原地修改）。
        max_attempts: 最大尝试次数。

    Returns:
        结果或 None（彻底失败）。
    """
    for attempt in range(1, max_attempts + 1):
        try:
            result = await server.get(url)
            if attempt > 1:
                stats.retried += 1
            stats.success += 1
            return result
        except Exception as e:                      # noqa: BLE001
            if not should_retry(e) or attempt == max_attempts:
                stats.failed += 1
                return None
            wait = backoff_jitter(attempt, base=0.1, cap=1.0)
            stats.total_wait += wait
            await asyncio.sleep(wait)
    return None


async def demo_resilient_batch() -> None:
    """演示整批任务在部分失败时的表现。"""
    print("\n" + "=" * 78)
    print("【实验 4】完整容错链条 —— 一批任务里有成功有失败")
    print("=" * 78)

    # 每个 URL 用独立的 server，失败次数随机
    servers = {i: FlakyServer(fail_times=random.choice([0, 1, 1, 2, 2, 3, 99]))
               for i in range(1, 16)}
    stats = FetchStats()

    async def one(i: int) -> tuple[int, str | None]:
        return i, await resilient_fetch(f"https://x.com/{i}", servers[i], stats)

    t0 = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(1, 16)))
    elapsed = time.perf_counter() - t0

    ok = [i for i, r in results if r]
    bad = [i for i, r in results if not r]
    print(f"  成功：{len(ok)} 个   {ok}")
    print(f"  失败：{len(bad)} 个   {bad}")
    print(f"  其中靠重试救回的：{stats.retried} 个")
    print(f"  总退避等待：{stats.total_wait:.2f}s   总耗时：{elapsed:.2f}s")
    print("\n  关键设计：单条失败不影响整批（gather 里每个任务自己吞掉异常）。")
    print("  失败的 {n} 条会被记进失败清单，下轮单独重爬。".replace("{n}", str(len(bad))))


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    """运行全部重试与容错实验。"""
    print_error_table()
    compare_backoff()

    print("\n" + "=" * 78)
    print("【实验 1】手写重试（理解原理）")
    print("=" * 78)
    server = FlakyServer(fail_times=2)
    print("  场景：服务器前 2 次返回 503，第 3 次成功\n")
    result, waits = await manual_retry(server, "https://x.com/a")
    print(f"  结果：{result}")
    print(f"  等待序列：{[f'{w:.3f}' for w in waits]} 秒")
    print(f"  服务器实际收到 {server.calls} 次请求")

    print("\n" + "=" * 78)
    print("【实验 2】tenacity（生产推荐）")
    print("=" * 78)
    server2 = FlakyServer(fail_times=2)
    print("  同样的场景，用 AsyncRetrying\n")
    r = await tenacity_retry(server2, "https://x.com/b")
    print(f"  结果：{r}")
    print(f"  服务器实际收到 {server2.calls} 次请求")

    await demo_circuit_breaker()
    await demo_resilient_batch()

    print("\n" + "=" * 78)
    print("容错四件套（记住这个顺序）")
    print("=" * 78)
    print("  1. 分类   —— 先判断该不该重试（90% 的性能浪费在这里）")
    print("  2. 退避   —— 指数增长 + 随机抖动（别让所有爬虫同时苏醒）")
    print("  3. 熔断   —— 连续失败到阈值就跳闸（快速失败优于慢慢失败）")
    print("  4. 记账   —— 失败项落盘，下轮单独重爬（永不丢数据）")


if __name__ == "__main__":
    asyncio.run(main())
