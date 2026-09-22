"""
阶段 3 · 3.1 四种并发模型对比实验
======================================================
对应网页章节：#s3-1

本脚本用同一批任务（20 次"模拟网络请求"，每次 sleep 0.5 秒）
分别跑四种模型，实测耗时差异，并验证 GIL 对 CPU 密集型任务的影响。

运行：python3 30_concurrency_models.py
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

# ============================================================
# 实验参数
# ============================================================
N_IO = 20          # IO 任务数量
IO_DELAY = 0.5     # 每次"网络请求"耗时（秒）
N_CPU = 4          # CPU 任务数量
CPU_ROUNDS = 900_000


# ============================================================
# 一、任务定义
# ============================================================
def io_task(n: int) -> int:
    """
    模拟一次网络请求：线程在 sleep 期间会释放 GIL，CPU 处于空闲。

    Args:
        n: 任务编号。

    Returns:
        任务编号本身（作为"结果"）。
    """
    time.sleep(IO_DELAY)
    return n


def cpu_task(n: int) -> int:
    """
    纯计算任务：一直占用 CPU，不会释放 GIL。

    Args:
        n: 任务编号。

    Returns:
        累加结果。
    """
    total = 0
    for i in range(CPU_ROUNDS):
        total += i * i
    return total


# ============================================================
# 二、四种执行方式
# ============================================================
def run_serial(tasks: list[int], fn: Callable[[int], int]) -> tuple[float, list[int]]:
    """串行执行：一个跑完再跑下一个。"""
    t0 = time.perf_counter()
    results = [fn(i) for i in tasks]
    return time.perf_counter() - t0, results


def run_threads(tasks: list[int], fn: Callable[[int], int], workers: int = 10) -> tuple[float, list[int]]:
    """
    多线程执行：适合 IO 密集型（阻塞时自动让出 GIL）。

    Args:
        tasks: 任务编号列表。
        fn: 任务函数。
        workers: 线程池大小。

    Returns:
        (耗时秒数, 结果列表)
    """
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # map 会按输入顺序返回结果，比 as_completed 更适合"要完整结果"的场景
        results = list(pool.map(fn, tasks))
    return time.perf_counter() - t0, results


def run_processes(tasks: list[int], fn: Callable[[int], int], workers: int = 4) -> tuple[float, list[int]]:
    """
    多进程执行：绕开 GIL，适合 CPU 密集型。

    注意：进程有启动开销（fork/spawn），任务太轻时反而更慢。

    Args:
        tasks: 任务编号列表。
        fn: 任务函数（必须可 pickle）。
        workers: 进程池大小。

    Returns:
        (耗时秒数, 结果列表)
    """
    t0 = time.perf_counter()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fn, tasks))
    return time.perf_counter() - t0, results


async def _async_io(n: int) -> int:
    """异步版 IO 任务：await 期间事件循环去处理别的协程。"""
    await asyncio.sleep(IO_DELAY)
    return n


def run_async(tasks: list[int]) -> tuple[float, list[int]]:
    """
    异步执行：单线程内靠事件循环切换，IO 等待完全重叠。

    Args:
        tasks: 任务编号列表。

    Returns:
        (耗时秒数, 结果列表)
    """

    async def main() -> list[int]:
        # gather 会并发调度所有协程，并保持返回顺序
        return await asyncio.gather(*(_async_io(i) for i in tasks))

    t0 = time.perf_counter()
    results = asyncio.run(main())
    return time.perf_counter() - t0, results


# ============================================================
# 三、GIL 实证：同一函数在两种任务下的表现
# ============================================================
@dataclass
class ThreadProbe:
    """记录多线程下主线程度过的循环次数，用于观察 GIL 抢占。"""

    counter: int = 0
    rounds: int = 6_000_000

    def spin(self, name: str, results: dict[str, int]) -> None:
        """纯计算自旋，不让出 GIL。"""
        c = 0
        for _ in range(self.rounds):
            c += 1
        results[name] = c


def gil_experiment() -> None:
    """对比"两个纯计算线程并行"与"串行"的耗时。"""
    probe = ThreadProbe()
    results: dict[str, int] = {}

    t0 = time.perf_counter()
    probe.spin("a", results)
    probe.spin("b", results)
    serial = time.perf_counter() - t0

    t0 = time.perf_counter()
    t1 = threading.Thread(target=probe.spin, args=("a", results))
    t2 = threading.Thread(target=probe.spin, args=("b", results))
    t1.start(); t2.start(); t1.join(); t2.join()
    threaded = time.perf_counter() - t0

    print("\n【GIL 实证】两个纯计算任务（各 600 万次循环）")
    print(f"  串行执行      : {serial:6.3f} 秒")
    print(f"  双线程执行    : {threaded:6.3f} 秒")
    ratio = threaded / serial
    print(f"  比值          : {ratio:6.2f}x  →  {'几乎无加速，GIL 生效' if ratio > 0.85 else '有加速？意外'}")


# ============================================================
# 四、主流程
# ============================================================
@dataclass
class BenchRow:
    """一行跑分结果。"""

    name: str
    elapsed: float
    speedup: float = field(default=0.0)


def bench_io() -> list[BenchRow]:
    """跑分：IO 密集型（模拟网络请求）。"""
    tasks = list(range(N_IO))
    rows: list[BenchRow] = []

    print(f"【实验 1】IO 密集型：{N_IO} 次请求，每次 {IO_DELAY} 秒")
    print("-" * 58)

    serial_t, _ = run_serial(tasks, io_task)
    rows.append(BenchRow("串行", serial_t, 1.0))
    print(f"  串行            : {serial_t:6.2f} 秒   (基准)")

    for w in (5, 20):
        t, _ = run_threads(tasks, io_task, workers=w)
        rows.append(BenchRow(f"多线程({w})", t, serial_t / t))
        print(f"  多线程({w:>2} 线程) : {t:6.2f} 秒   {serial_t / t:5.2f}x")

    t, _ = run_async(tasks)
    rows.append(BenchRow("异步", t, serial_t / t))
    print(f"  异步 asyncio    : {t:6.2f} 秒   {serial_t / t:5.2f}x")

    print(f"\n  理论极限：{IO_DELAY:.1f} 秒（所有等待完全重叠）")
    return rows


def bench_cpu() -> list[BenchRow]:
    """跑分：CPU 密集型。"""
    tasks = list(range(N_CPU))
    rows: list[BenchRow] = []

    print(f"\n【实验 2】CPU 密集型：{N_CPU} 个任务，每个 {CPU_ROUNDS:,} 次循环")
    print("-" * 58)

    serial_t, _ = run_serial(tasks, cpu_task)
    rows.append(BenchRow("串行", serial_t, 1.0))
    print(f"  串行            : {serial_t:6.2f} 秒   (基准)")

    t, _ = run_threads(tasks, cpu_task, workers=4)
    rows.append(BenchRow("多线程(4)", t, serial_t / t))
    print(f"  多线程(4 线程)  : {t:6.2f} 秒   {serial_t / t:5.2f}x   ← GIL 拖累")

    t, _ = run_processes(tasks, cpu_task, workers=4)
    rows.append(BenchRow("多进程(4)", t, serial_t / t))
    print(f"  多进程(4 进程)  : {t:6.2f} 秒   {serial_t / t:5.2f}x   ← 真并行")

    return rows


def main() -> None:
    """运行全部实验。"""
    import os

    print("=" * 58)
    print("4.1 四种并发模型对比实验")
    print(f"CPU 核心数：{os.cpu_count()}")
    print("=" * 58)

    io_rows = bench_io()
    cpu_rows = bench_cpu()
    gil_experiment()

    print("\n" + "=" * 58)
    print("结论")
    print("=" * 58)
    print("  1. IO 密集 → 异步最快（无线程切换开销），多线程次之，串行最慢")
    print("  2. CPU 密集 → 多进程唯一有效，多线程因 GIL 几乎无加速甚至更慢")
    print("  3. 爬虫属于典型 IO 密集型 → 优先选 asyncio")
    print("\n  IO 跑分速查：")
    for r in io_rows:
        bar = "█" * max(1, int(r.speedup * 4))
        print(f"    {r.name:<12} {r.elapsed:6.2f}s  {r.speedup:5.2f}x  {bar}")
    print("\n  CPU 跑分速查：")
    for r in cpu_rows:
        bar = "█" * max(1, int(r.speedup * 4))
        print(f"    {r.name:<12} {r.elapsed:6.2f}s  {r.speedup:5.2f}x  {bar}")


if __name__ == "__main__":
    main()
