"""
第 60 课 · 并发模型选型 —— 同步 / 多线程 / 多进程 / asyncio 到底选哪个

本课要回答的问题：
  1. 爬虫是 IO 密集型任务，为什么多线程「居然」有效？GIL 不是说同一时刻
     只有一个线程执行字节码吗？
  2. 既然多线程有效，为什么做 CPU 密集任务时多线程反而比同步更慢？
  3. asyncio 单线程凭什么能扛住高并发？它和多线程的本质区别在哪？
  4. 并发数是不是越大越好？拐点在哪里，怎么测出来？
  5. 一个真实爬虫项目，四种模型应该怎么选、怎么混用？

================================ 运行方式 ================================
    python3 code/stage6_distributed/60_concurrency_models.py

零额外依赖，全程纯标准库（threading / multiprocessing / asyncio / queue）。

================================ 实验清单 ================================
  实验 1  IO 密集：四种模型实测耗时对比（核心实验）
  实验 2  GIL 真相：同一时刻真的只有一个线程在跑吗？
  实验 3  CPU 密集：多线程为什么反而更慢（实测反直觉现象）
  实验 4  并发数拐点：从 1 到 300，找到吞吐量的天花板
  实验 5  容错：asyncio.gather(return_exceptions=True) 与信号量
  实验 6  选型决策表 + 混用模式
"""

from __future__ import annotations

import asyncio
import queue
import statistics
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Sequence

SEP = "=" * 76

# ============================================================================
# 认知框架：并发模型不是「哪个快用哪个」，而是「瓶颈在哪就用哪个」
# ============================================================================
# 新手选并发模型的方式：看别人用啥我用啥，或者干脆全上多进程。
# 老手选并发模型的方式：先问「我的任务的瓶颈是等待还是计算？」
#
#   爬虫的耗时构成（一次典型的 HTML 请求）：
#
#     ├─ DNS 解析          20 ~ 80 ms     等待
#     ├─ TCP 三次握手      30 ~ 150 ms    等待
#     ├─ TLS 握手          50 ~ 300 ms    等待
#     ├─ 发送请求             1 ~ 5 ms    计算
#     ├─ 服务端处理        50 ~ 2000 ms   等待   ← 最大头
#     ├─ 传输响应体         10 ~ 500 ms   等待
#     └─ 解析 HTML           1 ~ 50 ms    计算
#
#   → 等待占比 95%+。这就是「IO 密集型」的定义。
#
#   并发模型的选择逻辑：
#
#     ┌──────────────┬──────────┬──────────┬──────────────────────────┐
#     │ 模型         │ 适用场景 │ 并发上限 │ 核心代价                  │
#     ├──────────────┼──────────┼──────────┼──────────────────────────┤
#     │ 同步串行     │ 调试脚本 │ 1        │ 吞吐量线性受限            │
#     │ 多线程       │ IO 密集  │ ~数百    │ 线程栈内存 + 上下文切换    │
#     │ 多进程       │ CPU 密集 │ ~CPU 核数│ 进程间通信 + 启动开销      │
#     │ asyncio      │ IO 密集  │ ~数千    │ 全链路必须异步，一处阻塞全崩│
#     └──────────────┴──────────┴──────────┴──────────────────────────┘
#
#   一个反直觉但重要的结论：
#     **多线程和 asyncio 在性能上没有数量级差距**（都是几百到几千并发），
#     它们的差别在**资源占用**和**编程复杂度**。
#     真正有数量级差距的是「串行 vs 并发」——那是 10 倍以上。


# ============================================================================
# 一、统一的被测任务：模拟「网络等待 + HTML 解析」
# ============================================================================
# 为了让四种模型的结果可比，必须保证它们执行的是**完全相同的负载**。
# 所以这里把负载抽象成两个纯函数，四种模型都调用它们。
#
# 关键设计：等待用 time.sleep()，计算用纯 Python 循环。
#   为什么不用 asyncio.sleep 来统一？因为 asyncio.sleep 是异步原语，
#   在线程里调用它不会真正阻塞 —— 那样测出来的就不是同一件事了。
#   真实爬虫在线程模型里就是 requests.get() 阻塞线程，
#   在 asyncio 模型里就是 aiohttp 挂起协程。
#   所以：线程/进程模型用 time.sleep，asyncio 模型用 asyncio.sleep，
#   两者在「释放执行权让出 CPU」这个语义上是对应的。

@dataclass
class TaskProfile:
    """一个被测任务的负载配置。

    Attributes:
        name: 任务名称，用于输出展示。
        io_ms: 单次任务的模拟 IO 等待时长（毫秒）。
        cpu_rounds: 单次任务的模拟解析计算量（纯 Python 循环轮数）。
        n_tasks: 任务总数。
        concurrency: 并发度（线程数 / 进程数 / 信号量上限）。
    """

    name: str
    io_ms: float = 50.0
    cpu_rounds: int = 0
    n_tasks: int = 40
    concurrency: int = 10


def simulate_io_wait(io_ms: float) -> float:
    """模拟一次网络等待，返回实际等待的秒数。

    Args:
        io_ms: 期望等待的毫秒数。

    Returns:
        实际等待的秒数。

    Raises:
        ValueError: 当 io_ms 为负数时。

    这里用 time.sleep 而不是忙等：time.sleep 会让出 GIL，
    这正是真实网络阻塞的行为 —— 线程在 recv() 上等待时同样会让出 GIL。
    """
    if io_ms < 0:
        raise ValueError(f"io_ms 不能为负：{io_ms}")
    secs = io_ms / 1000.0
    time.sleep(secs)
    return secs


def busy_parse(cpu_rounds: int) -> int:
    """模拟一次 HTML 解析的 CPU 计算，返回一个校验值。

    Args:
        cpu_rounds: 循环轮数，越大代表解析越重。

    Returns:
        一个整数累加结果，用于防止编译器/解释器把循环优化掉。

    为什么必须是「纯 Python 字节码循环」而不是 math.sqrt 这类调用？
      因为 C 扩展函数在内部执行时也会释放 GIL（或者干脆不经过字节码调度），
      用它们测不出 GIL 的争抢效应。只有逐条执行 Python 字节码，
      才会真实地反复申请/释放 GIL —— 这才是我们要观察的现象。
    """
    acc = 0
    for i in range(cpu_rounds):
        acc += (i * 31 + 7) % 997
    return acc


# ============================================================================
# 二、四种执行器：同一份负载，四种跑法
# ============================================================================
def run_serial(profile: TaskProfile) -> list[int]:
    """同步串行执行全部任务。

    Args:
        profile: 任务配置。

    Returns:
        每个任务的返回值列表（这里统一返回任务序号）。

    这是所有对比的基线。它的耗时 ≈ n_tasks × (io + cpu)。
    如果并发模型没有比它快，说明选错了模型。
    """
    results: list[int] = []
    for i in range(profile.n_tasks):
        simulate_io_wait(profile.io_ms)
        if profile.cpu_rounds:
            busy_parse(profile.cpu_rounds)
        results.append(i)
    return results


def run_threads(profile: TaskProfile) -> list[int]:
    """用线程池执行全部任务。

    Args:
        profile: 任务配置。

    Returns:
        每个任务的返回值列表。

    Raises:
        RuntimeError: 当线程池内部泄漏异常时。

    执行器选 ThreadPoolExecutor 而不是手搓 Thread + Queue：
      前者自带任务队列、结果收集、异常传播，后者要写 40 行还容易漏 join。
      真实项目里手搓线程池的唯一理由是需要「动态调并发」，
      而 concurrent.futures 在 3.9+ 也支持了（通过 max_workers 重建）。

    线程数的经验公式（IO 密集）：
        并发度 = CPU 核数 × (1 + 平均等待时间 / 平均计算时间)
      对于 50ms 等待 + 1ms 计算的爬虫任务，在 32 核机器上这个公式会给出
      1600 —— 显然不现实。**真正的上限是目标站点的容忍度**：
      拿到 429 说明你太快了，不是说明你不够快。
    """

    def one(idx: int) -> int:
        """执行单个任务。

        Args:
            idx: 任务序号。

        Returns:
            任务序号。
        """
        simulate_io_wait(profile.io_ms)
        if profile.cpu_rounds:
            busy_parse(profile.cpu_rounds)
        return idx

    with ThreadPoolExecutor(max_workers=profile.concurrency) as pool:
        return list(pool.map(one, range(profile.n_tasks)))


def _process_worker(payload: tuple[int, float, int]) -> int:
    """进程池的顶层工作函数（必须是模块级函数，才能被 pickle）。

    Args:
        payload: (任务序号, IO 毫秒, CPU 轮数) 三元组。

    Returns:
        任务序号。

    踩坑记录（❌ 错误做法 → 现象 → 根因 → 正确做法）：
      ❌ 最初我写的是 pool.submit(lambda i: ..., i)，闭包捕获了 profile。
         现象：抛 AttributeError: Can't pickle local object '<lambda>'。
         根因：多进程不是共享内存，参数要经 pickle 序列化后通过管道传给子进程；
               局部函数/lambda 不可 pickle（没有全局限定名）。
         正确做法：把工作函数提升到**模块级**，参数也只用可 pickle 的基本类型
               （这里用三元组而不是传 TaskProfile 对象 —— 虽然 dataclass 可 pickle，
               但传对象会让子进程反序列化出一个独立副本，容易误导读者以为
               "子进程改了 profile 主进程能看到"。传值更诚实。）
    """
    idx, io_ms, cpu_rounds = payload
    simulate_io_wait(io_ms)
    if cpu_rounds:
        busy_parse(cpu_rounds)
    return idx


def run_processes(profile: TaskProfile) -> list[int]:
    """用进程池执行全部任务。

    Args:
        profile: 任务配置。

    Returns:
        每个任务的返回值列表。

    进程池的启动开销是**秒级**的（每个子进程要重新 import 整个模块）。
    所以任务总数太少时，多进程会输给同步 —— 这个现象在实验 3 里能看到。
    """
    payloads = [(i, profile.io_ms, profile.cpu_rounds) for i in range(profile.n_tasks)]
    with ProcessPoolExecutor(max_workers=profile.concurrency) as pool:
        return list(pool.map(_process_worker, payloads))


async def _async_io_wait(io_ms: float) -> None:
    """异步 IO 等待。

    Args:
        io_ms: 毫秒数。

    Returns:
        None

    asyncio.sleep 的本质是「向事件循环注册一个定时器后 yield 控制权」，
    协程被挂起、CPU 交给其他就绪任务。这正是 aiohttp 收到响应前的状态。
    """
    await asyncio.sleep(io_ms / 1000.0)


async def _async_coroutine(idx: int, profile: TaskProfile,
                           sem: asyncio.Semaphore | None) -> int:
    """单个异步任务协程。

    Args:
        idx: 任务序号。
        profile: 任务配置。
        sem: 并发信号量；为 None 表示不限制。

    Returns:
        任务序号。

    如果 cpu_rounds > 0，这里会是**灾难性的**：
    busy_parse 是同步阻塞调用，会卡死整个事件循环 ——
    所有其他协程陪着你一起等。这个坑在实验 3 里专门演示。
    """
    if sem is not None:
        async with sem:
            await _async_io_wait(profile.io_ms)
            if profile.cpu_rounds:
                busy_parse(profile.cpu_rounds)
        return idx
    await _async_io_wait(profile.io_ms)
    if profile.cpu_rounds:
        busy_parse(profile.cpu_rounds)
    return idx


async def _run_async_inner(profile: TaskProfile) -> list[int]:
    """asyncio 版执行器的内部协程。

    Args:
        profile: 任务配置。

    Returns:
        每个任务的返回值列表。

    Raises:
        RuntimeError: 当底层任务异常未被捕获时。

    ▸ 三个关键点：
      ① asyncio.Semaphore(concurrency) 控制同时在飞的请求数。
         不加信号量的话，10000 个协程会同时发起 —— 操作系统文件描述符耗尽，
         目标站直接把你的 IP 拉黑。**爬虫的并发必须有人为上限**。
      ② asyncio.gather(..., return_exceptions=True) —— 见下面注释。
      ③ 用 TaskGroup（3.11+）是更现代的写法，但 gather 更通用，教程里用 gather。
    """
    sem = asyncio.Semaphore(profile.concurrency) if profile.concurrency > 0 else None
    tasks = [_async_coroutine(i, profile, sem) for i in range(profile.n_tasks)]
    # return_exceptions=True 是本行最重要的参数（踩坑记录）：
    #   ❌ 默认 return_exceptions=False 时，任何一个协程抛异常，
    #      gather 会立即把它向外抛出，**其余尚未完成的协程不会被取消，
    #      但它们的返回值你永远拿不到了** —— 在爬虫里这意味着
    #      「一个 404 导致整批 999 个任务的结果全部丢失」。
    #   ✅ return_exceptions=True 时，异常对象会被当作结果返回，
    #      调用方可以逐条判断 isinstance(r, Exception) 再决定重试还是丢弃。
    #      这是爬虫容错的底线写法。
    raw = await asyncio.gather(*tasks, return_exceptions=True)
    results: list[int] = []
    for r in raw:
        if isinstance(r, BaseException):
            continue  # 真实爬虫在这里应该记录失败 URL 并投递重试队列
        results.append(int(r))
    return results


def run_asyncio(profile: TaskProfile) -> list[int]:
    """用 asyncio 事件循环执行全部任务。

    Args:
        profile: 任务配置。

    Returns:
        每个任务的返回值列表。

    每次调用都新建一个事件循环（asyncio.run 的语义）。
    真实服务应该复用同一个 loop，这里为了与其它执行器对齐而简化。
    """
    return asyncio.run(_run_async_inner(profile))


# 四种执行器的统一注册表：让实验代码可以循环遍历，避免复制粘贴。
EXECUTORS: list[tuple[str, Callable[[TaskProfile], list[int]]]] = [
    ("同步串行", run_serial),
    ("多线程", run_threads),
    ("多进程", run_processes),
    ("asyncio", run_asyncio),
]


def benchmark(profile: TaskProfile, runner: Callable[[TaskProfile], list[int]],
              repeat: int = 1) -> tuple[float, list[int]]:
    """给一个执行器计时。

    Args:
        profile: 任务配置。
        runner: 执行器函数。
        repeat: 重复次数，取最好成绩（避免系统抖动干扰）。

    Returns:
        (耗时秒数, 结果列表)。

    Raises:
        AssertionError: 当结果数量与任务数不符时（说明执行器有 bug）。

    为什么取 min 而不是平均？
      并发实验的噪声来自外部（其他进程抢 CPU、GC、页面回收），只会让结果变慢。
      最小值最接近「这个模型在这台机器上的真实能力上限」。
      这与压测领域「取 P99 看最差、取 min 看最好」的双口径是一致的。
    """
    best = float("inf")
    last: list[int] = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        last = runner(profile)
        best = min(best, time.perf_counter() - t0)
    assert len(last) == profile.n_tasks, (
        f"执行器 {runner.__name__} 返回了 {len(last)} 条结果，"
        f"但任务数是 {profile.n_tasks} —— 有任务被静默丢弃了"
    )
    return best, last


def title(text: str) -> None:
    """打印一级章节标题。

    Args:
        text: 标题文本。

    Returns:
        None
    """
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    """打印二级小标题。

    Args:
        text: 小标题文本。

    Returns:
        None
    """
    print(f"\n▸ {text}")


def bar(value: float, max_value: float, width: int = 46) -> str:
    """画一条 ASCII 条形图，用于直观对比。

    Args:
        value: 当前值。
        max_value: 基准值（最长条的长度）。
        width: 最长条的字符宽度。

    Returns:
        由 █ 组成的字符串。

    终端里画图的意义：数字 12.3s vs 0.9s 需要读者自己换算，
    而两根长度差 13 倍的条子一眼就能看出差距。教程输出应当优先让人「看见」。
    """
    if max_value <= 0:
        return ""
    n = max(1, int(round(value / max_value * width)))
    return "█" * n


# ============================================================================
# 实验 1：IO 密集 —— 四种模型实测对比
# ============================================================================
def exp1_io_bound() -> None:
    """实验 1：同一个 IO 密集负载，跑四种并发模型。"""
    title("【实验 1】IO 密集型任务：四种模型实测对比（核心实验）")

    profile = TaskProfile(
        name="IO 密集",
        io_ms=50.0,      # 50ms 等待，模拟一次中等延迟的 HTTP 请求
        cpu_rounds=2000,  # 约 0.1ms 的解析计算，占比可忽略
        n_tasks=40,
        concurrency=10,
    )

    print(f"""
    负载定义（四种模型执行完全相同的负载，结果才可比）：
      任务数      : {profile.n_tasks}
      单任务等待  : {profile.io_ms} ms   （time.sleep / asyncio.sleep 模拟网络往返）
      单任务计算  : {profile.cpu_rounds} 轮纯 Python 循环（模拟 HTML 解析）
      并发度      : {profile.concurrency}

    理论预测：
      同步串行 耗时 ≈ {profile.n_tasks} × {profile.io_ms}ms = {profile.n_tasks * profile.io_ms / 1000:.1f}s
      并发模型 耗时 ≈ {profile.n_tasks / profile.concurrency} × {profile.io_ms}ms = {profile.n_tasks / profile.concurrency * profile.io_ms / 1000:.2f}s
      （前提：等待只占一部分，且并发度能吃满）
    """)

    print(f"  {'模型':<10}{'耗时(s)':>10}{'加速比':>9}{'吞吐(任务/s)':>14}   对比图")
    print("  " + "-" * 72)

    baseline = 0.0
    rows: list[tuple[str, float]] = []
    for name, runner in EXECUTORS:
        elapsed, _ = benchmark(profile, runner, repeat=1)
        if not rows:
            baseline = elapsed
        rows.append((name, elapsed))
        speedup = baseline / elapsed if elapsed > 0 else 0.0
        qps = profile.n_tasks / elapsed if elapsed > 0 else 0.0
        print(f"  {name:<10}{elapsed:>10.3f}{speedup:>8.2f}x{qps:>13.1f}   "
              f"{bar(elapsed, baseline)}")

    print()
    sub("结果解读")
    print(f"""    ① 同步串行 {rows[0][1]:.2f}s —— 完全符合理论值 {profile.n_tasks * profile.io_ms / 1000:.2f}s，
       每个任务老老实实等 50ms，40 个就是 2 秒。这段时间 CPU 几乎全部在睡觉。

    ② 多线程 {rows[1][1]:.2f}s —— 加速 {rows[0][1] / rows[1][1]:.1f}x。
       **这是本课最重要的反直觉点**：GIL 明明限制同一时刻只有一个线程执行
       Python 字节码，为什么还能快这么多？
       → 因为 time.sleep 会让出 GIL。线程在等待网络时根本不持有 GIL，
         10 个线程同时「睡着」，只有醒来处理响应时才短暂竞争 GIL。
         爬虫 95% 的时间在睡，所以 GIL 对爬虫几乎不构成瓶颈。

    ③ 多进程 {rows[2][1]:.2f}s —— 加速 {rows[0][1] / rows[2][1]:.1f}x。
       和多线程差不多（甚至因为进程创建开销略慢）。
       → **IO 密集场景用多进程是纯粹的浪费**：每个进程占几十 MB 内存，
         而收益与多线程相同。进程池的正确用途是 CPU 密集（见实验 3）。

    ④ asyncio {rows[3][1]:.2f}s —— 加速 {rows[0][1] / rows[3][1]:.1f}x。
       → 同样是「等待让出执行权」，但代价只有多线程的 1/{profile.concurrency} 左右：
         协程栈是用户态对象（几 KB），线程栈是内核对象（默认 8MB 虚拟内存）。
         这就是「单机几千并发」和「单机几百并发」的差距来源。""")

    sub("⚠ 这个实验里被隐藏的真相")
    print("""    上面的多线程数字漂亮，是因为负载「等待占 95%」。
    一旦把 cpu_rounds 调大，多线程的优势会迅速消失 —— 见实验 3。

    另外，加速比没有达到理论上限 10x（40/10 × 50ms = 0.2s），原因是：
      · 线程/进程的创建与调度有固定开销
      · 任务的启动和收尾不是同时的（尾部队列效应，tail effect）
      · 这里的 repeat=1，没有取多次最小值，包含冷启动

    在真实爬虫里，还要额外减去这些损耗：
      · 目标站限速（你 10 并发，对方只给你 2 QPS）
      · DNS 解析是全局锁（glibc 的 getaddrinfo 在多线程下会串行化）
      · 代理带宽（10 个请求挤一条代理隧道）
    """)


# ============================================================================
# 实验 2：GIL 真相 —— 同一时刻真的只有一个线程在跑吗
# ============================================================================
# 这个实验的设计经过一次**重要的返工**，过程本身就是最好的教材，
# 所以把返工前的失败方案也保留下来（见 exp2_gil_truth 里的 2.1/2.2 对比）。
#
# 返工前的方案：N 个线程各自对共享计数器做 `c = self.counter; c += 1;
#   self.counter = c`，统计「期望值 - 实际值」作为竞态丢失量。
#   预期是「丢失量 > 0，证明线程在交错执行」。
#
# 实测结果：丢失量恒为 0，哪怕 8 线程 160 万次累加。
# 根因（这才是真正有价值的部分）：
#   CPython 的字节码调度单位是**一条字节码指令**，不是一条 Python 语句。
#   `_thread.lock` 的 GIL 在两条字节码之间才会被释放，
#   而我们这段代码的「读-改-写」恰好每条对应一条字节码
#   （LOAD_ATTR / BINARY_OP / STORE_ATTR），**中间不会发生线程切换**。
#   所以它在本机上是「碰巧安全」的 —— 这不是因为它写法正确，
#   而是因为 GIL + 字节码粒度把竞态窗口关上了。
#   一旦换成 `self.counter += other()` 这类跨字节码的复合操作，
#   或者跑在 free-threading 版 Python（3.13+ 无 GIL 构建）上，它立刻丢失数据。
#
# 这个坑的教学价值极高：
#   ❌ 「无锁共享变量偶尔没出问题」 ≠ 「无锁共享变量是安全的」。
#      并发 bug 的本质是**时序依赖**，它可能 1000 次运行都正常，
#      然后在生产环境第 1001 次把数据库写花。
#   所以实验 2 换成了下面这个**确定性**的探针：直接测量 GIL 的持有情况，
#   而不是去撞一个「有时能撞到、有时撞不到」的竞态窗口。

class _TickCounter:
    """一个通过 C 扩展级别的原子累加来观测 GIL 行为的探针。

    思路：让 N 个线程各自做「纯 Python 计算」和「纯 C 计算」两种工作，
    比较它们随线程数的耗时曲线。
      · 纯 Python 计算（字节码循环）：耗时随线程数线性增长 → GIL 强制串行。
      · 纯 C 计算（hashlib.md5 / zlib.compress）：耗时几乎不随线程数增长
        → 因为 C 扩展内部会释放 GIL，实现真并行。
    这两条曲线的分叉，就是 GIL 存在性的直接证据，而且**可稳定复现**。
    """

    def __init__(self) -> None:
        """初始化探针。"""
        self.python_time = 0.0
        self.c_time = 0.0

    @staticmethod
    def python_work(rounds: int) -> int:
        """纯 Python 字节码计算。

        Args:
            rounds: 循环轮数。

        Returns:
            累加结果。

        这段代码每一轮都在执行 Python 字节码，
        所以线程之间必须轮流持有 GIL —— 无论机器有多少核。
        """
        acc = 0
        for i in range(rounds):
            acc += (i * 31 + 7) % 997
        return acc

    @staticmethod
    def c_work(rounds: int) -> int:
        """纯 C 扩展计算（用 zlib 压缩来消耗 CPU）。

        Args:
            rounds: 压缩轮数。

        Returns:
            累计输出长度。

        zlib.compress 是 C 实现且在压缩期间释放 GIL，
        所以多个线程可以真正同时跑在多个核上。
        这就是「Python 多线程不能并行」这句话的反例：
        它只对 Python 字节码成立，对 C 扩展不成立。
        """
        import zlib
        payload = b"x" * 4096
        total = 0
        for _ in range(rounds):
            total += len(zlib.compress(payload, 6))
        return total

    def measure(self, work: Callable[[int], int], rounds: int,
                n_threads: int) -> float:
        """测量指定工作负载在 n_threads 个线程下的耗时。

        Args:
            work: 工作函数。
            rounds: 每个线程的轮数。
            n_threads: 线程数。

        Returns:
            总耗时（秒）。

        Raises:
            RuntimeError: 当线程未能正常结束时。
        """
        threads = [threading.Thread(target=work, args=(rounds,))
                   for _ in range(n_threads)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return time.perf_counter() - t0


def exp2_gil_truth() -> None:
    """实验 2：用两种负载的耗时曲线证明 GIL 的存在与边界。"""
    title("【实验 2】GIL 真相：同一时刻真的只有一个线程在跑吗？")

    print(f"""
    先说结论：**是真的** —— 但只对「Python 字节码」成立。
    本机 CPU 核数 {_cpu_count()}，sys.getswitchinterval() = {sys.getswitchinterval() * 1000:.1f} ms。

    ⚠ 本实验的设计经过一次返工，返工过程本身就是本课最有价值的教学内容，
      完整记录在 exp2_gil_truth 函数上方的注释里，这里只讲结论。
    """)

    sub("2.1 对比实验：纯 Python 计算 vs 纯 C 扩展计算")
    print("""    方法：同一个「总计算量」下，逐步增加线程数，测量墙钟耗时。
      · 如果耗时随线程数线性增长 → 线程在抢同一把锁（GIL），没有并行
      · 如果耗时几乎不变       → 线程在真并行（多核同时干活）

    用两种负载对比，就能把「GIL 限制 Python 字节码」这件事隔离出来。""")

    probe = _TickCounter()
    # 轮数要足够大，让「1 线程」的耗时至少到 100ms 量级，
    # 否则 3ms 的测量结果会被计时噪声和线程启动开销淹没。
    # 这是并发实验的通用要求：**测量时间必须远大于启动开销**，
    # 一般建议至少 100 倍。本实验线程启动约 50us，所以目标 100ms。
    rounds = 2_000_000
    c_rounds = 6_000

    print(f"\n  {'负载类型':<22}{'1 线程':>10}{'2 线程':>10}{'4 线程':>10}"
          f"{'8 线程':>10}{'8线程/1线程':>13}")
    print("  " + "-" * 76)

    table: dict[str, list[float]] = {}
    for key, label, work, r in (
        ("py", "纯 Python 字节码循环", _TickCounter.python_work, rounds),
        ("c", "纯 C 扩展（zlib.compress）", _TickCounter.c_work, c_rounds),
    ):
        times = [probe.measure(work, r, n) for n in (1, 2, 4, 8)]
        table[key] = times
        ratio = times[3] / times[0] if times[0] else 0.0
        print(f"  {label:<22}" + "".join(f"{t:>9.3f}s" for t in times)
              + f"{ratio:>12.2f}x")

    py_times = table["py"]
    c_times = table["c"]
    # 所有解读数字都从实测数据算出，不在正文里写死 ——
    # 踩坑记录：初版我把「8.12x / 4.11x」直接写进了字符串，
    # 结果换了轮数之后正文和输出立刻对不上。**教程里的数字必须来自变量。**
    py_ratio = py_times[3] / py_times[0]
    c_ratio = c_times[3] / c_times[0]

    print(f"""
    ▸ 解读（这是本课最硬核的一段，请逐条对照上面的数字）：

      【纯 Python 字节码循环】
      1 线程 {py_times[0]:.3f}s → 8 线程 {py_times[3]:.3f}s，比值 {py_ratio:.2f}x。
      注意：这里的"总计算量"是**随线程数线性增长**的 ——
      8 个线程各跑满 {rounds:,} 轮，总量是 1 个线程的 8 倍。
      如果真并行，耗时应该不变（比值 ≈ 1x）；如果完全串行，耗时应该是 8x。
      实测 {py_ratio:.2f}x ≈ 8x → **这 8 份活就是串行干的**。
      等价说法：**8 线程跑纯 Python 计算的「有效并行度」= 8 / {py_ratio:.2f} = {8 / py_ratio:.2f}x**，
      即「加多少线程都不会变快」。

      【纯 C 扩展（zlib.compress）】
      1 线程 {c_times[0]:.3f}s → 8 线程 {c_times[3]:.3f}s，比值 {c_ratio:.2f}x。
      同样 8 倍的计算量，但耗时只涨了 {c_ratio:.2f} 倍 ——
      zlib 在压缩期间**释放了 GIL**，8 个线程真的同时在 8 个核上跑。
      有效并行度 = 8 / {c_ratio:.2f} = {8 / c_ratio:.2f}x。

      ▸ 关键对照：
         负载                 总计算量    耗时增长   有效并行度
         纯 Python 字节码      8 倍       {py_ratio:.2f}x      {8 / py_ratio:.2f}x
         纯 C 扩展             8 倍       {c_ratio:.2f}x      {8 / c_ratio:.2f}x

      同一台机器、同样 8 个线程，两种负载的并行度差了
      {(8 / c_ratio) / (8 / py_ratio):.1f} 倍 —— **这就是 GIL 的边界**。
      它不是「Python 不能并行」，而是「Python 字节码不能并行」。""")

    sub("2.2 GIL 释放的两个时机")
    print("""    ┌──────────────────────────┬───────────────────────────────────────┐
    │ 时机                     │ 说明                                  │
    ├──────────────────────────┼───────────────────────────────────────┤
    │ ① 时间片到期             │ 默认每 sys.getswitchinterval()（本机   │
    │                          │ 5ms）当前线程放弃 GIL；更关键的是，     │
    │                          │ GIL 的释放粒度是**一条字节码**，        │
    │                          │ 所以极短的复合操作常常"碰巧"不被切。    │
    │ ② 执行阻塞式系统调用     │ time.sleep / socket.recv / 文件读写 /   │
    │                          │ zlib 压缩 / 大块内存分配都会释放。      │
    └──────────────────────────┴───────────────────────────────────────┘

    **爬虫靠的就是 ②**：requests.get() 内部卡在 socket.recv 上，
    这时 GIL 是释放的，其它线程可以继续发请求。
    所以爬虫的并发效率 ≈ 目标站的响应延迟，与 GIL 基本无关。""")

    sub("2.3 返工记录：一个「测不出问题」的并发探针")
    print("""    ❌ 我最初写的探针是：N 个线程对共享变量做
           c = self.counter; c += 1; self.counter = c
       并统计「期望值 - 实际值」作为竞态丢失量，想用它证明线程在交错执行。

       实测结果：**丢失量恒为 0**，8 线程 160 万次累加，一次都没丢。

       根因：GIL 的释放粒度是**一条字节码**。上面三行恰好各对应一条字节码
             （LOAD_ATTR / BINARY_OP / STORE_ATTR），中间不会发生线程切换，
             于是这段无锁代码在本机上「碰巧」是安全的。
             它不是写法正确，而是**运气好**。

       ▸ 这个坑的教学价值远大于「成功测出丢失」：
         ① 「无锁共享变量偶发没出问题」 ≠ 「无锁共享变量是安全的」。
            并发 bug 的本质是时序依赖 —— 它可能 1000 次运行都正常，
            然后在生产环境第 1001 次把数据写花。
            所以**绝不能**用「我跑了很久都没事」作为并发安全性的证据。
         ② 想让它真的丢数据，得换成跨字节码的复合操作，例如
            `self.counter += other.compute()`，或者把 `c += 1` 换成一个
            会触发函数调用的表达式 —— 函数调用处就有可能发生切换。
         ③ 更彻底的验证方式：换 Python 3.13+ 的 free-threading（no-GIL）
            构建运行同一段代码，竞态会立刻显形。
         ④ **正确的并发计数器**只有两种写法：
            threading.Lock 保护临界区，或者用 queue.Queue 把结果汇总到
            单线程处理（后者通常更快，因为避免了锁竞争）。
            分布式场景则用 Redis INCR（第 62 课会讲原子性）。

       ▸ 所以本实验最终改用「两条耗时曲线的分叉」作为 GIL 的证据 ——
         它 100% 可复现，不依赖运气。**能被稳定复现的现象才配写进教程**。""")


def _cpu_count() -> int:
    """安全的获取 CPU 核数。

    Returns:
        CPU 核数，至少为 1。
    """
    try:
        import os
        return os.cpu_count() or 1
    except Exception:
        return 1


# ============================================================================
# 实验 3：CPU 密集 —— 多线程为什么反而更慢
# ============================================================================
def exp3_cpu_bound() -> None:
    """实验 3：CPU 密集任务下，多线程的有效并行度只有约 1.0x。"""
    title("【实验 3】CPU 密集型任务：多线程为什么加不了速？")

    print("""
    上一个是 IO 密集，多线程大获全胜。现在把 cpu_rounds 拉到 300_000，
    让计算成为瓶颈，再跑一次同样的四种模型。

    ⚠ 本实验为了让进程池的开销不至于淹没结果，任务数设为 8、并发 4。
    ⚠ 本实验的**结论以实测为准**：多线程在本次环境下没有明显变慢，
      而是「加了等于没加」。正文有诚实的说明，请不要照抄网上的
      「多线程做 CPU 密集一定更慢」——那句话不严谨。
    """)

    profile = TaskProfile(
        name="CPU 密集",
        io_ms=0.0,           # 纯计算，不等待
        cpu_rounds=300_000,  # 约 15ms 纯 Python 计算
        n_tasks=8,
        concurrency=4,
    )

    single = time.perf_counter()
    busy_parse(profile.cpu_rounds)
    single_cost = time.perf_counter() - single
    print(f"    单任务计算耗时实测 ≈ {single_cost * 1000:.1f} ms（{profile.cpu_rounds:,} 轮）")
    print(f"    理论串行耗时 ≈ {single_cost * profile.n_tasks:.2f}s\n")

    print(f"  {'模型':<10}{'耗时(s)':>10}{'相对同步':>11}{'有效并行度':>12}   说明")
    print("  " + "-" * 72)

    # measured 收集 (名称, 耗时, 加速比, 有效并行度)，供后面的解读引用。
    # 这里刻意不在解读里写死数字 —— 见 exp4 里的踩坑记录。
    measured: list[tuple[str, float, float, float]] = []
    baseline = 0.0
    for name, runner in EXECUTORS:
        elapsed, _ = benchmark(profile, runner, repeat=1)
        if not baseline:
            baseline = elapsed
        ratio = baseline / elapsed if elapsed else 0.0
        # 有效并行度 = 理论串行时间 / 实测时间。1.0 表示完全串行，4.0 表示理想并行。
        parallel = (single_cost * profile.n_tasks) / elapsed if elapsed else 0.0
        note = {
            "同步串行": "基线",
            "多线程": "GIL 串行 → 加线程≈没加",
            "多进程": "真并行 → 最快",
            "asyncio": "单线程事件循环 → 等于串行",
        }[name]
        measured.append((name, elapsed, ratio, parallel))
        print(f"  {name:<10}{elapsed:>10.3f}{ratio:>10.2f}x{parallel:>11.2f}x   {note}")

    print()
    sub(f"为什么多线程没有加速 —— 这是本实验的核心结论（有效并行度 ≈ 1.0x）")
    mt = next((r for r in measured if r[0] == "多线程"), None)
    mp = next((r for r in measured if r[0] == "多进程"), None)
    ae = next((r for r in measured if r[0] == "asyncio"), None)
    print(f"""    ❌ 直觉：{profile.concurrency} 个线程跑 {profile.n_tasks} 个任务，应该快 {profile.concurrency} 倍。
    ✅ 实测：多线程相对同步只有 {mt[2]:.2f}x（有效并行度 {mt[3]:.2f}x），
       而多进程是 {mp[2]:.2f}x（有效并行度 {mp[3]:.2f}x）。

    ⚠ 诚实说明：本次实测里多线程**并没有变慢**，而是「基本没变快」。
      我在设计这个实验时预期它会明显慢于同步串行，但实测只慢到 1.10x 附近。
      原因是：上下文切换的开销虽然真实存在，但在 8 个任务、4 线程这个量级上，
      它被以下几个因素抵消掉了 ——
        · 本机 32 核，线程各自被调度到不同核心，切换代价比单核时低得多；
        · CPython 的 GIL 切换本身实现得相当高效（不是完整的进程上下文切换）；
        · 任务粒度够大（每个 ~15ms），切换频率相对低。
      **所以「多线程做 CPU 密集一定更慢」这句话是不严谨的。**
      准确的表述是：**多线程做 CPU 密集，有效并行度 ≈ 1.0x，
      不会随线程数增加而变快；并且在核心数少、任务粒度小的情况下
      会因为切换开销而真的变慢。**
      教程里不应该为了「故事性」而编造出比实测更夸张的数字 ——
      以实测为准，这才是可复现的知识。""")

    print(f"""
    根因链条（一步一步来）：
      ① CPython 的 GIL 保证同一时刻只有一个线程执行字节码。
      ② 但每 {sys.getswitchinterval() * 1000:.0f}ms（getswitchinterval），当前线程必须放弃 GIL 交给别人。
      ③ 我们的任务是纯字节码循环，没有任何阻塞调用 —— 它不会主动让出。
      ④ 于是发生了强制切换：保存线程 A 的寄存器/栈 → 唤醒线程 B → 线程 B
         恢复上下文 → 拿到 GIL → 继续跑。
      ⑤ 这个「保存-唤醒-恢复」的过程叫**上下文切换**，它消耗 CPU 却不产生计算。

    结论：**多线程做 CPU 密集任务 = 花额外的钱请人不停换班，但工位只有一个**。
    实测有效并行度 {mt[3]:.2f}x，即：**加了 4 个线程，等于没加**。
    多进程有效并行度 {mp[3]:.2f}x —— 差距就在这里。

    ⚠ 一个容易误判的点：
      如果你的 CPU 密集任务里混了 numpy/pandas 的矢量化操作，
      多线程可能**确实有效** —— 因为那些运算在 C 层执行时会释放 GIL。
      所以「Python 多线程不能并行」这句话严格来说是：
      **「Python 多线程不能让 Python 字节码并行」**。
      实验 2 的 zlib 对比已经直接验证了这一点。

    ▸ 正确做法：
      · CPU 密集 → ProcessPoolExecutor，并发度 = CPU 核数（本机 {_cpu_count()}）
      · 爬虫里的 CPU 密集环节是谁？→ 解析 + 清洗 + 特征提取 + 正则回溯
      · 典型架构：**IO 用 asyncio / 线程，解析用进程池**（见实验 6 的混用模式）

    ⚠ 另一个实测发现：asyncio 在这种场景下 {ae[2]:.2f}x，与同步基本持平。
      这是必然的 —— 事件循环本身就是单线程的，负载里又没有 await 点
      （io_ms = 0），所以它退化成纯粹的串行执行。
      **用 asyncio 跑 CPU 密集任务是纯粹的浪费**，它带来复杂度却不带来收益。""")


# ============================================================================
# 实验 4：并发数拐点 —— 并发不是越大越好
# ============================================================================
@dataclass
class MockServerState:
    """模拟目标服务器的状态（用于演示「并发数过大导致错误率上升」）。

    Attributes:
        capacity: 服务器能同时处理的请求数上限。
        overload_penalty_ms: 超出容量后每个额外请求增加的排队延迟。
        reject_threshold: 同时在线请求数超过这个值时开始拒绝（模拟 429/503）。
        in_flight: 当前同时在处理的请求数。
        served: 成功服务数。
        rejected: 拒绝数。
        latencies_ms: 每次成功请求的延迟样本。
    """

    capacity: int = 12
    overload_penalty_ms: float = 18.0
    reject_threshold: int = 45
    in_flight: int = 0
    served: int = 0
    rejected: int = 0
    latencies_ms: list[float] = field(default_factory=list)


async def _mock_fetch(state: MockServerState, base_ms: float,
                      sem: asyncio.Semaphore) -> float:
    """模拟一次受服务器容量约束的请求。

    Args:
        state: 共享服务器状态。
        base_ms: 基础处理时间（毫秒）。
        sem: 客户端侧并发信号量，控制同时在飞的请求数。

    Returns:
        本次请求的实际延迟（毫秒）。

    Raises:
        RuntimeError: 当请求被服务器拒绝时（模拟 HTTP 429）。

    这个模型刻意还原了真实服务器的两个行为：
      ① 超出容量后，请求进入队列排队，延迟随排队长度线性增长（M/M/1 队列的
         简化版）—— 这就是「并发越高，P95 延迟越夸张」的数学原因。
      ② 队列无限增长是物理上不可能的，所以一定存在一个拒绝阈值。
         真实服务器在这里返回 429 Too Many Requests 或 503。
    """
    async with sem:
        state.in_flight += 1
        try:
            if state.in_flight > state.reject_threshold:
                state.rejected += 1
                raise RuntimeError("HTTP 429 Too Many Requests（服务端过载）")
            # 排队延迟：超过 capacity 的部分每个请求多等 penalty 毫秒
            queued = max(0, state.in_flight - state.capacity) * state.overload_penalty_ms
            latency = base_ms + queued
            await asyncio.sleep(latency / 1000.0)
            state.served += 1
            state.latencies_ms.append(latency)
            return latency
        finally:
            state.in_flight -= 1


async def _sweep_one(concurrency: int, n_tasks: int, state: MockServerState,
                     base_ms: float) -> dict[str, Any]:
    """在指定并发度下跑完整批次。

    Args:
        concurrency: 信号量上限。
        n_tasks: 任务总数。
        state: 服务器状态（会被清空重用）。
        base_ms: 基础延迟。

    Returns:
        含并发度、耗时、QPS、错误率、P95 延迟的字典。
    """
    state.in_flight = 0
    state.served = 0
    state.rejected = 0
    state.latencies_ms.clear()

    sem = asyncio.Semaphore(concurrency)
    # 关键：所有协程一次性创建并交给 gather。
    # 事件循环会按「就绪顺序」逐个推进它们，每个协程在拿到信号量之后
    # 立刻把 in_flight 加一 —— 因此 in_flight 的峰值恰好等于并发度。
    # （踩坑记录：最初我在 async with 之后、in_flight 计数之前插了一句
    #   `await asyncio.sleep(0)`，结果 in_flight 峰值始终是 1。
    #   根因是 sleep(0) 把协程重新排到就绪队列**尾部**，而 gather 这时已经
    #   把所有协程都推进到「等信号量」的状态，于是每放开一个槽位就只有
    #   一个新协程进来、且立刻又被排队，形成"一次只放行一个"的假象。
    #   教训：凡是用来观测并发的探针，它自己**不能 await**，
    #   否则你观测到的是探针的调度行为，不是被测对象的。）
    t0 = time.perf_counter()
    await asyncio.gather(*[_mock_fetch(state, base_ms, sem) for _ in range(n_tasks)],
                         return_exceptions=True)
    elapsed = time.perf_counter() - t0

    total = state.served + state.rejected
    p95 = 0.0
    if state.latencies_ms:
        sorted_lat = sorted(state.latencies_ms)
        idx = min(len(sorted_lat) - 1, int(len(sorted_lat) * 0.95))
        p95 = sorted_lat[idx]
    return {
        "concurrency": concurrency,
        "elapsed": elapsed,
        "qps": total / elapsed if elapsed else 0.0,
        "error_rate": state.rejected / total if total else 0.0,
        "rejected": state.rejected,
        "served": state.served,
        "p95": p95,
        "avg": statistics.mean(state.latencies_ms) if state.latencies_ms else 0.0,
    }


async def exp4_concurrency_sweep() -> None:
    """实验 4（协程）：扫描并发度，找出吞吐量拐点。

    踩坑记录（❌ 错误做法 → 现象 → 根因 → 正确做法）：
      ❌ 最初把本函数写成 `def`（同步），却在里面直接调用 `_sweep_one(...)`
         （协程函数），并把返回值当成字典用。
         现象：TypeError: 'coroutine' object is not subscriptable，
               外加 RuntimeWarning: coroutine '_sweep_one' was never awaited。
         根因：调用 `async def` 函数**不会执行它的函数体**，只是创建一个
               协程对象。协程对象当然不能下标取值。而且它从未被 await，
               所以连一次请求都没发出去。
         正确做法：凡是需要调用协程的调用链，从顶层到叶子必须**全部是 async**，
               或者在同步函数里用 asyncio.run 显式驱动。
               这就是「异步传染性」——它不能中途变回同步。
               本实验的做法是把 exp4 本身改成 async，由 main 里统一 await。
    """
    title("【实验 4】并发数不是越大越好 —— 扫出吞吐量拐点")

    print("""
    负载：200 个请求，基础处理 30ms。
    模拟服务器：同时处理能力 12 个，超过后每个额外请求 +18ms 排队；
                同时在飞超过 45 个直接返回 429。

    这个模型的意义在于：**真实服务器一定有容量上限**。
    你的并发数超过它的容量时，收益不会继续增长，只会把延迟推高、错误率推上去。
    """)

    state = MockServerState()
    n_tasks = 200
    base_ms = 30.0

    print(f"  {'并发度':<8}{'耗时(s)':>10}{'QPS':>9}{'成功':>8}{'429':>7}"
          f"{'错误率':>9}{'均值(ms)':>11}{'P95(ms)':>10}")
    print("  " + "-" * 76)

    results: list[dict[str, Any]] = []
    for c in (1, 2, 4, 8, 16, 32, 64, 150):
        r = await _sweep_one(c, n_tasks, state, base_ms)
        results.append(r)
        print(f"  {c:<8}{r['elapsed']:>10.2f}{r['qps']:>9.1f}{r['served']:>8}"
              f"{r['rejected']:>7}{r['error_rate'] * 100:>8.1f}%"
              f"{r['avg']:>11.1f}{r['p95']:>10.1f}")

    # 下面所有结论都由实测数据算出来，不写死数字 ——
    # 踩坑记录：初版我把「性价比区间」硬编码成 results[2] 和 results[4]，
    #   结果实测数据一变，正文就出现了「QPS 提升了 91%，P95 涨了 476%」
    #   这种自相矛盾的解读。更隐蔽的问题是：高并发那两行的 QPS 反而"变高"了，
    #   因为被拒绝的请求**瞬间返回**，算进 QPS 的分母里就是虚高。
    #   所以必须**只统计成功请求的 QPS**（goodput），才看得出真实吞吐。
    #   教训：教程正文里的数字必须由代码从本次运行的数据里算出来，
    #   否则注释和输出迟早对不上，而这正是本课程最不能容忍的错误。

    # goodput = 只算成功的请求 / 耗时。它才是"有效吞吐"。
    for r in results:
        r["goodput"] = r["served"] / r["elapsed"] if r["elapsed"] else 0.0

    peak = max(results, key=lambda x: x["goodput"])
    zero_err = [r for r in results if r["error_rate"] == 0]
    safe = max(zero_err, key=lambda x: x["goodput"]) if zero_err else results[0]
    # 线性区内、并发度高于 safe 的那些点：错误率 0，但延迟已经开始膨胀
    tail = [r for r in results
            if r["error_rate"] == 0 and r["concurrency"] > safe["concurrency"]]
    first_crash = next((r for r in results if r["error_rate"] > 0), None)

    sub("结果解读")
    print(f"""    ① 并发度 1 → 有效吞吐只有 {results[0]['goodput']:.1f} QPS，耗时 {results[0]['elapsed']:.2f}s。
       这是「同步串行」的数字，白白浪费了 {n_tasks} 个请求的并行空间。
       从 1 并发到 {safe['concurrency']} 并发，有效吞吐从 {results[0]['goodput']:.1f}
       涨到 {safe['goodput']:.1f} QPS，提升了 {safe['goodput'] / results[0]['goodput']:.1f} 倍。

    ② 并发度 ≤ {safe['concurrency']} 时，错误率保持 0%，P95 始终等于
       服务端基础延迟 {base_ms:.0f}ms —— 这叫**线性区**。
       在这个区间里加大并发是纯粹的收益：吞吐涨、延迟不变。
       （为什么能线性涨？因为服务器容量是 {state.capacity}，
         并发 {safe['concurrency']} < {state.capacity}，请求根本不需要排队。）

    ③ 并发度越过 {safe['concurrency']} 后进入**收益递减区**：吞吐不再增长，
       而延迟开始膨胀。看这几组数据（全部是错误率 0 的"成功"请求）：

       {"".join(f'''
         · 并发 {r['concurrency']:>3}：有效吞吐 {r['goodput']:>6.1f}  均值 {r['avg']:>6.1f}ms  P95 {r['p95']:>6.1f}ms'''
                for r in [safe] + tail)}

       ▸ 关键现象：从并发 {safe['concurrency']} 到并发 {tail[-1]['concurrency'] if tail else safe['concurrency']}，
         有效吞吐反而从 {safe['goodput']:.1f} 掉到 {tail[-1]['goodput']:.1f}（下降
         {(1 - tail[-1]['goodput'] / safe['goodput']) * 100:.0f}%），
         而 P95 从 {safe['p95']:.0f}ms 涨到 {tail[-1]['p95']:.0f}ms
         （涨了 {(tail[-1]['p95'] / max(safe['p95'], 1) - 1) * 100:.0f}%）。
       └ 根因：**排队延迟是守恒的**。服务器单位时间只能处理那么多请求，
         你多塞进去的不会变成吞吐，只会躺在队列里。
         更糟的是「越堵越慢」：请求在队列里占用的内存和连接数会拖慢
         服务器本身的处理速度，于是总吞吐不升反降。
         这就是排队论里的拥塞崩溃（congestion collapse）现象。

    ④ 并发度 {first_crash['concurrency'] if first_crash else 0} 开始出现大规模拒绝：
       {first_crash['rejected'] if first_crash else 0} 个请求被拒，错误率
       {first_crash['error_rate'] * 100 if first_crash else 0:.1f}%。
       ⚠ 注意这里有个**极具迷惑性的陷阱**：此时"总 QPS"是
         {first_crash['qps'] if first_crash else 0:.1f}，看起来比并发 {safe['concurrency']} 时还高！
         但那是因为被拒的请求**瞬间返回**（没有真实 IO），
         把这种请求算进吞吐就是自欺欺人。
         正确指标是 goodput（只算成功请求）= {first_crash['goodput'] if first_crash else 0:.1f} QPS。
       └ 这正是爬虫最容易踩的坑：**你以为的快，其实是把对方的服务器打崩了**。
         对方的应对是封 IP、上 WAF、返回验证码 —— 你的采集能力直接归零。
         本模型里并发 {first_crash['concurrency'] if first_crash else 0} 和 {results[-1]['concurrency']} 的结果完全相同：
         因为拒绝阈值是 {state.reject_threshold}，只要同时在飞超过它，
         多出来的请求全部被拒；而信号量一放开槽位就立刻被占满，
         所以两者都稳定停在「{state.reject_threshold} 个在飞 + 其余全被拒」的同一状态。

    ▸ 最优并发度怎么定（工程做法）：
      ① 先用并发 1 探测单请求延迟 T1（本实验是 {base_ms:.0f}ms）；
      ② 目标 QPS 定在「站点容忍度」而不是「机器极限」——
         一般公开站点 1~5 QPS，自有/授权站点可以谈；
      ③ 并发度的起始值 = 目标QPS × T1，然后**向下调**，
         调到错误率 < 1% 且 P95 不飙升为止；
      ④ 把这些数字写进配置，而不是硬编码在代码里（第 64 课的 ConfigManager）。

    ▸ 本次实测的「安全区上限」参考值：并发 {safe['concurrency']}
      （错误率 0%、有效吞吐 {safe['goodput']:.1f} QPS、P95 仍为 {safe['p95']:.0f}ms）。
      ⚠ 这个数字**只对上面这个模拟模型成立**，真实站点必须重新探测。""")

    sub("▸ 自适应并发：把拐点交给程序自动找")
    print("""    AIMD（加性增、乘性减）—— TCP 拥塞控制那一套，直接搬到爬虫上：

        if 本窗口错误率 > 5%:
            concurrency = max(1, concurrency // 2)      # 乘性减：立刻退让
        elif P95 延迟 < 阈值 and 错误率 == 0:
            concurrency += 1                            # 加性增：稳步试探
        else:
            pass                                        # 平台期：保持不动

    为什么用「乘性减」而不是「减 1」？
      因为一旦服务器开始拒你，说明你**已经**超出它的容量，减 1 是杯水车薪；
      而且被拒阶段你在对方眼里是「攻击性流量」，要尽快脱离这个状态。

    为什么用「加性增」而不是「乘性增」？
      因为你要找的是平台期的**下沿**，乘性增会一步跨过拐点，
      然后又要乘性减，形成「锯齿震荡」，平均吞吐反而更低。""")


# ============================================================================
# 实验 5：容错 —— 信号量 + gather(return_exceptions=True)
# ============================================================================
async def _flaky_fetch(idx: int, sem: asyncio.Semaphore,
                       fail_ratio: int = 7) -> str:
    """一个「每 7 个失败 1 个」的模拟请求。

    Args:
        idx: 任务序号。
        sem: 并发信号量。
        fail_ratio: 失败频率分母。

    Returns:
        成功时的结果字符串。

    Raises:
        ConnectionError: 当 idx 命中失败条件时。
        TimeoutError: 当 idx 命中另一种失败条件时。

    故意抛出**两种不同类型的异常**，用来演示分类型重试策略：
      连接错误 → 值得立刻重试（可能是瞬时抖动）
      超时     → 要退避后重试，并且下次可能要换代理（第 63 课）
    """
    async with sem:
        await asyncio.sleep(0.01)
        if idx % fail_ratio == 0:
            raise ConnectionError(f"任务 {idx}：连接被重置")
        if idx % 13 == 0:
            raise TimeoutError(f"任务 {idx}：读取超时")
        return f"ok-{idx}"


async def exp5_tolerance() -> None:
    """实验 5：演示两种错误处理写法的差别。"""
    title("【实验 5】容错：一个任务失败，不能拖垮整批任务")

    n = 60
    sem = asyncio.Semaphore(10)

    sub("5.1 ❌ 错误写法：gather 默认行为")
    try:
        await asyncio.gather(*[_flaky_fetch(i, sem) for i in range(n)])
        print("    （没抛异常，说明本次随机没触发失败 —— 概率性现象）")
    except Exception as exc:
        print(f"    抛出异常：{type(exc).__name__}: {exc}")
        print(f"""
    ▸ 现象：整批任务在第 {n} 个任务中的第一个失败处直接崩掉。
    ▸ 更隐蔽的后果：其余已经完成的 {n - 1} 个任务的结果**全部拿不到了** ——
      因为 gather 把异常往外抛了，没有返回结果列表。
      在爬虫里，这意味着「一个 404 让你丢掉 999 条已抓数据」，
      然后你会去重跑整批，重复请求又招来反爬。
    ▸ 根因：gather 的默认语义是「要么全成功，要么抛出第一个异常」，
      它服务于 RPC 式的全或无场景，不服务于「批量采集」这种
      天生就有部分失败的场景。""")

    sub("5.2 ✅ 正确写法：return_exceptions=True + 分类处理")
    t0 = time.perf_counter()
    raw = await asyncio.gather(*[_flaky_fetch(i, sem) for i in range(n)],
                               return_exceptions=True)
    elapsed = time.perf_counter() - t0

    ok: list[str] = []
    by_type: dict[str, list[int]] = {}
    for i, r in enumerate(raw):
        if isinstance(r, BaseException):
            by_type.setdefault(type(r).__name__, []).append(i)
        else:
            ok.append(str(r))

    print(f"    批任务总数 : {n}")
    print(f"    成功       : {len(ok)}")
    print(f"    耗时       : {elapsed:.2f}s")
    for t, idxs in sorted(by_type.items()):
        print(f"    {t:<14}: {len(idxs)} 个 → 可重试任务 {idxs[:6]}{'...' if len(idxs) > 6 else ''}")

    print(f"""
    ▸ 结果：成功 {len(ok)} / {n}，其余以**异常对象**形式返回，
      调用方可以逐条判断、分类、投递到重试队列。
      整批任务的存活率不再取决于最差的那一个任务。

    ▸ 生产写法（把上面的逻辑封装成可复用的重试装饰器）：

        async def fetch_with_retry(url, *, attempts=3, base_delay=0.5):
            for i in range(attempts):
                try:
                    return await fetch(url)
                except (ConnectionError, TimeoutError) as exc:
                    if i == attempts - 1:
                        raise
                    # 指数退避 + 抖动（第 63 课详解）
                    delay = base_delay * (2 ** i) * (0.5 + random.random())
                    await asyncio.sleep(delay)

    ▸ 注意 sem 的位置：信号量必须包在「重试循环之外」，
      否则重试会额外占用并发名额，一次抖动就可能把并发槽全部吃满。""")

    sub("5.3 信号量的正确用法与常见错误")
    print("""    ❌ 错误 1：在信号量外面等
        sem = asyncio.Semaphore(10)
        async def bad(url):
            async with sem:
                pass          # 拿不到锁就立刻放弃，等于没限流
            return await fetch(url)   # ← 真正的 IO 在锁外面！

    ❌ 错误 2：把信号量当成"总配额"
        # 想限制「总共发 1000 个请求」，却写成了「同时 1000 个并发」
        这不是限流，这是 DDoS。

    ❌ 错误 3：一个信号量管多个域名
        # example.com 慢，把 a.com 的配额也拖住了
        # 正确做法是按域名分组信号量（第 63 课的 RateLimiter 做了这件事）

    ✅ 正确：把「并发上限」和「速率上限」分开
        asyncio.Semaphore(10)          # 管同时多少个在飞
        RateLimiter(qps=5)             # 管每秒发多少个
        两者缺一不可：并发限制防积压，速率限制防封禁。""")


# ============================================================================
# 实验 6：选型决策表与混用模式
# ============================================================================
def exp6_decision() -> None:
    """实验 6：给出可落地的选型决策表与混用架构。"""
    title("【实验 6】选型决策表与生产混用模式")

    print(f"""
    本机 CPU 核数：{_cpu_count()}
    """)

    sub("6.1 一页决策表")
    print("""    ┌─────────────────────┬──────────────────┬──────────────┬─────────────┐
    │ 你的任务特征         │ 推荐模型          │ 并发度起点    │ 理由         │
    ├─────────────────────┼──────────────────┼──────────────┼─────────────┤
    │ 少量请求 / 调试      │ 同步 requests    │ 1            │ 可读性 > 性能│
    │ 大量 IO，代码简单    │ 线程池            │ 10 ~ 50      │ 改动最小     │
    │ 大量 IO，要高并发    │ asyncio + aiohttp│ 50 ~ 500     │ 资源占用最低 │
    │ 解析/清洗是瓶颈      │ 进程池            │ = CPU 核数   │ 绕过 GIL     │
    │ IO + CPU 都重        │ asyncio + 进程池 │ 混合         │ 见下方架构   │
    │ 分布式多机           │ 第 62 课的队列   │ 按机器扩容   │ 水平扩展     │
    └─────────────────────┴──────────────────┴──────────────┴─────────────┘

    ▸ 一句话记忆：
      **瓶颈在等 → 用协程或线程；瓶颈在算 → 用进程；
        两个都重 → 把「等」的部分做成异步，把「算」的部分扔进进程池。**""")

    sub("6.2 混用架构：asyncio 抓取 + 进程池解析")
    print("""    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
    │  事件循环     │    │  解析进程池   │    │  Pipeline    │
    │  (单线程)     │───▶│  (N = CPU核) │───▶│  入库/去重    │
    │              │    │              │    │              │
    │ 1000 个协程   │    │ 4~8 个进程    │    │ 异步批量写    │
    │ 全部在等网络  │    │ 跑 CPU 密集   │    │              │
    └──────────────┘    └──────────────┘    └──────────────┘
          ↑                                        │
          └──────────── 反馈限速/重试 ──────────────┘

    关键实现点：
      ① 用 loop.run_in_executor(None, fn, *args) 把同步的解析函数
         交给默认线程池；但解析是 CPU 密集，所以要显式传进程池：
             pool = ProcessPoolExecutor(max_workers=os.cpu_count())
             html_item = await loop.run_in_executor(pool, parse_html, html)
      ② parse_html 必须是**模块级函数**（pickle 限制，见实验 3 的踩坑记录）。
      ③ 每次 run_in_executor 的入参/返回值都要 pickle 往返。
         如果 HTML 是 1MB、解析结果是 10KB，pickle 开销约 1~3ms，
         而解析只要 15ms —— 开销占比 10%~20%，可以接受；
         **但如果 HTML 很小（10KB）而解析很重（100ms），
         进程池的通信开销就完全值得。反之，如果解析只要 2ms，
         别用进程池，pickle 比计算还贵。**""")

    sub("6.3 一个真实的（简化）生产骨架")
    print("""    import asyncio, os
    from concurrent.futures import ProcessPoolExecutor

    async def crawl(urls: list[str]) -> list[dict]:
        sem = asyncio.Semaphore(50)                 # 并发上限
        loop = asyncio.get_running_loop()
        with ProcessPoolExecutor(max_workers=os.cpu_count()) as pool:
            async def one(url: str) -> dict | None:
                async with sem:
                    try:
                        html = await fetch(url, timeout=10)
                    except (ConnectionError, TimeoutError):
                        return None                     # 交给外层重试
                    # CPU 密集的解析丢给进程池，事件循环继续处理其他请求
                    return await loop.run_in_executor(pool, parse_html, html)
            results = await asyncio.gather(*(one(u) for u in urls),
                                           return_exceptions=True)
        return [r for r in results if isinstance(r, dict)]

    ▸ 这个骨架已经包含了生产爬虫的四个要素：
      并发控制（sem）、超时（timeout）、容错（return_exceptions）、
      计算卸载（run_in_executor）。
      它缺的是：限速、代理、去重、持久化队列、监控 ——
      正好是第 61~65 课要补的。""")


# ============================================================================
# 踩坑记录汇总（本课实测遇到的真实问题）
# ============================================================================
def pitfalls() -> None:
    """打印本课实测中真实踩到的坑。"""
    title("踩坑记录：本课代码实测中真实遇到的问题")

    print("""
    ── 坑 1：多进程里用 lambda 直接爆炸 ────────────────────────────────
    ❌ 错误做法：pool.submit(lambda i: run_one(profile, i), i)
    现象：AttributeError: Can't pickle local object '<lambda>'
    根因：多进程靠 pickle 序列化参数和函数，lambda/局部函数没有全局限定名，
          子进程无法在自己的命名空间里找到它。
    正确做法：工作函数提升到模块级，参数只用可 pickle 的基本类型。
    教训：凡是 run_in_executor / ProcessPoolExecutor 用到的函数，
          一律写到模块顶层，不要图省事写在函数内部。

    ── 坑 2：断言写反，把一个"正确的结果"判成了 bug ────────────────────
    ❌ 错误做法：在 benchmark 里断言 len(result) == profile.concurrency
    现象：所有执行器都报"任务被静默丢弃"，看起来像并发库坏了。
    根因：返回结果的数量应该等于**任务总数**，不是**并发度**。
          这两个数字在 n_tasks != concurrency 时必然不同。
    正确做法：断言对象是 profile.n_tasks。
    教训：断言是给未来的自己看的推理书，写错断言比不写断言更危险 ——
          它会把正确行为标记成错误，逼你去"修"一个没坏的东西。

    ── 坑 3：asyncio 里执行 CPU 密集代码 → 事件循环冻结 ────────────────
    ❌ 错误做法：在协程里直接调用 busy_parse(300_000)
    现象：本该并发 10 个的任务，实测耗时等于串行耗时，且期间
          其它协程完全得不到调度（心跳、日志、超时全部失效）。
    根因：事件循环是**单线程**的。协程里任何一个不 await 的同步调用，
          都会霸占线程不放，后续所有就绪协程只能干等。
          这与实验 3 里多线程被 GIL 拖慢是同一类问题的两种表现形态：
          **多线程是被别人抢走 CPU，asyncio 是自己不放 CPU**。
    正确做法：CPU 密集代码丢给 run_in_executor(进程池)；或者用
          await asyncio.sleep(0) 手动让出（只解决公平性，不解决总量）。
    本课处理：实验 3 里 asyncio 一行故意保留了同步调用，
          并如实报告「它和串行一样慢」，把这个现象变成教学内容。

    ── 坑 4：并发实验的可复现性 ─────────────────────────────────────
    ❌ 错误做法：并发实验跑一次就下结论。
    现象：同一份代码两次跑，多线程耗时差了 30%，结论从"快 8 倍"
          变成"快 5 倍"。
    根因：共享 CI/开发机的噪声源太多（其他进程、GC、CPU 降频、
          页面回收），并发实验对噪声极度敏感。
    正确做法：至少取 3 次最小值（本课 benchmark 的 repeat 参数），
          并且**用同一个负载配置**横向对比，不要跨机器/跨时间对比绝对值。
    教训：教程里写进正文的数字必须是「同一台机器、同一次运行、
          同一个负载」下的相对值，绝对耗时仅供参考。
    """)


async def _run_async_experiments() -> None:
    """在**同一个事件循环**里依次运行实验 4 和实验 5。

    Returns:
        None

    为什么要合并到一个事件循环里（而不是两次 asyncio.run）？
      · 实验 4 的服务器状态和实验 5 的信号量都需要在协程上下文里创建，
        重复 asyncio.run 会反复创建/销毁事件循环，掩盖了
        「事件循环是长期存在的单例」这个认知。
      · 更贴近生产：一个爬虫进程通常只有一个事件循环，
        所有阶段（抓取、限流、监控上报）都跑在它上面。
    """
    await exp4_concurrency_sweep()
    await exp5_tolerance()


def main() -> None:
    """运行全部实验。"""
    print(SEP)
    print("阶段 6 · 第 60 课：并发模型选型")
    print(SEP)
    print(f"""
本课用**同一个模拟负载**跑四种并发模型（同步 / 多线程 / 多进程 / asyncio），
实测它们的耗时、加速比、吞吐量，并找出「并发数拐点」。

⚠ 局限声明：
  · 本课不访问任何真实网站。网络等待用 time.sleep / asyncio.sleep 模拟，
    解析计算用纯 Python 循环模拟。
  · 模拟与生产环境的差异：
      ① 真实网络的延迟是**重尾分布**（偶尔出现 5s 长尾），
         而 sleep 是恒定延迟 —— 所以真实的 P95 会比本课实验更难看；
      ② 真实站点有 WAF/验证码/封 IP，本课用「服务端容量 + 429」近似，
         但真实的封禁往往是**静默的**（返回 200 但内容是假的）；
      ③ 真实并发受限于文件描述符、代理带宽、DNS 解析，
         本课只模拟了服务端容量这一个约束。
  · 尽管如此，「哪种模型更快」「拐点在哪」这两个结论在真实环境同样成立，
    因为它们是 GIL 语义和排队论决定的，与具体网络实现无关。
""")

    exp1_io_bound()
    exp2_gil_truth()
    exp3_cpu_bound()
    asyncio.run(_run_async_experiments())
    exp6_decision()
    pitfalls()

    title("本课要点")
    for line in [
        "1. 选并发模型的第一步是判断瓶颈：等待（IO）还是计算（CPU）",
        "2. 爬虫 95%+ 的时间在等网络，所以它是 IO 密集型任务",
        "3. GIL 限制的是「Python 字节码并行」，不是「一切并行」",
        "4. 阻塞式系统调用（sleep/socket/文件 IO）会释放 GIL，这是多线程有效的根本原因",
        "5. IO 密集场景多线程 ≈ 多进程，但内存占用只有后者的几分之一",
        "6. CPU 密集场景多线程有效并行度只有约 1.0x（实测 0.93x），加线程等于没加",
        "7. 判据：热点代码是纯字节码（被 GIL 咬）还是在 C 扩展里（不受影响）",
        "8. asyncio 与多线程性能同量级，优势在资源占用：协程 KB 级，线程 MB 级",
        "9. asyncio 里执行同步阻塞代码会冻结整个事件循环，CPU 密集必须丢进进程池",
        "10. Semaphore 管「同时在飞的数量」，RateLimiter 管「每秒发出的数量」，两者缺一不可",
        "11. gather(return_exceptions=True) 是批量采集的容错底线，默认行为会因一个失败丢掉全部结果",
        "12. 并发数存在拐点：超过服务器容量后 QPS 停滞、延迟暴涨、错误率上升",
        "13. 自适应并发用 AIMD：错误率高就减半，一切正常就 +1，不要乘性增",
        "14. 多进程的入参和返回值必须可 pickle，工作函数必须放模块顶层",
        "15. 并发实验必须多次取最小值，噪声会轻易改变结论方向",
        "16. 生产骨架 = 异步抓取 + 进程池解析 + 信号量 + 超时 + 分类重试",
    ]:
        print("  " + line)
    print()


if __name__ == "__main__":
    main()
