"""
阶段 3 · 3.2 asyncio 基础实验
======================================================
对应网页章节：#s3-2

本脚本从"串行 await 的反面教材"开始，逐步演示：
  1. 协程对象 vs 协程函数（不 await 会怎样）
  2. await 的语义：它是"让出点"，不是"并发开关"
  3. create_task / gather / as_completed 三种并发方式
  4. 事件循环是怎么调度的（打印时间线看得见）
  5. 阻塞代码如何毁掉整个事件循环

运行：python3 31_asyncio_basics.py
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

T0 = time.perf_counter()


def ts() -> str:
    """返回相对起始时刻的时间戳，用于观察调度顺序。"""
    return f"[{time.perf_counter() - T0:6.2f}s]"


async def fetch(name: str, delay: float) -> str:
    """
    模拟一次 IO 操作。

    Args:
        name: 任务名。
        delay: 耗时秒数。

    Returns:
        形如 "任务A 完成" 的结果字符串。
    """
    print(f"{ts()}   {name} 开始")
    await asyncio.sleep(delay)   # ← 唯一的让出点
    print(f"{ts()}   {name} 结束")
    return f"{name} 完成"


# ============================================================
# 实验 0：协程对象不是"已经开始执行"
# ============================================================
async def demo_coroutine_object() -> None:
    """演示协程函数调用后返回的只是一个待执行对象。"""
    print(f"\n{'=' * 60}")
    print("实验 0：协程对象 vs 协程函数")
    print("=" * 60)

    coro = fetch("A", 0.3)          # 注意：这行不会打印任何东西
    print(f"  调用 fetch('A', 0.3) 得到：{type(coro).__name__}")
    print("  此时函数体一行都没执行（上面的『开始』没打印）")
    print("  必须 await 或交给事件循环，才会真正运行")

    result = await coro
    print(f"  await 之后拿到结果：{result}")

    # 反面教材：协程对象创建了却从未 await
    print("\n  ⚠️ 常见错误：忘了 await")
    fetch("幽灵任务", 0.1)          # 会产生 RuntimeWarning: never awaited
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fetch("幽灵任务2", 0.1)
        import gc; gc.collect()
    if caught:
        print(f"  捕获到警告：{caught[0].message}")


# ============================================================
# 实验 1：await 不等于并发（反面教材 → 正确写法）
# ============================================================
async def serial_await() -> float:
    """
    反面教材：顺序 await，退化成串行。

    Returns:
        耗时秒数。
    """
    t = time.perf_counter()
    print(f"\n{'=' * 60}")
    print("实验 1a：顺序 await —— 这不是并发")
    print("=" * 60)
    print(f"{ts()}   ── 任务开始 ──")
    a = await fetch("A", 1.0)
    b = await fetch("B", 1.0)
    c = await fetch("C", 1.0)
    print(f"{ts()}   ── 全部完成 ──  {a} / {b} / {c}")
    return time.perf_counter() - t


async def concurrent_gather() -> float:
    """
    正确写法：先创建任务，再一起 await。

    Returns:
        耗时秒数。
    """
    t = time.perf_counter()
    print(f"\n{'=' * 60}")
    print("实验 1b：gather —— 真正的并发")
    print("=" * 60)
    print(f"{ts()}   ── 任务开始 ──")
    results = await asyncio.gather(
        fetch("A", 1.0),
        fetch("B", 1.0),
        fetch("C", 1.0),
    )
    print(f"{ts()}   ── 全部完成 ──  {' / '.join(results)}")
    return time.perf_counter() - t


async def demo_create_task() -> None:
    """演示 create_task 的"立刻排进调度队列"语义。"""
    print(f"\n{'=' * 60}")
    print("实验 2：create_task —— 创建即调度")
    print("=" * 60)

    print(f"{ts()}   创建 task1")
    task1 = asyncio.create_task(fetch("task1", 0.5))
    print(f"{ts()}   task1 已排入队列，主协程继续往下走")

    print(f"{ts()}   创建 task2")
    task2 = asyncio.create_task(fetch("task2", 0.8))

    print(f"{ts()}   主协程再 await 一会儿（模拟做别的事）")
    await asyncio.sleep(0.2)

    print(f"{ts()}   主协程准备等待结果")
    r1 = await task1
    r2 = await task2
    print(f"{ts()}   结果：{r1} / {r2}")


# ============================================================
# 实验 3：as_completed —— 谁先完成谁先处理
# ============================================================
async def demo_as_completed() -> None:
    """演示 as_completed：按完成顺序产出，适合"边完成边入库"。"""
    print(f"\n{'=' * 60}")
    print("实验 3：as_completed —— 完成顺序 ≠ 创建顺序")
    print("=" * 60)

    tasks = [
        asyncio.create_task(fetch("慢", 0.9)),
        asyncio.create_task(fetch("快", 0.2)),
        asyncio.create_task(fetch("中", 0.5)),
    ]

    order: list[str] = []
    for coro in asyncio.as_completed(tasks):
        result = await coro
        order.append(result.split()[0])
        print(f"{ts()}   处理到结果：{result}")

    print(f"\n   创建顺序：慢 → 快 → 中")
    print(f"   完成顺序：{' → '.join(order)}")
    print("   → gather 保证顺序、as_completed 保证低延迟。按需选择。")


# ============================================================
# 实验 4：阻塞代码 = 事件循环杀手
# ============================================================
async def demo_blocking_kills_loop() -> None:
    """演示同步阻塞调用如何卡死整个事件循环。"""

    async def heartbeat(stop_after: float = 2.0) -> None:
        """每秒打一次心跳，用于检测事件循环是否被卡住。"""
        elapsed = 0.0
        while elapsed < stop_after:
            await asyncio.sleep(0.5)
            elapsed += 0.5
            print(f"{ts()}   ♥ 心跳（事件循环正常）")

    async def bad_blocker() -> None:
        """错误示范：在协程里调用阻塞函数。"""
        await asyncio.sleep(0.3)
        print(f"{ts()}   ⚠️ 开始阻塞 2 秒（time.sleep）…")
        time.sleep(2.0)          # ← 这里整个事件循环都停转了
        print(f"{ts()}   ⚠️ 阻塞结束")

    async def good_blocker() -> None:
        """正确做法：用 to_thread 把阻塞调用丢到线程池。"""
        await asyncio.sleep(0.3)
        print(f"{ts()}   ✅ 开始把阻塞任务丢到线程（to_thread）…")
        await asyncio.to_thread(time.sleep, 2.0)
        print(f"{ts()}   ✅ 阻塞任务结束，循环全程未卡")

    print(f"\n{'=' * 60}")
    print("实验 4a：在协程里直接 time.sleep（错误）")
    print("=" * 60)
    await asyncio.gather(heartbeat(), bad_blocker())

    print(f"\n{'=' * 60}")
    print("实验 4b：用 asyncio.to_thread 包装（正确）")
    print("=" * 60)
    await asyncio.gather(heartbeat(), good_blocker())

    print("\n   对比可见：4a 中心跳停了约 2 秒（只剩 3 次），")
    print("   4b 中心跳正常跳完全程。这就是『一个阻塞调用拖垮整个爬虫』的原理。")


# ============================================================
# 实验 5：超时控制
# ============================================================
async def demo_timeout() -> None:
    """演示 asyncio.timeout 的用法（Python 3.11+）。"""
    print(f"\n{'=' * 60}")
    print("实验 5：超时控制")
    print("=" * 60)

    try:
        async with asyncio.timeout(0.5):
            await fetch("慢任务", 3.0)
    except TimeoutError:
        print(f"{ts()}   ⏱ 已超时，任务被取消")

    # 3.11 之前的写法（兼容性参考）
    try:
        await asyncio.wait_for(fetch("慢任务2", 3.0), timeout=0.5)
    except asyncio.TimeoutError:
        print(f"{ts()}   ⏱ wait_for 超时（旧写法）")


# ============================================================
# 主流程
# ============================================================
@dataclass
class Result:
    """汇总各实验的关键数据。"""

    serial: float = 0.0
    gather: float = 0.0

    @property
    def speedup(self) -> float:
        """并发相对串行的提速倍数。"""
        return self.serial / self.gather if self.gather else 0.0


async def main() -> Result:
    """按顺序运行全部实验。"""
    r = Result()

    await demo_coroutine_object()

    r.serial = await serial_await()
    r.gather = await concurrent_gather()

    await demo_create_task()
    await demo_as_completed()
    await demo_blocking_kills_loop()
    await demo_timeout()

    print(f"\n{'=' * 60}")
    print("总结")
    print("=" * 60)
    print(f"  顺序 await （3×1.0s）：{r.serial:5.2f} 秒")
    print(f"  gather    （3×1.0s）：{r.gather:5.2f} 秒")
    print(f"  提速                ：{r.speedup:5.2f}x")
    print("\n  核心心法（背下来）：")
    print("    · 协程函数调用 ≠ 执行，await 才是执行")
    print("    · await 的语义是『让出控制权』，不是『开启并发』")
    print("    · 要并发，必须先 create_task（或直接交给 gather）")
    print("    · 任何同步阻塞调用都必须 to_thread 包起来")
    return r


if __name__ == "__main__":
    asyncio.run(main())
