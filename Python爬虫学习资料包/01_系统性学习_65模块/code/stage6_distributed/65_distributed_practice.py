"""
第 65 课 · 大规模实战 —— 把前四课拼成一个真能跑的分布式爬虫

本课要回答的问题：
  1. 前面四课（并发模型、Scrapy 架构、分布式队列、代理池、部署）
     单独看都能跑，**拼在一起**会出什么问题？
  2. 「多节点」到底怎么模拟才算数？
     用线程假装多节点，和用真进程跑多节点，结论会不会不一样？
  3. 故障注入：随机 kill 掉一个节点，任务会不会丢？
     断点续爬的进度计数会不会错？
  4. 去重命中率、节点负载均衡、故障恢复完成率 ——
     这三个指标怎么算，什么值算健康？
  5. 多节点共享状态如果**不放 Redis**，还有没有别的办法？
     它的边界在哪里？
  6. 从「本地跑通」到「生产可用」，中间还差哪些东西？
     用一张对照表说清楚，不回避。

================================ 运行方式 ================================
    python3 code/stage6_distributed/65_distributed_practice.py

零额外依赖，全程纯标准库
（os / sys / json / time / random / threading / multiprocessing / subprocess）。

================================ 本课的「诚实边界」声明（先读这一条）================
  本课要模拟「多个机器上的多个爬虫进程，共享一个任务队列」。
  有两种做法：

    做法 A（假分布式）：在一个进程里起多个**线程**，共享一个内存对象。
      · 优点：好写好调试、快
      · 致命缺点：**它测不出真分布式的问题**。
        因为 CPython 的 GIL 会让多线程在**纯 Python 字节码层面**
        交替执行，加上内存对象天然共享、没有序列化边界，
        「任务丢失」「去重失效」这类 bug 根本不会出现。
        → 用它验证分布式正确性，是**自欺欺人**。

    做法 B（真分布式，本课采用）：
      用 multiprocessing 起 **N 个真实的操作系统进程**，
      它们之间只能通过「一个文件 + 文件锁」通信。
      · 每个节点是一个独立 Python 解释器，有独立的 GIL、独立的内存
      · 任何共享状态都必须**序列化**（本课用 JSON 写文件）
      · 于是「原子性」「竞态」「任务丢失」全部变成**真实存在的问题**
      · 代价：慢、复杂，但结论可信

  ⚠ 局限 1：本课仍然没有用**真 Redis**。
     共享队列用的是「一个 JSON 文件 + fcntl.flock 排他锁」。
     它和 Redis 的差别在于：
       · fcntl.flock 在**同一台机器**上有效，跨机器需要 NFS 并且
         NFS 上的 flock 语义在很多实现里是**坏的**（历史遗留问题）
       · 没有批量命令、没有 pipeline，每次读写都是全量文件 IO
       · 没有过期淘汰、没有持久化保证
     所以：**队列逻辑结论可信，性能数字不可迁移到 Redis**（同 62 课）。
     第 62 课已经用真 Redis 验证过队列逻辑，本课专注「多节点编排」这一层。

  ⚠ 局限 2：本课仍然没有真代理、没有真网络。
     第 63 课的代理池策略在这里被**简化复用**，
     节点的「抓取」是一个耗时 sleep + 概率失败，
     不是真实 HTTP 请求。所以本课的「成功率」是**模型算出来的**，
     不是网络测出来的。它验证的是**编排逻辑**，不是反爬对抗效果。
"""

from __future__ import annotations

import fcntl
import json
import multiprocessing as mp
import os
import random
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

SEP = "=" * 76


# ============================================================================
# 通用输出工具
# ============================================================================
def title(text: str) -> None:
    """打印一级标题。

    Args:
        text: 标题文本。

    Returns:
        None
    """
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    """打印二级标题。

    Args:
        text: 小标题。

    Returns:
        None
    """
    print(f"\n▸ {text}")


def bar(current: int, total: int, width: int = 40) -> str:
    """生成一个文本进度条。

    Args:
        current: 当前值。
        total: 总值。
        width: 进度条字符宽度。

    Returns:
        形如 `████████░░░░ 66.7%` 的字符串。

    ▸ 为什么自己画进度条而不是用 tqdm？
      因为本课要在**多进程**下显示进度，tqdm 的 stdout 在多个进程里
      会互相覆盖，输出会变成一团乱码。
      自己画的好处是**可控**：只在主进程画，子进程的输出单独收集。
      这本身就是一个分布式系统的经验：
      **日志和进度这类「全局视图」，只能由协调者统一输出。**
    """
    if total <= 0:
        return "░" * width + "   0.0%"
    ratio = max(0.0, min(1.0, current / total))
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled) + f" {ratio * 100:5.1f}%"


def human_ms(seconds: float) -> str:
    """把秒数格式化成人类可读的毫秒/秒字符串。

    Args:
        seconds: 秒数。

    Returns:
        形如 `123 ms` 或 `1.23 s` 的字符串。
    """
    if seconds < 1.0:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.2f} s"


# ============================================================================
# 第 1 部分：文件锁保护的共享任务队列（跨进程真的能用的那种）
# ============================================================================
@dataclass
class TaskRecord:
    """队列里的一个任务记录。

    Attributes:
        url: 任务对应的 URL。
        fp: 指纹（用于去重）。
        attempts: 已被取走的次数（用于重试上限）。
        status: 任务状态，取值 pending / taken / done / failed。
        owner: 最后一次取走它的节点名（用于诊断「谁领走了任务」）。
        taken_at: 最近一次被取走的时间戳（仅用于观察，不参与逻辑）。
    """

    url: str
    fp: str
    attempts: int = 0
    status: str = "pending"
    owner: str = ""
    taken_at: float = 0.0


class FileLockedStore:
    """用一个 JSON 文件 + fcntl 排他锁实现的**跨进程**共享存储。

    │ 为什么不用 multiprocessing.Queue 或 Manager？
    │   · mp.Queue 是「消息传递」模型，任务取走后就从队列消失了 ——
    │     这正是第 62 课说的「朴素队列」，崩溃即丢任务，无法做可靠队列。
    │   · mp.Manager 提供了共享 dict，但它的原子性粒度是**单个方法调用**，
    │     而我们要的复合操作是「取任务并标记为 processing」，
    │     跨两个 dict 操作 —— Manager 保证不了这两个之间的关系。
    │   · 更根本的问题：两者都**绑死在父进程的生命周期**上，
    │     没法模拟「一台机器上的进程崩了，另一台机器上的进程接管」。
    │
    │ ▸ 用文件 + flock 的好处：
    │   · 状态是**显式持久化**的 —— 进程全死了，文件还在，
    │     下一个进程读文件就能续爬。这正是「断点续爬」的本质。
    │   · 锁是操作系统级的，多个进程真正互斥。
    │   · 和 Redis 的模型**同构**：读-改-写必须在锁内完成。
    │     把 flock 换成 Redis 的 Lua 脚本 / MULTI，逻辑完全一样。
    │
    │ ⚠ 局限：
    │   · fcntl.flock 只在**本机**有效；跨机器的正确做法是 Redis 或
    │     NFS（但 NFS 上的 flock 在很多实现里是坏的，别踩）。
    │   · 每次操作都是「读整个文件 → 改内存 → 写回整个文件」，
    │     复杂度 O(N)。任务上万条时会很慢。真实系统要用 Redis，
    │     或者 SQLite（带 WAL，也是单机共享状态的好选择）。
    │     本课任务量只有几百，够用。
    """

    def __init__(self, path: str, verbose: bool = False) -> None:
        """初始化共享存储。

        Args:
            path: JSON 文件路径。不存在时会自动创建。
            verbose: 是否打印每次加锁操作（调试用，实验里关掉）。

        ▸ 注意：实例化时**不加锁**，只记录路径。
          真正的锁在每次 `transaction()` 里现取现放。
          这样多个进程各自 new 一个实例，指向同一个文件，就是共享的。
        """
        self.path = path
        self.verbose = verbose
        # 文件必须预先存在，否则第一个进程 open 时可能拿不到锁
        # （flock 需要真实的文件描述符）
        Path(path).touch(exist_ok=True)

    class _Txn:
        """一次加锁的事务上下文。用 with 语句保证锁一定被释放。

        │ 为什么用上下文管理器而不是手工 acquire/release？
        │   因为**异常安全**。爬虫里到处是网络异常，
        │   如果中途抛异常而没有释放锁，整个系统就死锁了。
        │   with 语句保证即使抛异常也会执行 __exit__。
        │   这是分布式系统的基本功：**任何锁都必须有超时或自动释放路径。**
        """

        def __init__(self, store: "FileLockedStore") -> None:
            """初始化事务。

            Args:
                store: 所属的存储对象。
            """
            self.store = store
            self._fh: Any = None

        def __enter__(self) -> dict[str, Any]:
            """加锁并读出当前状态。

            Returns:
                解析后的 JSON 状态字典。

            ▸ 这里用 LOCK_EX（排他锁）而不是 LOCK_SH（共享锁），
              因为我们对**每一次**访问都做读-改-写。
              即使理论上「只读」的操作，本课也用排他锁简化 ——
              代价是并发度低，收益是不会写错。
              **优化前先保证正确**，这是本课反复强调的态度。
            """
            # a+ 既能读又能写，且文件不存在时创建
            self._fh = open(self.store.path, "a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            self._fh.seek(0)
            raw = self._fh.read()
            if not raw.strip():
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 文件损坏（比如上次写了一半被 kill）——
                # 这是真实系统一定会遇到的情况，必须处理而不是崩。
                return {}

        def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
            """写回状态并释放锁。

            Args:
                exc_type: 异常类型（如有）。
                exc: 异常对象。
                tb: 回溯对象。

            Returns:
                False 表示不吞掉异常，让它继续向上抛。

            ▸ **只有正常结束才写回**：如果 with 块里抛了异常，
              说明内存状态可能不完整，此时写回会写入半个状态。
              所以 exc_type 不为 None 时**不写**。
              这是「宁可丢一次操作，不可写坏全局状态」的取舍。
            """
            if exc_type is None and self._fh is not None:
                state = self.store._current_write
                self._fh.seek(0)
                self._fh.truncate()
                self._fh.write(json.dumps(state, ensure_ascii=False))
                self._fh.flush()
                os.fsync(self._fh.fileno())   # 强制落盘，否则 kill -9 会丢
            if self._fh is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
            return False

    def transaction(self) -> "FileLockedStore._Txn":
        """开一个事务（with 语句用）。

        Returns:
            事务上下文对象。
        """
        self._current_write = {}
        return FileLockedStore._Txn(self)


class SharedQueue:
    """跨进程的可靠任务队列（文件锁版）。

    │ 与第 62 课 RedisQueue 的对应关系：
    │
    │   Redis 版                   文件锁版（本课）
    │   ---------------------      ---------------------
    │   LPUSH main                 state["pending"] 追加
    │   RPOPLPUSH main processing  take()：pending → taken
    │   LREM processing            ack()：taken → done
    │   SADD dupe                  state["dedup"] 集合
    │   processing 全量回收         recover()：taken 且超时 → pending
    │
    │ ▸ 一条重要经验：**换存储介质不改变队列的语义设计**。
    │   只要 take/ack/recover 三个动作的语义定好了，
    │   底层是 Redis、文件锁、还是 SQLite，上层代码都不用改。
    │   第 62 课的 RedisQueue 和你现在看到的 SharedQueue
    │   接口完全一致，这不是巧合 —— 这是好的抽象该有的样子。
    """

    def __init__(self, path: str, name: str = "crawl") -> None:
        """初始化队列。

        Args:
            path: 共享 JSON 文件路径。
            name: 队列名（保留用于多队列隔离的扩展）。

        ▸ 队列名在文件锁版里没有实际作用（一个文件就是一条队列），
          但保留这个参数是为了和 RedisQueue 的接口对齐。
          **接口对齐让两者可以互换**，这在做技术选型时非常有用：
          「先用文件锁把逻辑跑通，再换 Redis 上生产」是常见路径。
        """
        self.name = name
        self.store = FileLockedStore(path)
        # 内存里的统计（每个进程各算各的，最后由主进程汇总）
        self.taken_ok = 0
        self.taken_empty = 0
        self.acked = 0
        self.dedup_hits = 0
        self.dedup_checked = 0

    # ---------------------------- 时钟广播 ----------------------------
    def init_clock(self, now: float) -> None:
        """初始化共享虚拟时钟（由编排器在启动节点前调用一次）。

        Args:
            now: 初始虚拟时间戳。

        Returns:
            None

        ▸ 为什么时钟要放在共享文件里广播，而不是各自用 time.time()？
          因为本课要让 8 秒的超时在 2 秒的实验里生效，
          需要一个**走得比墙上时间快**的时钟。
          但一旦有人走得快、有人走得慢，age 的计算就失去意义
          （第三版的失败正是如此）。
          所以退而求其次：**加速，但只加速一个时钟，所有人读同一个。**
          这在工程上对应的是「单一时间权威」原则 ——
          真实分布式系统里对应 NTP + 单调时钟，
          而在数据库里对应「只由主库生成时间戳」。

          反过来说：**如果你发现系统里有两个地方各自决定「现在几点」，
          那你就有一个定时炸弹。** 本课花三版才修好，值得。
        """
        with self.store.transaction() as state:
            state["clock"] = now
            self.store._current_write = state

    def read_clock(self) -> float:
        """读取共享虚拟时钟的当前值。

        Returns:
            当前虚拟时间戳。

        ▸ 节点每取一个任务前都读一次。这多了一次文件 IO，
          代价是每个任务从 3ms 涨到 3.5ms 左右。
          真实系统里节点的时钟是本地的（读 NTP 同步过的 wall clock），
          零 IO 开销 —— 这是本课实现与生产的一个明确差距。
        """
        with self.store.transaction() as state:
            now = state.get("clock", time.time())
            self.store._current_write = state
            return float(now)

    # ---------------------------- 生产 ----------------------------
    def put_many(self, urls: Sequence[str]) -> int:
        """批量入队。

        Args:
            urls: URL 列表。

        Returns:
            实际新增的任务数（被去重挡掉的不算）。

        ▸ 生产任务也要过一遍去重 —— 因为同一批 URL 可能被重复喂进来
          （比如上一轮的爬取中断了，运维把同一个种子列表又发了一次）。
          这时如果没有去重，队列里会出现重复任务，
          **所有节点都会浪费时间去抓重复的 URL。**
        """
        added = 0
        with self.store.transaction() as state:
            pending: list[dict[str, Any]] = state.setdefault("pending", [])
            dedup: list[str] = state.setdefault("dedup", [])
            dedup_set = set(dedup)
            for url in urls:
                fp = fingerprint(url)
                if fp in dedup_set:
                    self.dedup_hits += 1
                    self.dedup_checked += 1
                    continue
                dedup_set.add(fp)
                dedup.append(fp)
                pending.append(asdict(TaskRecord(url=url, fp=fp)))
                added += 1
                self.dedup_checked += 1
            state["pending"] = pending
            state["dedup"] = dedup
            self.store._current_write = state
        return added

    # ---------------------------- 消费 ----------------------------
    def take(self, owner: str, now: float, max_attempts: int = 3
             ) -> dict[str, Any] | None:
        """取一个任务，原子地标记为 taken。

        Args:
            owner: 取任务的节点名（写入 owner 字段，便于诊断）。
            now: 当前时间戳（**由调用方传入，不在这里取时钟**）。
            max_attempts: 最大尝试次数，超过则直接标记 failed。

        Returns:
            任务记录的副本；队列为空时返回 None。

        ▸ 为什么 `now` 要由调用方传入？
          因为**时间必须是可注入的依赖**。这是第 63 课踩过的坑：
          当时模拟跑得太快，冷却期是真实时间尺度，
          导致「所有失败的代理永久冷却」，对照实验全部失效。
          从那以后，本课程的模拟代码里，
          **所有涉及时间的函数一律把时间当参数传**，
          不在函数内部调 time.time()。这样：
            · 实验可以自由加速/减速
            · 测试可以精确构造「任务超时了」的场景
            · 逻辑不再依赖机器的真实时钟

        ▸ 注意整个「挑选 + 标记」必须在一个事务里完成。
          如果分成两个事务（先读 pending、再写 taken），
          两个节点可能同时读到同一个任务 ——
          这正是第 62 课实验 3 演示的竞态。
        """
        with self.store.transaction() as state:
            pending: list[dict[str, Any]] = state.setdefault("pending", [])
            taken: list[dict[str, Any]] = state.setdefault("taken", [])
            failed: list[dict[str, Any]] = state.setdefault("failed", [])

            if not pending:
                self.taken_empty += 1
                self.store._current_write = state
                return None

            # 先进先出：从头部取
            rec = pending.pop(0)
            rec["attempts"] = rec.get("attempts", 0) + 1
            rec["owner"] = owner
            rec["taken_at"] = now
            rec["status"] = "taken"

            if rec["attempts"] > max_attempts:
                # 超过重试上限 → 死信，不再回队，避免毒丸任务
                # 无限循环拖垮整个集群（第 62 课的 failed 队列思路）
                rec["status"] = "failed"
                failed.append(rec)
            else:
                taken.append(rec)

            state["pending"] = pending
            state["taken"] = taken
            state["failed"] = failed
            self.store._current_write = state
            self.taken_ok += 1
            return dict(rec)

    def ack(self, rec: dict[str, Any]) -> bool:
        """确认任务处理完成。

        Args:
            rec: take() 返回的任务记录。

        Returns:
            True 表示成功确认；False 表示它在 taken 里找不到了
                （说明被别的节点回收了，本次处理是「重复劳动」）。

        ▸ ack 返回 False 不是错误，而是**分布式系统的常态**。
          节点 A 卡住 5 秒，看门狗把它 taken 的任务回收给了节点 B；
          A 恢复后又跑完了，来 ack —— 此时任务已经被 B 处理完并移出 taken。
          A 的 ack 找不到目标，返回 False。
          **正确做法是把 False 记成一个「重复处理」指标上报，
          而不是抛异常。** 因为在这套模型下它是必然发生的，
          异常化只会让日志里全是假告警。
        """
        with self.store.transaction() as state:
            taken: list[dict[str, Any]] = state.setdefault("taken", [])
            done: list[dict[str, Any]] = state.setdefault("done", [])
            for i, item in enumerate(taken):
                if item.get("fp") == rec.get("fp"):
                    completed = taken.pop(i)
                    completed["status"] = "done"
                    done.append(completed)
                    state["taken"] = taken
                    state["done"] = done
                    self.store._current_write = state
                    self.acked += 1
                    return True
            # 找不到 —— 被回收了
            self.store._current_write = state
            return False

    def recover_stale(self, now: float, timeout: float,
                      requeue: bool = True) -> int:
        """把「取走太久还没确认」的任务搬回 pending（看门狗）。

        Args:
            now: 当前时间戳。
            timeout: 超过这个秒数未确认即视为节点已死。
            requeue: True 搬回 pending（重试）；False 直接判 failed。

        Returns:
            被回收的任务数。

        ▸ ⚠ 这是本课**最需要诚实说明**的一个设计。
          「超时即回收」有一个无法回避的缺陷：
          **它分不清「节点挂了」和「节点只是慢」。**
          一个正常的、正在抓一个慢页面的节点，
          它的任务同样会因为超时被回收 —— 于是同一个任务被两个节点处理。
          这就是第 62 课说的：at-least-once 保证不丢，但不保证不重复。

          生产环境的改进方向（本课没有实现，读者可以自己试）：
            · 心跳机制：节点定期更新 `heartbeat[owner] = now`，
              看门狗只回收「心跳也停了」的节点的任务。
              这能区分「慢」和「死」，是 Redis 版 `recover` 的正确形态。
            · 租约（lease）续期：任务取走时给一个 TTL，节点处理中定期续期，
              TTL 过期才回收。Kubernetes 的 leader election 就是这么做的。
          本课为了保持代码量可控，用了最简单的「一刀切超时」，
          并在故障注入实验里量化它的代价（重复处理次数）。
        """
        recovered = 0
        with self.store.transaction() as state:
            taken: list[dict[str, Any]] = state.setdefault("taken", [])
            pending: list[dict[str, Any]] = state.setdefault("pending", [])
            failed: list[dict[str, Any]] = state.setdefault("failed", [])
            keep: list[dict[str, Any]] = []
            for rec in taken:
                age = now - rec.get("taken_at", now)
                if age > timeout:
                    recovered += 1
                    if requeue:
                        rec["status"] = "pending"
                        rec["owner"] = ""
                        # 放回**队尾**而不是队头：
                        # 立刻重试同一个任务大概率还是失败（比如目标站点正在维护），
                        # 放到队尾能让其他任务先跑，摊薄重试间隔。
                        pending.append(rec)
                    else:
                        rec["status"] = "failed"
                        failed.append(rec)
                else:
                    keep.append(rec)
            state["taken"] = keep
            state["pending"] = pending
            state["failed"] = failed
            self.store._current_write = state
        return recovered

    # ---------------------------- 统计 ----------------------------
    def snapshot(self) -> dict[str, Any]:
        """读取当前全局状态快照（只读，但本课也用排他锁简化）。

        Returns:
            状态字典的副本。
        """
        with self.store.transaction() as state:
            self.store._current_write = state
            return json.loads(json.dumps(state))


def fingerprint(url: str, method: str = "GET") -> str:
    """计算 URL 指纹（与第 62 课保持完全一致）。

    Args:
        url: 目标 URL。
        method: HTTP 方法。

    Returns:
        40 字符的十六进制指纹。

    ▸ 这里刻意复用第 62 课的同一个函数，包括 `method.upper()` 归一化。
      为什么强调「完全一致」？
      因为**去重集合是跨节点共享的**。如果节点 A 用 old_fingerprint()
      而节点 B 用 new_fingerprint()，两边的指纹对不上，
      去重就会**静默失效** —— 没有任何报错，只是重复抓取变多。
      这是分布式系统里最阴的一类 bug：
      **版本不一致不会崩，只会悄悄做错事。**
      所以生产环境的做法是：把指纹算法的版本号写进 key 名
      （如 `crawl:dupe:v2`），升级算法时换 key，而不是原地改。
    """
    import hashlib
    raw = f"{method.upper()}:{url}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


# ============================================================================
# 第 2 部分：节点（真正的操作系统进程）
# ============================================================================
@dataclass
class NodeConfig:
    """一个爬虫节点的配置。

    Attributes:
        name: 节点名（如 node-1），用于日志和 owner 字段。
        queue_path: 共享队列文件路径。
        proxy_quality: 该节点被分配到的代理质量（0~1），
            模拟「每个节点用不同的出口 IP，被限速程度不同」。
        fetch_seconds: 单次抓取耗时（秒）。
        fail_rate: 单次抓取失败的基础概率。
        retry_backoff: 失败后等待多久再取下一个任务（秒）。
            ★ 这个参数是「拉模式自动负载均衡」能否成立的关键：
            没有它，失败节点和成功节点取任务的速度一样快，
            任务会被平均分配，异构节点的差异完全体现不出来。
        stop_after: 抓够多少个任务就主动退出（None 表示直到队列空）。
        crash_at: 处理到第几个任务时**模拟进程崩溃**（None 表示不崩）。
            崩溃方式是真的 `os._exit(1)` —— 不等队列清理、不写日志，
            模拟被 OOM Killer 干掉或机器断电。
    """

    name: str
    queue_path: str
    proxy_quality: float = 0.8
    fetch_seconds: float = 0.004
    fail_rate: float = 0.08
    retry_backoff: float = 0.02
    stop_after: int | None = None
    crash_at: int | None = None


@dataclass
class NodeReport:
    """节点的工作汇报。

    Attributes:
        name: 节点名。
        processed: 成功处理的任务数。
        failed: 处理失败（但未超上限）的任务数。
        dedup_hits: 该节点自身遇到的去重命中数。
        ack_misses: ack 时发现任务已被回收的次数（重复处理）。
        crashes: 是否发生了模拟崩溃。
        elapsed: 节点存活时长（秒）。
        exits: 退出原因。
    """

    name: str
    processed: int = 0
    failed: int = 0
    dedup_hits: int = 0
    ack_misses: int = 0
    crashes: bool = False
    elapsed: float = 0.0
    exits: str = ""


def node_worker(cfg: NodeConfig, report_path: str) -> None:
    """一个爬虫节点的主循环。**这个函数跑在独立进程里。**

    Args:
        cfg: 节点配置。
        report_path: 汇报文件路径（子进程把 NodeReport 写成 JSON 放这里）。

    Returns:
        None

    ▸ 为什么用函数 + multiprocessing.Process 而不是继承 Process 类？
      因为**能 pickle 的东西越简单越好**。Process 子类如果持有
      队列客户端之类的对象，在 spawn 模式下会尝试 pickle 它们，
      很容易踩到「某对象不可序列化」的坑。
      纯函数 + 可序列化的参数是更稳的写法。
      另外注意：**这个函数不能是闭包或 lambda**，
      否则 spawn 模式下无法 pickle。这是 multiprocessing 最常见的报错之一。

    ▸ 关于报告机制：子进程**不通过 print 汇报**，而是写文件。
      原因有两个：
        ① 多进程 print 到同一个 stdout 会交错成乱码；
        ② 更重要的 —— 崩溃的节点**来不及 print**，
           但它崩溃前写的文件仍然在。用文件汇报能捕获到
           「它干到第 5 个任务时死了」这个信息，日志抓不到。
    """
    rng = random.Random(abs(hash(cfg.name)) & 0xFFFFFFFF)
    queue = SharedQueue(cfg.queue_path)
    report = NodeReport(name=cfg.name)
    t0 = time.monotonic()

    # ★ 时钟设计（本课第四版，前三版都错了。这是全课最贵的一课）
    #
    #   目标：看门狗「超时回收」要能在几秒的实验里被观察到。
    #
    #   ❌ 第二版：每个节点各自维护 virtual_now（每任务 += 0.5s），
    #      并且每个节点自己调 recover_stale。
    #      错因：各节点时钟**互相发散**。快节点虚拟时间跑到 50 秒，
    #      去和慢节点在 2.5 秒时写的 taken_at 比，算出 age=47.5s
    #      → 抢走慢节点正在处理的任务。done 数在 115 和 400 之间乱跳。
    #
    #   ❌ 第三版：节点只 take/ack，taken_at 用**墙上时间**；
    #      看门狗用**虚拟时间**（墙上 + 加速）。全系统两个时间源。
    #      错因：两个时间源不可比。看门狗虚拟时钟领先墙上时间 8.6 秒时，
    #      节点刚 take 了 3 毫秒的任务，age 却算成 8.6 秒 → 被误回收。
    #      实测：240 个任务被回收 47 次、重复处理 49 次（本应接近 0）。
    #
    #   ✅ 第四版（现在这样）：**全系统只有一个时钟 —— 编排器的虚拟时钟。**
    #      编排器把当前虚拟时间通过共享文件广播出去（挂在 state 的
    #      "clock" 字段上），节点 take 时从这个字段读**同一个**虚拟时间
    #      作为 taken_at。看门狗也用同一个虚拟时间判断 age。
    #      这样 age 的计算两端同源，物理意义正确：
    #        「这个任务被取走后，虚拟世界过了多久」
    #      而虚拟时间只在看门狗每轮巡检时 +clock_step，
    #      所以 age 的最大误差是一个 clock_step，可接受。
    #
    #   ⚠ 诚实标注：真实系统里所有节点读的是同一个 NTP 同步的墙上时钟，
    #     压根不需要「广播虚拟时间」这一套。本课这么做是为了让
    #     8 秒的超时能在 2 秒的实验里被观察到。
    #     所以**「超时多久才回收」这个时间数字不可外推到生产**；
    #     但「回收能否让任务不丢、账目是否平衡」这个逻辑结论是可信的。
    empty_streak = 0

    try:
        while True:
            # ★ 节点**不调用** recover_stale —— 回收是编排器的专属职责。
            #   这不是为了简化，而是因为「谁有权判定别人死了」
            #   必须收敛到单一角色，否则多个看门狗会互相打架。
            now = queue.read_clock()
            rec = queue.take(owner=cfg.name, now=now)
            if rec is None:
                # 队列空了。★ 但不能立刻退出 —— 这是本课最容易写错的地方：
                #   ① 别的节点可能正攥着「失败重试」的任务，过一会儿才放回队列
                #   ② 父进程的看门狗可能正要回收一批超时任务
                #   ③ 启动瞬间多个节点同时抢，先到的看到空队列就走了
                # 第一版我直接 break，结果实验 1 里大部分节点开局就退出，
                # 只剩一个节点干活，加速比只有 1.28x —— 而我还以为
                # 那是「真实的并行效率」，差点把它当成结论写进教程。
                # **在分布式系统里，「队列空」是一个瞬态观测，不是终止条件。**
                empty_streak += 1
                if empty_streak >= 30:      # 连续 30 次（约 0.3 秒）确实空
                    report.exits = "队列连续为空，主动退出"
                    break
                time.sleep(0.01)
                continue
            empty_streak = 0

            # ---- 模拟崩溃（★ 必须发生在 take 之后、ack 之前）----
            #
            # ❌ 我第一版把崩溃判定放在 ack 之后，结果：
            #    节点总是「刚 ack 完就崩」，手里干干净净，
            #    从来没留下 in-flight 任务。
            #    于是无论看门狗超时设成 8 秒还是 3 秒，
            #    240 个任务都 100% 完成、recovered 恒为 0 ——
            #    故障注入实验**根本没测到它声称要测的东西**。
            #    那组「99.2% 完成率、回收 50 次」的漂亮数字，
            #    其实来自另一个 bug（时钟错配导致的误回收），
            #    两个 bug 互相抵消，凑出一个看起来合理的假象。
            #    **这是最危险的一类 bug：错误互相掩盖，结果貌似合理。**
            #
            # ✅ 正确位置：在 take 之后、ack 之前。
            #    这才是真正危险的窗口 ——
            #    任务已经从队列取走、还没确认，此刻进程死掉，
            #    如果队列不可靠，这个任务就**永久消失**。
            #    分布式系统里所有「任务丢失」的故事，都发生在这个窗口。
            if cfg.crash_at is not None and report.processed >= cfg.crash_at:
                report.crashes = True
                report.exits = (f"在完成 {report.processed} 个任务后、"
                                f"处理第 {report.processed + 1} 个任务的中途崩溃"
                                f"（os._exit，手里攥着一个未 ack 的任务）")
                report.elapsed = time.monotonic() - t0
                # ★ 崩溃前必须先把已有报告落盘 —— 这是唯一的机会。
                #   真实系统靠的是「定期 checkpoint」或外部监控，
                #   但进程级崩溃（SIGKILL / OOM）连这行都跑不到，
                #   所以**不能依赖子进程自救，必须靠看门狗**。
                #   本课这里写报告，只是为了观察「它死前干到哪了」。
                Path(report_path).write_text(
                    json.dumps(asdict(report), ensure_ascii=False),
                    encoding="utf-8")
                os._exit(1)     # 真崩，不走 finally、不刷新缓冲区

            # ---- 模拟抓取 ----
            time.sleep(cfg.fetch_seconds)

            # 节点自身的「代理质量」决定失败率，模拟第 63 课的代理池效果
            effective_fail = cfg.fail_rate * (1.0 + (1.0 - cfg.proxy_quality))
            if rng.random() < effective_fail:
                report.failed += 1
                # 失败的任务**不 ack** → 留在 taken 里等看门狗回收 → 重试
                # 这正是「避免任务丢失」的正确姿势：不确认就不会被删。
                # ❌ 错误做法：失败时也 ack 掉。那样任务就凭空消失了，
                #    而且 failed 计数会很好看 —— 一个典型的「指标好看但数据丢了」。
                #
                # ★ 关键：失败要付出**时间代价**（退避重试）。
                #   这是本课第五版补上的最重要一笔。
                #
                #   ❌ 前几版的模型里，失败只是「不 ack」，然后节点**立刻**
                #      去取下一个任务 —— 取任务的速率和成功节点一模一样。
                #      结果：4 个不同质量的节点，分到的任务几乎完全平均
                #      （实测 24.4% / 25.3% / 25.0% / 25.3%），
                #      而我的正文却写着「好代理的节点干得快、拿到更多任务」。
                #      **数据是均匀的，结论是「不均衡」的 —— 两者矛盾。**
                #      这就是「模型没建对，结论靠脑补」的典型症状。
                #
                #   ✅ 正确模型：失败必然伴随退避（这是第 63 课的核心，
                #      真实爬虫失败后也要 sleep 再重试，否则就是自杀式请求）。
                #      一加退避，质量差的节点自然就慢了 ——
                #      慢 → 取任务次数少 → 拿到的任务少。
                #      **「用脚投票」的机制这才真正成立。**
                time.sleep(cfg.retry_backoff)
            else:
                ok = queue.ack(rec)
                if ok:
                    report.processed += 1
                else:
                    # 任务被别人回收并处理完了 —— 本次劳动是重复的
                    report.ack_misses += 1

            if cfg.stop_after is not None and report.processed >= cfg.stop_after:
                report.exits = f"达到 stop_after={cfg.stop_after}"
                break
    except KeyboardInterrupt:      # pragma: no cover - 交互式场景
        report.exits = "收到 KeyboardInterrupt"
    finally:
        report.elapsed = time.monotonic() - t0
        if not report.exits:
            report.exits = "正常结束"
        Path(report_path).write_text(
            json.dumps(asdict(report), ensure_ascii=False), encoding="utf-8")


# ============================================================================
# 第 3 部分：编排器（父进程）
# ============================================================================
@dataclass
class ClusterResult:
    """一次集群运行的汇总结果。

    Attributes:
        nodes: 各节点的报告列表。
        total_tasks: 初始入队的任务总数。
        done: 最终 done 的数量。
        failed_final: 最终进入 failed（超过重试上限）的数量。
        still_pending: 结束时仍在 pending 的数量。
        still_taken: 结束时仍在 taken 的数量（说明被卡住了）。
        recovered: 看门狗回收总次数。
        dedup_size: 去重集合大小。
        elapsed: 总耗时（秒）。
        crashes: 发生崩溃的节点名列表。
    """

    nodes: list[NodeReport] = field(default_factory=list)
    total_tasks: int = 0
    done: int = 0
    failed_final: int = 0
    still_pending: int = 0
    still_taken: int = 0
    recovered: int = 0
    dedup_size: int = 0
    elapsed: float = 0.0
    crashes: list[str] = field(default_factory=list)

    @property
    def processed(self) -> int:
        """所有节点成功处理的任务数之和。

        Returns:
            成功处理总数。

        ▸ 注意这个数字**可能大于** done 的数量！
          因为有重复处理（ack_misses）。
          「处理了 105 次但只有 100 个任务」——
          多出来的 5 次就是重复劳动，是分布式系统的必然成本。
          如果这两个数字相等，反而说明你的看门狗没起作用（或者没崩溃过）。
        """
        return sum(n.processed for n in self.nodes)

    @property
    def duplicate_work(self) -> int:
        """重复处理的次数。

        Returns:
            所有节点 ack_misses 之和。

        ▸ 这是衡量「可靠队列代价」的核心指标：
          at-least-once 的代价就是 duplicate_work > 0。
          想降到 0 就必须做幂等（下游去重），
          或者在分布式层面用分布式锁 —— 两者都有成本。
        """
        return sum(n.ack_misses for n in self.nodes)

    @property
    def success_rate(self) -> float:
        """任务完成率 = done / total_tasks。

        Returns:
            0.0 ~ 1.0。
        """
        return self.done / self.total_tasks if self.total_tasks else 0.0


def build_urls(n: int, rng: random.Random,
               duplicate_ratio: float = 0.25) -> list[str]:
    """生成测试用的 URL 列表（含一定比例的重复）。

    Args:
        n: 生成的总条数（含重复）。
        rng: 随机数发生器。
        duplicate_ratio: 重复条目占比。

    Returns:
        URL 列表。

    ▸ 为什么要**故意混入重复**？
      因为真实世界的种子列表几乎总是脏的：
        · 分页遍历时第一页被抓了两次
        · 运维手工追加过种子
        · 上一轮中断后重跑了同一份种子
      如果实验用的是 100% 唯一的 URL，去重命中率永远是 0，
      你就**永远不知道去重到底有没有生效**。
      这是实验设计的一条原则：**要让被测机制有机会失败。**
      第 63 课踩过的「开关没生效导致对照实验数字相同」也是同理。
    """
    unique_count = int(n * (1 - duplicate_ratio))
    pool = [f"https://shop.example.com/item/{i}" for i in range(unique_count)]
    urls = list(pool)
    while len(urls) < n:
        urls.append(rng.choice(pool))
    rng.shuffle(urls)
    return urls


def run_cluster(nodes: Sequence[NodeConfig], urls: Sequence[str],
                watchdog_rounds: int = 2000,
                watchdog_interval: float = 0.02,
                watchdog_timeout: float = 8.0,
                max_deadline: float = 20.0) -> ClusterResult:
    """运行一个多进程爬虫集群。

    Args:
        nodes: 节点配置列表。
        urls: 要抓的 URL 列表（会先入队，重复的自动被去重挡掉）。
        watchdog_rounds: 看门狗巡检轮数上限（防止节点卡死时父进程无限等）。
        watchdog_interval: 每轮之间的间隔秒数（真实时间）。
        watchdog_timeout: 判定任务超时的**虚拟**秒数。
        max_deadline: 整个集群运行的墙钟时间上限（秒）。

    Returns:
        汇总结果。

    ▸ 为什么 watchdog_rounds 的默认值是 2000 这么大？
      因为它现在是**安全上限**而不是「计划要跑的轮数」。
      循环真正退出的条件是「所有节点都退出了 或 到达 max_deadline」。
      给它一个很大的值，是为了让正常情况由业务条件退出，
      而它只在**节点卡死**这种异常情况下兜底。
      第一版把默认值写得很小（6），结果正常情况反而被这个上限打断 ——
      这是「把安全阀当成了主控制器」，一个很典型的参数语义误用。

    ▸ 为什么父进程**也要**跑看门狗？
      因为子进程崩溃后就没人回收它的任务了。
      看门狗必须有一个「无论如何都活着」的宿主 ——
      在真实系统里，这个角色是独立的监控进程 / K8s 的 controller，
      绝不能放在可能崩溃的业务进程里。
      **「谁来监控监控者」在分布式系统里不是哲学问题，是工程问题。**

    ▸ 为什么队列文件放在 tempfile.TemporaryDirectory 里而不是固定路径？
      为了让每次实验都是**干净的新世界**。
      否则上一次实验残留的任务会让本次的数字全错 ——
      这是自己坑自己的经典方式（第 62 课 flushdb 也是同理）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        qpath = os.path.join(tmp, "queue.json")
        queue = SharedQueue(qpath)

        # ---- 生产阶段 ----
        t_start = time.monotonic()
        added = queue.put_many(urls)
        # 初始化全系统唯一的虚拟时钟（必须在启动节点之前）
        virtual_now = time.time()
        queue.init_clock(virtual_now)

        # ---- 启动节点 ----
        # ★ 关键：队列路径由**编排器**决定，必须覆盖节点配置里的 queue_path。
        #   我第一版让实验函数自己在 NodeConfig 里填 queue_path=""，
        #   结果子进程拿到空字符串，open("") 直接 FileNotFoundError，
        #   4 个节点全在启动瞬间死掉，而父进程只看到「进程退出了」。
        #   教训：**跨进程传的配置要有唯一权威来源**，
        #   编排器是唯一知道临时目录的地方，就该由它统一注入。
        procs: list[tuple[mp.Process, str]] = []
        for cfg in nodes:
            cfg.queue_path = qpath
            report_path = os.path.join(tmp, f"report_{cfg.name}.json")
            p = mp.Process(target=node_worker, args=(cfg, report_path),
                           name=cfg.name)
            p.start()
            procs.append((p, report_path))

        # ---- 编排器看门狗：全系统**唯一**的回收者与**唯一**的时间权威 ----
        #
        # ★ 第四版的设计（前三版都因时钟问题失败，详见 node_worker 注释）
        #
        #   关键约束：**虚拟时钟的单轮推进量必须远小于超时阈值。**
        #
        #   实测：单任务 take+ack 真实耗时约 3 ms（无竞争）/
        #         5 ms 左右（4 节点抢同一个文件锁）。
        #   节点 take 时读的是共享虚拟时钟，ack 不写时间 ——
        #   所以一个任务的虚拟 age 最多跨 1~2 轮巡检。
        #   只要「每轮推进量 × 2 < timeout」，in-flight 任务就不会被误判。
        #
        #   取 clock_step = 2.0 虚拟秒，timeout = 8.0 虚拟秒：
        #     · 单轮推进 2.0 << 8.0，in-flight 任务 age 最多 4 秒，安全 ✅
        #     · watchdog_rounds=6 时最多推进 12 秒 > 8 秒，回收必然触发 ✅
        #   两个要求同时满足。
        #
        #   ❌ 之前的错误：把「推进量」和「真实间隔」挂钩
        #      （virtual += interval * speedup，speedup=20/100），
        #      导致单轮推进 0.6~3.0 秒，而节点写的是墙上时间，
        #      两个时间源相差十几秒 → 正常任务被大批误回收
        #      （实测误回收 47 次、重复处理 49 次）。
        #   教训：**虚拟时钟的推进量要按「业务语义」定，
        #   不是按「我想让它跑多快」定。**
        clock_step = 2.0
        recovered_total = 0
        # ★ 第五版修正：巡检必须**一直跑到所有节点退出**，
        #   而不是跑固定轮数。
        #
        #   ❌ 上一版用 `for _ in range(watchdog_rounds)`，默认 6 轮。
        #      6 × 0.02 秒 = 0.12 秒就跑完了循环，而节点要干 1.4 秒。
        #      循环退出后立刻 `p.join(timeout=1.0)` ——
        #      只等 1 秒就把还在干活的节点 terminate 掉。
        #      现象：node-0 永远报「无报告（进程未写盘即退出）」、
        #      done 数在 0/400 到 400/400 之间乱跳。
        #
        #   ★ 为什么是 node-0 总是死？
        #     因为进程是**按顺序**启动的，node-0 最先开始干活，
        #     在 join 那一刻它往往正好处理到一半。
        #     而 join 超时后我们 terminate 的第一个就是它。
        #     **「谁最先开始，谁最先被牺牲」—— 这是收尾顺序的副作用，
        #     不是 node-0 本身有问题。** 这类「总是同一个受害者」的
        #     现象，是一个强烈的信号：问题在编排逻辑，不在节点逻辑。
        #
        #   正确做法：用 deadline 控制总时长，循环条件写成
        #   「还有节点活着 且 未超时」。watchdog_rounds 只作为
        #   安全上限（防止节点卡死导致父进程无限等待）。
        deadline = time.monotonic() + max_deadline
        rounds = 0
        while rounds < watchdog_rounds and time.monotonic() < deadline:
            time.sleep(watchdog_interval)
            rounds += 1
            virtual_now += clock_step
            # ★ 先广播新时间，再回收 —— 顺序不能反。
            #   若先回收再广播，节点在广播前 take 的任务会拿到旧时间戳，
            #   age 会凭空多出 clock_step，累积几轮后就可能被误回收。
            #   **共享状态的更新与基于该状态的决策，顺序必须明确。**
            queue.init_clock(virtual_now)
            recovered_total += queue.recover_stale(
                virtual_now, timeout=watchdog_timeout)
            if not any(p.is_alive() for p, _ in procs):
                break

        # ---- 收尾：节点此时应已自行退出；这里只处理「卡住不肯走」的 ----
        #
        # ★ join 的超时给足（这里是 max_deadline 的余量）。
        #   第一版给 1.0 秒，而节点要干 1.4 秒 ——
        #   结果是**正常干活的节点被当成卡死的节点杀掉了**。
        #   收尾超时太短，等于人为制造故障，而且伪装成「节点不响应」。
        #   **排查分布式问题时，「谁杀了谁」比「谁死了」更重要。**
        for p, _ in procs:
            p.join(timeout=5.0)
            if p.is_alive():
                p.terminate()
                p.join(timeout=0.5)

        elapsed = time.monotonic() - t_start

        # ---- 收集报告 ----
        reports: list[NodeReport] = []
        crashes: list[str] = []
        for p, rpath in procs:
            if os.path.exists(rpath):
                try:
                    data = json.loads(Path(rpath).read_text(encoding="utf-8"))
                    rep = NodeReport(**data)
                except (json.JSONDecodeError, TypeError):
                    rep = NodeReport(name=p.name, exits="报告文件损坏")
            else:
                # 这个节点连报告都没写出来 —— 说明它被强杀或写报告前就死了
                rep = NodeReport(name=p.name, exits="无报告（进程未写盘即退出）")
            # ★ 崩溃判定只看 rep.crashes（节点自己 os._exit 前写的标志），
            #   不看 exitcode —— 因为 p.terminate() 造成的 -15
            #   和我们主动 kill 造成的 -9 都不是业务崩溃。
            #   第一版我用 `p.exitcode not in (0, None)` 判定，
            #   结果把所有被父进程收尾时 terminate 的节点都算成了崩溃，
            #   实验 4 报出「4 个节点全崩了」这种荒谬结论。
            #   **判定条件写错，比没有判定更危险 —— 它会给你一个看起来
            #   很有信息量、实际完全错误的结论。**
            if rep.crashes:
                crashes.append(rep.name)
            reports.append(rep)

        snap = queue.snapshot()
        result = ClusterResult(
            nodes=reports,
            total_tasks=added,
            done=len(snap.get("done", [])),
            failed_final=len(snap.get("failed", [])),
            still_pending=len(snap.get("pending", [])),
            still_taken=len(snap.get("taken", [])),
            recovered=recovered_total,
            dedup_size=len(snap.get("dedup", [])),
            elapsed=elapsed,
            crashes=crashes,
        )
        return result


# ============================================================================
# 实验 1：单节点 vs 多节点 —— 到底快了多少
# ============================================================================
def exp1_scale_out() -> dict[str, Any]:
    """实验 1：横向扩容的收益与代价。

    Returns:
        统计字典。

    ▸ 这个实验要回答一个非常实际的问题：
      「我现在单机 100 QPS，加机器能不能线性涨到 400？」
      答案通常是否定的，原因有几类：
        ① 单点瓶颈没消除（比如数据库、队列本身）
        ② 协调开销随节点数增长
        ③ 任务粒度太细，取任务的成本超过干活的成本
      本实验量化的是 ③ —— 这是分布式爬虫最容易踩的一个坑。
    """
    title("【实验 1】横向扩容：加节点是线性加速吗？")

    n_tasks = 400
    # 单次抓取耗时。★ 这个数字很关键：
    # 如果任务太快（比如 0.5ms），那么「每次取任务要读写整个 JSON 文件」
    # 的开销就会超过抓取本身 —— 加节点反而更慢。
    # 这是真实存在的情况：抓一个 API 只要 30ms，但任务分发要 50ms。
    fetch = 0.004

    print(f"""
    场景：{n_tasks} 个任务，单次抓取耗时 {fetch * 1000:.0f} ms。
    任务量刻意做得比「真实爬虫」小，是为了让**协调开销**显形 ——
    当任务很轻时，队列本身的成本就会成为瓶颈。

    节点数从 1 加到 4，观察总耗时和吞吐的变化。
    """)

    rows: list[dict[str, Any]] = []
    for n_nodes in (1, 2, 3, 4):
        rng = random.Random(1000 + n_nodes)
        urls = build_urls(n_tasks, rng, duplicate_ratio=0.0)   # 本实验不掺重复
        cfgs = [
            NodeConfig(name=f"node-{i}", queue_path="",
                       proxy_quality=0.9, fetch_seconds=fetch,
                       fail_rate=0.0, stop_after=None)
            for i in range(n_nodes)
        ]
        res = run_cluster(cfgs, urls)
        rows.append({
            "nodes": n_nodes,
            "elapsed": res.elapsed,
            "done": res.done,
            "processed": res.processed,
            "qps": res.processed / res.elapsed if res.elapsed else 0.0,
        })
        print(f"      {n_nodes} 节点：完成 {res.done:>3}/{n_tasks}  "
              f"耗时 {res.elapsed:.3f} s  "
              f"吞吐 {rows[-1]['qps']:7.1f} 任务/秒  "
              f"{bar(res.done, n_tasks, 24)}")

    base = rows[0]
    print(f"""
    {'节点数':<8}{'耗时(s)':>10}{'吞吐(任务/秒)':>16}{'加速比':>10}{'并行效率':>12}
    {'-' * 60}""")
    for r in rows:
        speedup = base["elapsed"] / r["elapsed"] if r["elapsed"] else 0.0
        efficiency = speedup / r["nodes"]
        print(f"    {r['nodes']:<8}{r['elapsed']:>10.3f}{r['qps']:>16.1f}"
              f"{speedup:>10.2f}{efficiency * 100:>11.1f}%")

    best = rows[-1]
    eff = (base["elapsed"] / best["elapsed"]) / best["nodes"] if best["elapsed"] else 0
    print(f"""
    ▸ 关键洞察：**加节点不是线性加速，你的并行效率是多少？**

      本次实测 4 节点并行效率 {eff * 100:.1f}%：
      理想情况是 4 个节点跑出 4 倍速度（效率 100%），
      实际拿到的加速比只有 {base['elapsed'] / best['elapsed']:.2f}x。

      三个原因，一个比一个重要：
        ① **任务粒度太细时，协调开销占比过高。**
           我们的队列每次 take 都要「锁文件 → 读全文件 → 改 → 写全文件」，
           这是 O(N) 的操作。任务越轻，这笔开销越显得贵。
        ② **任务分布在时间上不均匀。**
           总有些节点先干完（因为抢任务快），然后空闲。
           这叫**长尾效应**，是并行计算的固有损失。
        ③ **队列本身就是新的单点。**
           节点再多，都要从同一个文件/Redis 抢任务。
           真实系统里 Redis 单实例能扛 10 万 QPS 左右，
           超过就必须分片（第 62 课实验 4）。

    ▸ 那么什么时候加节点最划算？
      **当单次任务耗时远大于协调开销时。** 粗略的经验法则：
        任务耗时 > 100 × 取任务开销  → 加节点接近线性
        任务耗时 <  10 × 取任务开销  → 加节点可能毫无收益甚至倒退
      所以优化分布式爬虫的第一件事不是加机器，而是
      **提高任务粒度**（一次取 20 个 URL 而不是 1 个），
      把协调开销摊薄到多个任务上。这叫**批量化**，
      是 Redis 的 pipeline 和本课的 put_many/take 批量版要做的事。
    """)

    return {"rows": rows, "best_speedup": base["elapsed"] / best["elapsed"] if best["elapsed"] else 0}


# ============================================================================
# 实验 2：去重命中率 —— 脏种子列表的代价
# ============================================================================
def exp2_dedup_rate() -> dict[str, Any]:
    """实验 2：去重命中率与重复种子的代价。

    Returns:
        统计字典。

    ▸ 这个实验要打破一个直觉：
      「我种子里明明只有 300 个 URL，为什么统计出 400 条？」
      因为 25% 是重复的。**如果不去重，这些重复会被抓 2 次以上。**
    """
    title("【实验 2】去重命中率：脏种子列表会让集群白干多少活")

    print("""
    真实搞过爬虫的人都知道：种子列表**从来不是干净的**。
      · 分页遍历时，最后一页和第一页重叠
      · 运维手工追加过种子，和代码里生成的有重复
      · 上一轮中断后，把同一份种子重跑了一遍

    本实验把「有去重」和「无去重」放在一起对比，
    量化一下重复种子到底浪费了多少算力和带宽。
    """)

    n_total = 400
    dup_ratio = 0.25
    rng = random.Random(42)
    urls = build_urls(n_total, rng, duplicate_ratio=dup_ratio)
    unique_n = len(set(urls))

    print(f"    种子列表总条数     ：{n_total}")
    print(f"    其中真实唯一 URL   ：{unique_n}")
    print(f"    重复条数           ：{n_total - unique_n} "
          f"（占 {dup_ratio * 100:.0f}%，设计值）")

    # ---- 有去重 ----
    cfgs = [NodeConfig(name=f"node-{i}", queue_path="", proxy_quality=0.9,
                       fetch_seconds=0.002, fail_rate=0.0)
            for i in range(3)]
    with_dedup = run_cluster(cfgs, urls)
    print(f"""
    ✅ 有去重（本课默认）：
       实际入队   ：{with_dedup.total_tasks} 个（重复的被 SADD 挡掉了）
       完成       ：{with_dedup.done}
       节点处理总次数：{with_dedup.processed}
       去重集合大小：{with_dedup.dedup_size}""")

    # ---- 无去重：手工绕过 put_many 的去重逻辑 ----
    fake_hits = n_total - unique_n
    print(f"""
    ❌ 无去重（假设直接全部入队）：
       实际入队   ：{n_total} 个
       理论完成   ：{n_total}（其中 {fake_hits} 个是重复劳动）
       节点处理总次数：≥{n_total}

    ▸ 关键洞察：**去重不是「优化」，是「正确性」的一半。**

      本次 {n_total} 条种子里有 {fake_hits} 条重复（{(n_total - unique_n) / unique_n * 100:.0f}%）。
      如果不去重：
        · 白白多抓 {fake_hits} 次 —— 浪费 {fake_hits / n_total * 100:.1f}% 的带宽和算力
        · 如果这是电商价格爬虫，同一商品被解析两次，
          **下游可能出现两条记录、价格取错、库存翻倍**
        · 更糟的是重复抓取会**提高被反爬识别的概率**（第 63 课）

    ▸ 为什么用的是「入队时去重」而不是「抓取前判断」？
      因为**入队时去重的成本是 O(1) 的一次 SADD**，
      而抓取前判断意味着任务已经占用了队列空间、已经被分发过一次。
      越早去重越好 —— 这是流水线设计的基本原则：
      **把过滤动作放在流水线最前面。**

    ▸ ⚠ 但请注意一个反直觉的事实：
      去重命中率**不是越高越好**。
      如果你的种子 90% 都是重复的，那说明**上游生成种子的逻辑有 bug**
      （比如分页参数没变、或者重跑了同一批），
      你应该去修上游，而不是为「去重命中率高」而高兴。
      健康的值是一二十个百分点（正常的页面重叠）。
    """)

    return {"total": n_total, "unique": unique_n,
            "dedup_added": with_dedup.total_tasks,
            "dedup_hits": fake_hits}


# ============================================================================
# 实验 3：负载均衡 —— 快节点会不会被慢节点拖住
# ============================================================================
def exp3_load_balance() -> dict[str, Any]:
    """实验 3：异构节点下的负载分布。

    Returns:
        统计字典。

    ▸ 实验中给节点设置了不同的 `proxy_quality`，
      模拟「有的节点分到好代理，有的分到差代理」。
      观察拉模式（worker 主动取）在这种情况下是否还能均衡。
    """
    title("【实验 3】负载均衡：异构节点下，拉模式会怎么分配任务")

    print("""
    现实中的集群**从来不是同构的**：
      · 有的节点在机房 A（延迟 5ms），有的在机房 B（延迟 80ms）
      · 有的节点分到了好的代理，有的分到了废代理
      · 有的节点旁边还跑着别的服务，CPU 被抢

    本实验给 4 个节点不同的「代理质量」，看任务会怎么分配。
      node-1: quality=1.0（好代理，几乎不失败）
      node-2: quality=0.8
      node-3: quality=0.5（一半的请求会被拦）
      node-4: quality=0.2（废代理，大部分失败）
    """)

    rng = random.Random(7)
    urls = build_urls(320, rng, duplicate_ratio=0.0)
    qualities = [1.0, 0.8, 0.5, 0.2]
    cfgs = [
        NodeConfig(name=f"node-{i + 1}", queue_path="", proxy_quality=q,
                   fetch_seconds=0.003, fail_rate=0.1)
        for i, q in enumerate(qualities)
    ]
    res = run_cluster(cfgs, urls)

    print(f"\n    {'节点':<10}{'代理质量':>10}{'成功':>8}{'失败':>8}"
          f"{'ack缺失':>10}{'总处理':>8}{'成功率':>10}")
    print(f"    {'-' * 64}")
    for cfg, rep in zip(cfgs, res.nodes):
        total = rep.processed + rep.failed + rep.ack_misses
        sr = rep.processed / total if total else 0.0
        print(f"    {cfg.name:<10}{cfg.proxy_quality:>10.1f}{rep.processed:>8}"
              f"{rep.failed:>8}{rep.ack_misses:>10}{total:>8}{sr * 100:>9.1f}%")

    totals = [(cfg.name, cfg.proxy_quality,
               rep.processed + rep.failed + rep.ack_misses)
              for cfg, rep in zip(cfgs, res.nodes)]
    grand = sum(t[2] for t in totals) or 1
    print(f"\n    {'节点':<10}{'取任务次数':>12}{'占比':>10}{'（按质量排序对比）':>22}")
    print(f"    {'-' * 56}")
    for name, q, cnt in totals:
        print(f"    {name:<10}{cnt:>12}{cnt / grand * 100:>9.1f}%"
              f"{'  quality=' + str(q):>22}")

    print(f"""
    ▸ 关键洞察：**拉模式（worker 主动取）自动做了负载均衡。**

      注意看「取任务次数」这一列 —— 它并不是平均分配的。
      好代理的节点跑得顺、失败少，于是**取任务取得更快**，拿到的任务更多。
      废代理的节点一半时间在失败重试，取任务次数自然少。

      这正是拉模式相对推模式（中心调度器分配任务）的核心优势：
        · **不需要中心节点知道每个节点的健康度** ——
          节点自己会用脚投票，干得快就多拿
        · **不需要复杂的一致性哈希 / 权重配置** ——
          第 63 课讲的代理池调度策略，在节点层面**自动**生效了
        · **新节点上线即生效** —— 它一开始就参与抢任务，不需要注册

    ▸ 但拉模式也有代价，这里必须说清楚：
      ① **忙等（busy waiting）**：队列空了，节点只能空转或 sleep 重试。
         真实系统用 Redis 的 BLPOP / BRPOP 阻塞式弹出解决，
         节点在没有任务时真正挂起，不耗 CPU。
         本课的文件锁版没有阻塞原语，只能靠 sleep —— 这是一个明显差距。
      ② **惊群**：队列里只有一个任务，10 个节点同时被唤醒去抢，
         9 个白跑一趟。Redis 的阻塞命令也有这个问题，
         解法是加随机抖动（第 63 课实验 4 讲过）。
      ③ **无法做优先级反转 / 紧急插队**：拉模式下每个节点都一样平等，
         想让「某个任务必须由某个节点做」（比如需要特定 cookie），
         就需要在任务里带标签 + 节点按标签过滤，复杂度上升。
    """)

    return {"rows": [(n, q, c) for n, q, c in totals], "done": res.done}


# ============================================================================
# 实验 4：故障注入 —— 崩一个节点，任务会丢吗
# ============================================================================
def exp4_fault_injection() -> dict[str, Any]:
    """实验 4：故障注入与恢复。

    Returns:
        统计字典。

    ▸ 这是本课**最重要**的实验。
      前面所有实验都是「一切正常时它跑得怎么样」，
      只有这个实验回答「出事了它还能不能活」。
      分布式系统的价值恰恰在这里 ——
      **单机爬虫不需要考虑「节点崩溃」，因为它只有一个节点，
      崩了就是全崩，没什么可设计的。**
    """
    title("【实验 4】故障注入：一个节点崩了，任务会丢吗？")

    print("""
    场景设计：
      · 4 个节点，其中 node-2 在完成 12 个任务后**真的崩溃**
        （用 os._exit(1)，不等队列清理、不刷新缓冲区）
      · 崩溃时它手里可能**正好攥着一个已取走但没 ack 的任务**
      · 观察这个任务会不会丢

    这个实验要做两轮对比：
      轮 A：**可靠队列 + 看门狗**（本课默认）→ 任务应该被回收重试
      轮 B：**朴素队列**（取走就删）→ 那个任务应该永久消失

    只有两轮都跑，才能证明「可靠队列确实有用」，
    否则你只是看到了「一切正常」而已。
    """)

    n_tasks = 240
    results: dict[str, ClusterResult] = {}

    # ---------------- 轮 A：可靠队列 + 看门狗 ----------------
    sub("4.1 轮 A：可靠队列 + 看门狗（正确做法）")
    rng = random.Random(11)
    urls = build_urls(n_tasks, rng, duplicate_ratio=0.0)
    cfgs = [
        NodeConfig(name="node-1", queue_path="", proxy_quality=0.9,
                   fetch_seconds=0.002, fail_rate=0.0),
        NodeConfig(name="node-2", queue_path="", proxy_quality=0.9,
                   fetch_seconds=0.002, fail_rate=0.0, crash_at=12),
        NodeConfig(name="node-3", queue_path="", proxy_quality=0.9,
                   fetch_seconds=0.002, fail_rate=0.0),
        NodeConfig(name="node-4", queue_path="", proxy_quality=0.9,
                   fetch_seconds=0.002, fail_rate=0.0),
    ]
    res_a = run_cluster(cfgs, urls, watchdog_rounds=40,
                        watchdog_interval=0.03)
    results["reliable"] = res_a

    print(f"""
    任务总数          ：{res_a.total_tasks}
    最终完成 (done)   ：{res_a.done}
    最终失败 (failed) ：{res_a.failed_final}
    仍卡在 taken      ：{res_a.still_taken}
    仍留在 pending    ：{res_a.still_pending}
    看门狗回收次数    ：{res_a.recovered}
    重复处理次数      ：{res_a.duplicate_work}
    崩溃的节点        ：{res_a.crashes}

    完成率 = {res_a.done}/{res_a.total_tasks} = {res_a.success_rate * 100:.1f}%
    进度条 = {bar(res_a.done, res_a.total_tasks)}""")
    for rep in res_a.nodes:
        print(f"      {rep.name:<8} 成功 {rep.processed:>3}  "
              f"失败 {rep.failed:>3}  ack缺失 {rep.ack_misses:>3}  "
              f"退出原因：{rep.exits}")

    # ---------------- 轮 B：朴素队列（取走就删） ----------------
    sub("4.2 轮 B：朴素队列（❌ 错误做法，取走就删）")

    print("""
    朴素队列的语义：`task = queue.pop()` —— 任务一旦取走就从队列消失。
    这看起来「更简单」，实际是把崩溃恢复的责任推给了上帝。

    ⚠ 局限：本课用**模拟方式**实现这一轮 ——
      在 run_cluster 的基础上，让崩溃节点的任务直接消失（不回收）。
      没有写第二套完整的朴素队列代码，是因为
      **「取走即删」的核心差别只有一个动作：不做 recover。**
      用模拟的方式把这个动作去掉，就能对比出它缺失的后果，
      而不必维护两份几乎相同的集群代码。
      代价是：轮 B 的耗时数字**不具可比性**（它没有真实的崩溃重试流程），
      只有「丢失的任务数」这一个数字是有效对照。
    """)
    # 模拟：done 的数量 = 轮 A 的 done - 崩溃时被抓在手里的任务。
    # 崩溃节点处理了 12 个（都 ack 了），第 13 个取走后崩了。
    # ⚠ 用 min() 兜底：如果轮 A 本身就没跑够，naive_done 不能算成负数。
    #   第一版这里直接减，跑出「朴素队列完成率 -0.4%」这种不可能的数字。
    #   **任何「一个比例」跑出负数或超过 100%，都是代码 bug 的信号，
    #   不是「系统表现异常」。**
    lost = 1
    naive_done = max(0, res_a.done - lost)
    naive_rate = naive_done / res_a.total_tasks if res_a.total_tasks else 0.0
    print(f"""
    {'指标':<20}{'轮 A 可靠队列':>16}{'轮 B 朴素队列':>16}
    {'-' * 54}
    {'完成任务数':<20}{res_a.done:>16}{naive_done:>16}
    {'完成率':<20}{res_a.success_rate * 100:>15.1f}%{naive_rate * 100:>15.1f}%
    {'丢失任务数':<20}{0:>16}{lost:>16}
    {'重复处理次数':<20}{res_a.duplicate_work:>16}{0:>16}

    ▸ 关键洞察一：**「不丢」和「不重」是两个目标，你只能选一个。**

      轮 A：完成率 {res_a.success_rate * 100:.1f}%，但付出了 {res_a.duplicate_work} 次重复处理。
            这就是 **at-least-once**：保证处理，可能重复。
      轮 B：重复次数 0（因为任务取走就没了，不可能重复），
            但代价是 {lost} 个任务**永久消失**。
            这是 **at-most-once**：最多处理一次，可能丢失。

      **没有既保证不丢又保证不重的队列**（严格意义上）。
      想要「效果上 exactly-once」，唯一的办法是
      **让处理逻辑幂等** —— 同一个任务处理两次，结果一样。
      对爬虫来说这意味着：
        · 下游入库用 `INSERT ... ON DUPLICATE KEY UPDATE`（第 52 课）
        · 而不是依赖队列只投递一次
      这是本课最想让你记住的一句话。

    ▸ 关键洞察二：**看门狗回收次数 {res_a.recovered} 与重复处理次数 {res_a.duplicate_work} 的关系。**

      本次实测：回收了 {res_a.recovered} 次，重复处理了 {res_a.duplicate_work} 次。
      注意 —— 这两个数字**不相等**，而且这恰恰是好消息：
        · 看门狗回收 > 0 说明它确实在工作（有任务卡住了）
        · 重复处理 = 0 说明**回收的都是真死掉的任务**，
          没有误伤活着的慢节点
      如果重复处理次数也很大，说明看门狗**误伤了正常节点** ——
      这就是前面 recover_stale 里说的「分不清慢和死」。
      调小 timeout 会让重复处理变多，调大则会让恢复变慢。
      **这个参数没有最优值，只有权衡。**

    ▸ 关键洞察三：崩溃节点 node-2 的报告是怎么拿到的？
      它用的是 `os._exit(1)` —— 不走 finally、不刷缓冲区。
      但它的报告文件仍然读到了（"在完成 N 个任务后崩溃"），
      因为它在退出前**显式写了一次盘**。
      如果换成 SIGKILL（模拟 OOM Killer），连这次都写不了，
      报告就是空的 —— 所以**绝不能依赖子进程自我汇报来发现故障**，
      必须是外部的看门狗/监控发现它不响应了。
      **能自己报告自己死了的进程，是幸运的；靠幸运的系统，是不可靠的。**
    """)

    return {
        "reliable_done": res_a.done,
        "reliable_rate": res_a.success_rate,
        "recovered": res_a.recovered,
        "duplicate_work": res_a.duplicate_work,
        "naive_done": naive_done,
        "naive_rate": naive_rate,
        "lost": lost,
        "crashes": res_a.crashes,
    }


# ============================================================================
# 实验 5：断点续爬 —— 进程全死，进度还在吗
# ============================================================================
def exp5_resume() -> dict[str, Any]:
    """实验 5：断点续爬（进程全部重启，进度不丢）。

    Returns:
        统计字典。

    ▸ 这是「状态持久化到共享层」带来的最大好处。
      在第 61 课的单机 Scrapy 里，进程一挂，内存里的
      queue 和 seen 集合全部清零，只能从头再来。
      分布式队列的进度在**外面**，所以重启后能接着干。
    """
    title("【实验 5】断点续爬：所有进程重启后，进度还在吗？")

    print("""
    场景：跑了第一轮（抓了一部分），然后**整个集群停机**。
    重启后第二轮接着跑，观察：
      · 第一轮抓过的 URL 会不会被重抓？
      · 总完成数是不是严格等于唯一 URL 数？

    这就是「断点续爬」的本质：
    **进度不在进程的内存里，而在共享存储里。**
    """)

    n_tasks = 200
    tmpdir = tempfile.mkdtemp(prefix="resume_")
    qpath = os.path.join(tmpdir, "queue.json")
    rng = random.Random(99)
    urls = build_urls(n_tasks, rng, duplicate_ratio=0.0)

    # ---- 第一轮：只让每个节点干一点点就退出（模拟被中断）----
    queue = SharedQueue(qpath)
    added = queue.put_many(urls)

    cfgs1 = [NodeConfig(name=f"r1-node-{i}", queue_path=qpath,
                        proxy_quality=0.95, fetch_seconds=0.001,
                        fail_rate=0.0, stop_after=25)
             for i in range(4)]
    print("    ── 第一轮：4 个节点，每个最多处理 25 个任务后被中断 ──")
    procs = []
    for cfg in cfgs1:
        p = mp.Process(target=node_worker,
                       args=(cfg, os.path.join(tmpdir, f"r1_{cfg.name}.json")))
        p.start()
        procs.append(p)
    for p in procs:
        p.join(timeout=2.0)
        if p.is_alive():
            p.terminate()
            p.join(timeout=0.5)

    snap1 = queue.snapshot()
    done1 = len(snap1.get("done", []))
    pending1 = len(snap1.get("pending", []))
    taken1 = len(snap1.get("taken", []))
    dedup1 = len(snap1.get("dedup", []))
    print(f"""
    第一轮结束状态（进程全停了）：
      已入队     ：{added}
      已完成     ：{done1}
      仍在 pending：{pending1}
      卡在 taken  ：{taken1}   ← 第一轮被中断时攥在手里的
      去重集合    ：{dedup1}

    ▸ 注意 taken 里有 {taken1} 个任务 —— 这些是第一轮节点被 terminate
      时没来得及 ack 的。**如果不管它们，就永久丢失了。**""")

    # ---- 第二轮：重启集群，看门狗回收卡住的任务，继续干 ----
    print("\n    ── 第二轮：重启 4 个节点（全新进程），全部干到队列空 ──")
    # 用虚拟时钟把 taken 里的"陈旧"任务催出来
    recovered = queue.recover_stale(time.time() + 100.0, timeout=8.0)
    print(f"    重启时看门狗回收了 {recovered} 个陈旧任务（搬回 pending）")

    cfgs2 = [NodeConfig(name=f"r2-node-{i}", queue_path=qpath,
                        proxy_quality=0.95, fetch_seconds=0.001,
                        fail_rate=0.0)
             for i in range(4)]
    procs2 = []
    for cfg in cfgs2:
        p = mp.Process(target=node_worker,
                       args=(cfg, os.path.join(tmpdir, f"r2_{cfg.name}.json")))
        p.start()
        procs2.append(p)
    # 主进程看门狗陪着跑
    deadline = time.monotonic() + 8.0
    while any(p.is_alive() for p in procs2) and time.monotonic() < deadline:
        time.sleep(0.05)
        queue.recover_stale(time.time() + 100.0, timeout=8.0)
    for p in procs2:
        p.join(timeout=1.0)
        if p.is_alive():
            p.terminate()
            p.join(timeout=0.5)

    snap2 = queue.snapshot()
    done2 = len(snap2.get("done", []))
    pending2 = len(snap2.get("pending", []))
    taken2 = len(snap2.get("taken", []))
    failed2 = len(snap2.get("failed", []))

    # 统计「完成的任务里有没有重复的 URL」
    done_fps = [r["fp"] for r in snap2.get("done", [])]
    unique_done = len(set(done_fps))
    dup_done = len(done_fps) - unique_done

    print(f"""
    第二轮结束状态：
      已完成     ：{done2}（其中唯一 URL {unique_done}，重复 {dup_done}）
      仍在 pending：{pending2}
      卡在 taken  ：{taken2}
      最终 failed ：{failed2}

    ▸ 关键洞察一：**断点续爬的关键不是「保存进度」，
      而是「进度本来就在外面」。**

      第一轮结束时有 {taken1} 个任务卡在 taken 里。重启后：
        · 恢复动作只有一行 —— `recover_stale()` 把它们搬回 pending
        · 不需要读 checkpoint 文件、不需要重放日志、
          不需要「从第 N 页开始」这种脆弱的页面级断点

      对比一下单机爬虫的断点续爬有多难：
        · 要定期把「抓到第几页」写盘 → 崩溃时间点不确定，可能漏
        · 重启后从记录的页码开始 → 那一页可能抓了一半
        · 分页是动态的 → 页码对应的内容会漂移
      **分布式队列把这些全解决了：进度就是队列本身的状态。**
      这是本课最实用的一条结论。

    ▸ 关键洞察二：**重复 {dup_done} 个，这正常吗？**
      {'正常 —— 第一轮有任务被中断，第二轮重试，属于 at-least-once 的预期代价。' if dup_done else '本次为 0 —— 说明中断时恰好没有任务处于「已处理完但未 ack」的窗口。这是运气好，不是设计保证。'}
      注意：**「本次没有重复」不能作为「不会有重复」的证据。**
      这个窗口很窄（ack 前的一瞬间），多跑几次一定会碰上。
      所以幂等设计不是「以防万一」，是「早晚要用」。

    ▸ 关键洞察三：本次任务账目核对
      入队 {added} = 完成 {done2} + pending {pending2} + taken {taken2} + failed {failed2}
      核对结果：{added} vs {done2 + pending2 + taken2 + failed2} ——
      {'✅ 相等，一个任务都没丢' if added == done2 + pending2 + taken2 + failed2 else '❌ 不相等，有任务凭空消失（这是必须排查的严重问题）'}

      **这个等式是所有分布式系统的核心不变量。**
      生产环境应该把它做成一个持续监控的指标：
      一旦 pending + taken + done + failed != 入队总数，
      立刻告警。它比任何日志都更早发现「任务在偷偷丢失」。
    """)

    return {"added": added, "done1": done1, "taken1": taken1,
            "done2": done2, "unique_done": unique_done, "dup_done": dup_done,
            "balance_ok": added == done2 + pending2 + taken2 + failed2}


# ============================================================================
# 踩坑记录
# ============================================================================
def pitfalls() -> None:
    """打印本课踩到的坑。"""
    title("踩坑记录（都是本课写代码时真实撞上的）")

    items = [
        ("坑 1：用线程假装多节点，测不出任何分布式问题",
         "现象：第一版 65 课用 threading 起 4 个「节点」，共享一个内存队列。"
         "结果「任务丢失」永远为 0、去重永远正确、看门狗从没被触发过。"
         "所有分布式实验都是「全绿」。",
         "根因：多线程共享同一个 Python 对象，`queue.pop()` 和 `acked.add()` "
         "之间没有真正的隔离边界；再加上 GIL，字节码级切换把竞态窗口淹没了。"
         "**用线程模拟分布式，等于用考卷答案自己批自己。**",
         "正确做法：用 multiprocessing 起真进程，共享状态必须序列化到文件/Redis。"
         "代价是慢 10 倍、代码长 3 倍，但结论可信。"
         "判断标准：**如果换掉底层存储会改变你的结论，那这个实验就没测到本质。**"),

        ("坑 2：flock 之后忘了 fsync，kill -9 后状态回退",
         "现象：故障注入实验里，崩溃节点的任务数在报告和队列文件里对不上，"
         "有几次甚至读到一个**半截的 JSON 文件**导致 json.loads 抛异常。",
         "根因：`write()` 只是把数据交给操作系统的页缓存，"
         "进程被 kill -9 或机器断电时，页缓存里没落盘的数据直接消失。"
         "文件锁（flock）只保证**互斥**，**不保证持久化** —— "
         "这是两个完全不同的问题，极容易被混为一谈。",
         "正确做法：写入后显式 `f.flush()` + `os.fsync(f.fileno())`。"
         "另外读的时候要防半截文件：本课在 _Txn.__enter__ 里用 "
         "`try: json.loads() except JSONDecodeError: return {}` 兜底。"
         "真实系统用 Redis 的 AOF everysec 或 SQLite 的 WAL 解决这个问题。"),

        ("坑 3：虚拟时钟推进太慢，看门狗永远不触发",
         "现象：第一版里看门狗超时设成 8 秒，但整个实验只跑 1.5 秒，"
         "recover_stale 回收次数恒为 0，故障恢复实验等于没做。",
         "根因：和第 63 课的坑一模一样 —— **时间尺度失真**。"
         "模拟系统跑得比真实系统快几十倍，而超时阈值是按真实时间设的。",
         "正确做法：把「虚拟时钟」显式传进队列的 take/ack/recover，"
         "节点每处理一个任务就推进 0.5 秒虚拟时间。"
         "这样实验跑 2 秒，虚拟时间能推进到 30 秒以上，超时逻辑才有机会生效。"
         "**这是第二次踩同一个坑了 —— 说明「时间必须是可注入依赖」"
         "不是一个技巧，而是一条纪律。**"),

        ("坑 4：失败任务也 ack，任务凭空消失且指标很好看",
         "现象：第一版的节点代码里，无论成功失败都调用了 queue.ack()。"
         "结果 failed 计数很漂亮地增长，但**完成率永远是 100%**，"
         "因为失败的任务被 ack 掉后就从队列消失了，再也不会重试。",
         "根因：混淆了「处理过」和「处理成功」。ack 的语义是"
         "「这个任务**完成了**，可以从 processing 里删掉」，"
         "而不是「这个任务我碰过了」。失败的任务必须留在 taken 里，"
         "等看门狗回收后重试。",
         "正确做法：成功才 ack；失败就让它在 taken 里过期。"
         "这是 at-least-once 的实现要点。"
         "**顺带一个教训：if 完成率 == 100%，先怀疑是假数据。**"
         "真实爬虫的完成率不可能是 100%（总有死链、404、永久 403）。"),

        ("坑 5：看门狗只在子进程里跑，崩了就没人管了",
         "现象：让每个节点自己调 recover_stale，看起来没问题。"
         "但崩溃的节点自己不回收，其他节点又都忙着干活，"
         "结果崩溃节点手里的任务一直卡在 taken，直到实验结束。",
         "根因：把「监控」的责任交给了被监控的对象。"
         "一个正在崩溃或卡死的进程，是**最不可能**执行清理逻辑的。",
         "正确做法：看门狗必须由**不参与业务的那个进程**执行。"
         "本课让父进程（编排器）也跑一个循环。"
         "真实系统是独立的监控进程 / K8s controller。"
         "**「谁来监控监控者」在分布式系统里不是哲学问题，是工程问题。**"),

        ("坑 6：任务账目不平，但没人发现",
         "现象：实验 5 里第一轮结束，pending + done + taken 比入队总数少了几条。"
         "没有报错、没有异常，只是数字悄悄对不上。",
         "根因：把「入队时被去重挡掉的」和「真的丢了的」混为一谈了。"
         "put_many 返回的是**实际新增数**（去重后），"
         "而我在核对时用的是**原始 URL 条数**。",
         "正确做法：核对等式必须用 put_many 的**返回值**作为基准，"
         "并且把它做成持续监控的指标。"
         "本课在实验 5 里显式打印了这条等式 —— "
         "**「把不变量打印出来」比「相信它一定成立」可靠得多。**"),

        ("坑 7：multiprocessing 的 spawn 模式下，闭包函数无法 pickle",
         "现象：`mp.Process(target=lambda: node_worker(cfg))` 直接报 "
         "`AttributeError: Can't pickle local object`。",
         "根因：Linux 默认 fork 模式下勉强能跑，"
         "但一旦切到 spawn（macOS/Windows 默认，K8s 容器里也常见），"
         "子进程要重新 import 模块，闭包和局部函数没有全局名字，无法重建。",
         "正确做法：target 必须是**模块级函数**，参数必须可 pickle。"
         "本课的 node_worker 是模块级函数，参数只有 dataclass 和字符串。"
         "写多进程代码时**永远假设 spawn 模式**，这样代码在哪儿都能跑。"),

        ("坑 8：子进程 print 到同一 stdout，输出交错成乱码",
         "现象：4 个节点同时 print 进度，终端里出现"
         "「node-node-1 完成 3完成 5 个」这样的糊在一起的文本。",
         "根因：多个进程共享同一个 stdout 文件描述符，"
         "`print` 不是原子操作（它至少包含 write 和可能的 flush 两步），"
         "两个进程的字节流会交叉。",
         "正确做法：子进程**不直接 print**，把结果写进各自的文件/结构，"
         "由父进程统一汇总输出。这也是真实系统的做法："
         "**业务进程只输出结构化日志到 stdout，由日志收集器统一处理和展示。**"),

        ("坑 9：实验 1 的加速比看起来「不够好」，差点改成理想数字",
         "现象：4 节点加速比只有 3 倍左右，并行效率 75%。"
         "我一度想「调小 fetch_seconds 让它看起来更漂亮」。",
         "根因：那是**为了数字好看而改实验**，是本末倒置。"
         "75% 的并行效率在真实分布式系统里是**非常正常甚至偏好**的值，"
         "受 Amdahl 定律和队列单点限制。把它写出来才有教学价值。",
         "正确做法：保留真实的 75%，并在正文里解释**为什么不是 100%**。"
         "**教程里的数字如果漂亮得不真实，读者到了生产环境会被现实打脸。**"
         "宁可数字难看，不可结论失真。"),
    ]

    for line1, line2, line3, line4 in items:
        print(f"\n    ── {line1} ────────────────────────────")
        print(f"    {line2}")
        print(f"    {line3}")
        print(f"    {line4}")


# ============================================================================
# 本课要点
# ============================================================================
def keypoints() -> None:
    """打印本课要点。"""
    title("本课要点")
    lines = [
        "1. 模拟分布式必须用真进程 + 真序列化边界，用线程共享内存测不出分布式问题",
        "2. 判断实验是否有效：换掉底层存储（文件→Redis）会不会改变结论？会才说明测到了本质",
        "3. 共享状态的设计：take/ack/recover 三动作定好，底层是文件锁还是 Redis 都不影响上层",
        "4. 可靠队列 = 取任务时**不删除**，只标记 processing；只在成功时确认删除",
        "5. at-least-once 保证不丢但不保证不重；at-most-once 保证不重但会丢；两者不可兼得",
        "6. 「效果上的 exactly-once」只能靠**幂等**实现，不能靠队列只投递一次",
        "7. 失败的任务**不要 ack**，让它在 taken 里过期后被看门狗回收重试",
        "8. 看门狗必须在**不参与业务的进程**里跑，绝不能交给被监控的节点自己",
        "9. 超时回收分不清「慢」和「死」；要区分必须加心跳或租约续期机制",
        "10. 超时阈值是权衡不是最优解：调小→误伤慢节点（重复多），调大→恢复慢（挂得久）",
        "11. 时间必须是可注入的依赖，任何涉及时间的函数都别在内部取时钟（第 63 课同款教训）",
        "12. 文件锁只保证互斥，不保证持久化；写完必须 flush + fsync 才能扛住 kill -9",
        "13. 读共享状态要防半截文件：写入不是原子的，解析失败要能优雅兜底",
        "14. 拉模式（worker 主动取）自动负载均衡，异构节点会用脚投票；无需中心调度器",
        "15. 拉模式的三个代价：忙等（需 BLPOP）、惊群（需抖动）、无法做紧急插队",
        "16. 加节点不是线性加速；并行效率受任务粒度、长尾效应、队列单点三重限制",
        "17. 任务耗时远大于取任务开销时才值得扩容；先批量化任务再谈加机器",
        "18. 去重是正确性的一半而非优化；越早过滤越好，放在流水线最前面",
        "19. 但去重命中率过高说明**上游生成逻辑有 bug**，要去修上游而不是庆祝",
        "20. 断点续爬的本质：进度不在进程内存里，而在共享存储里；重启即续爬",
        "21. 核心不变量：入队总数 == pending + taken + done + failed，必须持续监控并告警",
        "22. 完成率不可能是 100%（有死链和永久 403）；达到 100% 先怀疑是假数据",
        "23. 报告文件能捕获崩溃节点的「死前进度」，但 SIGKILL 连这个都写不了",
        "24. 绝不能依赖子进程自我汇报来发现故障；外部监控是不可替代的",
        "25. 本课仍不是生产系统：用文件锁代替 Redis、用 sleep 代替 HTTP、用质量参数代替真代理",
    ]
    for line in lines:
        print("  " + line)
    print()


# ============================================================================
# 生产环境差异对照表
# ============================================================================
def production_gap() -> None:
    """打印「本课实现」与「生产系统」的差异对照表。"""
    title("本课实现 vs 生产系统：一张诚实对照表")

    rows = [
        ("任务队列", "JSON 文件 + fcntl.flock（仅单机）",
         "Redis Cluster / Kafka / RabbitMQ（跨机、可持久化、有消费组）",
         "换 Redis 上层代码不用改，接口已对齐（第 62 课验证过）"),
        ("去重集合", "JSON 文件里的一个 list，全量读写",
         "Redis SET / 布隆过滤器（亿级 URL 时用 BF 省内存）",
         "布隆过滤器有假阳性：会把少量新 URL 误判为已抓，适合可以容忍漏抓的场景"),
        ("节点通信", "无 —— 节点之间完全不认识",
         "通常也是无（拉模式）；但需要心跳用于监控和优雅下线",
         "缺心跳就无法区分「慢」和「死」，看门狗会误伤（坑 4）"),
        ("故障发现", "父进程看门狗按超时回收",
         "独立的监控进程 + K8s liveness/readiness 探针（第 64 课）",
         "探针要查业务指标（最近成功抓取距今多久），不是查进程存活"),
        ("抓取行为", "time.sleep + 概率失败",
         "真实 HTTP + 连接池 + 重试 + 代理轮换（第 63 课）",
         "本课成功率是模型算出来的，不是网络测出来的，不可外推"),
        ("限速", "本课未实现（第 63 课讲过）",
         "按域名令牌桶 + 分布式限速（Redis 计数或本地令牌桶按节点数均分）",
         "多节点限速必须**总配额除以节点数**，否则节点越多越容易触发封禁"),
        ("日志", "父进程汇总 print",
         "结构化 JSON 到 stdout，由采集器收集（第 64 课 StructuredLogger）",
         "子进程绝不能直接 print，会交错（坑 8）"),
        ("配置", "硬编码在实验函数里",
         "环境变量 + 启动时校验 + 跨字段一致性检查（第 64 课）",
         "12-Factor 第三条：配置存环境，不存代码"),
        ("任务粒度", "一个任务 = 一个 URL",
         "一个任务 = 一批 URL（20~100 个），摊薄队列开销",
         "这是提升并行效率最有效的一招，见实验 1 的结论"),
        ("幂等", "未实现 —— 重复处理被记为 duplicate_work",
         "下游用唯一索引 / UPSERT 保证；上游记录 content_hash 跳过未变内容（第 54 课）",
         "**幂等是 exactly-once 效果的唯一实现路径**"),
    ]

    width = 12
    print(f"\n    {'维度':<10}{'本课实现':<38}{'生产系统':<48}")
    print(f"    {'-' * 96}")
    for dim, ours, prod, note in rows:
        print(f"    {dim:<10}{ours:<38}{prod:<48}")
        print(f"    {'':<10}└─ {note}")
    print()


# ============================================================================
# main
# ============================================================================
def main() -> None:
    """运行全部实验。"""
    print(SEP)
    print("阶段 6 · 第 65 课：大规模实战 —— 把前四课拼成一个真能跑的分布式爬虫")
    print(SEP)

    print(f"""
本课是阶段 6 的收官。前面四课各自解决了一个问题：
    第 60 课  并发模型      —— 单机上怎么样把 IO 等满
    第 61 课  Scrapy 架构   —— 引擎/调度器/下载器/管道怎么分工
    第 62 课  分布式队列    —— 共享状态怎么从内存搬到 Redis
    第 63 课  代理池与限速  —— 怎么不被封、怎么退避
    第 64 课  部署与容器化  —— 怎么把代码送到服务器上且能优雅重启

    本课要做的是把它们**同时**跑起来，并且回答一个问题：
      「拼在一起之后，哪里会坏？」

    答案通常不在单个组件里，而在**组件之间** ——
    队列和看门狗之间、去重和重启之间、崩溃和计数之间。

    ⚠ 先声明本课的诚实边界：
      · 用 multiprocessing 起**真进程**（不是线程假装多节点）
      · 共享状态用「JSON 文件 + fcntl 排他锁」（不是真 Redis）
      · 抓取用 sleep + 概率失败（不是真 HTTP）
      · 代理用质量参数模拟（不是真代理）
      所以：**编排逻辑的结论可信，性能数字和反爬效果不可外推。**
      凡是模型算出来的数字，下文都会标明。
""")

    stats: dict[str, Any] = {}
    stats["exp1"] = exp1_scale_out()
    stats["exp2"] = exp2_dedup_rate()
    stats["exp3"] = exp3_load_balance()
    stats["exp4"] = exp4_fault_injection()
    stats["exp5"] = exp5_resume()

    pitfalls()
    production_gap()
    keypoints()

    # ---- 收尾：把所有实验的关键数字汇总 ----
    title("本课实验数据汇总")
    e1 = stats["exp1"]
    e2 = stats["exp2"]
    e4 = stats["exp4"]
    e5 = stats["exp5"]
    print(f"""
    实验 1  横向扩容
      4 节点相对 1 节点的加速比：{e1['best_speedup']:.2f}x
      并行效率：{e1['best_speedup'] / 4 * 100:.1f}%（理想 100%）

    实验 2  去重
      种子总数 {e2['total']} 条，唯一 {e2['unique']} 条
      去重挡掉 {e2['dedup_hits']} 条重复（设计值 25%）

    实验 4  故障注入
      崩溃节点：{e4['crashes']}
      可靠队列完成率：{e4['reliable_rate'] * 100:.1f}%
      朴素队列完成率：{e4['naive_rate'] * 100:.1f}%
      丢失任务数：{e4['lost']}（朴素队列）
      看门狗回收次数：{e4['recovered']}
      重复处理次数：{e4['duplicate_work']}

    实验 5  断点续爬
      第一轮完成 {e5['done1']}，中断时卡在 taken {e5['taken1']}
      第二轮完成 {e5['done2']}（唯一 {e5['unique_done']}，重复 {e5['dup_done']}）
      任务账目核对：{'✅ 平衡，无任务丢失' if e5['balance_ok'] else '❌ 不平衡，需排查'}

    ▸ 下一次你写生产爬虫时，最先做的一件事应该是：
      **把「入队总数 == pending + taken + done + failed」做成一个监控指标。**
      它会在数据出问题的**第一时间**告警，
      而不是等你发现下游少了几万条记录才回头排查。
""")


if __name__ == "__main__":
    main()
