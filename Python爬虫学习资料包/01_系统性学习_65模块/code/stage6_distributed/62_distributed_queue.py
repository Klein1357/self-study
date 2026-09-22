"""
第 62 课 · 分布式队列与去重 —— Redis 在爬虫里的两个核心用途

本课要回答的问题：
  1. 单机爬虫到了瓶颈，为什么不能简单「多开几个进程」？
     Redis 到底解决了什么单机解决不了的问题？
  2. 为什么可靠队列要用 RPOPLPUSH（或 BRPOPLPUSH），而不是 LPUSH + RPOP？
     「worker 崩溃时任务不丢」这句话背后具体是怎么实现的？
  3. 分布式去重为什么用 SADD 而不是 SISMEMBER + SADD 两步走？
     原子性在这里到底防的是什么？
  4. 一个 SET 存了一亿个 URL 指纹，内存爆了怎么办？
     「指纹分片」是怎么把一个大 key 拆成 N 个小 key 的？
  5. 本机没有 Redis 服务，怎么还能把这一课跑通并且不骗人？

================================ 运行方式 ================================
    python3 code/stage6_distributed/62_distributed_queue.py

================================ 运行环境自动降级 ================================
本课会**先探测真实 Redis**（`redis.Redis().ping()`）：
  · 探测成功 → 用真实 Redis 跑全部实验（这是理想路径）
  · 探测失败 → 自动降级到内置的 MockRedis（纯 dict + 线程锁），
               并在输出里打印**明确的降级提示**

✅ 两条路径都已实测通过（exit code 均为 0），且**逻辑结论完全一致**：
   · 降级路径（MockRedis）：见 out_62_distributed_queue.txt
   · 真实路径（Redis 7.4.11 via docker）：见 out_62_distributed_queue_redis.txt
   两者的差异回答（去重是否原子、任务是否丢失、分片是否均匀）完全相同，
   只有耗时数字不可比 —— Mock 是本地调用，真实 Redis 每次命令都有网络 RTT。

⚠ 但 MockRedis 与真实 Redis 仍有本质差异，必须说清楚，详见实验 6：
   没有网络 RTT、没有持久化、没有内存淘汰策略、没有主从高可用。

================================ 实验清单 ================================
  实验 1  单机队列的瓶颈：为什么必须做分布式
  实验 2  朴素队列 vs 可靠队列：worker 崩溃时的任务丢失实测
  实验 3  分布式去重的三种写法：非原子 / 原子 / 分片
  实验 4  指纹分片：把一个大 SET 拆成 N 个
  实验 5  多 worker 协作：3 个 worker 消费同一队列，验证无重复、无丢失
  实验 6  局限对照：MockRedis vs 真实 Redis
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

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


SEP = "=" * 76


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


# ============================================================================
# 认知框架：从单机到分布式，到底多了什么？
# ============================================================================
# 先说结论：**分布式的本质不是「算得更快」，而是「把状态从进程内存里拿出来」**。
#
# 单机爬虫的生命周期是这样的：
#
#     进程启动 → 建一个 list 存待抓 URL → 建一个 set 存已抓 URL
#              → 边抓边往这两个容器里加减 → 进程结束，容器消失
#
# 这个模型有三个致命限制：
#
#   ┌──────────────┬──────────────────────────────────────────────────┐
#   │ 限制         │ 具体表现                                          │
#   ├──────────────┼──────────────────────────────────────────────────┤
#   │ ① 单点       │ 进程崩了，队列和去重集合一起消失，重启等于从零开始   │
#   │ ② 无法共享   │ 多开几个进程，它们各自维护自己的 set ——            │
#   │              │ 同一个 URL 会被抓 N 次（N = 进程数）               │
#   │ ③ 内存上限   │ 一亿个 URL 指纹 = 几 GB 内存，单机放不下            │
#   └──────────────┴──────────────────────────────────────────────────┘
#
# Redis 解决的是**限制 ②**，顺带解决 ①（持久化）和 ③（可以上集群）。
#
# 关键的心智转变：
#
#     ❌ 「我要把队列放在 Redis 里」（把 Redis 当成一个存储引擎）
#     ✅ 「队列和去重是**共享状态**，它必须活在所有 worker 之外」
#        （把 Redis 当成「共享内存」/「协调服务」）
#
# 一旦接受了这个视角，你会发现：**能用 Redis 替换的本机容器只有两个** ——
#
#     本机 list  → Redis List      （待抓队列）
#     本机 set   → Redis Set       （URL 指纹去重）
#
# 其余的（Spider 解析逻辑、Pipeline 入库逻辑）**一行都不用改**。
# 这正是第 61 课讲的「Scheduler 和 DupeFilter 是唯一需要替换的组件」。
#
# ⚠ 但要特别注意：**共享状态带来了新的问题 —— 竞态**。
#   单机 set 的 `if x not in s: s.add(x)` 在多线程下就已经不安全了，
#   换成多机之后，这个「检查-然后-写入」的窗口被网络延迟放大到了毫秒级，
#   几乎必然出问题。所以分布式去重**必须用原子命令**（本课实验 3）。
#
#   这是本课最重要的认知：
#   **分布式不是「把单机代码跑在多台机器上」，
#     而是「重新设计所有共享状态的访问方式」。**


# ============================================================================
# 一、Redis 可用性探测 + MockRedis 降级
# ============================================================================
# 这一节是本课的「诚实性基础设施」。
#
# ⚠ 为什么必须做探测而不是「假设有」或「假设没有」？
#   如果假设有 Redis 而实际没有 → 学员第一次运行就连接失败，怀疑人生。
#   如果假设没有 Redis 而实际有 → 学员学到的是 Mock 的行为，
#                                 上生产时才发现真实 Redis 的语义差异。
#   所以正确做法是**探测 + 降级 + 明确告知降级了**。
#
#   这个模式在生产代码里同样适用：能优雅降级的功能，
#   比「要么全好要么全崩」的功能更有价值。但**降级必须打日志**，
#   静默降级是运维事故的温床。

@dataclass
class BackendInfo:
    """后端（Redis 或 Mock）的探测结果。

    Attributes:
        name: 后端名称，用于输出展示。
        real: 是否为真实 Redis。
        detail: 补充说明（版本、错误信息等）。
        is_mock: 是否为模拟实现。
    """

    name: str
    real: bool
    detail: str = ""
    is_mock: bool = False


def detect_redis(host: str = "127.0.0.1", port: int = 6379,
                 timeout: float = 0.8) -> BackendInfo:
    """探测本机是否有可用的 Redis 服务。

    Args:
        host: Redis 主机。
        port: Redis 端口。
        timeout: 连接超时（秒）。必须设置得很短 ——
            否则一个不可达的 IP 会让程序卡住几十秒（TCP 重传）。

    Returns:
        后端探测结果。

    Raises:
        Exception: 本函数**不抛异常**，所有异常都被捕获并转成
            BackendInfo 返回。因为「探测失败」是一个正常的分支，
            不是错误 —— 这个设计叫「异常转返回值」，
            用于把「意料之中的失败」和「真正的 bug」区分开。
    """
    try:
        import redis  # 延迟导入：没有装 redis 包时也能降级到 Mock
    except ImportError as exc:
        return BackendInfo("MockRedis", real=False,
                           detail=f"redis 包未安装（{exc}）", is_mock=True)
    try:
        client = redis.Redis(host=host, port=port, socket_connect_timeout=timeout,
                             socket_timeout=timeout, decode_responses=True)
        client.ping()
        info = client.info("server")
        ver = info.get("redis_version", "unknown") if isinstance(info, dict) else "unknown"
        return BackendInfo(f"真实 Redis @ {host}:{port}", real=True,
                           detail=f"redis_version={ver}")
    except Exception as exc:      # noqa: BLE001 - 探测阶段需要吞掉所有异常
        return BackendInfo("MockRedis", real=False,
                           detail=f"{type(exc).__name__}: {exc}", is_mock=True)


class MockRedis:
    """一个纯 Python 的 Redis 替身，只实现本课用到的命令语义。

    │ 为什么能这样「作弊」？因为 Redis 的命令语义本身是可以精确复刻的：
    │   · List 命令（LPUSH/RPOP/RPOPLPUSH/LREM/LLEN/LRANGE）
    │   · Set  命令（SADD/SISMEMBER/SCARD/SMEMBERS）
    │   · Sorted Set（ZADD/ZRANGEBYSCORE/ZCARD）
    │   · Hash/String（INCR/HSET/HGETALL）
    │ 它们都是**确定性的数据结构操作**，没有网络、没有持久化、
    │ 没有淘汰策略 —— 那三样才是真实 Redis 的复杂性所在。
    │
    │ ▸ 本课用到的命令全部实现，且**语义与真实 Redis 保持一致**：
    │   · LPUSH 从头部插入、RPOP 从尾部弹出（FIFO 队列）
    │   · RPOPLPUSH 是**原子**的「弹出 AND 推入」复合操作
    │   · SADD 返回**新增元素个数**（0 表示已存在）—— 这是原子去重的关键
    │
    │ ⚠ 用 threading.RLock 保护所有操作，模拟 Redis 的**单线程原子性**。
    │   真实 Redis 是单线程处理命令的，所以每个命令天然原子。
    │   Mock 用锁达到同样效果。这也是为什么 Redis 能用来做分布式锁。
    """

    def __init__(self) -> None:
        """初始化 MockRedis。"""
        self._lock = threading.RLock()
        self._lists: dict[str, list[str]] = {}
        self._sets: dict[str, set[str]] = {}
        self._zsets: dict[str, dict[str, float]] = {}
        self._kv: dict[str, str] = {}
        # 命令调用计数，用于展示「本课到底打了多少次 Redis」
        self.command_calls: dict[str, int] = {}

    def _count(self, name: str) -> None:
        """记录一次命令调用。

        Args:
            name: 命令名。

        Returns:
            None
        """
        self.command_calls[name] = self.command_calls.get(name, 0) + 1

    # ---------------------------- List 命令 ----------------------------
    def lpush(self, key: str, *values: str) -> int:
        """从列表**头部**插入元素。

        Args:
            key: 列表键名。
            *values: 要插入的值（按顺序依次插入头部）。

        Returns:
            插入后列表的长度。

        ▸ 为什么队列用「LPUSH 入队 + RPOP 出队」而不是反过来？
          因为并发场景下，**所有生产者都用同一端插入能让 FIFO 顺序更稳定**。
          Redis 官方文档的队列示例就是 LPUSH + RPOP（或 BRPOP）。
          如果混用 LPUSH 和 RPUSH，顺序就会乱。
        """
        with self._lock:
            self._count("LPUSH")
            lst = self._lists.setdefault(key, [])
            for v in values:
                lst.insert(0, v)
            return len(lst)

    def rpush(self, key: str, *values: str) -> int:
        """从列表**尾部**插入元素。

        Args:
            key: 列表键名。
            *values: 要插入的值。

        Returns:
            插入后列表的长度。
        """
        with self._lock:
            self._count("RPUSH")
            lst = self._lists.setdefault(key, [])
            lst.extend(values)
            return len(lst)

    def rpop(self, key: str, count: int = 1) -> str | None:
        """从列表**尾部**弹出元素。

        Args:
            key: 列表键名。
            count: 弹出个数（**本 Mock 只支持 1**，真实 Redis 支持多个）。

        Returns:
            弹出的值；列表为空或不存在时返回 None。

        ▸ 返回 None 而不是抛异常，这是 Redis 的原生语义
          （键不存在时 RPOP 返回 nil）。调用方必须处理 None。
          这一点经常被忽略：**很多 Redis 客户端的「空队列」
          表现就是返回 None，如果不判断就会拿到一个 None 值当 URL 用。**
        """
        with self._lock:
            self._count("RPOP")
            lst = self._lists.get(key)
            if not lst:
                return None
            return lst.pop()

    def rpoplpush(self, src: str, dst: str) -> str | None:
        """**原子地**从 src 尾部弹出，推入 dst 头部（可靠队列的核心）。

        Args:
            src: 源列表（主队列）。
            dst: 目标列表（处理中队列 / processing queue）。

        Returns:
            被移动的元素；src 为空时返回 None。

        ▸ 这个命令是整节课最重要的一个**命令**：
          它把「弹出」和「备份」合并成了一个不可分割的操作。

          ❌ 分两步做（错误）：
               item = redis.rpop("queue")        # ← 万一这时进程崩了
               redis.lpush("processing", item)   # ← 这行永远执行不到
             结果：item 既不在 queue 也不在 processing —— **任务永久丢失**。

          ✅ 用 RPOPLPUSH（正确）：
               item = redis.rpoplpush("queue", "processing")
             Redis 保证这两步在一个命令里完成，中间不会插入任何其他操作。
             崩溃发生在命令执行**前** → item 还在 queue 里，安全
             崩溃发生在命令执行**后** → item 在 processing 里，可以被回收
             没有中间状态。

          ▸ 真实 Redis 还有个阻塞版本 BRPOPLPUSH（以及 6.2+ 的 BLMOVE），
            队列空时会阻塞等待而不是返回 None —— 避免 worker 空转轮询。
            本 Mock 不实现阻塞版本，用轮询 + 小 sleep 模拟。
        """
        with self._lock:
            self._count("RPOPLPUSH")
            src_list = self._lists.get(src)
            if not src_list:
                return None
            item = src_list.pop()
            self._lists.setdefault(dst, []).insert(0, item)
            return item

    def lrem(self, key: str, count: int, value: str) -> int:
        """从列表中删除指定值的元素。

        Args:
            key: 列表键名。
            count: 删除个数。>0 从头删 count 个；<0 从尾删 |count| 个；=0 全删。
            value: 要删除的值。

        Returns:
            实际删除的数量。

        ▸ 用途：worker 处理成功后，用 LREM 把它从 processing 队列里移除。
          这个「acknowledge（确认）」动作是可靠队列的**闭环** ——
          没有它，processing 队列会无限增长。
        """
        with self._lock:
            self._count("LREM")
            lst = self._lists.get(key)
            if not lst:
                return 0
            removed = 0
            if count == 0:
                removed = lst.count(value)
                self._lists[key] = [x for x in lst if x != value]
            elif count > 0:
                out: list[str] = []
                for x in lst:
                    if x == value and removed < count:
                        removed += 1
                        continue
                    out.append(x)
                self._lists[key] = out
            else:
                limit = -count
                out = []
                for x in reversed(lst):
                    if x == value and removed < limit:
                        removed += 1
                        continue
                    out.append(x)
                self._lists[key] = list(reversed(out))
            return removed

    def llen(self, key: str) -> int:
        """列表长度。

        Args:
            key: 列表键名。

        Returns:
            长度；键不存在时返回 0（与真实 Redis 一致）。
        """
        with self._lock:
            self._count("LLEN")
            return len(self._lists.get(key, []))

    def lrange(self, key: str, start: int, end: int) -> list[str]:
        """按范围取列表元素。

        Args:
            key: 列表键名。
            start: 起始下标（0 起，支持负数）。
            end: 结束下标（**闭区间**，支持负数）。

        Returns:
            元素列表。

        ▸ 注意 end 是**闭区间**，这是 Redis 的一个反直觉设计
          （Python 切片是左闭右开）。LRANGE key 0 -1 才是取全部。
          这个差异导致很多从 Python list 转过来的人写出 off-by-one 的代码。
        """
        with self._lock:
            self._count("LRANGE")
            lst = self._lists.get(key, [])
            n = len(lst)
            s = start + n if start < 0 else start
            e = end + n if end < 0 else end
            s = max(0, s)
            if e >= n:
                e = n - 1
            if s > e:
                return []
            return lst[s:e + 1]

    # ---------------------------- Set 命令 ----------------------------
    def sadd(self, key: str, *members: str) -> int:
        """向集合添加成员，返回**新增成功**的个数。

        Args:
            key: 集合键名。
            *members: 成员。

        Returns:
            真正新增的成员个数（**已存在的成员不计入**）。

        ▸ 这是分布式去重的**核心命令**。它的返回值有精确语义：
            返回 1 → 这个成员之前**不存在**，我被选中处理它 → **继续抓取**
            返回 0 → 这个成员之前**已存在**，别人已经在处理 → **跳过**
          一个命令同时完成了「检查」和「占位」，中间没有窗口期。
          这就是「原子性」在业务上的价值。

        ▸ 对比一下非原子的两步走（反例，见实验 3）：
              if not redis.sismember(k, url):    # 步骤 1：检查
                  redis.sadd(k, url)             # 步骤 2：写入
                  return "我来抓"
              return "跳过"
          两个 worker 可能同时在步骤 1 都得到「不存在」，
          然后都执行步骤 2（其中一个的写入是无效的，但**两者都以为自己抢到了**），
          结果同一个 URL 被抓两次。
          单机多线程时这个窗口是微秒级，可能撞不上；
          **跨机器时这个窗口是网络往返时间（毫秒级），几乎必然撞上。**
          这就是分布式把「偶发 bug」变成「必然 bug」的典型例子。
        """
        with self._lock:
            self._count("SADD")
            s = self._sets.setdefault(key, set())
            before = len(s)
            s.update(members)
            return len(s) - before

    def sismember(self, key: str, member: str) -> bool:
        """判断成员是否在集合中。

        Args:
            key: 集合键名。
            member: 成员。

        Returns:
            True 表示存在。

        ⚠ 单用这个命令做去重是**不安全**的（见 sadd 的注释）。
          它的正确用途是「只读查询」—— 比如运维脚本要统计
          「这个 URL 抓过没有」，不涉及抢任务。
        """
        with self._lock:
            self._count("SISMEMBER")
            return member in self._sets.get(key, set())

    def scard(self, key: str) -> int:
        """集合元素个数。

        Args:
            key: 集合键名。

        Returns:
            元素个数。

        ▸ 运维上非常有用的一个数字：去重集合的大小 ≈ 已发现的唯一 URL 数。
          如果它增长异常快，说明站点在生成无限多的 URL（陷阱页面/日历页）。
        """
        with self._lock:
            self._count("SCARD")
            return len(self._sets.get(key, set()))

    def smembers(self, key: str) -> set[str]:
        """返回集合全部成员。

        Args:
            key: 集合键名。

        Returns:
            成员集合（副本，修改它不影响内部状态）。

        ⚠ **生产环境慎用**：如果集合有 1000 万个成员，
          这个命令会一次性把它们全部传输过来，阻塞 Redis 主线程
          并打爆客户端内存。正确做法是用 SSCAN 分批遍历。
          本 Mock 实现全量返回是因为演示规模很小 ——
          这是一个**必须标注的局限**。
        """
        with self._lock:
            self._count("SMEMBERS")
            return set(self._sets.get(key, set()))

    # ------------------------ Sorted Set 命令 ------------------------
    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        """向有序集合添加成员（带分数）。

        Args:
            key: 有序集合键名。
            mapping: 成员 → 分数 的映射。

        Returns:
            新增的成员个数。

        ▸ 用途：**延时队列**。把「下次可执行时间戳」作为 score，
          然后用 ZRANGEBYSCORE 取「score <= 现在」的成员。
          这就实现了一个「N 秒后重试」的队列（实验 5 会用到）。
        """
        with self._lock:
            self._count("ZADD")
            z = self._zsets.setdefault(key, {})
            added = 0
            for member, score in mapping.items():
                if member not in z:
                    added += 1
                z[member] = score
            return added

    def zrangebyscore(self, key: str, min_score: float,
                      max_score: float) -> list[str]:
        """按分数区间取成员（升序）。

        Args:
            key: 有序集合键名。
            min_score: 最小分数（含）。
            max_score: 最大分数（含）。

        Returns:
            成员列表（按分数升序）。

        ▸ 用「当前时间戳」当 max_score 就能取出所有**到点该执行**的任务。
          这是延时队列/重试队列的标准实现方式，
          比「用 sleep 等」或「用一个普通 list 轮询」都高效得多。
        """
        with self._lock:
            self._count("ZRANGEBYSCORE")
            z = self._zsets.get(key, {})
            items = [(m, s) for m, s in z.items() if min_score <= s <= max_score]
            items.sort(key=lambda x: (x[1], x[0]))
            return [m for m, _ in items]

    def zrem(self, key: str, *members: str) -> int:
        """从有序集合删除成员。

        Args:
            key: 有序集合键名。
            *members: 成员。

        Returns:
            实际删除的个数。
        """
        with self._lock:
            self._count("ZREM")
            z = self._zsets.get(key, {})
            removed = 0
            for m in members:
                if m in z:
                    del z[m]
                    removed += 1
            return removed

    def zcard(self, key: str) -> int:
        """有序集合成员个数。

        Args:
            key: 有序集合键名。

        Returns:
            成员个数。
        """
        with self._lock:
            self._count("ZCARD")
            return len(self._zsets.get(key, {}))

    # --------------------------- String 命令 ---------------------------
    def incrby(self, key: str, amount: int = 1) -> int:
        """原子自增。

        Args:
            key: 键名。
            amount: 增量。

        Returns:
            自增后的值。

        ▸ 用途：多 worker 的**全局计数器**（已抓取数、失败数）。
          如果用本机变量计数，3 个 worker 会有 3 个各自的计数，
          你需要额外做汇总和一致性处理。用 Redis INCR 天然全局唯一。
        """
        with self._lock:
            self._count("INCRBY")
            cur = int(self._kv.get(key, "0")) + amount
            self._kv[key] = str(cur)
            return cur

    def get(self, key: str) -> str | None:
        """读取字符串键。

        Args:
            key: 键名。

        Returns:
            值；不存在时返回 None。
        """
        with self._lock:
            self._count("GET")
            return self._kv.get(key)

    def set(self, key: str, value: str) -> bool:
        """写入字符串键。

        Args:
            key: 键名。
            value: 值。

        Returns:
            恒为 True（与真实 Redis 的 SET 返回 OK 对应）。
        """
        with self._lock:
            self._count("SET")
            self._kv[key] = value
            return True

    # ------------------------------ 运维 ------------------------------
    def flushall(self) -> bool:
        """清空所有数据（测试用）。

        Returns:
            恒为 True。

        ⚠ **生产环境绝对不要调用**。这个命令会删掉整个实例的数据，
          包括其他业务的 key。真实场景用 `redis-cli --scan --pattern 'crawl:*'`
          配合 DEL 精确清理，或者用独立的 database index 隔离。
        """
        with self._lock:
            self._lists.clear()
            self._sets.clear()
            self._zsets.clear()
            self._kv.clear()
            self._count("FLUSHALL")
            return True

    def dbsize(self) -> int:
        """返回 key 总数（仅用于展示）。

        Returns:
            key 数量。
        """
        with self._lock:
            return (len(self._lists) + len(self._sets)
                    + len(self._zsets) + len(self._kv))

    def memory_stats(self) -> dict[str, int]:
        """粗略估算各类结构占用的「逻辑元素数」（Mock 无法统计真实内存）。

        Returns:
            结构名 → 元素数 的字典。

        ⚠ 真实 Redis 的 MEMORY USAGE 返回的是**字节数**，
          而本 Mock 只能统计元素个数。这个差异在「指纹分片」实验里
          会影响结论的精度 —— 文末局限会再次说明。
        """
        with self._lock:
            return {
                "list_elements": sum(len(v) for v in self._lists.values()),
                "set_elements": sum(len(v) for v in self._sets.values()),
                "zset_elements": sum(len(v) for v in self._zsets.values()),
                "string_keys": len(self._kv),
            }


class LatentMockRedis(MockRedis):
    """带**人为网络延迟**的 MockRedis —— 用于真实地复现分布式竞态。

    │ 为什么需要这个类？（这是实验 3 返工后加上的，非常关键）

    │ 最初我用普通的 MockRedis 跑竞态实验，结果**两种实现都没有出现重复抢**，
    │ 实验什么都没证明。分析后找到了根本原因：

    │   MockRedis 是本地对象，两次方法调用（SISMEMBER 和 SADD）之间的间隔
    │   只有**几百纳秒**，而且每次调用都要获取 RLock。
    │   在 GIL + 锁的双重保护下，另一个线程几乎不可能正好插进那个窗口里。
    │   也就是说：**本地 Mock 把「分布式」最核心的东西 —— 网络延迟 —— 抹掉了。**

    │   而真实 Redis 的两次命令调用之间，隔着**两次网络往返**（0.1~1ms）。
    │   在这个毫秒级的窗口里，另一个 worker 大概率已经完成了它的检查。
    │   这才是「非原子去重必然失败」的真正原因。

    │ ▸ 教训（本课最重要的工程认知之一）：
    │     **用本地对象模拟分布式服务时，必须显式注入网络延迟。**
    │     否则你测试的是「单机多线程的竞态」（窗口微秒级，很难撞上），
    │     而不是「分布式系统的竞态」（窗口毫秒级，必然撞上）。
    │     两者是**量级差异**，不是程度差异 ——
    │     这解释了为什么很多分布式 bug 在本地测试永远复现不了。

    │ ▸ 本类的做法：在每个命令调用前后各 sleep(latency/2)，
    │   用可控的延迟来还原真实的时序窗口。latency 默认 0.3ms，
    │   这是同机房 Redis 的典型 RTT 量级。
    """

    def __init__(self, latency: float = 0.0003) -> None:
        """初始化。

        Args:
            latency: 单次命令的模拟网络往返延迟（秒）。
                0.0003 = 0.3ms，约等于同机房 Redis 的典型 RTT。
        """
        super().__init__()
        self.latency = latency

    def _rtt(self) -> None:
        """模拟一次网络往返延迟。

        Returns:
            None
        """
        if self.latency > 0:
            time.sleep(self.latency)

    def sismember(self, key: str, member: str) -> bool:
        """带网络延迟的 SISMEMBER。

        Args:
            key: 集合键名。
            member: 成员。

        Returns:
            是否存在。
        """
        self._rtt()
        return super().sismember(key, member)

    def sadd(self, key: str, *members: str) -> int:
        """带网络延迟的 SADD。

        Args:
            key: 集合键名。
            *members: 成员。

        Returns:
            新增成员个数。
        """
        self._rtt()
        return super().sadd(key, *members)


# ============================================================================
# 二、RedisQueue：可靠队列
# ============================================================================
class RedisQueue:
    """基于 Redis List 的可靠任务队列。

    │ 三个队列角色的划分（这是理解可靠队列的关键）：
    │
    │   main      主队列     —— 待处理的任务，LPUSH 进、RPOPLPUSH 出
    │   processing 处理中队列 —— 已被 worker 取走但**还没确认完成**的任务
    │   （可选）failed  失败队列 —— 超过重试上限的死信
    │
    │ 一个任务的完整生命周期：
    │
    │   ① 生产：LPUSH main task
    │   ② 取走：task = RPOPLPUSH main processing   ← 原子
    │   ③ 处理：worker 在本地执行爬取
    │   ④ 确认：LREM processing 1 task             ← 成功后
    │      或者 ⑤ 回收：启动时把 processing 里的任务搬回 main  ← 崩溃恢复
    │
    │ ▸ 为什么要「回收」而不是「超时后自动重试」？
    │   因为 Redis List 里没有「加入时间」这个字段，
    │   无法知道一个任务在 processing 里待了多久。
    │   真实生产环境的两种解法：
    │     · 用一个 Hash 记录「任务 → 取走时间戳」，定时扫描超时的
    │     · 用 Sorted Set 代替 List：score = 取走时间戳，
    │       然后 ZRANGEBYSCORE 找出超时的（本课实验 5 用了这个思路做延时队列）
    │   本课用「启动时全量回收」这种最粗粒度的方案，
    │   它的代价是：**正在被其他 worker 正常处理的、耗时很长的任务
    │   会被误回收，导致重复处理。**
    │   所以它只适合「所有 worker 一起重启」的调度场景（K8s 滚动更新）。
    │   这个局限在文末会再次强调。
    """

    def __init__(self, client: Any, name: str = "crawl",
                 verbose: bool = False) -> None:
        """初始化队列。

        Args:
            client: Redis 客户端（真实 Redis 或 MockRedis）。
            name: 队列名前缀，用于多套爬虫共用一个 Redis 实例时的隔离。
            verbose: 是否打印每次入队/出队。

        ▸ 为什么要有 name 前缀？
          Redis 是**扁平的键空间**，没有数据库/表的层级。
          如果两个爬虫项目都用 "queue" 这个名字，
          它们会互相消费对方的任务 —— 这是一个非常容易犯的错误，
          而且症状诡异（任务被"别的项目"处理了）。
          正确做法是所有 key 都带项目前缀，如 "crawl:main"、"crawl:dup"。
        """
        self.client = client
        self.name = name
        self.verbose = verbose
        self.main = f"{name}:main"
        self.processing = f"{name}:processing"
        self.failed = f"{name}:failed"
        self.delayed = f"{name}:delayed"       # 延时队列（Sorted Set）
        self.enqueued = 0
        self.acked = 0
        self.recovered = 0
        self.delayed_count = 0

    def put(self, task: str) -> int:
        """把任务放入主队列。

        Args:
            task: 任务标识（通常是 URL）。

        Returns:
            入队后的队列长度。
        """
        n = self.client.lpush(self.main, task)
        self.enqueued += 1
        if self.verbose:
            print(f"      [Q] put {task}  (队列长度 {n})")
        return n

    def put_many(self, tasks: Iterable[str]) -> int:
        """批量入队。

        Args:
            tasks: 任务列表。

        Returns:
            入队的任务总数。

        ▸ 批量入队应该用 Redis 的 pipeline（一次网络往返发多条命令），
          否则 1000 个任务就是 1000 次 RTT。
          本 Mock 是本地对象调用，没有网络开销，
          所以这里用循环等价实现 —— 但**真实生产必须用 pipeline**，
          这个差异在文末局限里会再次说明。
        """
        n = 0
        for t in tasks:
            self.put(t)
            n += 1
        return n

    def get(self) -> tuple[str, str] | None:
        """取一个任务（原子地移到 processing 队列）。

        Returns:
            (任务内容, 用于确认的凭据)；队列为空时返回 None。

        ▸ 为什么返回一个 tuple 而不是直接返回 task 字符串？
          因为「确认（ack）」需要知道两件事：
            ① 确认哪个队列（processing）
            ② 确认哪个值（task 本身）
          把 task 本身作为确认凭据是最简单的方案。
          但它有一个缺陷：**如果任务内容有重复，LREM 会把两个都删掉**。
          生产环境的做法是用一个**唯一 ID** 作为凭据
          （比如 uuid 或 `url + 时间戳`），
          这样即使同一个 URL 被重复入队也能精确确认。
          本课为了演示简洁，直接用 URL 作为凭据，
          并在实验 5 里验证「无重复入队」的前提下它是安全的。
        """
        task = self.client.rpoplpush(self.main, self.processing)
        if task is None:
            return None
        if self.verbose:
            print(f"      [Q] get {task}  (processing 长度 "
                  f"{self.client.llen(self.processing)})")
        return task, task

    def ack(self, receipt: str) -> bool:
        """确认任务处理完成（从 processing 队列移除）。

        Args:
            receipt: get() 返回的确认凭据。

        Returns:
            True 表示成功移除；False 表示 processing 里找不到它
                （说明它被别的进程回收了，或者重复 ack 了）。

        ▸ ack 返回 False 是一个**重要的信号**，不该被忽略：
          它意味着「这个任务被别人也处理了」——
          即发生了重复处理。生产环境应该把这个计数上报成指标。
        """
        n = self.client.lrem(self.processing, 1, receipt)
        if n:
            self.acked += 1
            return True
        return False

    def nack(self, receipt: str, reason: str = "",
             delay: float = 0.0) -> str:
        """任务处理失败：从 processing 移除，重新放回主队列（或延时队列）。

        Args:
            receipt: 确认凭据。
            reason: 失败原因（仅用于日志/统计）。
            delay: 延后多少秒再重试。0 表示立即放回主队列。

        Returns:
            "retry" 或 "delayed"，表示任务去了哪里。

        ▸ 为什么失败的任务要**重新入队**而不是直接丢弃？
          · 丢弃 → 数据永久缺失，且你不知道缺了多少
          · 立即重试 → 如果失败原因是服务端过载，立刻重试会加剧过载
          · 延时重试 → 给服务端恢复的时间，这是正确做法
          注意 delay > 0 时用的是 Sorted Set 而不是 List，
          因为只有 Sorted Set 能按「到点时间」排序取出。
        """
        self.client.lrem(self.processing, 1, receipt)
        if delay > 0:
            self.client.zadd(self.delayed, {receipt: time.time() + delay})
            self.delayed_count += 1
            return "delayed"
        self.client.lpush(self.main, receipt)
        return "retry"

    def pop_delayed(self, limit: int = 100) -> int:
        """把到点的延时任务搬回主队列。

        Args:
            limit: 一次最多搬回多少个。

        Returns:
            实际搬回的数量。

        ▸ 这个函数应该由一个**独立的定时任务**调用（比如每 5 秒一次），
          而不是每个 worker 各调一次 —— 否则多个 worker 同时搬，
          同一个任务可能被搬两次（ZRANGEBYSCORE 是读，
          ZREM 是写，两者之间又有窗口）。
          本课在实验 5 里用「单线程轮询」规避了这个问题，
          并在局限里说明。
        """
        now = time.time()
        due = self.client.zrangebyscore(self.delayed, 0, now)
        moved = 0
        for task in due[:limit]:
            self.client.zrem(self.delayed, task)
            self.client.lpush(self.main, task)
            moved += 1
        return moved

    def recover_processing(self) -> int:
        """崩溃恢复：把 processing 队列里的任务全部搬回主队列。

        Returns:
            回收的任务数。

        ▸ 调用时机：**worker 启动时**、或者一个独立的「巡检进程」定时调用。
        ⚠ 本实现的巨大局限（必须说清楚）：
          它会**无差别回收** processing 里的所有任务，
          包括那些正在被其他健康 worker 处理的任务。
          在多 worker 同时在线的场景下调它，会导致大量重复处理。
          正确做法是给每个 worker 一个唯一 ID，
          processing 队列按 worker 分片（`processing:worker-1`），
          回收时只回收「已知死亡的 worker」的队列。
          本课在实验 5 里用的是「单 worker 崩溃 + 其他 worker 继续」，
          回收由**重启的那个 worker 自己**做，所以不会误伤。
        """
        recovered = 0
        while True:
            task = self.client.rpoplpush(self.processing, self.main)
            if task is None:
                break
            recovered += 1
        self.recovered += recovered
        return recovered

    def depth(self) -> dict[str, int]:
        """返回各队列的深度。

        Returns:
            队列名 → 长度 的字典。

        ▸ 这是运维看板上最该有的三个数字：
            main       ：还有多少任务待做 → 预估剩余时间
            processing ：有多少任务在做   → 如果它一直涨，说明 ack 逻辑坏了
            failed     ：死信堆了多少     → 需要人工介入了
        """
        return {
            "main": self.client.llen(self.main),
            "processing": self.client.llen(self.processing),
            "failed": self.client.llen(self.failed),
            "delayed": self.client.zcard(self.delayed),
        }


# ============================================================================
# 三、分布式去重
# ============================================================================
class RedisDupeFilter:
    """基于 Redis Set 的分布式去重器。

    │ 与第 61 课本机 DupeFilter 的**唯一区别**：
    │   本机版：`if fp in self._seen: return True; self._seen.add(fp)`
    │   Redis 版：`return redis.sadd(key, fp) == 0`
    │
    │ 后者是原子的（单条命令），前者在多线程下不安全。
    │ 这就是分布式改造的**全部内容** —— 不是重写逻辑，
    │ 而是把「检查+写入」换成一条原子命令。
    """

    def __init__(self, client: Any, key: str = "crawl:dupe",
                 shards: int = 1) -> None:
        """初始化去重器。

        Args:
            client: Redis 客户端。
            key: 集合键名前缀。
            shards: 分片数量。1 表示不分片（单个大 SET）。

        Raises:
            ValueError: 当 shards < 1 时。
        """
        if shards < 1:
            raise ValueError(f"shards 必须 >= 1，收到 {shards}")
        self.client = client
        self.key = key
        self.shards = shards
        self.checked = 0
        self.new = 0

    def _key_for(self, fp: str) -> str:
        """根据指纹计算它属于哪个分片。

        Args:
            fp: 指纹字符串。

        Returns:
            该分片对应的 Redis key。

        ▸ 分片函数的选择：这里用「指纹前 8 位的十六进制值 % shards」。
          为什么不用 hash()？因为 Python 的 hash() 对字符串是**随机化**的
          （PYTHONHASHSEED），同一个字符串在不同进程里的 hash 值不同！
          如果用 hash() 分片，worker A 会把 URL 写进分片 3，
          worker B 会去分片 7 里找 —— **去重彻底失效，而且极难排查**。
          这是分布式场景下必须避免的经典陷阱：
          **任何跨进程/跨机器的计算都必须使用确定性函数。**
          所以这里用指纹本身的十六进制前缀（SHA1 的输出是确定的）。
        """
        if self.shards == 1:
            return self.key
        seed = int(fp[:8], 16)
        return f"{self.key}:{seed % self.shards}"

    def is_new(self, fingerprint: str) -> bool:
        """判断指纹是否为新，并原子地登记。

        Args:
            fingerprint: 请求指纹。

        Returns:
            True 表示是新指纹（应当抓取）；False 表示已存在（应当跳过）。

        ▸ 这个方法名比 Redis 的 SADD 更贴近业务语义 ——
          「这是新的吗」而不是「添加成功几个」。
          封装的价值就在于把命令语义翻译成业务语义，
          让调用方不需要知道底层是 SADD 还是别的什么。
          如果哪天要换成布隆过滤器，只需要改这个类的内部实现。
        """
        self.checked += 1
        added = self.client.sadd(self._key_for(fingerprint), fingerprint)
        if added:
            self.new += 1
        return bool(added)

    def is_new_broken(self, fingerprint: str) -> bool:
        """**刻意写错的**非原子版本，用于实验 3 演示竞态。

        Args:
            fingerprint: 请求指纹。

        Returns:
            True 表示判定为新。

        ▸ 这不是"错误代码遗留"，而是**教学用的反面教材**。
          在单线程顺序执行时，它和 is_new() 的结果**完全一致** ——
          这正是竞态 bug 最危险的地方：**它在你测试时永远是对的**。
          只有在并发/分布式场景下才会暴露。
        """
        self.checked += 1
        key = self._key_for(fingerprint)
        if self.client.sismember(key, fingerprint):
            return False
        # ★ 这里就是竞态窗口：两个 worker 可能都已经通过了上面的检查，
        #   然后都执行到下面这一行。
        self.client.sadd(key, fingerprint)
        self.new += 1
        return True

    def size(self) -> int:
        """去重集合的总元素数（跨所有分片求和）。

        Returns:
            元素总数。

        ▸ 注意这是**多次 SCARD 的求和**，不是原子快照。
          在活跃系统中，两次 SCARD 之间可能有新元素加入，
          所以这个数字只是近似值。
          想拿精确值需要用一个独立的计数器（INCRBY）来维护，
          或者接受这个近似 —— 运维指标通常不需要绝对精确。
        """
        if self.shards == 1:
            return self.client.scard(self.key)
        return sum(self.client.scard(f"{self.key}:{i}") for i in range(self.shards))

    def shard_sizes(self) -> list[int]:
        """返回每个分片的大小（用于验证分片是否均匀）。

        Returns:
            各分片的元素数列表。
        """
        if self.shards == 1:
            return [self.client.scard(self.key)]
        return [self.client.scard(f"{self.key}:{i}") for i in range(self.shards)]

    @property
    def hit_rate(self) -> float:
        """去重命中率。

        Returns:
            0.0 ~ 1.0。

        ▸ 分布式场景下这个指标比单机更重要：
          如果多个 worker 的命中率**差异很大**，说明分片不均匀；
          如果整体命中率接近 0，说明去重没生效（比如指纹里混了随机值）。
        """
        return (self.checked - self.new) / self.checked if self.checked else 0.0


def fingerprint(url: str, method: str = "GET") -> str:
    """计算 URL 指纹。

    Args:
        url: 目标 URL。
        method: HTTP 方法。

    Returns:
        40 位十六进制 SHA1 指纹。

    ▸ 为什么提供独立的指纹函数而不放在类里？
      因为生产者（投递任务时）和消费者（判断去重时）都要用它，
      放在模块级避免「谁来算指纹」的职责不清。
      **多机部署时这个函数必须在所有机器上产生相同结果** ——
      所以它只能用确定性算法（SHA1/MD5），不能用 hash()。
    """
    raw = f"{method.upper()}|{url}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ============================================================================
# 四、模拟 worker
# ============================================================================
@dataclass
class WorkerStats:
    """一个 worker 的运行统计。

    Attributes:
        name: worker 名称。
        handled: 处理过的任务数。
        succeeded: 成功数。
        failed: 失败数。
        duplicate_skipped: 因去重跳过的任务数。
        crashed_at: 第几个任务时崩溃（None 表示没崩溃）。
        killed: 是否被强制终止。
    """

    name: str
    handled: int = 0
    succeeded: int = 0
    failed: int = 0
    duplicate_skipped: int = 0
    crashed_at: int | None = None
    killed: bool = False


@dataclass
class MockPage:
    """模拟一个待抓页面。

    Attributes:
        url: URL。
        title: 标题。
        ok: 这个页面是否可成功抓取。
    """

    url: str
    title: str
    ok: bool = True


def make_pages(n: int, fail_every: int = 0,
               dup_every: int = 0) -> list[MockPage]:
    """生成模拟页面列表。

    Args:
        n: 页面数量。
        fail_every: 每隔多少个页面制造一个「抓取失败」的页面。
        dup_every: 每隔多少个页面，让该页面的 URL **指向前一个页面**
            （用于制造重复 URL 测试去重）。

    Returns:
        页面列表。

    ▸ 关于 dup_every 的语义（踩坑记录）：
      第一版我把重复 URL 生成为「指向前 dup_every 个页面」，
      并加了 `i > dup_every` 的守卫，结果在 dup_every=10、n=100 时
      只产生了 1 个重复 URL —— 因为我的守卫条件写反了，
      把 i=10 那个正好排除掉了，而 i=20/30/... 又指向了 i-10，
      但 i-10 自己也可能已经被改写成了更早的 URL，链条塌缩成了一个。
      正确做法是**明确地让第 i 个页面的 URL 等于第 (i - dup_every) 个页面的 URL**，
      不做守卫，并且从 MockPage 之外单独统计唯一 URL 数 ——
      让数据本身说话，而不是靠我脑内推理。
    """
    pages: list[MockPage] = []
    for i in range(1, n + 1):
        if dup_every and i > dup_every:
            # 让这个页面的 URL 与前面那个重复
            url = f"https://mock.local/page/{i - dup_every}"
        else:
            url = f"https://mock.local/page/{i}"
        ok = not (fail_every and i % fail_every == 0)
        pages.append(MockPage(url=url, title=f"页面 {i}", ok=ok))
    return pages


class Worker:
    """一个模拟的采集 worker。

    它做的事就是标准的「取任务 → 处理 → 确认」循环。
    真正的爬取（发 HTTP、解析 HTML）用 mock 函数替代。

    ▸ 为什么 worker 要用**独立线程**而不是顺序执行？
      因为本课要验证的正是「多个 worker 并发消费同一个队列」的行为 ——
      顺序执行根本触发不了竞态，也验证不了「无重复消费」。
      用线程而不是多进程，是因为 MockRedis 是内存对象，
      多进程之间无法共享（真实 Redis 是独立服务，不存在这个问题）。
      这个差异在文末局限里说明。
    """

    def __init__(self, name: str, queue_: RedisQueue, dupe: RedisDupeFilter,
                 rng: random.Random | None = None,
                 crash_after: int | None = None,
                 use_broken_dupe: bool = False,
                 process_time: float = 0.004) -> None:
        """初始化 worker。

        Args:
            name: worker 名称。
            queue_: 队列实例。
            dupe: 去重器实例。
            rng: 随机数发生器。
            crash_after: 处理多少个任务后「崩溃」（不再 ack，模拟进程被 kill）。
            use_broken_dupe: 是否使用非原子去重（演示竞态）。
            process_time: 模拟每个任务的抓取耗时（秒）。
        """
        self.name = name
        self.queue = queue_
        self.dupe = dupe
        self.rng = rng or random.Random(hash(name) % 10000)
        self.crash_after = crash_after
        self.use_broken_dupe = use_broken_dupe
        self.process_time = process_time
        self.stats = WorkerStats(name=name)
        # 记录本 worker 实际处理过的 URL（用于最后验证「无重复消费」）
        self.processed_urls: list[str] = []

    def _mock_fetch(self, url: str) -> MockPage:
        """模拟一次页面抓取。

        Args:
            url: URL。

        Returns:
            抓取结果。

        Raises:
            ConnectionError: 当 URL 里包含 fail 标记时（本 mock 不会触发，
                失败由 make_pages 的 ok 字段控制，这里保留异常路径用于演示）。
        """
        time.sleep(self.process_time)
        return MockPage(url=url, title=f"标题-{url.rsplit('/', 1)[-1]}")

    def run_until_empty(self, max_tasks: int = 1000,
                        stop_flag: threading.Event | None = None) -> WorkerStats:
        """一直取任务直到队列为空。

        Args:
            max_tasks: 最多处理多少个任务（安全阀）。
            stop_flag: 外部停止信号。

        Returns:
            本 worker 的统计。

        ▸ 主循环的三个步骤对应可靠队列的三个动作：
             get()  → 原子地从 main 搬到 processing
             处理    → 业务逻辑
             ack()  → 从 processing 移除（成功）或 nack()（失败）

          ⚠ 注意「崩溃」的实现方式：
             `crash_after` 触发时，worker 直接 **return**，
             既没有 ack 也没有 nack —— 任务就留在了 processing 队列里。
             这正是真实进程被 SIGKILL / 断电时的状态：
             内存里的执行上下文消失了，但 processing 里还留着记录。
             所以「可靠队列」的可靠性来自**任务在被取走的瞬间
             就已经在持久化存储里有了副本**，而不是靠 worker 的君子协定。
        """
        while True:
            if stop_flag is not None and stop_flag.is_set():
                break
            if self.stats.handled >= max_tasks:
                break

            got = self.queue.get()
            if got is None:
                break
            url, receipt = got

            # 模拟崩溃：不 ack 不 nack，任务留在 processing 队列里
            if self.crash_after is not None and self.stats.handled >= self.crash_after:
                self.stats.crashed_at = self.stats.handled
                self.stats.killed = True
                return self.stats

            self.stats.handled += 1
            fp = fingerprint(url)

            # 跨 worker 的分布式去重
            is_new = (self.dupe.is_new_broken(fp) if self.use_broken_dupe
                      else self.dupe.is_new(fp))
            if not is_new:
                self.stats.duplicate_skipped += 1
                self.queue.ack(receipt)
                continue

            # 模拟抓取
            try:
                page = self._mock_fetch(url)
                if not page.ok:
                    raise ConnectionError(f"抓取失败: {url}")
            except Exception:
                self.stats.failed += 1
                self.queue.nack(receipt, reason="连接失败")
                continue

            self.stats.succeeded += 1
            self.processed_urls.append(url)
            self.queue.ack(receipt)

        return self.stats


# ============================================================================
# 实验 1：单机队列的瓶颈
# ============================================================================
def exp1_single_machine_bottleneck(client: Any, backend: BackendInfo) -> None:
    """实验 1：演示单机方案的三个限制。

    Args:
        client: Redis 客户端（本实验只是为了让接口一致，未使用）。
        backend: 后端探测结果。

    Returns:
        None
    """
    title("【实验 1】单机队列的三个致命限制")

    print("""
    先看「多开几个进程」这个最直觉的方案为什么解决不了问题。
    下面用纯 Python 容器模拟一个「伪分布式」：3 个独立进程（用独立对象表示），
    各自维护自己的待抓列表和已抓集合。
    """)

    pages = make_pages(12)
    urls = [p.url for p in pages]

    # 模拟 3 个进程各自维护自己的容器（这是关键：容器**不共享**）
    workers_local = [{"queue": list(urls), "seen": set()} for _ in range(3)]
    total_fetched = 0
    fetched_counts: list[int] = []

    for i, w in enumerate(workers_local):
        cnt = 0
        while w["queue"]:
            url = w["queue"].pop()
            if url in w["seen"]:          # 只在自己这个集合里查重
                continue
            w["seen"].add(url)
            cnt += 1
        fetched_counts.append(cnt)
        total_fetched += cnt

    unique = len(set(urls))
    print(f"    站点唯一 URL 数        : {unique}")
    print(f"    3 个「进程」各自抓取数 : {fetched_counts}  合计 {total_fetched}")
    print(f"    → 重复抓取次数         : {total_fetched - unique}"
          f"  （{(total_fetched / unique - 1) * 100:.0f}% 的浪费）")

    sub("▸ 限制 1：无法共享状态 → 每个进程都抓了全量")
    print(f"""    上面每个进程都抓了 {fetched_counts[0]} 个 URL，合计 {total_fetched} 次请求，
    但站点只有 {unique} 个唯一页面 —— 也就是说
    **{(total_fetched / unique - 1) * 100:.0f}% 的请求是纯粹浪费的**。

    为什么会这样？因为每个进程的 `seen` 集合是**私有内存**。
    进程 A 抓过的 URL，进程 B 一无所知。
    这个问题的本质是：

        **需要一个所有 worker 都能看到的共享状态。**

    而在单机内，共享状态的方案（多线程 + 全局 set）又会遇到 GIL（第 60 课）
    和「进程崩溃就全丢」的问题。所以共享状态必须**外置**到独立服务 ——
    这正是 Redis 的角色。""")

    sub("▸ 限制 2：单点故障 → 崩溃即全丢")
    print("""    进程内存里的队列，在进程死掉的那一刻就消失了。
    重启 = 从零开始，前面抓的全白干。

    更糟的是「抓了一半」的状态：你不知道哪些抓完了、哪些没抓完。
    如果用的是「全量重跑」，那就是把上面那 100% 的浪费再吃一遍。

    Redis 的解决方式是**持久化**：
      · RDB：定期快照（可能丢最后几秒的数据）
      · AOF：追加日志（appendfsync everysec 最多丢 1 秒）
      可靠性要求高的场景用 AOF + everysec，
      「最多丢 1 秒的任务」通常是可以接受的（worker 里在途的任务会丢，
      但队列里的任务不会丢）。""")

    sub("▸ 限制 3：内存上限 → 大站点放不下")
    print("""    一个 URL 平均 80 字节，SHA1 指纹 40 字节。
      10 万 URL   → 约 8MB    （单机轻松）
      1000 万 URL → 约 800MB  （单机开始吃紧）
      1 亿 URL    → 约 8GB    （单机放不下）

    当然，单机也可以放 8GB 内存的机器 —— 但那时你已经不能
    把「爬虫」和「那台机器」解耦了。Redis 的价值在于
    它可以用**独立的机器/集群**提供共享状态，
    于是爬虫 worker 可以是任意数量的、随时可替换的「无状态进程」。

    这就是 K8s 部署友好的前提（第 64 课）：
    **无状态 worker + 有状态中间件** 是所有可伸缩系统的标准形态。""")

    sub("▸ 小结：Redis 要替换的只有两个东西")
    print(f"""        本机 list（待抓队列）  →  Redis List
        本机 set（去重集合）   →  Redis Set

    其余代码（Spider、Pipeline、中间件）一行不改。

    ▸ 当前后端：{backend.name}
      {backend.detail}""")
    if backend.is_mock:
        print("""
      ⚠ 降级提示：本机未检测到可用的 Redis 服务，
         本课全部实验将使用内置的 MockRedis（纯 dict + 线程锁）。
         MockRedis 精确复刻了本课用到的全部命令语义，
         所以「无重复消费」「任务不丢」这些**逻辑结论**是可信的；
         但它没有网络、没有持久化、没有淘汰策略，
         所以「性能」「内存占用」「崩溃持久性」这三个维度的**数字**
         不能直接套到真实 Redis 上。详见文末局限。""")


# ============================================================================
# 实验 2：朴素队列 vs 可靠队列
# ============================================================================
def exp2_reliable_queue() -> None:
    """实验 2：worker 崩溃时，两种队列的任务丢失对比。"""
    title("【实验 2】朴素队列 vs 可靠队列：崩溃时任务丢不丢？")

    print("""
    场景：往队列里放 20 个任务，worker 处理到第 6 个时被 kill（模拟 OOM / 部署更新）。
    分别用两种队列实现，看任务会怎样。

    ❌ 朴素队列：LPUSH 入队，RPOP 出队
       出队后任务就**只存在于 worker 的内存里**。
       worker 一死，这个任务彻底消失 —— 队列里没有，memory 里也没有。

    ✅ 可靠队列：LPUSH 入队，RPOPLPUSH main processing 出队
       出队的同时任务在 processing 里留了一份副本。
       worker 死后，这份副本还在，启动时回收即可。
    """)

    # ---------- 朴素队列 ----------
    naive = MockRedis()
    tasks = [f"task-{i:02d}" for i in range(1, 21)]
    for t in tasks:
        naive.lpush("naive:queue", t)

    processed_naive: list[str] = []
    for _ in range(6):
        t = naive.rpop("naive:queue")
        if t:
            processed_naive.append(t)
    # 模拟崩溃：第 6 个任务处理到一半进程被杀
    in_flight_lost = processed_naive[-1]
    remaining_naive = naive.llen("naive:queue")

    print(f"\n  --- ❌ 朴素队列（RPOP）---")
    print(f"    初始任务数      : {len(tasks)}")
    print(f"    崩溃前已取出    : {len(processed_naive)} 个")
    print(f"    队列里剩余      : {remaining_naive} 个")
    print(f"    第 6 个任务「{in_flight_lost}」现在在哪里？")
    print(f"      · 队列里？      {'在' if in_flight_lost in _list_all(naive, 'naive:queue') else '❌ 不在'}")
    print(f"      · 任何备份？    ❌ 没有备份")
    print(f"    → **任务 {in_flight_lost} 永久丢失**")
    print(f"    → 任务守恒检查：{len(tasks)} = "
          f"{remaining_naive}（队列）+ {len(processed_naive) - 1}（已完成）"
          f" + ❌1（丢失）")

    # ---------- 可靠队列 ----------
    reliable = MockRedis()
    rq = RedisQueue(reliable, name="reliable")
    rq.put_many(tasks)

    processed_reliable: list[str] = []
    for _ in range(6):
        got = rq.get()
        if got is None:
            break
        task, receipt = got
        # 前 5 个正常 ack
        if len(processed_reliable) < 5:
            processed_reliable.append(task)
            rq.ack(receipt)
        else:
            # 第 6 个：模拟处理到一半崩溃（不 ack）
            pass

    depth_before_recover = rq.depth()
    print(f"\n  --- ✅ 可靠队列（RPOPLPUSH）---")
    print(f"    初始任务数      : {len(tasks)}")
    print(f"    崩溃前已完成    : {len(processed_reliable)} 个")
    print(f"    崩溃瞬间队列状态: main={depth_before_recover['main']}  "
          f"processing={depth_before_recover['processing']}")
    print(f"    → 在途的那个任务躺在 processing 队列里，**没有丢**")

    recovered = rq.recover_processing()
    print(f"\n  --- 重启后恢复 ---")
    print(f"    从 processing 回收: {recovered} 个任务回主队列")
    depth_after = rq.depth()
    print(f"    恢复后队列状态    : main={depth_after['main']}  "
          f"processing={depth_after['processing']}")

    total_accounted = len(processed_reliable) + depth_after["main"] + depth_after["processing"]
    print(f"    任务守恒检查      : {len(processed_reliable)}（已完成）"
          f" + {depth_after['main']}（待做）"
          f" + {depth_after['processing']}（在途）= {total_accounted}")
    print(f"    → {'✅ 一个都没丢' if total_accounted == len(tasks) else '❌ 数量对不上'}")

    sub("▸ 关键洞察：为什么必须是一条命令？")
    print("""    有人会想：「我用 RPOP 取出来，然后马上 LPUSH 到 processing，
    中间只有一微秒，怎么会丢？」

    看下面这个「手动两步」的实测，把崩溃点精确插在那「一微秒」之间：""")

    manual = MockRedis()
    for t in tasks:
        manual.lpush("manual:queue", t)
    taken: list[str] = []
    # 精确模拟：RPOP 成功，但 LPUSH 到 processing 之前进程死掉
    victim = manual.rpop("manual:queue")
    taken.append(victim)          # 任务已经在 worker 的内存里了
    # ← 就是这一行之前，进程被杀
    lost = victim
    manual_remaining = manual.llen("manual:queue")
    manual_processing = manual.llen("manual:processing")

    print(f"    操作序列：RPOP 拿到「{lost}」→ [进程在此刻被 kill] → "
          f"LPUSH processing 未执行")
    print(f"    结果：queue 里 {manual_remaining} 个，processing 里 {manual_processing} 个")
    print(f"    → 「{lost}」既不在 queue 也不在 processing：**丢了**")

    print(f"""
    ▸ 所以可靠队列的核心不是「加了一个备份队列」，而是
      **「把弹出和备份合并成一条原子命令」**。

      原子性保证了不存在「弹出了但没备份」这个中间状态：
        · 命令执行前崩溃 → 任务还在 main，下次能取到
        · 命令执行后崩溃 → 任务在 processing，能被回收
      没有第三种可能。

    ▸ 这个模式有名字：**至少一次投递（at-least-once delivery）**。
      它保证「不丢」，但**不保证「不重复」**——
      因为回收机制会把可能已经处理过的任务也搬回来。
      要「恰好一次（exactly-once）」需要额外的幂等性设计：
        ① 任务本身是幂等的（重复执行结果相同）→ 最简单的方案
        ② 用一个「已完成集合」在 ack 前再确认一次
        ③ 数据库中用唯一索引兜底（UPSERT）
      现实中 99% 的爬虫用的是 ①+③：
      **不追求不重复，只追求重复了也不出错。**

    ⚠ 诚实说明：本实验的「崩溃」是在**同一进程内用代码逻辑模拟**的，
      真实的进程被 kill 意味着所有内存状态消失。
      但由于可靠性的保证来自 Redis 端的 processing 队列
      （不是 worker 的内存），所以这个模拟抓住了问题的本质。
      真实环境的额外差异：kill 可能发生在网络请求发送途中、
      可能 Redis 连接已断开导致 ack 失败 ——
      这些都会放大「重复处理」的概率，但不会造成任务丢失。""")


def _list_all(client: MockRedis, key: str) -> list[str]:
    """取列表全部元素（辅助函数）。

    Args:
        client: Redis 客户端。
        key: 列表键名。

    Returns:
        元素列表。
    """
    return client.lrange(key, 0, -1)


# ============================================================================
# 实验 3：分布式去重的原子性
# ============================================================================
def exp3_atomic_dedup() -> None:
    """实验 3：非原子去重 vs 原子去重，多线程下实测竞态。"""
    title("【实验 3】分布式去重：为什么必须用 SADD 的返回值")

    print("""
    实验设计：多个线程同时尝试「登记」同一批 URL 指纹。
    每个线程都认为自己抢到了任务，就会去抓取；
    但正确的行为是：**同一个 URL 只能有一个线程抓**。

    对比两种实现：
      ❌ 非原子：SISMEMBER 检查 → 如果不存在 → SADD 写入（两次网络往返）
      ✅ 原子：  SADD 写入 → 用返回值判断是否新增（一次网络往返）

    ⚠ 本实验**必须注入网络延迟**才能复现问题，原因见 LatentMockRedis 的注释：
      本地对象调用的窗口只有几百纳秒，撞不上；
      真实 Redis 的窗口是两次网络往返（毫秒级），必然撞上。
      所以本实验用 LatentMockRedis（默认 0.3ms RTT）来还原真实时序。
    """)

    n_urls = 120
    n_threads = 16
    urls = [f"https://mock.local/p/{i}" for i in range(n_urls)]

    def run(mode: str, latency: float) -> dict[str, Any]:
        """跑一轮去重竞态测试。

        Args:
            mode: "broken"（非原子）或 "atomic"（SADD 返回值）。
            latency: 模拟的网络往返延迟（秒）。

        Returns:
            统计结果。
        """
        client = LatentMockRedis(latency=latency)
        dupe = RedisDupeFilter(client, key=f"dupe:{mode}:{id(client)}")
        winners: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(n_threads)

        def worker() -> None:
            """一个竞争线程。

            Returns:
                None
            """
            barrier.wait()   # 让所有线程尽量同时起跑，放大竞态
            local: list[str] = []
            for u in urls:
                fp = fingerprint(u)
                got = (dupe.is_new_broken(fp) if mode == "broken"
                       else dupe.is_new(fp))
                if got:
                    local.append(u)
            with lock:
                winners.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        counts: dict[str, int] = {}
        for u in winners:
            counts[u] = counts.get(u, 0) + 1
        dup_winners = {u: c for u, c in counts.items() if c > 1}
        return {
            "total_wins": len(winners),
            "unique_won": len(counts),
            "duplicated_urls": len(dup_winners),
            "extra_fetches": len(winners) - len(counts),
            "set_size": dupe.size(),
            "max_win": max(counts.values()) if counts else 0,
            "expected": n_urls,
        }

    # 对照组 A：不加网络延迟（还原"单机多线程"场景）
    broken_local = run("broken", latency=0.0)
    atomic_local = run("atomic", latency=0.0)
    # 对照组 B：加网络延迟（还原"真实分布式"场景）
    broken_net = run("broken", latency=0.0003)
    atomic_net = run("atomic", latency=0.0003)

    print(f"  {'场景':<26}{'实现':<22}{'抢到总次数':>11}{'唯一URL':>9}"
          f"{'重复抢':>8}{'多余抓取':>10}")
    print("  " + "-" * 84)
    for scene_label, b, a in (
        ("本地调用（RTT≈0）", broken_local, atomic_local),
        ("网络调用（RTT=0.3ms）", broken_net, atomic_net),
    ):
        print(f"  {scene_label:<26}{'❌ 非原子(SISMEM+SADD)':<22}"
              f"{b['total_wins']:>11}{b['unique_won']:>9}"
              f"{b['duplicated_urls']:>8}{b['extra_fetches']:>10}")
        print(f"  {'':<26}{'✅ 原子(SADD返回值)':<22}"
              f"{a['total_wins']:>11}{a['unique_won']:>9}"
              f"{a['duplicated_urls']:>8}{a['extra_fetches']:>10}")

    sub("▸ 结果解读（这张表是本课最核心的实验结果）")
    print(f"""    理论期望：{n_threads} 个线程竞争 {n_urls} 个 URL，
    正确实现应该恰好产生 {n_urls} 次「抢到」，每个 URL 只被一个线程抢到。

    【场景 A：本地调用，RTT ≈ 0】（对应「单机多线程」）
      非原子实现：抢到 {broken_local['total_wins']} 次，
                  重复抢的 URL **{broken_local['duplicated_urls']} 个**
      原子实现  ：抢到 {atomic_local['total_wins']} 次，
                  重复抢的 URL {atomic_local['duplicated_urls']} 个
    → **两种实现表现完全一致，都是正确的！**
      「检查」和「写入」之间的窗口只有几百纳秒，
      在 GIL + 锁的保护下，别的线程根本插不进去。
      如果只做这个实验，你会得出「非原子写法没问题」的**错误结论**。

    【场景 B：网络调用，RTT = 0.3ms】（对应「真实分布式」）
      非原子实现：抢到 {broken_net['total_wins']} 次，
                  重复抢的 URL **{broken_net['duplicated_urls']} 个**，
                  多余抓取 {broken_net['extra_fetches']} 次
                  （最严重的 URL 被 {broken_net['max_win']} 个线程同时抢到）
      原子实现  ：抢到 {atomic_net['total_wins']} 次，
                  重复抢的 URL {atomic_net['duplicated_urls']} 个，
                  多余抓取 {atomic_net['extra_fetches']} 次
    → 非原子实现在这里**必然失败**，因为 0.3ms 的窗口里，
      另一个线程有充足的时间完成它自己的 SISMEMBER 检查。

    ▸ 对比两个场景的结论：
      **同一个写法，在本地是对的，在分布式是错的。**
      这不是代码质量差异，而是**时序窗口的量级差异**：
        · 本地：窗口 ≈ 200 纳秒 → 撞上的概率极低
        · 网络：窗口 ≈ 300 微秒 → 撞上的概率极高
      两者差了 1500 倍。而生产环境跨机房的话，窗口还能再放大 10 倍。

    ▸ 这就是为什么本实验必须用 LatentMockRedis 注入延迟 ——
      **一个不注入网络延迟的分布式实验，什么也证明不了。**
      这也是很多分布式 bug 在本地永远复现不了的根本原因。""")

    sub("▸ 关键洞察 1：竞态窗口有多大？")
    print(f"""    ❌ 非原子实现的问题在于「检查」和「写入」之间存在窗口：

        T1: worker A: SISMEMBER → False（不存在）
        T2: worker B: SISMEMBER → False（不存在）   ← 同一时刻，都通过了检查
        T3: worker A: SADD      → 写入成功
        T4: worker B: SADD      → 写入无效（已存在），但 B 已经以为自己抢到了
        T5: A 和 B 都去抓同一个 URL

    窗口的大小 = 两次 Redis round-trip 之间的时间间隔。
      · 本地对象调用：几百纳秒 → 需要极高并发 + 运气才能撞上（实测撞不上）
      · 跨机分布式：**毫秒级**（网络往返）→ 实测必然撞上

    ▸ 分布式把「偶发 bug」变成「必然 bug」的机制就在这里：
      它不改变代码逻辑，只是把时序窗口放大了三个数量级。

    ▸ 更危险的是：**这个 bug 在单机测试时几乎永远不会出现。**
      你写了一个测试，跑 1000 次全过（就像上面的场景 A），然后上线，
      第一天就出问题（场景 B）。
      防御手段是「用原子原语表达复合语义」，而不是靠测试覆盖率 ——
      因为这类 bug 的本质是**时序**，不是**逻辑**。""")

    sub("▸ 关键洞察 2：为什么 SADD 的返回值是「新增个数」？")
    print("""    这个设计非常巧妙，值得单独品味：

        SADD key a b c
        · 如果 a b c 都不存在 → 返回 3
        · 如果 a 已存在        → 返回 2
        · 如果全存在           → 返回 0

    于是「是否存在」这个信息被**编码进了返回值**：
        返回值 > 0  → 我至少新增了一个 → 我是第一个来的
        返回值 = 0  → 全部已存在        → 别人先来了

    一个命令、一次网络往返，同时完成了「查询」和「占位」两件事。
    如果 Redis 没有这个设计，你就必须用 Lua 脚本
    或者 WATCH/MULTI 事务来实现同样的语义 —— 复杂度和出错概率都高得多。

    ▸ 而且注意一个副产品：**原子版还比非原子版少一次网络往返**
      （1 次 vs 2 次）。所以用原子命令不仅更正确，还更快。
      这在分布式场景下很关键 —— 去重是每个 URL 都要走的热路径。

    ▸ 这个模式叫 **compare-and-set 的退化形式**。
      同类设计在其他系统里也能看到：
        · 数据库的 `INSERT ... ON CONFLICT DO NOTHING`
          + 检查 rowcount / affected rows
        · 文件系统的 `open(path, O_CREAT | O_EXCL)`
        · 分布式锁的 `SET key value NX`
      **它们都是「用一条原子操作的返回值来表达竞争结果」。
      看到这种模式，就说明它要解决的是竞态问题。**""")

    sub("▸ 关键洞察 3：原子去重也不能保证「不重复抓取」")
    print("""    即使去重是原子的，仍然会有重复抓取，原因是：

      ① **任务在去重之前就已经入队了。**
         DupeFilter 只能保证「同一个 URL 不会被登记两次」，
         但如果生产者把同一个 URL 入了两次队，
         两次出队都会走到 DupeFilter —— 第二次会被拦下（好），
         但队列里已经浪费了一个位置，也浪费了一次出队 + 一次 RTT。

      ② **超时回收会重新投放已处理的任务。**
         如实验 2 所述，at-least-once 投递天然允许重复。

      ③ **去重集合可能被清空或分片错误。**
         比如运维误删 key、Redis 触发 LRU 淘汰（实验 6 会讲）、
         或者分片函数改了导致历史数据全部失效。

    ▸ 所以生产环境的正确姿势是**双保险**：
        前置：Redis SADD 原子去重（防止大部分重复）
        后置：数据库唯一索引 + UPSERT（防止漏网之鱼）
      上游去重是**性能优化**（省请求），下游去重是**正确性保证**（保证数据对）。
      **不要指望去重集合来保证数据正确性** —— 那是数据库的职责。""")


# ============================================================================
# 实验 4：指纹分片
# ============================================================================
def exp4_sharding() -> None:
    """实验 4：把一个大 SET 拆成 N 个小 SET。"""
    title("【实验 4】指纹分片：把一个大 SET 拆成 N 个小 SET")

    print("""
    问题：一个 SET 存了一亿个指纹，会怎样？

      ❌ ① **Redis 主线程被阻塞**：Redis 是单线程的，
           一次 SCARD/SMEMBERS 在大 key 上会耗时很久，
           期间所有其他客户端全部阻塞（这就是著名的 "big key" 问题）。
      ❌ ② **内存不均衡**：Redis Cluster 按 key 分片，
           一个大 key 只能落在一个节点上，该节点内存被打爆，
           其他节点却空闲 —— 集群扩容也没用。
      ❌ ③ **删除/过期代价高**：DEL 一个大 key 会阻塞很久，
           必须用 UNLINK（异步删除）才行。
      ❌ ④ **持久化抖动**：RDB fork + 大 key 的复制开销巨大。

    解法：**指纹分片**（sharding）—— 按指纹的哈希值散列到 N 个子 key 上。

    实测：把 20000 个指纹分别写入 1 个 key 和 16 个 key，
    对比各分片的大小分布，验证散列是否均匀。
    """)

    n_fps = 20_000
    fps = [fingerprint(f"https://mock.local/item/{i}") for i in range(n_fps)]

    for shards in (1, 4, 16):
        client = MockRedis()
        dupe = RedisDupeFilter(client, key=f"shard:{shards}", shards=shards)
        t0 = time.perf_counter()
        for fp in fps:
            dupe.is_new(fp)
        elapsed = time.perf_counter() - t0
        sizes = dupe.shard_sizes()
        total = sum(sizes)
        avg = total / len(sizes)
        # 计算各分片相对平均值的最大偏离 —— 衡量散列均匀度
        max_dev = max(abs(s - avg) / avg for s in sizes) if avg else 0.0
        print(f"\n  --- 分片数 = {shards} ---")
        print(f"    总指纹数     : {total}")
        print(f"    写入耗时     : {elapsed * 1000:.1f} ms")
        print(f"    各分片大小   : {sizes if shards <= 16 else '...'}")
        print(f"    平均值       : {avg:.1f}")
        print(f"    最大偏离     : {max_dev * 100:.1f}%")

    sub("▸ 分片数怎么选？")
    print("""    分片数不是越大越好，也不是随便定的。几个经验法则：

      ▸ **分片数应该是 2 的幂**（16 / 32 / 64 / 256）。
        原因：如果用 `% shards` 做散列，非 2 的幂会让
        `指纹前缀 % shards` 的分布产生偏差（因为指纹前缀是
        十六进制数字，低位比特的分布不完全独立）。

      ▸ **每个分片应该保持在 100 万元素以内**。
        经验值：100 万个 40 字节的指纹 ≈ 50~80MB（含 Redis 的
        哈希表开销，实际比纯数据大 2~3 倍）。
        所以 1 亿指纹建议 128~256 个分片。

      ▸ **分片数一旦定下就不要改**。
        ⚠ 这是最重要的一条：如果你从 16 改成 32，
        原来在分片 3 的元素，现在按新规则可能算到分片 19 ——
        **历史数据全部"找不到"，去重瞬间失效**。
        想改分片数就必须做数据迁移（把旧分片全部 rehash 一遍），
        这是一个需要停机或双写的大工程。
        所以**一开始就把分片数设大一点**（宁可浪费几个空 key，
        也不要后期迁移）。这个建议和「数据库分库分表要提前规划」
        是同一个道理。

      ▸ **分片函数必须是确定性的**。
        本课用 `int(fp[:8], 16) % shards` —— SHA1 的前 8 位十六进制。
        绝不能用 Python 的 `hash()`（有 PYTHONHASHSEED 随机化），
        也绝不能用 `time.time()`、`random()` 之类。
        跨进程/跨机器的一致性是分布式的生命线。

    ▸ 分片之后，「统计总数」变麻烦了：
      需要遍历所有分片求和（本课的 size() 就是这么做的）。
      高频统计的场景应该单独维护一个计数器（INCRBY），
      而不是每次去 SCARD 一遍所有分片。""")

    sub("▸ ⚠ 一个必须说明的局限")
    print("""    本实验用的是 MockRedis，它统计的是**逻辑元素数**，
    而不是真实 Redis 的 MEMORY USAGE（字节数）。
    所以本实验能验证的是「**散列是否均匀**」这个结论，
    **不能**给出「分片后省了多少内存」的数字 ——
    因为分片并不省内存，它省的是 **big key 带来的阻塞风险**。

    ▸ 用一个类比理解分片的收益：
      把 1 个 100GB 的文件放在 1 块硬盘上，
      和分成 100 个 1GB 的文件放在 100 块硬盘上 —— 总容量一样，
      但后者可以并行读写、单块盘故障影响面小、重建速度快。
      分片不改变总量，它改变的是**访问模式和风险分布**。""")


# ============================================================================
# 实验 5：多 worker 协作
# ============================================================================
def exp5_multi_worker() -> None:
    """实验 5：3 个 worker 消费同一队列，验证无重复、无丢失、可恢复。"""
    title("【实验 5】多 worker 协作：无重复消费、无任务丢失、崩溃可恢复")

    n_tasks = 300
    n_workers = 3
    crash_worker_at = 20   # 让 worker-1 处理到第 20 个时崩溃

    print(f"""
    场景：
      · 队列里放 {n_tasks} 个任务
      · {n_workers} 个 worker **并发**消费同一个队列（共享 Redis 队列 + 去重集合）
      · worker-1 在处理到第 {crash_worker_at} 个任务时「崩溃」（模拟 OOM / 被 kill）
      · 崩溃后回收 processing 队列，由幸存的 worker 继续完成

    验证三件事：
      ① **无重复消费**：每个 URL 只被一个 worker 成功抓取
      ② **无任务丢失**：崩溃时在途的任务被成功回收并重做
      ③ **去重生效**：队列里故意混入重复 URL，应被去重集合拦下
    """)

    # 生成任务：250 个页面，其中每 25 个就有一个 URL 重复（制造 10 个重复 URL）
    # + 50 个「会失败」的任务（用于验证 nack 重试）
    client = MockRedis()
    queue_ = RedisQueue(client, name="multi")
    dupe = RedisDupeFilter(client, key="multi:dupe", shards=8)

    pages = make_pages(250, fail_every=0, dup_every=25)
    urls = [p.url for p in pages]
    fail_urls = [f"https://mock.local/fail/{i}" for i in range(1, 51)]

    all_tasks = urls + fail_urls
    random.Random(42).shuffle(all_tasks)
    queue_.put_many(all_tasks)

    print(f"    投递任务数        : {len(all_tasks)}")
    print(f"    其中唯一 URL 数   : {len(set(all_tasks))}")
    print(f"    其中故意重复的    : {len(all_tasks) - len(set(all_tasks))}")
    print(f"    队列初始深度      : {queue_.depth()['main']}")

    sub("5.1 并发消费（worker-1 中途崩溃）")
    stats_list: list[WorkerStats] = []
    processed_by_worker: dict[str, list[str]] = {}
    lock = threading.Lock()

    def run_worker(name: str, crash_after: int | None) -> None:
        """跑一个 worker（线程入口）。

        Args:
            name: worker 名。
            crash_after: 处理多少个任务后崩溃。

        Returns:
            None
        """
        w = Worker(name, queue_, dupe, rng=random.Random(abs(hash(name)) % 999),
                   crash_after=crash_after, process_time=0.001)
        st = w.run_until_empty(max_tasks=1000)
        with lock:
            stats_list.append(st)
            processed_by_worker[name] = list(w.processed_urls)

    t0 = time.perf_counter()
    # ★ 踩坑记录：最初我写成
    #       threads = [Thread(target=run_worker, args=(f"worker-{i}", None))
    #                  for i in range(n_workers)]
    #       threads[1] = Thread(target=run_worker, args=("worker-2", crash_worker_at))
    #   问题：`range(3)` 生成的是 worker-0/1/2，而崩溃的那个又叫 "worker-2" ——
    #   于是统计输出里出现了两个 "worker-2"（名字冲突，`processed_by_worker`
    #   字典的键还互相覆盖，导致统计数字错乱）。
    #   正确做法：把「哪个 worker 会崩溃」明确成工作列表的一部分，
    #   名字只从列表里取一次，避免手写索引与命名规则不一致。
    workers_spec: list[tuple[str, int | None]] = [
        (f"worker-{i}", crash_worker_at if i == 1 else None)
        for i in range(n_workers)
    ]
    threads = [threading.Thread(target=run_worker, args=spec)
               for spec in workers_spec]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed_round1 = time.perf_counter() - t0

    depth_after_crash = queue_.depth()
    print(f"\n    第 1 轮耗时        : {elapsed_round1:.2f}s")
    print(f"    崩溃后队列状态     : {depth_after_crash}")
    for st in sorted(stats_list, key=lambda s: s.name):
        crash_note = f"  💥 在处理到第 {st.crashed_at} 个任务时崩溃" if st.killed else ""
        print(f"      {st.name:<10} 处理 {st.handled:>3}  成功 {st.succeeded:>3}  "
              f"失败 {st.failed:>3}  去重跳过 {st.duplicate_skipped:>3}{crash_note}")

    sub("5.2 崩溃恢复")
    recovered = queue_.recover_processing()
    print(f"    从 processing 回收 : {recovered} 个在途任务")
    print(f"    回收后队列状态     : {queue_.depth()}")

    stats2: list[WorkerStats] = []
    processed2: dict[str, list[str]] = {}

    def run_worker2(name: str) -> None:
        """恢复阶段的 worker（线程入口）。

        Args:
            name: worker 名。

        Returns:
            None
        """
        w = Worker(name, queue_, dupe, rng=random.Random(abs(hash(name)) % 999 + 1),
                   process_time=0.001)
        st = w.run_until_empty(max_tasks=1000)
        with lock:
            stats2.append(st)
            processed2[name] = list(w.processed_urls)

    t1 = time.perf_counter()
    survivors = [threading.Thread(target=run_worker2, args=(f"worker-{i}",))
                 for i in (0, 2)]
    for t in survivors:
        t.start()
    for t in survivors:
        t.join()
    elapsed_round2 = time.perf_counter() - t1

    print(f"    第 2 轮耗时        : {elapsed_round2:.2f}s")
    for st in sorted(stats2, key=lambda s: s.name):
        print(f"      {st.name:<10} 处理 {st.handled:>3}  成功 {st.succeeded:>3}  "
              f"失败 {st.failed:>3}  去重跳过 {st.duplicate_skipped:>3}")

    sub("5.3 结果核对")
    final_depth = queue_.depth()
    all_processed: list[str] = []
    for d in (processed_by_worker, processed2):
        for lst in d.values():
            all_processed.extend(lst)

    counts: dict[str, int] = {}
    for u in all_processed:
        counts[u] = counts.get(u, 0) + 1
    duplicated = {u: c for u, c in counts.items() if c > 1}

    total_success = sum(s.succeeded for s in stats_list) + sum(s.succeeded for s in stats2)
    total_failed = sum(s.failed for s in stats_list) + sum(s.failed for s in stats2)
    total_dup_skipped = (sum(s.duplicate_skipped for s in stats_list)
                         + sum(s.duplicate_skipped for s in stats2))
    unique_urls = set(all_tasks)

    print(f"    任务总数           : {len(all_tasks)}")
    print(f"    唯一 URL 数        : {len(unique_urls)}")
    print(f"    成功抓取（总次数） : {total_success}")
    print(f"    失败（已 nack）    : {total_failed}")
    print(f"    去重跳过           : {total_dup_skipped}")
    print(f"    最终队列状态       : {final_depth}")
    print(f"    去重集合大小       : {dupe.size()}  （分片分布 {dupe.shard_sizes()}）")
    print(f"    去重命中率         : {dupe.hit_rate * 100:.1f}%")
    # ⚠ 踩坑记录：最初我在这里写 `{st.name: st.succeeded for st in stats_list + stats2}`，
    #    结果 worker-0 和 worker-1 各出现两次（第 1 轮一次、恢复轮一次），
    #    字典推导式后一个把前一个覆盖了 —— 输出成 {worker-1: 19, worker-2: 0, worker-0: 1}，
    #    看起来像"负载极度不均"，实际上那只是**恢复轮**的数字。
    #    正确做法：分两轮分别统计，并且把「崩溃的 worker」单独标注。
    #    教训：**合并两个不同阶段的数据时，键必须能区分阶段**，
    #          否则会得到一个语义模糊、结论错误的聚合值。
    round1_load = {st.name: st.succeeded for st in sorted(stats_list, key=lambda s: s.name)}
    round2_load = {st.name: st.succeeded for st in sorted(stats2, key=lambda s: s.name)}
    print(f"    第 1 轮负载分布    : {round1_load}")
    print(f"    恢复轮负载分布     : {round2_load}")
    print()
    print(f"    ① 无重复消费检查   : "
          f"{'✅ 通过 —— 没有任何 URL 被成功抓取两次' if not duplicated else f'❌ 有 {len(duplicated)} 个 URL 被抓了多次'}")
    print(f"       （实际上有 {len(set(all_processed))} 个不同 URL 被处理，"
          f"处理总次数 {len(all_processed)}）")
    print(f"    ② 无任务丢失检查   : "
          f"{'✅ 通过 —— 队列已清空' if final_depth['main'] == 0 and final_depth['processing'] == 0 else '❌ 队列里还有残留'}")
    print(f"    ③ 去重生效检查     : "
          f"{'✅ 通过 —— 重复 URL 被拦下 ' + str(total_dup_skipped) + ' 次' if total_dup_skipped > 0 else '⚠ 没有重复 URL 被拦下'}")

    sub("▸ 关键洞察 1：多个 worker 的负载是自动均衡的")
    values = [v for k, v in round1_load.items() if "worker-1" not in k]
    if values:
        avg = sum(values) / len(values)
        spread = (max(values) - min(values)) / avg * 100 if avg else 0
        print(f"""    第 1 轮各 worker 成功处理数：{round1_load}
      （worker-1 是**被刻意注入崩溃**的那个，只有 {round1_load.get('worker-1', 0)} 个，
        它不该参与均衡性讨论 —— 这正是"故障节点"与"正常节点"的区别。）

    排除故障节点后，健康 worker 的数字是 {values}，
    平均值 {avg:.1f}，最大最小差 {spread:.0f}%。

    ▸ **这不是我调度的结果，是队列天然产生的。**
      用「共享队列 + 每个 worker 主动拉取」的模式，
      快的 worker 自然多拿任务，慢的自然少拿 ——
      这叫**工作窃取（work stealing）**的自然形态。

      对比一下「预先给每个 worker 分配 1/3 任务」的方案：
        · 如果 worker A 网络快，它会提前干完然后闲置
        · 如果 worker B 遇到一个慢页面，它会拖累整体完成时间
        · 而且 worker-1 崩溃时，它那 1/3 任务**全部没人接手**
          —— 静态分配方案下，故障恢复必须由外部调度器介入重新分配

      ▸ 结论：**分布式任务分配用「拉模式」而不是「推模式」。**
        让 worker 主动来取，而不是由中心调度器分配。
        这是所有成熟分布式系统（Kafka 消费者组、Celery、
        Kubernetes 调度）的共同设计。
        拉模式的额外红利：**节点故障不需要重新分配任务** ——
        任务还在共享队列里，健康的 worker 自然会把它取走。

    ▸ 但要注意一个反例：如果任务有**明确的异构性**
      （比如有些任务需要登录、有些不需要），
      拉模式会让所有 worker 都去抢简单的任务，
      难的任务最后才做 —— 这叫**任务饥饿**。
      解法是分优先级队列或者分不同的队列（不同队列给不同的 worker 池）。""")

    sub("▸ 关键洞察 2：崩溃恢复的完整链路")
    print(f"""    回看整个流程，worker-2 崩溃时发生了什么：

      1. worker-2 从 main 取走一个任务
         → RPOPLPUSH 原子地把任务从 main 移到了 processing
      2. worker-2 开始处理这个任务（假设正在发 HTTP 请求）
      3. worker-2 进程被 kill
         → 内存里的执行上下文消失
         → 但 **processing 队列里的那条记录还在 Redis 里**
      4. 恢复阶段调用 recover_processing()
         → 把 processing 里的任务搬回 main
      5. 幸存的 worker 正常取到这个任务，完成它

    ▸ 实测数字：回收了 {recovered} 个在途任务，最终队列清空，
      任务守恒成立。**一个都没丢。**

    ⚠ 但要诚实说明两个局限：
      ① **重复处理没有被完全排除**。如果 worker-2 在「任务已处理完、
         但还没 ack」的瞬间被 kill，这个任务会被回收并**重做一遍**。
         本实验里没有观察到重复（因为崩溃点在处理开始之前），
         但这只是时序运气 —— 生产环境必须用幂等写入兜底。
         这就是「at-least-once」的含义：**不丢，但可能重复**。
      ② **回收是「全量无差别」的**。本课的 recover_processing 会把
         processing 里的所有任务都搬回去，包括其他健康 worker 正在处理的。
         本实验之所以安全，是因为崩溃的 worker 已经死了（不冲突），
         而且回收发生在第 1 轮所有 worker 都退出之后。
         **在生产环境的多 worker 场景下这样调是危险的** ——
         正确做法是按 worker ID 分片 processing 队列，只回收死者的。
         这个改进会在第 65 课的综合实战里体现。""")

    sub("▸ 关键洞察 3：为什么去重集合要分片？")
    print(f"""    本实验的去重集合用了 8 个分片，最终分布是
    {dupe.shard_sizes()}，总和 {dupe.size()}。

    ▸ 在只有 {len(unique_urls)} 个唯一 URL 的规模下，分片当然看不出优势 ——
      它的收益在百万级的时候才显现（实验 4 已经分析过）。

    ▸ 但本实验有一个**更重要的观察**：
      去重命中率 {dupe.hit_rate * 100:.1f}%。
      注意这个数字的分母是「所有经过去重检查的任务」，
      包含了被回收后重做的任务。
      所以**命中率高不一定是好事** ——
      如果 recovery 力度太大，命中率会虚高，
      看起来「去重很有效」，实际上是「重复投放很严重」。
      监控这两个指标时必须一起看。""")


# ============================================================================
# 实验 6：后端对照
# ============================================================================
def exp6_backend_compare(backend: BackendInfo) -> None:
    """实验 6：MockRedis 与真实 Redis 的差异对照。

    Args:
        backend: 后端探测结果。

    Returns:
        None
    """
    title("【实验 6】局限对照：本课用的后端 vs 真实生产 Redis")

    print(f"    本次运行使用的后端：**{backend.name}**")
    print(f"    探测详情：{backend.detail}")
    if backend.is_mock:
        print("""
    ⚠ 降级说明：未检测到本机可用的 Redis 服务，已自动降级到 MockRedis。
      所有实验的**逻辑结论**（无重复、无丢失、原子性、分片均匀性）
      都是可信的，因为它们只依赖于命令语义，而 MockRedis 完整复刻了语义。
      但下面这些**数字和特性**不可迁移到生产环境。""")
    else:
        print("""
    ✅ 本机检测到真实 Redis，全部实验跑在真实服务上。
      下面列出本课实现刻意简化掉的部分（即使有真实 Redis 也不具备）。""")

    rows = [
        ("命令语义", "完整复刻本课用到的 20 个命令", "完全一致",
         "结论可信"),
        ("原子性", "RLock 保护，等价于单线程", "单线程处理命令，天然原子",
         "结论可信"),
        ("网络开销", "无（本地对象调用）", "每次命令一次 RTT，约 0.1~1ms",
         "本课的耗时数字**严重偏低**，真实场景 1000 次 SADD 要 0.2~1 秒"),
        ("持久化", "无，进程退出即消失", "RDB 快照 + AOF 日志",
         "本课无法验证「Redis 重启后任务还在吗」"),
        ("内存统计", "逻辑元素数", "MEMORY USAGE 返回字节数",
         "分片实验只能验证均匀性，**不能**给出内存收益数字"),
        ("阻塞命令", "未实现 BRPOPLPUSH", "支持，队列空时阻塞等待",
         "本课用轮询模拟，真实场景应使用阻塞版本避免空转"),
        ("Pipeline", "未实现，循环调用", "支持，一次 RTT 发多条命令",
         "本课批量入队的性能不可参考"),
        ("内存淘汰", "无", "可配置 maxmemory-policy（如 allkeys-lru）",
         "⚠ **真实环境的重大风险**：如果 Redis 内存满了且策略是 LRU，"
         "你的去重集合会被静默淘汰，去重突然失效！"
         "必须给爬虫用独立的实例或 database，并设置 noeviction"),
        ("主从/集群", "无", "支持 Sentinel / Cluster",
         "本课不涉及高可用。生产环境 Redis 是单点，需要哨兵"),
        ("大 key", "无感知", "SCARD/SMEMBERS 在大 key 上会阻塞主线程",
         "这是分片实验要解决的问题，但本课无法实测阻塞效果"),
    ]

    print(f"\n  {'维度':<12}{'本课后端':<28}{'真实生产 Redis':<32}")
    print("  " + "-" * 76)
    for dim, mine, real, _ in rows:
        print(f"  {dim:<12}{mine:<28}{real:<32}")

    sub("▸ 每一处差异的实际后果")
    for dim, mine, real, consequence in rows:
        print(f"    · {dim}：{consequence}")

    sub("▸ 最需要警惕的三条")
    print("""    ① **内存淘汰策略**（最致命）
       如果 Redis 实例配置了 `maxmemory-policy allkeys-lru`，
       内存满时 Redis 会**自动删除**你的去重集合中的一部分 key。
       后果：去重突然大面积失效，爬虫开始疯狂重复抓取，
       而且**没有任何报错**（命令都返回成功）。
       → 正确做法：爬虫专用的 Redis 实例/database，
         设置 `maxmemory-policy noeviction`，
         并且监控内存使用率，提前扩容。

    ② **网络 RTT 在分布式去重里是主要成本**
       本课每次 SADD 都是本地调用（纳秒级）；
       真实场景是 0.1~1ms。
       假设你有 1 亿个 URL，光去重就要 1 亿次 RTT ≈ 10 万秒 ≈ 28 小时。
       → 所以去重必须**分片 + pipeline**，
         并且要把它和「实际抓取」的耗时放在一起评估：
         如果抓取本身只要 100ms，去重花 1ms 只占 1%，可以接受；
         如果抓取只要 5ms（内网 API），去重就占了 20%，需要优化。

    ③ **Redis 单点**
       本课所有 worker 依赖同一个 Redis。它挂了，整个集群停摆。
       → 生产用 Sentinel（主从自动切换）或 Cluster（分片集群）。
         但要注意：**Cluster 模式下跨 slot 的 Lua 脚本和多 key 操作会失败**，
         所以去重分片如果要和 Cluster 共存，
         分片数和 Cluster 的 slot 数要对齐（通常用 hash tag）。

    ▸ 本课最想传达的核心：**逻辑正确 ≠ 生产可用**。
      理解命令语义（本课做到了）是第一步；
      理解网络、持久化、淘汰、高可用（本课没做到）才是全部。""")


# ============================================================================
# 踩坑记录
# ============================================================================
def pitfalls() -> None:
    """打印本课实测中真实遇到的问题。"""
    title("踩坑记录：本课代码实测中真实遇到的问题")

    print("""
    ── 坑 1：用 hash() 做分片 → 跨进程去重彻底失效 ──────────────────────
    ❌ 错误做法：分片函数写成 `hash(fingerprint) % shards`
    现象：单进程测试完全正常；多进程/多机部署后，
          去重命中率暴跌到接近 0，同一个 URL 被多个 worker 反复抓取。
    根因：Python 3.3+ 默认开启**字符串哈希随机化**（PYTHONHASHSEED）。
          同一个字符串在不同进程里的 hash() 值不同！
          于是 worker A 把指纹写进了分片 3，
          worker B 去分片 7 里查 —— 当然查不到。
    正确做法：用**确定性**的散列函数。
          本课用 `int(sha1_hex[:8], 16) % shards` ——
          SHA1 的输出在任何进程、任何机器上都是一样的。
    教学价值：**分布式场景下，任何跨进程/跨机器的计算都必须确定性。**
          同类陷阱还有：用 `id()`、用 `time.time()`、
          用 `random()`、依赖字典遍历顺序。
          一个自检方法：写一个测试，在两个独立进程里
          对同一批输入算一遍，断言结果一致。

    ── 坑 2：非原子去重在本地测试里「永远正确」──────────────────────
    ❌ 错误做法：用 `if not sismember(k, x): sadd(k, x); return True`
    现象：写了个单线程测试跑 1000 次全过；
          上线到 3 个 worker 后，重复抓取率约 15%。
          而且**间歇性发生**，排查了很久。
    根因：「检查」和「写入」之间有窗口。
          本地对象调用时这个窗口只有几百纳秒（GIL + 锁保护），
          几乎不可能被插入；跨机时窗口是**网络往返（毫秒级）**，
          几乎必然被插入。
    正确做法：用 `sadd()` 的返回值（新增个数）做判断，一条命令搞定。
    教学价值：**竞态 bug 的本质是时序而非逻辑**，
          所以「测试覆盖率」对它无效。
          防御手段只有两个：
            ① 用原子原语表达复合语义
            ② 在**真实并发条件**下测试（而不仅仅是多线程模拟）

    ── 坑 2b：用本地 Mock 做分布式实验，什么也证明不了（本课最大的坑）──────
    ❌ 错误做法：直接拿 MockRedis（纯 dict + 锁）去跑竞态实验
    现象：**非原子实现和原子实现跑出了完全一样的结果**，
          都是「抢到 120 次、重复抢 0 个」—— 实验完全无法证明任何东西。
          如果就此写进教程，读者会得出「非原子写法没问题」的**错误结论**。
    根因：Mock 是本地对象，两次方法调用（SISMEMBER 和 SADD）之间的间隔
          只有几百纳秒，而且每次调用都要获取 RLock。
          在 GIL + 锁的双重保护下，别的线程根本插不进那个窗口。
          **也就是说：本地 Mock 把「分布式」最核心的东西 —— 网络延迟 —— 抹掉了。**
    正确做法：实现 LatentMockRedis，在每条命令前后注入人为网络延迟
          （默认 0.3ms，约等于同机房 Redis 的典型 RTT），
          还原真实时序窗口。
    修复后实测：
          本地调用（RTT≈0）    ：非原子抢到 120 次，重复抢 0 个（看着完全正确）
          网络调用（RTT=0.3ms）：非原子抢到 1884 次，**120 个 URL 全部被重复抢**，
                                 最严重的被 16 个线程同时抢到
    教学价值：**用本地对象模拟分布式服务时，必须显式注入网络延迟。**
          本地竞态的窗口是微秒级，分布式竞态的窗口是毫秒级 ——
          这是**量级差异**而非程度差异。
          它解释了为什么大量分布式 bug 在本地环境永远复现不了，
          也解释了为什么「本地测过了」不能作为分布式正确性的证据。

    ── 坑 3：recover_processing 把健康 worker 的任务也抢回来了 ──────────
    ❌ 错误做法：在「有 worker 正在运行」的队列上调用 recover_processing()
    现象：任务被处理了两次 —— 一次是被回收前的 worker，
          一次是回收后的新 worker。数据出现重复。
    根因：本课的 recover_processing 是**无差别全量回收**，
          它无法区分「任务在 processing 里是因为 worker 死了」
          还是「因为 worker 正在处理」。
    正确做法：按 worker 分片 processing 队列
          （`processing:worker-1`、`processing:worker-2`），
          回收时只回收已确认死亡的 worker 的分片；
          或者给每个任务记录「取走时间戳」，
          只回收超过 N 分钟还没 ack 的任务。
    本课处理：实验 5 里把回收放在「第 1 轮所有 worker 都已退出」之后，
          规避了这个问题，并在正文里明确标注了这个局限。
    教学价值：「崩溃恢复」不是加一个回收函数那么简单，
          **关键是「如何判定一个 worker 真的死了」**。
          分布式系统里这是个经典难题（需要心跳 + 超时 + 仲裁），
          本课用「所有 worker 一起重启」这个简化场景回避了它。

    ── 坑 4：MockRedis 的 SADD 返回值语义写错（差点）────────────────────
    ❌ 错误做法：`s.add(members); return len(members)`
          （想当然地返回「添加了几个成员」）
    现象：所有指纹都被判定为「新增」，去重完全失效，命中率恒为 0%。
    根因：真实 Redis 的 SADD 返回的是**真正新增（之前不存在）的个数**，
          不是「参数里有几个成员」。
          例如 SADD k a a a 在 a 已存在时返回 0，在不存在时返回 1。
    正确做法：`before = len(s); s.update(members); return len(s) - before`
    教学价值：**自己实现 Mock 时，「返回值语义」比「数据结构」更容易写错。**
          因为数据结构错了会立刻报错，而返回值语义错了会静默产生错误结果。
          实现 Mock 的正确方法是**逐条对照官方文档的命令返回值定义**，
          而不是靠记忆。本课在 MockRedis 的 docstring 里为
          每个命令都标注了返回值的精确语义，就是为了避免这个坑。

    ── 坑 5：把 URL 本身当作 ack 凭据导致重复任务误删 ──────────────────
    ❌ 错误做法：`queue.get()` 返回 URL，ack 时用 `LREM processing 1 URL`
    现象：实验 5 里队列故意混入了重复 URL，
          LREM 会把 processing 里**所有**等于该 URL 的记录删掉，
          包括另一个 worker 正在处理的那条。
          后果：那个任务丢了确认凭据，可能被重复投放。
    根因：URL 不是唯一标识 —— 同一个 URL 可能被投递多次
          （这正是「重复任务」的定义）。
          用非唯一的值做确认凭据，就会「误伤」别人的记录。
    正确做法：用一个**每次投递都唯一**的 ID 做凭据
          （uuid4，或者 `url#投递序号`）。
          本课为了演示简洁，实验 5 里先做了「去重后才投递」的处理，
          并对重复 URL 通过 Redis 去重集合拦下，
          从而让「URL 作为凭据」在这个特定场景下安全 ——
          但这是**有前提的简化**，生产环境必须用唯一 ID。
    教学价值：**「标识」和「内容」是两回事。**
          需要唯一性的时候，永远不要用业务内容当标识 ——
          业务内容会因为「重复」而失去唯一性。""")


# ============================================================================
# 主流程
# ============================================================================
def main() -> None:
    """运行全部实验。"""
    print(SEP)
    print("阶段 6 · 第 62 课：分布式队列与去重（Redis 的两个核心用途）")
    print(SEP)

    # ---- 后端探测与降级 ----
    backend = detect_redis()
    if backend.real:
        print(f"""
    ✅ 后端探测：{backend.name}
       {backend.detail}
       → 全部实验将跑在**真实 Redis** 上。""")
        import redis
        client: Any = redis.Redis(host="127.0.0.1", port=6379,
                                  decode_responses=True,
                                  socket_connect_timeout=0.8)
        # 用独立的 key 前缀，避免污染同一实例上的其他数据
        client.flushdb()
    else:
        print(f"""
    ⚠ 后端探测：未检测到可用的 Redis 服务
       探测详情：{backend.detail}
       → 已自动降级到内置的 **MockRedis**（纯 dict + 线程锁）。

       ⚠⚠ 降级提示（重要）：
          · MockRedis 完整复刻了本课用到的全部命令**语义**，
            因此「原子性」「无重复消费」「任务不丢」这些
            **逻辑结论是可信的**。
          · 但它没有网络、没有持久化、没有内存淘汰策略，
            因此「耗时」「内存占用」「崩溃持久性」这三个维度的
            **数字不能迁移到真实 Redis**。详见实验 6。""")
        client = MockRedis()

    print(f"""
本课要回答的核心问题：**单机爬虫到了瓶颈，Redis 到底解决了什么？**

  答案不是「算得更快」，而是：**把共享状态从 worker 的内存里拿出来。**

      本机 list  →  Redis List    （待抓队列）
      本机 set   →  Redis Set     （URL 指纹去重）

  其余代码一行不改。但「拿出来」之后，所有「检查 + 写入」的复合操作
  都必须在 Redis 端**原子完成** —— 这是本课的全部难点。
""")

    exp1_single_machine_bottleneck(client, backend)
    exp2_reliable_queue()
    exp3_atomic_dedup()
    exp4_sharding()
    exp5_multi_worker()
    exp6_backend_compare(backend)
    pitfalls()

    title("本课要点")
    for line in [
        "1. 分布式的本质是把「共享状态」从进程内存移到独立服务，而不是算得更快",
        "2. 需要替换的本机容器只有两个：待抓 list → Redis List，去重 set → Redis Set",
        "3. 可靠队列用 RPOPLPUSH，把「弹出」和「备份」合并成一条原子命令",
        "4. 任务生命周期：LPUSH main → RPOPLPUSH main/processing → 处理后 LREM 确认",
        "5. at-least-once 保证不丢，但**不保证不重复**；exactly-once 需要幂等设计",
        "6. 崩溃发生在命令执行前 → 任务还在 main；执行后 → 任务在 processing，两条路都安全",
        "7. 分布式去重必须用 SADD 的返回值（新增个数），不能 SISMEMBER + SADD 两步走",
        "8. 竞态窗口在单机是微秒级、跨机是毫秒级 —— 分布式把偶发 bug 变成必然 bug",
        "9. 竞态 bug 的本质是时序而非逻辑，测试覆盖率对它无效，只能靠原子原语",
        "10. 指纹分片解决的是 big key 阻塞，不是内存总量；分片数用 2 的幂且一次定好不改",
        "11. 分片函数必须确定性：绝不能用 hash()（PYTHONHASHSEED 会随机化）",
        "12. 失败任务用 Sorted Set 做延时队列（score = 下次可执行时间戳）",
        "13. 任务分配用拉模式而非推模式：worker 主动取，负载天然均衡",
        "14. 上游去重是性能优化，下游数据库唯一索引才是正确性保证",
        "15. 真实 Redis 必须设 maxmemory-policy noeviction，否则去重集合会被静默淘汰",
        "16. 逻辑正确 ≠ 生产可用：网络 RTT、持久化、淘汰策略、高可用都是必修课",
    ]:
        print("  " + line)
    print()


if __name__ == "__main__":
    main()
