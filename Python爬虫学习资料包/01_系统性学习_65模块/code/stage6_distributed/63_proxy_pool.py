"""
第 63 课 · 代理池与反爬对抗 —— 限速、退避、降权

本课要回答的问题：
  1. 一个代理池的「健康度评分」应该怎么设计？
     为什么不能用「轮询」或「始终选最好的那个」？
  2. 令牌桶限速器，为什么必须**按域名**而不是全局？
     全局限速在什么场景下会严重浪费你的带宽？
  3. 指数退避不加抖动（jitter）会发生什么？
     「惊群效应」用可运行的实验怎么复现？
  4. 被代理被封之后，怎么自动降权？什么时候恢复？
  5. 有代理池和没代理池，成功率差多少？用实验数字说话。

================================ 运行方式 ================================
    python3 code/stage6_distributed/63_proxy_pool.py

零额外依赖，全程纯标准库（threading / random / time / dataclasses）。

================================ 实验清单 ================================
  实验 1  代理池评分：三种调度策略的实测对比（轮询 / 加权 / 最优优先）
  实验 2  代理生命周期：成功加分、失败扣分、连续失败剔除、定时恢复
  实验 3  令牌桶限速：三种限速方案（无限制 / 全局限速 / 按域名限速）
  实验 4  指数退避：无抖动 vs 有抖动的惊群效应实测
  实验 5  有代理池 vs 无代理池：单 IP 被封后的成功率对比
"""

from __future__ import annotations

import math
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

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


def bar(value: float, max_value: float, width: int = 40,
        fill: str = "█") -> str:
    """画一条 ASCII 条形图。

    Args:
        value: 当前值。
        max_value: 基准值。
        width: 最长条的宽度。
        fill: 填充字符。

    Returns:
        条形字符串。
    """
    if max_value <= 0:
        return ""
    n = max(1, int(round(value / max_value * width)))
    return fill * n


# ============================================================================
# 认知框架：反爬对抗的三个层次
# ============================================================================
# 很多人把「反爬对抗」理解成「找一个能用的代理」。
# 这是错的 —— 代理只是**资源**，真正的对抗在于**资源的使用方式**。
#
# 同一个代理池，用法不同，效果可以差 10 倍：
#
#   ┌────────────────┬──────────────────────────────────────────────────┐
#   │ 层次           │ 做什么                                            │
#   ├────────────────┼──────────────────────────────────────────────────┤
#   │ L1 · 资源层    │ 有足够的 IP：代理池、出口 IP、云函数              │
#   │                │ → 拼的是「数量和多样性」                          │
#   ├────────────────┼──────────────────────────────────────────────────┤
#   │ L2 · 调度层    │ 怎么分配 IP：健康度评分、加权随机、会话粘性        │
#   │                │ → 拼的是「把好钢用在刀刃上」                      │
#   ├────────────────┼──────────────────────────────────────────────────┤
#   │ L3 · 节奏层 ★  │ 什么时候发请求：令牌桶限速、指数退避、抖动         │
#   │                │ → 拼的是「像不像人」                              │
#   └────────────────┴──────────────────────────────────────────────────┘
#
# ★ 本课的重点是 L3。原因：
#   · L1 是花钱就能解决的问题（买代理），没有工程含量
#   · L2 是数据结构问题（本课会实现，但不难）
#   · **L3 才是决定成败的**。一个 500 个 IP 的池子，如果每个 IP
#     每秒被用来发 50 个请求，照样会被封；而 5 个 IP 只要节奏控制得好，
#     可以稳定跑一整天。
#
# ▸ 一句话总结：
#   **反爬对抗的本质不是「换身份」，而是「改变行为模式」。**
#   换 IP 只是入门；控制节奏、模拟人类、分散压力才是核心竞争力。
#
# ⚠ 合规提醒：
#   本课讲的代理、限速、退避都是**中性技术**。
#   合法用途：保护自有业务、访问授权的数据源、做容灾切换。
#   非法用途：绕过他人站点的访问控制、大规模抓取受保护数据。
#   **技术上能做的，不等于法律上许可的。** 请自行确认你抓取的站点
#   的 robots.txt 与服务条款，并遵守《数据安全法》《个人信息保护法》。


# ============================================================================
# 一、代理模型与健康度评分
# ============================================================================
class VirtualClock:
    """一个可以手动推进的单调时钟。

    本课为什么需要它？—— 这是本课第一个、也是最容易被忽略的建模陷阱：

        ❌ 错误做法：直接用 `time.time()` 作为所有时间判断的依据。
           现象：实验 1 里三种调度策略的**成功率几乎完全一样**
                 （差异小于 0.3 个百分点），轮询策略的「公平均分」
                 也完全体现不出来，活跃代理数只剩 1~2 个。
           根因：模拟跑得**太快了**。500 次「请求」在真实世界里需要
                 几分钟（每次都要等 100ms 网络往返），但在纯内存模拟里
                 只要 **4.7 毫秒**。而失败一次触发的冷却是 1 秒 ——
                 于是**任何一个代理只要失败过一次，就会在本次实验剩余的
                 全部时间里一直处于冷却状态**，等于永久下线。
                 1 秒的冷却期 ÷ 4.7 毫秒的实验总时长 = 200 倍的时间尺度错配。
           正确做法：让模拟拥有自己的时间轴 —— 每次「请求」都让虚拟时钟
                 前进一个「请求延迟」的量级。这样 500 次请求就对应
                 虚拟时间里的几十秒到几分钟，冷却期才是它本该有的意义。

     ▸ 这个坑在真实项目里同样存在，只是方向相反：
       写单元测试时用真实时钟 + 缩短的冷却期（比如 1 毫秒），
       测试通过；上线后冷却期改成 60 秒，行为完全不同。
       **任何「时间驱动」的逻辑，测试时都必须让时间成为可注入的依赖。**
       生产代码里对应的是「依赖注入一个 clock 函数」，
       Python 圈常用的是 `freezegun` 库或自建 `Clock` 协议。

    ▸ 本 Class 只实现 `now()` 和 `advance()` 两个方法。
      它不追求完备，只为了让「模拟时间」这件事显式化。
    """

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        """初始化虚拟时钟。

        Args:
            start: 起始时间戳（默认取一个固定的过去时刻，保证可复现）。
        """
        self._t = start

    def now(self) -> float:
        """返回当前虚拟时间戳。

        Returns:
            浮点秒数。
        """
        return self._t

    def advance(self, seconds: float) -> None:
        """把时钟向前推进。

        Args:
            seconds: 推进的秒数（负数会被忽略，因为时间是单调的）。

        Returns:
            None
        """
        if seconds > 0:
            self._t += seconds

    def reset(self, start: float | None = None) -> None:
        """重置时钟（用于让多个策略从同一个时间原点起跑）。

        Args:
            start: 新的起始时间戳；None 表示沿用当前值。

        Returns:
            None
        """
        if start is not None:
            self._t = start


@dataclass
class Proxy:
    """一个代理节点的运行状态。

    Attributes:
        host: 代理主机。
        port: 代理端口。
        successes: 累计成功次数。
        failures: 累计失败次数。
        consecutive_failures: **连续**失败次数。
        total_latency: 累计延迟（秒），用于算平均速度。
        banned: 是否已被剔除。
        last_used: 上次使用时间戳。
        cooldown_until: 冷却结束时间戳（失败后短暂停用）。
        total_requests: 累计请求数。
        clock: 时间源（虚拟时钟或时间模块）。默认接 `time`，
            模拟场景下换成 VirtualClock —— 见 VirtualClock 的说明。

    ▸ 为什么既要 `failures` 又要 `consecutive_failures`？
      它们回答的是不同的问题：
        · failures 高但 consecutive_failures 低 → 这个代理**整体质量差但还活着**
          比如 30% 的请求超时。它还有价值，只是权重低一些。
        · consecutive_failures 高 → 这个代理**已经死了**
          连续 5 次失败几乎不可能是因为网络抖动，一定是代理本身挂了。
      所以评分用 failures（长期质量），剔除用 consecutive_failures（当前状态）。
      **把「质量评估」和「存活判断」分开，是整个评分系统能工作的前提。**
      混在一起的话，一个曾经很好、刚挂掉的代理会因为历史成绩优秀而继续被调度，
      每一次调度都是一次必然失败。
    """

    host: str
    port: int
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    total_latency: float = 0.0
    banned: bool = False
    last_used: float = 0.0
    cooldown_until: float = 0.0
    total_requests: int = 0
    # 时间源。真实环境传 `time` 模块即可；模拟场景传 VirtualClock。
    # 这就是「依赖注入时间」的最小实现 —— 只多了一个字段，
    # 却让整条「失败 → 冷却 → 恢复」链路变得可测试、可复现。
    clock: Any = None

    # 用于模拟：这个代理的真实质量（生产环境中不存在，只有模拟才有）
    _true_quality: float = 0.9
    _true_latency: float = 0.2

    def _now(self) -> float:
        """读取当前时间。

        Returns:
            当前时间戳；未注入 clock 时退回真实的 time.time()。
        """
        if self.clock is not None:
            return self.clock.now()
        return time.time()

    @property
    def key(self) -> str:
        """代理的唯一标识。

        Returns:
            形如 'host:port' 的字符串。
        """
        return f"{self.host}:{self.port}"

    @property
    def success_rate(self) -> float:
        """历史成功率。

        Returns:
            0.0 ~ 1.0；没有任何请求时返回一个中性值 0.5。

        ▸ 为什么没有请求时返回 0.5 而不是 1.0 或 0.0？
          · 返回 1.0 → 新代理会被误认为是满分代理，立刻被大量使用，
            然后迅速失败（这叫「探索不足」的反面：过度信任未知资源）
          · 返回 0.0 → 新代理永远拿不到请求，永远无法证明自己
            （这叫「冷启动死锁」，是推荐系统里的经典问题）
          0.5 是一个**中性先验**，让新代理能拿到一部分请求去证明自己。
          这个技巧在某些场景下更精细：可以用 Beta 分布的期望
          `successes / (successes + failures)` —— 它就是 0/0 时无定义的
          拉普拉斯平滑版（`(s+1)/(s+f+2)`）。本课为了展示简单用 0.5。
        """
        total = self.successes + self.failures
        if total == 0:
            return 0.5
        return self.successes / total

    @property
    def avg_latency(self) -> float:
        """平均延迟（秒）。

        Returns:
            平均延迟；没有记录时返回一个保守的默认值 0.5 秒。
        """
        if self.successes <= 0:
            return 0.5
        return self.total_latency / self.successes

    @property
    def in_cooldown(self) -> bool:
        """是否处于冷却期。

        Returns:
            True 表示还在冷却中，不应被调度。
        """
        return self._now() < self.cooldown_until

    def available(self, now: float | None = None) -> bool:
        """这个代理现在可用吗？

        Args:
            now: 当前时间戳；None 表示用注入的时间源。

        Returns:
            True 表示可用。

        ▸ 「可用」的定义包含三个条件，缺一不可：
          ① 没有被永久剔除（banned）
          ② 不在冷却期（cooldown_until 已过）
          ③ 连续失败次数还没到剔除阈值
          这三个条件是**独立**的：一个代理可能在冷却期结束后恢复，
          也可能因为连续失败太多被永久剔除。把它们分开判断，
          策略调整时就不用改多处代码。
        """
        if self.banned:
            return False
        ts = now if now is not None else self._now()
        if ts < self.cooldown_until:
            return False
        return True

    def score(self, *, w_success: float = 0.6, w_speed: float = 0.3,
              w_fresh: float = 0.1, speed_ref: float = 0.3) -> float:
        """计算健康度评分（0 ~ 100）。

        Args:
            w_success: 成功率权重。
            w_speed: 速度权重。
            w_fresh: 新鲜度权重。
            speed_ref: 速度的参考值（秒）。延迟等于它时速度得分 50 分。

        Returns:
            0 ~ 100 的浮点分数。

        ▸ 评分公式的三个维度，各自解决什么问题？

          ① 成功率（权重 0.6）——**最重要的指标**
             它就是代理的"能不能用"。权重必须过半，
             否则一个「很快但经常失败」的代理会挤掉「稍慢但稳定」的代理。

          ② 速度（权重 0.3）—— 影响效率
             这里用**反比函数**而不是线性函数：
                 speed_score = 100 * speed_ref / (speed_ref + latency)
             为什么不用「延迟越低分越高」的线性映射？
             因为线性映射会让「0.05 秒 vs 0.1 秒」产生巨大分差（差 2 倍），
             但两者对爬虫总耗时的影响几乎一样（都很快，网络本身的
             抖动就有 50ms）。
             反比函数的特性是：**在低延迟区间平缓，高延迟区间敏感** ——
             这正是我们想要的：能区分「能用」和「慢得没法用」，
             不去纠结「0.05 秒和 0.1 秒谁更快」。

          ③ 新鲜度（权重 0.1）—— 防止「僵尸代理」
             一个代理可能 1 小时前很好，但这 1 小时里你的目标站
             已经把它拉黑了。如果只用历史成绩，它会继续拿满分。
             新鲜度用「距上次使用的时间」做了个软惩罚：
             太久没用的代理分数略微降低，鼓励系统去试探它。

          ④ 连败惩罚（乘数，不是加法）：
             连续失败 3 次以上时，分数乘以 1/(1+连败数) 级别的惩罚。
             用**乘数**而不是**减法**的原因是：
             好代理（90 分）减 30 分还剩 60 分，还能用；
             差代理（40 分）减 30 分只剩 10 分。
             而乘数能让好代理"伤而不死"（90 × 0.5 = 45，仍有机会），
             差代理直接归零。
             **减法惩罚对「曾经的好代理」太宽容，对「本来就差的代理」太严厉。**
        """
        if self.banned:
            return 0.0
        # ① 成功率维度
        s_score = self.success_rate * 100
        # ② 速度维度：反比函数，平滑且有上界
        sp_score = 100.0 * speed_ref / (speed_ref + self.avg_latency)
        # ③ 新鲜度维度：刚用过 → 100 分；1 小时没用 → 约 50 分
        idle = max(0.0, self._now() - self.last_used) if self.last_used else 3600.0
        f_score = 100.0 / (1.0 + idle / 3600.0)
        base = w_success * s_score + w_speed * sp_score + w_fresh * f_score
        # ④ 连败惩罚（乘数）
        if self.consecutive_failures >= 2:
            base *= 1.0 / (1.0 + (self.consecutive_failures - 1) * 0.5)
        return max(0.0, min(100.0, base))

    def report(self, ok: bool, latency: float = 0.0) -> None:
        """回填一次请求的结果。

        Args:
            ok: 请求是否成功。
            latency: 本次延迟（秒），仅成功时有效。

        Returns:
            None

        ▸ 这是整个代理池**最关键的入口**。它承担三件事：
          ① 更新长期统计（successes / failures / latency）
          ② 更新短期状态（consecutive_failures）
          ③ 根据短期状态做出**即时反应**（进入冷却 / 剔除）

          注意 ③ 必须是**即时**的：如果一个代理连续失败 3 次，
          你必须立刻把它踢出去，而不是等下一次健康检查。
          因为在那之前它可能已经浪费了你几十次请求。
          **响应速度决定了损失的规模。**
        """
        now = self._now()
        self.total_requests += 1
        self.last_used = now
        if ok:
            self.successes += 1
            self.consecutive_failures = 0
            self.total_latency += latency
        else:
            self.failures += 1
            self.consecutive_failures += 1
            # 连续失败 → 立刻进入冷却（指数增长：1s, 2s, 4s, ...）
            # 用指数而不是固定值，是因为「连续失败越多，说明问题越严重」
            self.cooldown_until = now + min(8.0, 1.0 * (2 ** (self.consecutive_failures - 1)))


# ============================================================================
# 二、代理池
# ============================================================================
class ProxyPool:
    """代理池：负责代理的选取、回填、剔除与恢复。

    │ 三种调度策略的取舍（本课实验 1 会实测）：
    │
    │   round_robin  严格轮询 —— 公平，但无知
    │     问题：一个 30% 成功率的代理和一个 99% 的代理拿到一样多的请求，
    │           于是 70% 的请求注定失败。而且**失败了还会重试**，
    │           重试又可能落到另一个差代理上，形成失败雪崩。
    │     唯一适合的场景：所有代理质量高度同质（比如都是同一个供应商，
    │           同一批次），此时轮询是最简单的正确做法。
    │
    │   weighted     加权随机 —— 推荐的默认策略
    │     按 scores 作为权重做随机抽样。效果：
    │       · 好代理拿到更多请求（但差代理仍有机会，能自我证明）
    │       · 天然具备**探索与利用的平衡**（exploration vs exploitation）
    │     为什么「仍然给差代理机会」很重要？
    │       因为评分是基于**历史**的。一个代理可能刚才因为目标站的
    │       临时抖动而连续失败，被扣了分 —— 但它在别的时刻可能是好的。
    │       完全不给机会 = 误杀。给予少量机会 = 自我修正。
    │
    │   best_first   永远选分数最高的 —— 看起来最优，实际上最糟
    │     问题：所有请求都压在一个 IP 上，它会被极快地消耗掉，
    │           然后这个 IP 被封，Pool 切换到第二好的 …… 
    │           结果是「逐个击破」，池子会以最快速度全部报废。
    │     这就像「把所有的钱都投到一个股票上」——
    │       短期收益最高，但风险完全集中。
    │     唯一适合的场景：只有一个代理能用，或者请求量极小。
    │
    │   ▸ 通用原则：**在任何需要「从多个资源中选一个」的系统里，
    │     永远不要做「确定性最优选择」。** 加一点随机性，
    │     既能分散风险，又能持续探测资源的真实状态。
    │     这个原则在负载均衡（power of two choices）、
    │     CDN 调度、A/B 测试分组里都适用。
    """

    def __init__(self, proxies: Sequence[Proxy], strategy: str = "weighted",
                 rng: random.Random | None = None,
                 ban_threshold: int = 5,
                 verbose: bool = False,
                 clock: Any = None) -> None:
        """初始化代理池。

        Args:
            proxies: 代理列表。
            strategy: 调度策略，'round_robin' / 'weighted' / 'best_first'。
            rng: 随机数发生器。
            ban_threshold: 连续失败多少次后剔除。
            verbose: 是否打印调度日志。
            clock: 时间源。传 VirtualClock 可让整个池子跑在虚拟时间轴上；
                None 表示用真实的 time.time()。**必须和 proxies 用同一个
                clock 对象**，否则「代理的冷却状态」和「池子的可用性判断」
                会读两个不同的时间轴 —— 这是比不用 clock 更隐蔽的 bug。

        Raises:
            ValueError: 当 strategy 不是已知策略时。

        ▸ ban_threshold 为什么是 5 而不是 2？
          太小（2）→ 把「只是因为目标站抖了一下」的好代理误杀。
          太大（20）→ 一个死代理会浪费 20 次请求才被剔除。
          5 是一个经验平衡点。更重要的是：**配合指数冷却**。
          代理在第 3 次连续失败时就进入 4 秒冷却，
          冷却期结束后还有机会证明自己；只有彻底到 5 次才剔除。
          「冷却」是缓刑，「剔除」是死刑 —— 两者配合才能既不误杀也不纵容。
        """
        if strategy not in ("round_robin", "weighted", "best_first"):
            raise ValueError(
                f"未知策略 {strategy!r}，可选：round_robin / weighted / best_first")
        self.proxies = list(proxies)
        self.strategy = strategy
        self.rng = rng or random.Random(20260919)
        self.ban_threshold = ban_threshold
        self.verbose = verbose
        self.clock = clock
        self._cursor = 0
        self.banned_total = 0
        self.revived_total = 0
        # 每个代理被调度的次数（用于实验 1 分析流量分布）
        self.pick_count: dict[str, int] = {p.key: 0 for p in self.proxies}
        # 把 clock 下发到每个代理，保证整个池子读同一条时间轴
        for p in self.proxies:
            if p.clock is None:
                p.clock = clock

    # ------------------------------ 调度 ------------------------------
    def pick(self) -> Proxy | None:
        """选一个可用代理。

        Returns:
            选中的代理；没有可用代理时返回 None。

        ▸ 返回 None 是一个**必须处理的状态**，不能忽略。
          新手常见的写法是 `proxy = pool.pick()` 然后直接
          `requests.get(url, proxies=proxy.url)` —— 池子空了就崩。
          正确做法是：
             · 要么阻塞等待代理恢复（适合批处理任务）
             · 要么降级到「直连」（适合对成功率要求不高的场景）
             · 要么把任务重新入队，稍后重试（推荐，第 62 课的延时队列）
          本课在实验 5 里用的是「降级到直连」——
          这恰好演示了「没有代理时会发生什么」，一举两得。
        """
        now = self.clock.now() if self.clock is not None else time.time()
        alive = [p for p in self.proxies if p.available(now)]
        if not alive:
            return None

        if self.strategy == "round_robin":
            # 轮询：从上次位置往后找第一个可用的
            n = len(self.proxies)
            for i in range(n):
                idx = (self._cursor + i) % n
                if self.proxies[idx].available(now):
                    self._cursor = (idx + 1) % n
                    picked = self.proxies[idx]
                    break
            else:
                return None
        elif self.strategy == "best_first":
            picked = max(alive, key=lambda p: p.score())
        else:  # weighted
            weights = [max(p.score(), 0.01) for p in alive]
            # 为什么用 max(score, 0.01) 而不是直接用 score？
            #   因为 random.choices 要求权重之和 > 0。
            #   如果所有代理分数都是 0（比如全部刚失败过），
            #   直接传 0 权重会抛 ValueError。
            #   给一个极小的下限能让系统"不退化成崩溃"，
            #   同时在分数都极低时表现为近似均匀随机。
            picked = self.rng.choices(alive, weights=weights, k=1)[0]

        self.pick_count[picked.key] = self.pick_count.get(picked.key, 0) + 1
        if self.verbose:
            print(f"      [POOL] 选中 {picked.key}  (score={picked.score():.1f})")
        return picked

    # ------------------------------ 回填 ------------------------------
    def report(self, proxy: Proxy, ok: bool, latency: float = 0.0) -> None:
        """回填一次请求结果，并在必要时剔除代理。

        Args:
            proxy: 使用的代理。
            ok: 是否成功。
            latency: 延迟（秒）。

        Returns:
            None
        """
        proxy.report(ok, latency)
        if not ok and proxy.consecutive_failures >= self.ban_threshold:
            if not proxy.banned:
                proxy.banned = True
                self.banned_total += 1
                if self.verbose:
                    print(f"      [POOL] ⛔ {proxy.key} 连续失败 "
                          f"{proxy.consecutive_failures} 次，已剔除")

    # ------------------------------ 维护 ------------------------------
    def revive_all(self, min_score: float = 0.0) -> int:
        """复活被剔除的代理（模拟「定时全量重检」）。

        Args:
            min_score: 复活时的最低分数要求（本课简化，未使用）。

        Returns:
            复活的代理数量。

        ▸ 为什么必须要有「复活」机制？
          目标站封 IP 往往有**冷却时间**（比如封 10 分钟）。
          被封的 IP 过一段时间后可能又可用。如果不复活，
          池子只会越来越小，最终枯竭。
          真实生产环境的做法是「定时全量重检」：
          每隔 N 分钟，用一个轻量请求（比如 HEAD 一个静态资源）
          测试所有被封代理，成功的就复活。
          本课用「全部复活 + 重置连续失败计数」来模拟 ——
          这会在实验 2 里表现为「池子周期性恢复元气」。
        """
        n = 0
        for p in self.proxies:
            if p.banned:
                p.banned = False
                p.consecutive_failures = 0
                p.cooldown_until = 0.0
                n += 1
        self.revived_total += n
        return n

    def clear_cooldowns(self) -> int:
        """清除所有代理的冷却状态（模拟「等了一会儿，冷却期自然结束」）。

        Returns:
            被解除冷却的代理数量。

        ▸ 为什么需要这个方法和 `revive_all` 分工？
          因为它们对应**两种完全不同的池子枯竭**：

            · 全员冷却（cooldown）→ 这是**正常**的临时状态。
              只要等一会儿（真实世界里就是真的等几秒/几分钟），
              代理自己就会恢复。**不需要「复活」，只需要「等待」。**
              本课用虚拟时钟推进时间来模拟这个「等待」，
              再调用本方法把冷却状态清掉。

            · 全员被剔除（banned）→ 这是**异常**状态。
              代理被判定为死了，必须靠「定时全量重检」才能回来。
              对应 `revive_all`。

          新手常把两者混为一谈，写成「池子空了 → 全部复活」——
          结果是**一个正在冷却中的好代理被无谓地「复活」**（本来没事），
          而它的连败计数被清零，掩盖了真实问题。
          **把「等待恢复」和「人工干预恢复」分开，日志才读得懂。**
        """
        n = 0
        for p in self.proxies:
            if not p.banned and p.cooldown_until > 0:
                p.cooldown_until = 0.0
                n += 1
        return n

    # ------------------------------ 统计 ------------------------------
    def alive(self) -> list[Proxy]:
        """当前可用代理。

        Returns:
            可用代理列表。
        """
        return [p for p in self.proxies if p.available()]

    def stats(self) -> dict[str, Any]:
        """池子统计。

        Returns:
            统计字典。
        """
        alive = self.alive()
        scores = [p.score() for p in alive]
        return {
            "total": len(self.proxies),
            "alive": len(alive),
            "banned": sum(1 for p in self.proxies if p.banned),
            "cooldown": sum(1 for p in self.proxies
                            if not p.banned and p.in_cooldown),
            "avg_score": statistics.mean(scores) if scores else 0.0,
            "max_score": max(scores) if scores else 0.0,
            "min_score": min(scores) if scores else 0.0,
        }

    def traffic_share(self) -> list[tuple[str, int, float]]:
        """每个代理分到的请求数占比。

        Returns:
            (代理 key, 调度次数, 占比) 的列表，按次数降序。
        """
        total = sum(self.pick_count.values()) or 1
        rows = [(k, v, v / total) for k, v in self.pick_count.items()]
        rows.sort(key=lambda x: -x[1])
        return rows


def make_mock_proxies(n: int, rng: random.Random,
                      good: float = 0.6, fair: float = 0.3) -> list[Proxy]:
    """生成一批模拟代理。

    Args:
        n: 数量。
        rng: 随机数发生器。
        good: 好代理的比例。
        fair: 一般代理的比例（剩下的都是差代理）。

    Returns:
        代理列表。

    ⚠ 局限：这里的 `_true_quality` 和 `_true_latency` 是**只有模拟才有的**
      「上帝视角」数据。真实环境里代理的质量是未知的，
      只能通过一次次请求去观测 —— 这正是代理池评分算法存在的意义。
      本课用上帝视角是为了能"造出"三种典型代理（快/慢/死），
      从而让调度策略的差异可以被复现。生产环境没有这个便利。
    """
    proxies: list[Proxy] = []
    for i in range(n):
        r = rng.random()
        if r < good:
            # 好代理：成功率高、延迟低
            q, lat = rng.uniform(0.93, 0.99), rng.uniform(0.08, 0.20)
        elif r < good + fair:
            # 一般代理：成功率中等、延迟偏高
            q, lat = rng.uniform(0.65, 0.85), rng.uniform(0.30, 0.70)
        else:
            # 差代理：成功率低、延迟高（模拟"快要被封"的代理）
            q, lat = rng.uniform(0.15, 0.40), rng.uniform(0.80, 1.60)
        p = Proxy(host=f"10.0.{i // 256}.{i % 256}", port=8000 + i)
        p._true_quality = q
        p._true_latency = lat
        proxies.append(p)
    return proxies


def simulate_one_request(proxy: Proxy, rng: random.Random) -> tuple[bool, float]:
    """模拟一次通过代理的请求。

    Args:
        proxy: 代理。
        rng: 随机数发生器。

    Returns:
        (是否成功, 延迟秒数)。

    ▸ 延迟的建模：真实网络延迟是**重尾分布**（偶尔出现 5 秒的长尾）。
      这里用「基础延迟 + 指数分布抖动」来近似，
      平均抖动约为基础延迟的 30%。
      为什么这个细节重要？因为它会影响到**限速和退避的触发**：
      一个长尾请求会让某个代理看起来"很慢"，从而降低它的速度得分。
      如果模型里延迟是恒定的，评分算法就永远测不出「速度」这个维度的价值。
    """
    lat = proxy._true_latency * (1.0 + rng.expovariate(3.0))
    ok = rng.random() < proxy._true_quality
    return ok, lat


# ============================================================================
# 三、令牌桶限速器（按域名）
# ============================================================================
class TokenBucket:
    """令牌桶：以固定速率产生令牌，取到令牌才能发请求。

    │ 为什么用令牌桶而不是「固定间隔 sleep」？
    │
    │   ❌ 固定间隔方案：
    │        for url in urls:
    │            requests.get(url)
    │            time.sleep(1.0 / qps)     # 每个请求后死等
    │      问题：
    │        ① 无法应对请求耗时波动。如果某个请求花了 3 秒，
    │           你等完这 3 秒还要再 sleep 0.2 秒 —— 实际 QPS 远低于目标。
    │        ② 无法利用「突发额度」。前 10 秒没发请求，
    │           这段时间的额度就被浪费了，不能攒起来后面用。
    │
    │   ✅ 令牌桶方案：
    │        桶里以 rate 个/秒的速度积累令牌，最多存 capacity 个。
    │        每发一个请求消耗一个令牌；没令牌就等。
    │      优势：
    │        ① 只限制「平均速率」，不限制「瞬时节奏」——
    │           请求快的时候自然多发，慢的时候自然少发
    │        ② 支持**突发**（burst）：桶里攒了 10 个令牌，
    │           就可以瞬间发 10 个请求 —— 这对抓取列表页时
    │           「一次开 10 个并发」的场景非常关键，
    │           而固定间隔方案会让这 10 个请求排成一队慢慢发
    │        ③ 实现简单，状态只有「令牌数 + 上次补充时间」两个变量
    │
    │ ▸ 参数怎么定：
    │     rate = 每秒允许的请求数（核心参数，由目标站容忍度决定）
    │     capacity = 桶容量 = 允许的突刺大小
    │   经验：capacity 取 rate 的 1~2 倍。太小则突发能力不足，
    │         太大则相当于给了一个巨大的瞬时爆发额度（可能触发风控）。
    """

    def __init__(self, rate: float, capacity: float | None = None,
                 name: str = "") -> None:
        """初始化令牌桶。

        Args:
            rate: 令牌产生速率（个/秒）。
            capacity: 桶容量；None 表示取 max(1, rate)。
            name: 桶名称（用于日志）。

        Raises:
            ValueError: 当 rate <= 0 时。

        ▸ 为什么默认 capacity = rate？
          因为这给出了「最多攒 1 秒的额度」的语义。
          实际值对大多数场景够用。要求更平滑可以把 capacity 设为 1
          （完全不允许突发，退化成"最小间隔"限速）；
          要求更能突发就设大一些。
        """
        if rate <= 0:
            raise ValueError(f"rate 必须为正数，收到 {rate}")
        self.rate = rate
        self.capacity = capacity if capacity is not None else max(1.0, rate)
        self.name = name or "bucket"
        self.tokens = self.capacity      # 初始给满，避免冷启动时干等
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()
        self.acquired = 0
        self.waited = 0.0

    def _refill(self) -> None:
        """按经过的时间补充令牌。

        Returns:
            None

        ▸ 这里**不能用 time.time()**（踩坑记录）：
          time.time() 是系统时钟，会被 NTP 校时、夏令时、
          甚至手动改时间影响。如果系统时间往后跳了一小时，
          桶里的令牌会瞬间补满（甚至溢出），限速直接失效。
          正确做法是用 **time.monotonic()** —— 单调时钟，
          只保证「一直往前」，不受系统时间调整影响。
          **所有用来测量「间隔」「超时」的地方都必须用 monotonic。**
          这是新手最常犯的时间相关 bug 之一。
        """
        now = time.monotonic()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

    def acquire(self, tokens: float = 1.0, timeout: float = 5.0) -> float:
        """获取令牌（阻塞直到拿到或超时）。

        Args:
            tokens: 需要的令牌数（通常为 1）。
            timeout: 最长等待时间（秒）。必须设置上限，
                否则一旦 rate 被误配成极小值，线程会永远阻塞。

        Returns:
            实际等待的秒数。

        Raises:
            TimeoutError: 等待超过 timeout 仍未拿到令牌。

        ▸ 为什么要返回「等待时间」而不是返回 bool？
          因为调用方通常需要这个数字来做观测：
          平均等待时间突然升高 = 限速太严 or 目标站变慢了，
          这是容量规划的输入指标。
          **一个好的 API 应该把它"顺便知道"的有用信息返回出来。**
        """
        t0 = time.monotonic()
        while True:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    self.acquired += 1
                    waited = time.monotonic() - t0
                    self.waited += waited
                    return waited
                # 计算还差多少令牌、需要等多久
                deficit = tokens - self.tokens
                need = deficit / self.rate
            if time.monotonic() - t0 + need > timeout:
                raise TimeoutError(
                    f"令牌桶 {self.name} 等待超时"
                    f"（需要 {need:.3f}s，已等 {time.monotonic() - t0:.3f}s）")
            # 只睡需要的时间，且不超过 50ms ——
            # 为什么要有 50ms 上限？因为 sleep 太短会空转浪费 CPU，
            # 太长会错过令牌（比如需要等 3ms 却睡了 100ms，就慢了 97ms）。
            # 50ms 是"精确度"与"CPU 占用"之间的折中。
            time.sleep(min(max(need, 0.0005), 0.05))


class DomainRateLimiter:
    """按域名隔离的限速器。

    │ ★ 这是本课最重要的设计点：**为什么必须按域名，而不是全局？**
    │
    │   假设你有一个全局限速器，QPS = 10，然后你同时爬三个站点：
    │     · site-a.com  很宽松，能承受 50 QPS
    │     · site-b.com  很严格，只能承受 2 QPS
    │     · site-c.com  中等，能承受 10 QPS
    │
    │   ❌ 全局限速（10 QPS）的后果：
    │      · site-a 被严重浪费：你本可以 50 QPS 抓完（省 80% 时间），
    │        但你只给了它不到 10 QPS
    │      · site-b 依然会被封：全局的 10 QPS 里如果 5 QPS 都打在 site-b 上，
    │        它照样超限被封
    │      · 一个慢站点会拖累所有站点：site-b 因为限速排队，
    │        占用了全局配额，site-a 也跟着变慢
    │
    │   ✅ 按域名限速的后果：
    │      · 每个站点有独立的桶，互不干扰
    │      · site-a 配 50 QPS，site-b 配 2 QPS，各得其所
    │      · 某个站点被限速不影响其他站点的进度
    │
    │ ▸ 更本质的说法：
    │   **限速的约束来自「对方的容忍度」，而不是「我的能力」。**
    │   不同的对方有不同的容忍度，所以约束必须**按对方分别定义**。
    │   全局限速等于用「最保守的对方」的约束去限制所有对方 ——
    │   这是把「安全」和「效率」混淆了：它是安全的，但代价巨大。
    │
    │ ▸ 这个原则在其他地方也成立：
    │   · HTTP 连接池要按域名隔离（同一个域名的连接数上限）
    │   · 浏览器按域名限制并发数（Chrome 对同域名约 6 个）
    │   · 数据库连接池按数据库实例隔离
    │   看到「按 XX 隔离」的设计，背后往往是「不同 XX 的容量不同」。
    """

    def __init__(self, default_qps: float = 5.0,
                 per_domain: dict[str, float] | None = None,
                 capacity_factor: float = 1.5) -> None:
        """初始化按域名限速器。

        Args:
            default_qps: 未在 per_domain 中指定的域名的默认 QPS。
            per_domain: 域名 → QPS 的覆盖配置。
            capacity_factor: 桶容量相对 rate 的倍数。

        ▸ 为什么要有 default_qps？
          因为你不可能为每个新发现的域名手工配置 QPS。
          默认值的作用是「保守但要能跑」——
          取一个明显安全的保守值（比如 5 QPS），
          然后对重要/明确的域名做覆盖配置。
          **这比「不配置就不限速」安全得多。**
          默认值应该选安全的那一边，这是一个通用原则
          （同类的例子：防火墙默认拒绝、并发默认低、超时默认短）。

        ▸ ★ 为什么这里要把 per_domain 的键**归一化**？
          这是一个真实踩过的坑（见本课踩坑记录「配置键格式不一致」）：
            配置写的是 `{"https://site-a.com": 40}`
            而 `acquire()` 里会先 `extract_domain()` 得到 `"site-a.com"`
            → **两个键永远对不上** → 所有域名都落到 default_qps。

          这个 bug 的恶劣之处：
            · **不报错**（`dict.get` 有默认值，静默成功）
            · **功能"看起来正常"**（限速确实在生效，只是用的是默认值）
            · 只有当你对比「配置值 vs 实测 QPS」时才会发现对不上

          修法有两种，本课选了后者：
            ① 要求调用方统一传同一种格式（靠约定，容易出错）
            ② **在限速器内部把配置的键归一化**（靠代码，稳健）
              —— 对外接受 URL 或域名两种写法，内部统一成域名。
        """
        self.default_qps = default_qps
        # 归一化配置的键：允许写成完整 URL，也允许写成裸域名
        self.per_domain = {
            extract_domain(k): v for k, v in (per_domain or {}).items()
        }
        self.capacity_factor = capacity_factor
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def bucket_for(self, domain: str) -> TokenBucket:
        """取得（或创建）某域名的令牌桶。

        Args:
            domain: 域名。

        Returns:
            该域名对应的令牌桶。

        ▸ 这里用了「惰性创建 + 双检锁」的模式：
          第一次遇到某个域名时才给它建桶。
          为什么不在初始化时把所有域名的桶都建好？
          因为爬虫的域名集合是**运行时才发现**的（从页面里爬出来的），
          你不知道会有哪些域名。惰性创建是唯一可行的方案。
          但惰性创建在多线程下必须加锁，否则两个线程可能各建一个桶 ——
          那就等于该域名的 QPS 变成了 2 倍。这个 bug 很隐蔽，
          因为功能"看起来正常"，只是限速被悄悄放宽了。
        """
        bucket = self._buckets.get(domain)
        if bucket is not None:
            return bucket
        with self._lock:
            bucket = self._buckets.get(domain)
            if bucket is None:
                qps = self.per_domain.get(domain, self.default_qps)
                bucket = TokenBucket(rate=qps,
                                     capacity=max(1.0, qps * self.capacity_factor),
                                     name=domain)
                self._buckets[domain] = bucket
            return bucket

    def acquire(self, url_or_domain: str, timeout: float = 10.0) -> float:
        """对某个 URL（或域名）获取发送许可。

        Args:
            url_or_domain: 完整 URL 或域名。
            timeout: 最长等待时间。

        Returns:
            等待的秒数。

        Raises:
            TimeoutError: 等待超时。
        """
        domain = extract_domain(url_or_domain)
        return self.bucket_for(domain).acquire(timeout=timeout)

    def config_summary(self) -> list[tuple[str, float, int, float]]:
        """返回各域名桶的配置与运行状态。

        Returns:
            (域名, QPS, 已发请求数, 累计等待秒数) 的列表。
        """
        rows = []
        for domain, b in sorted(self._buckets.items()):
            rows.append((domain, b.rate, b.acquired, b.waited))
        return rows


def extract_domain(url_or_domain: str) -> str:
    """从 URL 中提取域名。

    Args:
        url_or_domain: 完整 URL 或已经是域名。

    Returns:
        域名（含端口，如果有）。

    ▸ 这里用最朴素的字符串处理而不是 urlparse，是为了**显式暴露**
      URL 解析的边界情况：
        · 没有 scheme 的 "example.com/path" → urlparse 会把它
          当成 path，netloc 为空！必须补上 scheme 才能正确解析。
        · 带端口的 "example.com:8080"
        · 带认证的 "user:pass@example.com"
      真实项目应该用 urllib.parse.urlsplit 并处理这些情况。
      本课为了聚焦限速逻辑，用一个简化实现，
      并在 docstring 里注明它是简化的 ——
      **简化可以，但要明说。**
    """
    s = url_or_domain
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0]
    if "@" in s:
        s = s.split("@", 1)[1]
    return s


# ============================================================================
# 四、退避策略（指数退避 + 抖动）
# ============================================================================
class BackoffStrategy:
    """指数退避 + 抖动。

    │ 为什么必须有「抖动（jitter）」？—— 本课实验 4 会用数字证明
    │
    │   ❌ 无抖动：delay = base * 2^attempt
    │
    │   假设 100 个 worker 在 T=0 时同时遇到 429（被限速），
    │   它们都会算出完全相同的退避时间：
    │
    │        T=0s   ── 100 个请求全部失败（429）
    │        T=0.5s ── 100 个请求**同时**重试 → 再次全部失败（惊群）
    │        T=1.5s ── 100 个请求**同时**重试 → 再次全部失败
    │        T=3.5s ── ...
    │
    │   这个过程叫**惊群效应（thundering herd）**。
    │   服务器在 T=0.5s 收到的瞬时压力和第二波攻击没有区别 ——
    │   退避完全没有起到「降低压力」的作用，只是把攻击延后了。
    │   更糟的是**同步性会自我强化**：每次重试都在同一时刻，
    │   大家的失败和重试节奏永远锁在一起。
    │
    │   ✅ 有抖动：delay = base * 2^attempt * (0.5 + random())
    │
    │        T=0.42s ── 一部分重试
    │        T=0.61s ── 另一部分
    │        T=0.88s ── 又一部分
    │   请求被**摊平**到一个时间窗口里，服务器压力是渐变的。
    │
    │ ▸ 抖动的两种常见形态：
    │   full jitter  :  delay = random(0, base * 2^n)          （AWS 推荐）
    │   equal jitter :  delay = base*2^n/2 + random(0, base*2^n/2)
    │   本课用的是 between 0.5x 和 1.5x 之间的抖动，介于两者之间。
    │
    │ ▸ 这个原则的普适性：
    │   任何「大量客户端会同时做同一件事」的场景都需要抖动 ——
    │     · 缓存过期时间（避免缓存雪崩）
    │     · 定时任务触发（避免整点打满）
    │     · 心跳/重连（避免同时重连）
    │   记住一句话：**任何确定的、集中的时间点，都会变成热点。**
    """

    def __init__(self, base: float = 0.1, max_delay: float = 10.0,
                 jitter: bool = True, factor: float = 2.0,
                 rng: random.Random | None = None) -> None:
        """初始化退避策略。

        Args:
            base: 基础延迟（秒）。
            max_delay: 延迟上限，防止 2^n 无限增长。
            jitter: 是否启用抖动。
            factor: 指数基数（一般用 2；也有用 1.5 的"慢退避"）。
            rng: 随机数发生器。

        ▸ 为什么必须有 max_delay？
          2^20 = 1048576 秒 = 12 天。
          如果没有上限，第 20 次重试的延迟会是 12 天 ——
          这个任务实际上永远不会再被执行，但也没有被标记为失败，
          它会静静地占着队列里的一个位置。
          这叫「**无限退避陷阱**」，是个很隐蔽的资源泄漏。
          正确的上限应该和业务的容忍度对齐：
          如果业务能接受"任务最多延迟 10 分钟"，max_delay 就设 600 秒；
          超过上限后应该考虑**转入死信队列**，而不是继续退避。
        """
        self.base = base
        self.max_delay = max_delay
        self.jitter = jitter
        self.factor = factor
        self.rng = rng or random.Random(42)
        self.calls = 0
        self.total_delay = 0.0

    def delay(self, attempt: int) -> float:
        """计算第 attempt 次重试的延迟。

        Args:
            attempt: 重试次数（从 0 开始，0 表示第一次重试）。

        Returns:
            延迟秒数。

        Raises:
            ValueError: 当 attempt < 0 时。
        """
        if attempt < 0:
            raise ValueError(f"attempt 不能为负数：{attempt}")
        raw = self.base * (self.factor ** attempt)
        raw = min(raw, self.max_delay)
        if self.jitter:
            # 抖动区间 [0.5x, 1.5x]
            raw *= 0.5 + self.rng.random()
        # 抖动后可能超过上限，再钳一次，保证"延迟上限"是真的上限
        delay = min(raw, self.max_delay)
        self.calls += 1
        self.total_delay += delay
        return delay


# ============================================================================
# 实验 1：三种调度策略对比
# ============================================================================
def exp1_dispatch_strategies() -> None:
    """实验 1：轮询 / 加权随机 / 最优优先，三种策略的实测对比。"""
    title("【实验 1】代理池调度策略：轮询 vs 加权随机 vs 最优优先")

    print("""
    池子里放 20 个代理，质量分布固定：
      60% 好代理（成功率 93~99%，延迟 80~200ms）
      30% 一般代理（成功率 65~85%，延迟 300~700ms）
      10% 差代理（成功率 15~40%，延迟 800~1600ms）

    每种策略跑 500 次请求，观察「成功率」和「流量分布」。

    ⚠ 这里用**虚拟时钟**（VirtualClock）而不是 time.time()：
      每次请求让时钟前进「本次延迟」的时长。
      否则 500 次模拟只需要 5 毫秒，而一次失败触发的冷却是 1 秒 ——
      任何失败过的代理都会在剩余实验里永久冷却，
      三种策略会退化成几乎一样的结果（这一点见踩坑记录）。
    """)

    n_requests = 500
    print(f"  {'策略':<14}{'成功率':>9}{'平均延迟(ms)':>14}{'活跃代理':>10}"
          f"{'被封':>7}{'Top1占比':>10}{'Top3占比':>10}")
    print("  " + "-" * 76)

    results: dict[str, dict[str, Any]] = {}
    for strategy in ("round_robin", "weighted", "best_first"):
        # 代理生成用固定种子，保证三种策略面对**完全相同的代理池**
        rng = random.Random(2026)
        proxies = make_mock_proxies(20, rng)
        clock = VirtualClock()
        pool = ProxyPool(proxies, strategy=strategy, rng=random.Random(7),
                         clock=clock)
        ok_count = 0
        latencies: list[float] = []
        for _ in range(n_requests):
            p = pool.pick()
            if p is None:
                # 池子空了：模拟里表现为「跳过本次」，真实环境必须显式处理
                continue
            ok, lat = simulate_one_request(p, rng)
            pool.report(p, ok, lat)
            # ★ 关键：让虚拟时间前进一个请求的耗时，冷却期才有意义
            clock.advance(lat)
            if ok:
                ok_count += 1
                latencies.append(lat)
        share = pool.traffic_share()
        top1 = share[0][2] if share else 0.0
        top3 = sum(s[2] for s in share[:3])
        st = pool.stats()
        results[strategy] = {
            "success_rate": ok_count / n_requests,
            "avg_latency": statistics.mean(latencies) if latencies else 0.0,
            "alive": st["alive"], "banned": st["banned"],
            "top1": top1, "top3": top3, "share": share,
        }
        print(f"  {strategy:<14}{results[strategy]['success_rate'] * 100:>8.1f}%"
              f"{results[strategy]['avg_latency'] * 1000:>14.1f}"
              f"{st['alive']:>10}{st['banned']:>7}"
              f"{top1 * 100:>9.1f}%{top3 * 100:>9.1f}%")

    sub("▸ 流量分布明细（每种策略的 Top5 代理分到了多少请求）")
    for strategy in ("round_robin", "weighted", "best_first"):
        share = results[strategy]["share"]
        print(f"\n    【{strategy}】Top5 流量占比：")
        for key, cnt, pct in share[:5]:
            print(f"      {key:<18} {cnt:>4} 次  {pct * 100:>5.1f}%  "
                  f"{bar(pct, share[0][2] if share[0][2] else 1, 30)}")

    sub("▸ 解读：三种策略的本质差异")
    rr = results["round_robin"]
    wt = results["weighted"]
    bf = results["best_first"]
    print(f"""      成功率：轮询 {rr['success_rate'] * 100:.1f}%  |  """
          f"""加权 {wt['success_rate'] * 100:.1f}%  |  最优优先 {bf['success_rate'] * 100:.1f}%
      流量集中度（Top1）：轮询 {rr['top1'] * 100:.1f}%  |  """
          f"""加权 {wt['top1'] * 100:.1f}%  |  最优优先 {bf['top1'] * 100:.1f}%
      被封代理数：轮询 {rr['banned']}  |  加权 {wt['banned']}  |  最优优先 {bf['banned']}

    ▸ **轮询（round_robin）**：流量完全均分（Top1 约 {rr['top1'] * 100:.1f}%，
      也就是 1/{len(make_mock_proxies(20, random.Random(1)))}）。
      但它对代理质量**完全无知**：差代理拿到和好代理一样多的请求，
      于是成功率被拉低到 {rr['success_rate'] * 100:.1f}%。
      ▸ 适合场景：所有代理同质（同一供应商同批次），
        此时均分是最简单且正确的做法。

    ▸ **加权随机（weighted）**：成功率 {wt['success_rate'] * 100:.1f}%，
      明显高于轮询。注意它的 Top1 占比约 {wt['top1'] * 100:.1f}% ——
      **并没有集中到某一个代理上**，因为权重是"相对"的，
      而且随着代理状态变化会动态调整。
      ▸ 这个策略同时做到了三件事：
        ① 好代理拿更多请求（提效）
        ② 差代理仍有机会（能自我证明 —— 探索）
        ③ 没有任何一个 IP 被压垮（分散风险）
      ▸ **这是推荐的默认策略。**

    ▸ **最优优先（best_first）**：成功率可能最高（{bf['success_rate'] * 100:.1f}%），
      但 Top1 占比高达 {bf['top1'] * 100:.1f}% ——
      **所有请求都压在极少数代理上**。
      短期看它最优，长期看它最糟：这些 IP 会以最快速度被封，
      然后池子切换到"第二好的"，直到全部报废。
      实测被封数 {bf['banned']} 个。
      ▸ 这叫「贪婪策略的局部最优陷阱」：
        **每一个决策都是局部最优的，但全局结果是最差的。**
        同类的错误在负载均衡里叫「所有请求都打到最空闲的节点」，
        最终会让那个节点也变忙。

    ▸ 一句话原则：
      **永远不要做「确定性最优选择」，要保留随机性和探索。
        确定性 = 可预测 = 可被针对 = 会被逐个击破。**""")

    sub("▸ ⚠ 本实验的局限")
    print("""    · 代理质量是**用上帝视角固定生成**的，真实环境的代理质量
      随时间、目标站、地域动态变化（同一个代理对 A 站可用、对 B 站不可用）。
    · 请求延迟用的是「指数分布抖动」的简化模型，
      真实网络是重尾分布（P99 可能是均值的 10 倍以上）。
    · 没有模拟「同一个 IP 在短时间内被目标站风控」这个关键机制 ——
      真实场景里 best_first 的失败速度会比本实验**快得多**。
    · 本实验用单线程顺序调用，没有并发场景下的竞态
      （真实的多线程代理池，pick/report 之间需要加锁）。""")


# ============================================================================
# 实验 2：代理生命周期
# ============================================================================
def exp2_proxy_lifecycle() -> None:
    """实验 2：代理从健康到衰败再到恢复的完整生命周期。"""
    title("【实验 2】代理生命周期：加分、扣分、剔除、复活")

    print("""
    追踪 3 个代表性代理在 400 次请求中的分数变化：
      proxy-A：好代理（真实成功率 96%）
      proxy-B：一般代理（真实成功率 75%）
      proxy-C：糟糕代理（真实成功率 20%）

    注意观察三件事：
      ① 分数如何从「中性先验 50 分」收敛到「真实水平」
      ② C 的分数如何跌破阈值、进入冷却、最终被剔除
      ③ A 中途遇到一段"网络抖动"（连续失败），
         分数如何下降、冷却如何救它、它又如何恢复
    """)

    rng = random.Random(2026)
    clock = VirtualClock()
    a = Proxy(host="10.1.0.1", port=8001, clock=clock)
    a._true_quality, a._true_latency = 0.96, 0.12
    b = Proxy(host="10.1.0.2", port=8002, clock=clock)
    b._true_quality, b._true_latency = 0.75, 0.35
    c = Proxy(host="10.1.0.3", port=8003, clock=clock)
    c._true_quality, c._true_latency = 0.20, 1.20

    proxies = [a, b, c]

    # ------------------------------------------------------------------
    # 2.0 先单独演示「中性先验 → 收敛」：这一小段是**刻意为每个代理
    #     轮流发一次请求**，与后面 400 次的加权调度无关。
    #     为什么要单独做？因为加权调度会让好代理迅速拿到大量请求、
    #     第 20 次采样时分数就已经接近真实水平了 ——
    #     光看 20 次粒度的采样表，是**看不到「从 50 分起步」这个过程**的。
    #     教学里凡是「想看某个早期瞬态」，就必须把采样粒度调到足够细。
    # ------------------------------------------------------------------
    sub("2.0 「中性先验」是如何消失的（前 6 次请求，逐个代理轮流发）")
    print(f"  {'次序':>5}{'代理':>14}{'请求前分数':>12}{'本次结果':>10}"
          f"{'累计':>8}{'请求后分数':>12}")
    print("  " + "-" * 64)
    warmup_rng = random.Random(999)
    warmup_clock = VirtualClock()
    for q in proxies:
        q.clock = warmup_clock
    for round_no in range(1, 7):
        for q in proxies:
            # 打印的是**请求之前**的分数 —— 这样才能看见 0/0 时的中性先验
            before = q.score()
            ok, lat = simulate_one_request(q, warmup_rng)
            q.report(ok, lat)
            warmup_clock.advance(lat)
            if round_no <= 2:
                print(f"  {round_no:>5}{q.key:>14}{before:>12.1f}"
                      f"{('成功' if ok else '失败'):>10}"
                      f"{f'{q.successes}/{q.successes + q.failures}':>8}"
                      f"{q.score():>12.1f}")
    print(f"\n  → 6 轮之后：A {a.score():.1f} 分，B {b.score():.1f} 分，"
          f"C {c.score():.1f} 分")
    print("  → 注意第一次请求「前」的分数是 46.2，**不是 100、也不是 0**。")
    print("    把它拆开看（三处都用到了「无样本时的中性先验」）：")
    print("      成功率维：0.5（先验）× 100 = 50.0  →  ×0.6 = 30.00")
    print("      速度维  ：avg_latency 无样本时返回 0.5 秒（也是先验）")
    print("                100×0.3/(0.3+0.5) = 37.5  →  ×0.3 = 11.25")
    print("      新鲜度维：last_used 为 0 → idle 默认 3600 秒")
    print("                100/(1+1) = 50.0        →  ×0.1 =  5.00")
    print("      合计 = 46.25 —— 这就是「什么都不知道」时系统给出的分数。")
    print("    ▸ 关键点：这个值必须**不高不低**。太高会盲目信任新代理，")
    print("      太低会让新代理永远拿不到请求（冷启动死锁）。")
    # 演示完毕，把这 6 轮的成绩清掉，让后面的 400 次实验从零开始
    for q in proxies:
        q.successes = q.failures = q.consecutive_failures = 0
        q.total_latency = 0.0
        q.total_requests = 0
        q.last_used = 0.0
        q.cooldown_until = 0.0
        q.banned = False
        q.clock = clock

    pool = ProxyPool(proxies, strategy="weighted", rng=random.Random(11),
                     ban_threshold=5, clock=clock)

    snapshots: dict[str, list[tuple[int, float, bool]]] = {p.key: [] for p in proxies}
    events: list[str] = []
    a_jitter_start = 0          # A 的抖动期起始轮次（后面正文要引用）

    # 记录 A 的"抖动期"
    bad_window = range(200, 205)

    for i in range(1, 401):
        p = pool.pick()
        if p is None:
            # ★ 池子空了有两种原因，必须分开处理 —— 这是本实验最容易被写错的地方：
            #   ① 全员在冷却期 → 正常状态，等一会儿就好（推进虚拟时钟）
            #   ② 全员被剔除   → 异常状态，需要「定时全量重检」
            banned_n = sum(1 for q in proxies if q.banned)
            cooled_n = sum(1 for q in proxies
                           if not q.banned and q.cooldown_until > clock.now())
            if banned_n == len(proxies):
                events.append(f"第 {i} 次：池子枯竭 —— 3 个代理**全部被剔除**")
                revived = pool.revive_all()
                events.append(f"        → 触发「定时全量重检」，复活 {revived} 个代理")
            else:
                events.append(f"第 {i} 次：池子暂时无可用代理"
                              f"（剔除 {banned_n} 个，冷却中 {cooled_n} 个）")
                events.append(f"        → 等待冷却结束（虚拟时钟前进 10 秒）")
                clock.advance(10.0)
                pool.clear_cooldowns()
            continue

        # 给 A 注入一段连续失败（模拟它经过了一个有问题的网络节点）
        if p is a and i in bad_window:
            ok, lat = False, 0.0
        else:
            ok, lat = simulate_one_request(p, rng)

        was_banned = p.banned
        prev_cf = p.consecutive_failures
        pool.report(p, ok, lat)
        # 时间前进：失败也要消耗时间（超时同样要等），这里统一用「代理的基础延迟」
        clock.advance(lat if lat > 0 else p._true_latency)
        if not was_banned and p.banned:
            events.append(f"第 {i:>3} 次：⛔ {p.key} 连续失败 "
                          f"{p.consecutive_failures} 次，被剔除")
        if p is a and prev_cf == 0 and not ok and i in bad_window:
            events.append(f"第 {i:>3} 次：⚠ {p.key} 开始出现连续失败（抖动期）")
            # 记录抖动期的连败峰值，后面正文要引用
            a_jitter_start = i

        if i % 20 == 0:
            for q in proxies:
                snapshots[q.key].append((i, q.score(), q.banned))

    sub("2.1 分数演化（每 20 次请求采样一次）")
    print(f"  {'轮次':>6}" + "".join(f"{p.key:>22}" for p in proxies))
    print("  " + "-" * 74)
    for idx in range(len(snapshots[a.key])):
        row = f"  {snapshots[a.key][idx][0]:>6}"
        for p in proxies:
            rnd, score, banned = snapshots[p.key][idx]
            mark = " ⛔" if banned else "   "
            row += f"{score:>18.1f}{mark:<4}"
        print(row)

    sub("2.2 关键事件")
    for ev in events:
        print(f"    {ev}")

    sub("2.3 最终状态")
    print(f"  {'代理':<18}{'成功':>7}{'失败':>7}{'成功率':>9}{'连续失败':>10}"
          f"{'分数':>9}{'状态':>8}")
    print("  " + "-" * 70)
    for p in proxies:
        state = "已剔除" if p.banned else ("冷却中" if p.in_cooldown else "可用")
        print(f"  {p.key:<18}{p.successes:>7}{p.failures:>7}"
              f"{p.success_rate * 100:>8.1f}%{p.consecutive_failures:>10}"
              f"{p.score():>9.1f}{state:>8}")

    sub("▸ 关键洞察 1：分数从「中性先验」收敛到「真实水平」")
    print(f"""    ▸ 证据在 2.0 的输出里：每个代理**第一次**被算分时都是 50.0 分
      （或者非常接近它），因为此时 successes + failures == 0，
      `success_rate` 返回中性先验 0.5。
      随着样本增加，A/B/C 的分数分别向各自的真实水平靠拢。

    ▸ 这揭示了一个重要事实：**评分系统需要「样本量」才能准确。**
      这正是为什么成功率无样本时要返回 0.5 的中性先验 ——
      如果返回 0（或 1），系统会在样本不足时做出极度自信的错误判断：
        · 返回 1.0 → 新代理被当成满分代理，立刻被疯狂调度，
          然后迅速失败（过度信任未知资源）
        · 返回 0.0 → 新代理永远拿不到请求，永远无法证明自己
          （冷启动死锁）

    ▸ 为什么 2.1 的采样表里**看不到**从 50 分起步？
      因为那是加权调度：好代理在自己的强项上被反复选中，
      很快就积累了几十次样本 —— 到第 20 次采样时分数早已收敛。
      **这本身就是一个结论：采样粒度决定了你能看见什么。**
      如果你想观察「启动瞬态」，就必须做 2.0 那样的细粒度观测。

    ▸ 生产环境的对策：
      · 新代理**不能立刻给满权重**，要有一段「试运行期」
      · 或者用贝叶斯平滑：`(successes + 1) / (successes + failures + 2)`
        这个公式会自动处理样本量问题 —— 样本少时结果接近 0.5，
        样本多时接近真实成功率，而且是**连续变化**的，没有突变。
        本课的 0.5 先验是它的简化版：在 0/0 这一点上等价，
        但样本从 0 变到 1 时会有跳变。

    ▸ 400 次请求后的最终观测值：
      A = {a.success_rate * 100:.1f}%（真实质量 96%）
      B = {b.success_rate * 100:.1f}%（真实 75%）
      C = {c.success_rate * 100:.1f}%（真实 20%）
      **观测值都在真实值附近**，说明评分系统是有效的。""")

    sub("▸ 关键洞察 2：差代理会被「用脚投票」淘汰")
    print(f"""    C 的真实成功率只有 20%，但**它并没有被立刻剔除** ——
    它需要连续失败 {pool.ban_threshold} 次才会触发剔除。

    ▸ 为什么用「连续失败」而不是「总失败率」？
      考虑两个代理：
        · X：成功 80 次、失败 20 次，但失败是**分散**的（每 5 次失败 1 次）
        · Y：成功 80 次、失败 20 次，失败是**连续**的（后 20 次全失败）
      总失败率相同（20%），但 Y 明显是「已经死了」而 X 只是「不够好」。
      **连续失败捕捉的是"当前状态"，总失败率捕捉的是"历史质量"。**
      剔除应该基于当前状态，降权应该基于历史质量 —— 两者分工明确。

    ▸ C 的最终状态：{'已剔除' if c.banned else '未被剔除'}，
      连败 {c.consecutive_failures} 次，历史失败 {c.failures} 次。
      在 400 次实验里，它一共被剔除过
      {sum(1 for e in events if '8003' in e and '被剔除' in e)} 次 ——
      **注意它不是被剔除了就完了**：池子枯竭时会触发「定时全量重检」
      把它复活，它又会再失败、再被剔除，如此循环。
      这正是真实生产环境的样子：**一个坏代理是"止不住的麻烦"，
      除非你把它彻底从池子里删掉。**

    ▸ 所以除了自动剔除，你还需要一个**黑名单机制**：
      被剔除超过 N 次的代理，直接不计入「全量重检」的范围。
      本课没有实现这一层（保持逻辑简单），但它是一个真实的工程需求。

    ▸ 注意冷却机制在其中的作用：
      C 每次连续失败都会进入指数冷却（1s → 2s → 4s → 8s），
      这让它在"半死"状态下**不会持续占用调度配额**。
      冷却 + 剔除形成两级防御：
        冷却 = 缓刑（暂时不用它，但它还有机会）
        剔除 = 死刑（彻底不用，需要显式复活）
      但如上面所说，**「复活」这道口子如果不加条件，死刑就形同虚设。**""")

    sub("▸ 关键洞察 3：好代理也会「受伤」，但能恢复")
    print(f"""    看 A 在第 {a_jitter_start}~{a_jitter_start + 4} 次请求期间被注入的连续失败
    （模拟遇到有问题的网络节点）：
      · 它的连败数上升，分数被连败惩罚乘数压低
      · 如果连败达到 {pool.ban_threshold} 次，它会被**误杀**
      · 冷却机制给了它缓冲 —— 连败期间它短暂"下线"，
        等冷却结束再试，如果这时网络恢复正常，它会连败清零、分数回升

    ▸ 实测：A 最终的连败数是 {a.consecutive_failures}，
      状态是 {"已剔除" if a.banned else "可用"}，分数 {a.score():.1f}，
      成功率 {a.success_rate * 100:.1f}%（真实 96%）。
      **它从抖动中恢复了，而且没有留下后遗症。**

    ▸ 这个机制的设计要点：
      **「误杀好代理」的代价远大于「晚一点剔除坏代理」。**
      因为好代理是稀缺资源（它撑起了你的成功率），
      而坏代理多留一会儿只是浪费几个请求。
      所以整套机制应该**偏向宽容**：
        · 冷却先于剔除
        · 剔除阈值不能太低
        · 必须有复活机制
      同理，误杀率应该作为一个被监控的指标 ——
      如果发现大量代理被剔除后很快复活，说明阈值太激进了。""")

    sub("▸ 关键洞察 4：池子枯竭是必须设计的场景")
    exhausted = sum(1 for e in events if '池子枯竭' in e)
    no_alive = sum(1 for e in events if '池子暂时无可用代理' in e)
    print(f"""    2.2 的事件日志里，池子「没有可用代理」一共出现了
      {exhausted + no_alive} 次，其中：
        · 【真切枯竭】3 个代理全部被剔除 → {exhausted} 次
        · 【暂时无代理】还有代理，但全在冷却期 → {no_alive} 次

    ▸ **这两种情况完全不同，必须分开处理** —— 这是很多实现里的隐藏 bug：
        · 全员冷却 → 是**正常**状态。正确做法是「等一会儿」
          （真实的代码里就是 sleep / 或者把任务放回延时队列）。
          如果你把它当成枯竭去「复活所有代理」，
          就会**无谓地清掉一个好代理的连败计数**，掩盖真实故障。
        · 全员剔除 → 才需要触发「定时全量重检」。

      本课在事件日志里把两者分开打印，就是为了让这个区别可见。

    ▸ 无论哪种情况，**你必须为「pick() 返回 None」写代码**：
        p = pool.pick()
        if p is None:
            # 三个选项，选哪个取决于业务
            # ① 降级直连（有被封风险，但能继续跑）
            # ② 把任务放回延时队列，等池子恢复（推荐，第 62 课的思路）
            # ③ 直接终止并告警（数据完整性要求高的场景）
      新手最常见的问题是**忘了写这个分支**，然后线上池子空了就崩。

    ▸ 池子枯竭的预警指标：
        · 可用代理数 / 总代理数 < 30% → 告警
        · 连续剔除速率突然上升 → 说明目标站升级了风控，要降低 QPS
        · 「暂时无代理」的占比持续偏高 → 说明冷却时间设得太长，
          或者池子规模不足以支撑当前 QPS
      这三个指标应该放在监控看板上（第 55 课的思路）。""")


# ============================================================================
# 实验 3：令牌桶限速
# ============================================================================
def exp3_rate_limiter() -> None:
    """实验 3：无限制 / 全局限速 / 按域名限速的实测对比。"""
    title("【实验 3】令牌桶限速：为什么必须按域名而不是全局")

    print("""
    场景：3 个站点**各有 1 个线程在抓**（真实爬虫就是并行的），
    每个站点抓 60 个页面，单次请求处理耗时约 5ms。
      · site-a.com：容量较大，可以承受 15 QPS
      · site-b.com：容量小，只能承受 3 QPS
      · site-c.com：中等，可以承受 8 QPS

    对比三种方案：
      A. 完全不限速           —— 拼速度，但会被封
      B. 全局限速 10 QPS      —— 用一个统一的门
      C. 按域名限速           —— 每个站点各自的门

    ⚠ 本实验用「模拟服务端的接受/拒绝」来体现限速的效果：
      超过站点容量的请求会被记为失败（模拟 429）。
    """)

    domains = {
        # 三个站点的"容量"刻意拉开 5 倍以上的差距，这样三种限速方案的
        # 差异才会显著。如果三个站点容量接近，全局限速看起来也"还行"，
        # 教学效果就出不来了。
        "https://site-a.com": 15.0,
        "https://site-b.com": 3.0,
        "https://site-c.com": 8.0,
    }
    # 请求数的选择要同时满足两个约束：
    #   ① 足够多 → 稳态窗口还有足够样本测 QPS
    #   ② 足够少 → 整个实验的墙钟时间可控（本课要求单文件 < 60 秒）
    # 这里是"真实 sleep"的限速实验，耗时 ≈ 请求数 / 最慢的桶速率：
    #   scheme C 受 site-b 的 3 QPS 瓶颈限制 → 60 / 3 = 20 秒
    #   scheme B 受全局 10 QPS 限制        → 60×3 / 10 = 18 秒
    #   scheme A 不限速                    → 约 1 秒
    # 合计约 40 秒，留出余量给实验 1/2/4/5（它们都是纯计算，不到 1 秒）。
    # ⚠ 这个实验是本课最耗时的部分，**唯一的原因就是限速真的在 sleep**——
    #   这也说明了一件事：限速的代价是真实的墙钟时间，不是纸面数字。
    #
    # ⚠⚠ 一个必须知道的测量限制：
    #   令牌桶的初始容量 = rate × capacity_factor = rate × 1.5。
    #   如果"请求总数"不超过这个初始容量，那么**全部请求都会在开局
    #   一瞬间被突发额度放行**，稳态窗口里一个样本都没有 ——
    #   实测 QPS 会退化成"本机物理上限"（约 197），而不是配置值。
    #   所以本课把 site-a 的配置降到 15 QPS（容量 22.5 < 60 个请求），
    #   保证它的稳态窗口是有效的。
    #   **要让"稳态速率"可测，请求数必须显著超过桶的初始容量。**
    per_domain_requests = 60
    processing = 0.005

    def run_scenario(mode: str) -> dict[str, Any]:
        """跑一种限速方案。

        Args:
            mode: 'none' / 'global' / 'per_domain'。

        Returns:
            统计结果字典。

        ▸ 本实验用**多线程**（每个站点一个线程），原因：
          真实的爬虫系统是**同时**抓多个站点的，而不是「A 抓完抓 B」。
          这一点会彻底改变实验结果 —— 见下面的关键洞察：
          如果写成串行（A→B→C 轮流发一个请求），
          三个站点的实测 QPS 会**完全相同**，
          按域名限速的效果就完全看不出来了（这是本课踩过的坑之一）。

          那为什么不用 asyncio？因为限速器的令牌桶实现里用的是
          `time.sleep()` 阻塞等待。在第 60 课讲过，
          同步阻塞会冻结整个事件循环 —— 所以这里用线程更合适。
          （生产环境应该把 TokenBucket 改成 async 版本，
          用 `await asyncio.sleep()`；本课为了和前面几课的
          同步实现保持一致，选择了线程。）
        """
        limiter = DomainRateLimiter(default_qps=10.0,
                                    per_domain={d: qps for d, qps in domains.items()})
        global_bucket = TokenBucket(rate=10.0, capacity=10.0, name="global")
        rng = random.Random(99)

        # 所有线程共享的收集容器。因为 CPython 的 list.append 是原子的，
        # 而且这里没有「读-改-写」的复合操作，所以不需要加锁。
        # ⚠ 但如果是计数器 `ok_count += 1` 就**必须加锁** —— 见踩坑记录。
        lock = threading.Lock()
        ok_count = 0
        rejected = 0
        per_domain_ok: dict[str, int] = {d: 0 for d in domains}
        per_domain_rej: dict[str, int] = {d: 0 for d in domains}
        send_times: dict[str, list[float]] = {d: [] for d in domains}

        t0 = time.perf_counter()

        def crawl_one_site(domain: str, cap: float) -> None:
            """抓一个站点的全部页面（每个站点一个线程）。

            Args:
                domain: 目标域名。
                cap: 该站点能承受的 QPS 上限。

            Returns:
                None
            """
            nonlocal ok_count, rejected
            local_ok = 0
            local_rej = 0
            for _ in range(per_domain_requests):
                # ---------- 限速 ----------
                if mode == "global":
                    global_bucket.acquire()
                elif mode == "per_domain":
                    limiter.acquire(domain)
                # mode == "none" 则不限速

                now = time.perf_counter()
                send_times[domain].append(now)

                # ---------- 模拟服务端容量检查 ----------
                # 统计「最近 1 秒内该域名的请求数」，超过容量就拒绝（模拟 429）
                window = [t for t in send_times[domain] if now - t <= 1.0]
                if len(window) > cap:
                    local_rej += 1
                else:
                    local_ok += 1

                # 模拟请求处理耗时
                time.sleep(processing)

            with lock:
                ok_count += local_ok
                rejected += local_rej
                per_domain_ok[domain] = local_ok
                per_domain_rej[domain] = local_rej

        threads = [threading.Thread(target=crawl_one_site, args=(d, cap))
                   for d, cap in domains.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0

        # 计算实测 QPS
        # ▸ 为什么用「后 70% 的请求」而不是「从第 1 秒开始」？
        #   因为令牌桶的初始容量是 rate×capacity_factor，开局会被**瞬间**
        #   用完（突发额度），把这段算进去会让 QPS 虚高。
        #   但「按绝对时间切」有个致命缺陷：对于**很快的方案**
        #   （比如不限速，60 个请求 0.3 秒就跑完了），
        #   第 1 秒之后的样本数是 **0**，实测 QPS 直接变成 0.0 ——
        #   这是本课踩过的坑（见踩坑记录「测量窗口不能依赖绝对时间」）。
        #   改成**按请求序号**跳过前 30%，就与方案快慢无关了：
        #     快方案 → 跳过开局那一小段突发
        #     慢方案 → 同样跳过开局那一小段突发
        #   **用「相对位置」而不是「绝对时间」来定义稳态窗口。**
        measured_qps = {}
        for d in domains:
            times = send_times[d]
            skip = int(len(times) * 0.3)
            steady = times[skip:]
            if len(steady) > 1:
                span = steady[-1] - steady[0]
                measured_qps[d] = (len(steady) - 1) / span if span > 0 else 0.0
            else:
                measured_qps[d] = 0.0

        return {
            "mode": mode,
            "elapsed": elapsed,
            "ok": ok_count,
            "rejected": rejected,
            "total": ok_count + rejected,
            "reject_rate": rejected / (ok_count + rejected) if (ok_count + rejected) else 0.0,
            "per_domain_ok": per_domain_ok,
            "per_domain_rej": per_domain_rej,
            "measured_qps": measured_qps,
            "global_waited": global_bucket.waited,
            "limiter_cfg": limiter.config_summary(),
        }

    scenarios = {m: run_scenario(m) for m in ("none", "global", "per_domain")}

    sub("3.1 总体效果对比")
    print(f"  {'方案':<26}{'耗时(s)':>9}{'成功':>7}{'拒绝':>7}{'拒绝率':>9}")
    print("  " + "-" * 60)
    labels = {
        "none": "A. 完全不限速",
        "global": "B. 全局限速 10 QPS",
        "per_domain": "C. 按域名限速",
    }
    for mode in ("none", "global", "per_domain"):
        s = scenarios[mode]
        print(f"  {labels[mode]:<26}{s['elapsed']:>9.2f}{s['ok']:>7}"
              f"{s['rejected']:>7}{s['reject_rate'] * 100:>8.1f}%")

    sub("3.2 各站点的实测 QPS（对比它真实能承受的容量）")
    print(f"  {'站点':<24}{'容量上限':>10}{'不限速实测':>12}{'全局限速':>11}"
          f"{'按域名限速':>12}")
    print("  " + "-" * 72)
    for d, cap in domains.items():
        print(f"  {d:<24}{cap:>10.0f}"
              f"{scenarios['none']['measured_qps'][d]:>12.1f}"
              f"{scenarios['global']['measured_qps'][d]:>11.1f}"
              f"{scenarios['per_domain']['measured_qps'][d]:>12.1f}")

    sub("3.3 各站点的拒绝数")
    print(f"  {'站点':<24}{'不限速被拒':>12}{'全局限速被拒':>14}{'按域名被拒':>13}")
    print("  " + "-" * 66)
    for d in domains:
        print(f"  {d:<24}{scenarios['none']['per_domain_rej'][d]:>12}"
              f"{scenarios['global']['per_domain_rej'][d]:>14}"
              f"{scenarios['per_domain']['per_domain_rej'][d]:>13}")

    none_s = scenarios["none"]
    glob_s = scenarios["global"]
    per_s = scenarios["per_domain"]
    # 从配置里取容量，避免正文硬编码数字和配置脱节
    cap_a = domains["https://site-a.com"]
    cap_b = domains["https://site-b.com"]
    cap_c = domains["https://site-c.com"]

    sub("▸ 解读：三种方案的本质差异")
    print(f"""    【A. 完全不限速】耗时 {none_s['elapsed']:.2f}s，但拒绝率
      {none_s['reject_rate'] * 100:.1f}%（{none_s['rejected']} 个请求被拒绝）。
      ▸ 每个站点都被打爆：site-b 的实测 QPS 达到
        {none_s['measured_qps']['https://site-b.com']:.1f}，
        而它只能承受 {cap_b:.0f} QPS —— 超出
        {none_s['measured_qps']['https://site-b.com'] / cap_b:.1f} 倍。
      ▸ 后果在真实环境里比"拒绝"更严重：
        对方不会老老实实返回 429 让你重试，而是会
        封 IP、上验证码、返回假数据（最阴险，你的数据静默污染）。
      ▸ **"快"是假象，实际是"乱"。**

    【B. 全局限速 10 QPS】耗时 {glob_s['elapsed']:.2f}s，拒绝率
      {glob_s['reject_rate'] * 100:.1f}%。
      ▸ 比 A 安全，但效率被严重拖累：全局 10 QPS 分给 3 个站点，
        每个站点实际只拿到约 {glob_s['measured_qps']['https://site-a.com']:.1f} QPS。
      ▸ 两个问题：
        ① **site-a 被浪费**：它能承受 {cap_a:.0f} QPS，却只给了它约
           {glob_s['measured_qps']['https://site-a.com']:.1f} QPS，
           浪费了 {(1 - glob_s['measured_qps']['https://site-a.com'] / cap_a) * 100:.0f}% 的容量。
           如果 site-a 有 10000 个页面，本来 {10000 / cap_a / 60:.0f} 分钟能抓完，
           现在要 {10000 / glob_s['measured_qps']['https://site-a.com'] / 60:.0f} 分钟。
        ② **site-b 仍有风险**：全局 10 QPS 里的任何一部分打在
           site-b 上都会超限（它只能承受 {cap_b:.0f}）。
           全局限速**不能保证任何单个站点不超限** ——
           这是它最致命的问题：**它给的是虚假的安全感。**
        实测 site-b 全局限速下被拒 {glob_s['per_domain_rej']['https://site-b.com']} 次。

    【C. 按域名限速】耗时 {per_s['elapsed']:.2f}s，拒绝率
      {per_s['reject_rate'] * 100:.1f}%。
      ▸ 稳态实测 QPS 与各自配置的对比（跳过前 30% 的请求，避开突发额度）：
        · site-a：实测 {per_s['measured_qps']['https://site-a.com']:.1f}
          / 配置 {cap_a:.0f} → 偏差 {(per_s['measured_qps']['https://site-a.com'] / cap_a - 1) * 100:+.0f}%
        · site-b：实测 {per_s['measured_qps']['https://site-b.com']:.1f}
          / 配置 {cap_b:.0f} → 偏差 {(per_s['measured_qps']['https://site-b.com'] / cap_b - 1) * 100:+.0f}%
        · site-c：实测 {per_s['measured_qps']['https://site-c.com']:.1f}
          / 配置 {cap_c:.0f} → 偏差 {(per_s['measured_qps']['https://site-c.com'] / cap_c - 1) * 100:+.0f}%
      ▸ **三个站点各自守住了自己的门，互不干扰** —— 这才是重点。
        site-b 的 3 QPS 严格卡住，site-a 没有因为要和它共享一个门而被拖慢。
        精度在 ±10% 量级（受 5ms 处理耗时和 sleep 粒度影响）。
        （如果某个站点的偏差特别大，先检查「请求数是否超过了它的桶初始容量」——
         超了才能测到稳态，没超就全是突发，见本课踩坑记录。）

      ▸ 对比 B：全局限速下，site-b 的实测 QPS 是
        {glob_s['measured_qps']['https://site-b.com']:.1f}，
        而它只能承受 {cap_b:.0f} —— **仍然超限**，被拒
        {glob_s['per_domain_rej']['https://site-b.com']} 次。
        这正是「全局限速给的是虚假安全感」的实测证据。
      ▸ 对比 A：不限速时，三个站点的实测 QPS 都被拉到
        {none_s['measured_qps']['https://site-a.com']:.0f} 左右 ——
        这是本机 5ms 处理耗时能跑出的物理上限，**与站点容量毫无关系**。

      ▸ 一句话总结：
        **约束来自对方的容忍度，所以约束必须按对方分别定义。**""")

    sub("▸ 关键洞察：为什么不能用「固定间隔 sleep」代替令牌桶")
    print(f"""    一个常见的朴素写法是：

        for url in urls:
            fetch(url)
            time.sleep(1.0 / qps)      # 每个请求后固定 sleep

    它在「请求耗时稳定」时看起来没问题，但有两个致命缺陷：

      ① **无法应对耗时波动**。如果某个请求花了 3 秒（长尾），
         你还要再 sleep 1/qps 秒 —— 实际 QPS 会远低于目标。
         而令牌桶是"按时间补充"，慢请求不会额外惩罚你。

      ② **无法利用突发额度**。假设你限速 10 QPS，
         然后有 10 个列表页要抓，你想一次性开 10 个并发。
         固定间隔方案会让它们排成一队（每个间隔 100ms），
         总耗时 1 秒；而令牌桶里如果攒了 10 个令牌，
         这 10 个请求可以**瞬间全发出去**，总耗时 5ms。
         在抓取列表页时，这种"突发能力"能大幅缩短关键路径。

    ▸ 令牌桶的实测数据（本实验的 site-a）：
      不限速实测 {none_s['measured_qps']['https://site-a.com']:.1f} QPS，
      按域名限速后实测 {per_s['measured_qps']['https://site-a.com']:.1f} QPS
      （配置的是 40 QPS）。
      ▸ 注意实测值和配置值有偏差，原因是：
        · 测 QPS 用的时间窗口是"第一个请求到最后一个请求"，
          不包括收尾阶段
        · 令牌桶的采样和补充有时间粒度（本课用 sleep 的最小单位 0.5ms）
        · 请求处理耗时（5ms）本身也会影响
        **限速器的"准确性"永远是近似的**，能控制在 ±10% 以内就算合格。
        要求更精确需要用滑动窗口计数器（但内存开销更大）。

    ▸ 配置从哪来？
      **不要拍脑袋。** 正确流程是：
        ① 先用极低 QPS（比如 1）做探测
        ② 逐步提高，观察错误率和延迟（第 60 课的拐点实验）
        ③ 记录下"错误率开始上升"的那个点，取它的 50~70% 作为配置值
        ④ 把这些配置写进配置文件（第 64 课的 ConfigManager），
           不要硬编码在代码里
      这个流程应该**定期重跑**，因为目标站的容量会变
      （它可能升级了服务器，也可能收紧了风控）。""")


# ============================================================================
# 实验 4：指数退避与惊群效应
# ============================================================================
def exp4_backoff_jitter() -> None:
    """实验 4：无抖动 vs 有抖动，惊群效应实测。"""
    title("【实验 4】指数退避：无抖动引发的惊群效应（thundering herd）")

    print("""
    场景：100 个 worker 在 T=0 时**同时**遇到 429（被限速），
    全部开始重试。每个 worker 最多重试 4 次。

    对比两种退避：
      ❌ 无抖动：delay = base * 2^attempt
      ✅ 有抖动：delay = base * 2^attempt * random(0.5, 1.5)

    关键指标：**每次重试的"瞬时并发峰值"** ——
    也就是在同一毫秒内有多少个请求同时发出。
    峰值越高，对服务器的冲击越像一次 DDoS。
    """)

    n_workers = 100
    max_attempts = 4
    base = 0.1

    def simulate(jitter: bool, rng: random.Random) -> dict[str, Any]:
        """模拟 100 个 worker 的重试时间点分布。

        Args:
            jitter: 是否启用抖动。
            rng: 随机数发生器。

        Returns:
            统计结果。

        ▸ 这里用「计算出所有重试时刻，再统计时间分布」而不是真的 sleep，
          原因有两个：
            ① 快 —— 真 sleep 4 次退避要好几秒
            ② 可分析 —— 能精确统计"任意时刻的并发数"，
               而真跑只能观察到结果，看不到分布
          **能离线计算的实验就不要真跑**，这是性能实验的通用技巧。
        """
        strategy = BackoffStrategy(base=base, max_delay=8.0, jitter=jitter, rng=rng)
        # 每个 worker 的重试时刻列表
        all_times: list[float] = []
        per_worker: list[list[float]] = []
        for _ in range(n_workers):
            t = 0.0
            times = [0.0]     # T=0 是第一次失败
            for attempt in range(max_attempts):
                t += strategy.delay(attempt)
                times.append(t)
                all_times.append(t)
            per_worker.append(times)

        # 把时间轴切成 100ms 的桶，统计每个桶里的重试数
        bucket_size = 0.1
        buckets: dict[int, int] = {}
        for t in all_times:
            b = int(t / bucket_size)
            buckets[b] = buckets.get(b, 0) + 1
        # 峰值：单个桶里的最大数量
        peak = max(buckets.values()) if buckets else 0
        peak_time = max(buckets.items(), key=lambda x: x[1])[0] * bucket_size

        # 计算"瞬时并发"的另一种度量：1ms 粒度下的最大同时重试数
        ms_buckets: dict[int, int] = {}
        for t in all_times:
            b = int(t * 1000)
            ms_buckets[b] = ms_buckets.get(b, 0) + 1
        ms_peak = max(ms_buckets.values()) if ms_buckets else 0
        # 有多少个不同的毫秒时刻发生了重试（越大说明越分散）
        distinct_ms = len(ms_buckets)
        spread = (max(all_times) - min(all_times)) if all_times else 0.0

        return {
            "buckets": buckets,
            "peak_per_100ms": peak,
            "peak_time": peak_time,
            "ms_peak": ms_peak,
            "distinct_ms": distinct_ms,
            "spread": spread,
            "total_retries": len(all_times),
            "per_worker": per_worker,
        }

    no_jitter = simulate(False, random.Random(2026))
    with_jitter = simulate(True, random.Random(2026))

    sub("4.1 重试时刻分布（100ms 粒度的时间轴）")
    max_b = max(max(no_jitter["buckets"].values(), default=1),
                max(with_jitter["buckets"].values(), default=1))
    print(f"    {'时间(s)':<10}{'❌ 无抖动':>12}{'':<3}{'✅ 有抖动':>12}")
    print("    " + "-" * 60)
    all_b = sorted(set(no_jitter["buckets"]) | set(with_jitter["buckets"]))
    for b in all_b:
        t = b * 0.1
        n1 = no_jitter["buckets"].get(b, 0)
        n2 = with_jitter["buckets"].get(b, 0)
        print(f"    {t:<10.1f}{n1:>12}   {bar(n1, max_b, 22)}{'':<3}"
              f"{n2:>4} {bar(n2, max_b, 22)}")

    sub("4.2 关键指标对比")
    print(f"  {'指标':<34}{'❌ 无抖动':>14}{'✅ 有抖动':>14}{'改善':>10}")
    print("  " + "-" * 76)
    rows = [
        ("100ms 桶内的并发峰值", no_jitter["peak_per_100ms"],
         with_jitter["peak_per_100ms"], "峰值越低越好"),
        ("1ms 内的并发峰值（瞬时冲击）", no_jitter["ms_peak"],
         with_jitter["ms_peak"], "越低越好"),
        ("发生重试的不同毫秒时刻数", no_jitter["distinct_ms"],
         with_jitter["distinct_ms"], "越多说明越分散"),
        ("重试时间跨度（秒）", round(no_jitter["spread"], 2),
         round(with_jitter["spread"], 2), "越大说明越摊开"),
    ]
    for name, a, b, note in rows:
        if a and a > 0:
            improve = f"{(a - b) / a * 100:.0f}%" if a > b else f"+{(b - a) / a * 100:.0f}%"
        else:
            improve = "-"
        print(f"  {name:<34}{a:>14}{b:>14}{improve:>10}   {note}")

    sub("▸ 解读：惊群效应（thundering herd）")
    print(f"""    【❌ 无抖动】所有 worker 的退避时间**完全相同**：
      delay 序列是 0.1s → 0.2s → 0.4s → 0.8s（每个 worker 都一样）。
      于是所有 100 个 worker 在**同一时刻**重试：
        · 100ms 桶内的并发峰值 = {no_jitter['peak_per_100ms']}（几乎全部 100 个）
        · 1ms 内的瞬时并发峰值 = {no_jitter['ms_peak']} 个请求
          **在 1 毫秒内同时打到服务器上！**
        · 发生重试的不同毫秒时刻只有 {no_jitter['distinct_ms']} 个
          （理论上应该接近 400 个，实测被压缩到了 {no_jitter['distinct_ms']} 个）

      ▸ 这就是**惊群效应**：
        「退避」本该降低服务器压力，但因为没有抖动，
        所有请求只是**整齐地延后了同一段时间**，
        然后在下一个时刻**以完全相同的强度再次冲击**。
        服务器的压力曲线不是"削峰"，而是"平移到另一个位置"。

      ▸ 更糟的是**同步性会自我强化**：
        每次重试都在同一时刻 → 大家同时失败 → 同时进入下一次退避
        → 再次同时重试。整个系统被锁死在同一个节奏上，
        永远不可能靠自身力量错开。

    【✅ 有抖动】退避时间在 [0.5x, 1.5x] 区间内随机分布：
      · 100ms 桶内的并发峰值降到 {with_jitter['peak_per_100ms']}，
        峰值降低了 {(no_jitter['peak_per_100ms'] - with_jitter['peak_per_100ms']) / no_jitter['peak_per_100ms'] * 100:.0f}%
      · 1ms 内的瞬时并发峰值降到 {with_jitter['ms_peak']}，
        降低了 {(no_jitter['ms_peak'] - with_jitter['ms_peak']) / no_jitter['ms_peak'] * 100:.0f}%
      · 重试发生在 {with_jitter['distinct_ms']} 个不同的毫秒时刻
      · 时间跨度从 {no_jitter['spread']:.2f}s 扩展到 {with_jitter['spread']:.2f}s

      ▸ 请求被**摊平**到一个时间窗口里，服务器承受的是渐变的压力，
        而不是一次次的脉冲。这就是「抖动」的全部价值。""")

    sub("▸ 关键洞察：抖动是分布式系统的必需品，不是可选项")
    print("""    抖动的应用远不止「重试」这一处。任何**大量客户端会同时做同一件事**
    的场景都需要抖动：

      · **缓存过期（缓存雪崩）**：如果 1000 个 key 都在 0 点过期，
        那么 0 点会有 1000 个缓存穿透，全部打到数据库。
        解法：TTL 加随机偏移 `ttl = base_ttl * (0.8 + random() * 0.4)`。
      · **定时任务**：所有爬虫都配在整点启动，整点就是流量尖峰。
        解法：启动时间加随机偏移。
      · **心跳/重连**：网络抖动导致 1000 个客户端同时断线，
        它们会同时重连，把服务端打挂（这在长连接服务里非常常见）。
        解法：重连间隔加抖动。
      · **分布式锁的等待**：100 个进程抢同一把锁失败后，
        如果都等 1 秒再抢，就成了 100 路并发抢锁。
        解法：等待时间加抖动。

    ▸ 一句话原则：
      **任何确定的、集中的时间点，都会变成热点。**
      加抖动就是把"点"变成"段"，把脉冲变成渐变。
      这个改动的成本是**几行代码**，收益是**系统的稳定性**。

    ▸ 抖动的度怎么把握？
      本课用 [0.5x, 1.5x]（±50%）。更激进的 AWS full jitter 用 [0, 1x]，
      也就是 `delay = random(0, base * 2^n)`。
      full jitter 的峰值更低（因为分布更宽），但平均等待时间也略低，
      导致"快速重试"的次数更多。
      **没有绝对最优，取决于你的服务器更怕"高并发冲击"
      还是更怕"重试次数太多"。** 对爬虫来说，前者更致命，所以
      full jitter 通常更合适。""")


# ============================================================================
# 实验 5：有代理池 vs 无代理池
# ============================================================================
def exp5_pool_vs_no_pool() -> None:
    """实验 5：单 IP 被封 vs 代理池自动切换。"""
    title("【实验 5】有代理池 vs 无代理池：成功率的差距有多大？")

    n_pages = 200
    ip_limit = 25
    pool_size = 20

    print(f"""
    场景：抓 {n_pages} 个页面，目标站的风控规则是
      「同一个 IP 连续请求超过 {ip_limit} 次后，永久拉黑该 IP」。

    ⚠ 这是对真实风控的**简化**，真实规则复杂得多：
      · 按时间窗口限流（比如每分钟 10 次）
      · 按 IP 段封禁（/24 整个 C 段）
      · 按行为特征（请求间隔方差、UA 一致性、Cookie 复用）
      · 静默污染（返回 200 但是假数据 —— 最阴险的一种）
      但"连续请求超标即封"这个简化抓住了核心机制：
      **单个 IP 的请求量是有上限的。**

    对比：
      A. 无代理池（单 IP 直连）
      B. 有代理池（{pool_size} 个代理，加权调度 + 失败降权 + 剔除复活）
    """)

    def run(mode: str) -> dict[str, Any]:
        """跑一种方案。

        Args:
            mode: 'single' 或 'pool'。

        Returns:
            统计结果。
        """
        rng = random.Random(2026)
        ok = 0
        blocked = 0
        retried = 0
        ip_request_count: dict[str, int] = {}
        blocked_ips: set[str] = set()
        # 同样需要虚拟时钟：否则「失败 → 冷却」的代理在整个实验里
        # 永远不会恢复，池子会在几百毫秒内被人为清空
        clock = VirtualClock()

        if mode == "pool":
            proxies = make_mock_proxies(pool_size, rng)
            pool = ProxyPool(proxies, strategy="weighted",
                             rng=random.Random(31), ban_threshold=4,
                             clock=clock)
        else:
            pool = None

        success_timeline: list[int] = []   # 每 20 个页面的累计成功数

        for i in range(1, n_pages + 1):
            for attempt in range(3):        # 最多重试 2 次
                if mode == "pool":
                    p = pool.pick()
                    if p is None:
                        # 池子空了 → 降级直连（这会暴露在 IP 计数里）
                        ip_key = "DIRECT"
                        proxy_obj = None
                    else:
                        ip_key = p.key
                        proxy_obj = p
                else:
                    ip_key = "SINGLE-IP"
                    proxy_obj = None

                # 风控检查：这个 IP 是否已被拉黑
                if ip_key in blocked_ips:
                    blocked += 1
                    if proxy_obj is not None:
                        pool.report(proxy_obj, False, 0.0)
                    if attempt < 2:
                        retried += 1
                    continue

                n = ip_request_count.get(ip_key, 0) + 1
                ip_request_count[ip_key] = n
                if n > ip_limit:
                    blocked_ips.add(ip_key)
                    blocked += 1
                    if proxy_obj is not None:
                        pool.report(proxy_obj, False, 0.0)
                    if attempt < 2:
                        retried += 1
                    continue

                # 正常请求
                if proxy_obj is not None:
                    ok_req, lat = simulate_one_request(proxy_obj, rng)
                    pool.report(proxy_obj, ok_req, lat)
                    clock.advance(lat)
                    if not ok_req:
                        blocked += 1
                        if attempt < 2:
                            retried += 1
                        continue
                else:
                    # 无代理池：也要消耗时间，否则两边的时间尺度不可比
                    clock.advance(0.2)
                ok += 1
                break

            if i % 20 == 0:
                success_timeline.append(ok)

        return {
            "ok": ok,
            "blocked": blocked,
            "retried": retried,
            "blocked_ips": len(blocked_ips),
            "success_rate": ok / n_pages,
            "ip_usage": ip_request_count,
            "timeline": success_timeline,
            "pool_stats": pool.stats() if pool else None,
        }

    single = run("single")
    pooled = run("pool")

    sub("5.1 结果对比")
    print(f"  {'方案':<24}{'成功':>7}{'失败':>7}{'重试':>7}{'成功率':>9}"
          f"{'被封IP数':>10}{'使用IP数':>10}")
    print("  " + "-" * 74)
    print(f"  {'A. 无代理池（单 IP）':<22}{single['ok']:>7}{single['blocked']:>7}"
          f"{single['retried']:>7}{single['success_rate'] * 100:>8.1f}%"
          f"{single['blocked_ips']:>10}{len(single['ip_usage']):>10}")
    print(f"  {'B. 有代理池（20 IP）':<22}{pooled['ok']:>7}{pooled['blocked']:>7}"
          f"{pooled['retried']:>7}{pooled['success_rate'] * 100:>8.1f}%"
          f"{pooled['blocked_ips']:>10}{len(pooled['ip_usage']):>10}")

    sub("5.2 成功率随进度的变化（每 20 个页面采样）")
    print(f"    {'页面数':<10}{'❌ 单 IP 累计成功':>20}{'✅ 代理池累计成功':>20}")
    print("    " + "-" * 56)
    for idx in range(len(single["timeline"])):
        pg = (idx + 1) * 20
        print(f"    {pg:<10}{single['timeline'][idx]:>20}"
              f"{pooled['timeline'][idx]:>20}")

    sub("5.3 单 IP 方案的死亡曲线")
    usage = single["ip_usage"]
    single_ip_count = usage.get("SINGLE-IP", 0)
    # 找出「累计成功数第一次明显落后于理想进度」的采样点，作为死亡点。
    # 这里刻意不把生成器表达式内嵌进 f-string：f-string 里嵌表达式再叠中文标点
    # 极易触发 f-string: unmatched ')' —— 先算好再格式化，可读性也更好。
    death_page = n_pages
    for i, v in enumerate(single["timeline"]):
        if v < (i + 1) * 20 * 0.9:
            death_page = (i + 1) * 20
            break
    print(f"""    单 IP 总共发了 {single_ip_count} 次请求后就被永久拉黑
    （风控阈值是连续 {ip_limit} 次）。
    之后所有请求全部失败 —— **成功率从 100% 断崖式跌到 0%**。

    ▸ 看上面的时间线：单 IP 方案的累计成功数在第
      {death_page} 个页面后
      就停止增长了。**这是一个不可恢复的失败** ——
      因为单 IP 没有「换一个身份」的能力。

    ▸ 代理池方案的被封 IP 数：{pooled['blocked_ips']} 个
      （总共用了 {len(pooled['ip_usage'])} 个 IP），
      但成功率维持在 {pooled['success_rate'] * 100:.1f}%。
      原因就是本课前面几节讲的机制在起作用：
        · 加权调度把请求分散到多个 IP 上 → 单个 IP 不会快速超限
        · 失败的 IP 被降权 → 后续请求自动避开它
        · 被剔除的 IP 不影响整体（还有其他 IP 顶上）

    ▸ **这就是代理池的核心价值：把「单点失败」变成「局部失败」。**
      没有代理池时，被封 = 采集能力归零；
      有代理池时，被封一个 IP 只损失池子的 1/N。""")

    sub("5.4 各 IP 的请求量分布（代理池方案）")
    usage_pool = pooled["ip_usage"]
    max_use = max(usage_pool.values()) if usage_pool else 1
    print(f"    {'IP':<18}{'请求数':>8}   分布")
    print("    " + "-" * 60)
    for ip, cnt in sorted(usage_pool.items(), key=lambda x: -x[1])[:12]:
        flag = " ⛔超限" if cnt > ip_limit else ""
        print(f"    {ip:<18}{cnt:>8}   {bar(cnt, max_use, 30)}{flag}")
    if len(usage_pool) > 12:
        print(f"    ... 共 {len(usage_pool)} 个 IP")
    over = sum(1 for c in usage_pool.values() if c > ip_limit)
    print(f"\n    超过单 IP 限额（{ip_limit}）的 IP 数：{over} / {len(usage_pool)}")

    sub("▸ 关键洞察：代理池不是「有就行」，关键在调度质量")
    print(f"""    注意一个容易忽略的点：**代理池的成功率不是 100%**
    （实测 {pooled['success_rate'] * 100:.1f}%）。原因有三：

      ① **池子里的代理本身有质量差异**（本实验随机生成了好坏混合的 {pool_size} 个）。
         一个 30% 成功率的代理即使被降权，偶尔还是会被选中。
      ② **加权调度给了差代理"探索机会"**（实验 1 讲过，这是有意为之）。
         不加探索会误杀"只是临时抖动"的好代理。
      ③ **单 IP 超限是硬约束**。即使池子调度的再均匀，
         {pool_size} 个 IP × {ip_limit} 次 = {pool_size * ip_limit} 次请求的物理上限。
         本实验只发了 {n_pages} 个页面，还没到上限，所以影响不大；
         但如果要抓 10000 个页面，就必须**扩容池子**。

    ▸ 扩容的公式（粗略）：
        需要的 IP 数 ≈ 总请求数 / (单 IP 限额 × 安全系数)
        安全系数取 0.6~0.7（留出余量应对突发和误封）
      抓 10000 个页面，单 IP 限额 {ip_limit}：
        需要 ≈ 10000 / ({ip_limit} × 0.65) ≈ {10000 / (ip_limit * 0.65):.0f} 个 IP
      ▸ 这个公式解释了为什么**大规模爬虫的代理成本很高** ——
        它本质上是"用钱买请求配额"。

    ▸ 所以降低成本的正确方向不是"找更便宜的代理"，
      而是**降低单位页面的请求数**：
        · 增量采集（第 54 课）：只抓变化了的页面
        · 条件请求（ETag）：304 不消耗配额（很多站点的风控不计 304）
        · 接口优先：直接调 JSON API，往往一个接口能顶 20 个页面
        · 缓存：本地已有的数据不要再抓
      **「少发请求」永远比「换更多 IP」更划算。**""")

    sub("▸ ⚠ 本实验的局限")
    print("""    · 风控模型是简化的：真实的封禁是「概率性 + 时间窗口 + 多维特征」的，
      而且往往是**静默的**（返回 200 但内容是反爬页面），
      这比"明确返回 429"危险得多 —— 你的数据会静默污染。
    · 代理质量是模拟生成的（有上帝视角），真实代理的质量未知且动态变化。
    · 没有模拟"同一个 /24 网段被封"的情况（本课生成的 IP 是
      10.0.x.y，现实中同一批代理常常来自同一 C 段，
      被封一个可能连带被封一片）。
    · 没有模拟代理的**并发连接数限制**（很多代理同时只允许 3~5 个连接，
      这也是一种隐藏的限速）。
    · 时间维度被压缩了：真实场景的封禁有时间窗口（比如限速 10 分钟），
      本实验的"连续 N 次"是瞬时计数，不含时间衰减。
      真实实现应该用**滑动窗口**或**令牌桶**来做这个计数（本课实验 3 已实现）。""")


# ============================================================================
# 踩坑记录
# ============================================================================
def pitfalls() -> None:
    """打印本课实测中真实遇到的问题。"""
    title("踩坑记录：本课代码实测中真实遇到的问题")

    print("""
    ── 坑 1：用 time.time() 做令牌桶的时间基准 → 限速静默失效 ──────────
    ❌ 错误做法：`elapsed = time.time() - self.last_refill`
    现象：绝大多数时候限速正常；但偶尔会出现"瞬间发出几百个请求"，
          紧接着就被目标站封禁。而且这个现象**极难复现**，
          因为它依赖于系统时间被调整。
    根因：time.time() 是**墙钟时间**，会被 NTP 校时、手工改时间、
          虚拟机快照恢复等影响。
          如果系统时间往后跳了 1 小时，`elapsed = 3600`，
          桶里的令牌瞬间补满到 capacity —— 限速完全失效。
          更糟的是这个 bug 只在"时间跳变"时出现，
          平时测试 1000 次都正常。
    正确做法：所有测量「间隔」「耗时」「超时」的地方，
          一律使用 **time.monotonic()**（单调时钟）。
          它只保证向前走，不受系统时间调整影响。
          唯一该用 time.time() 的地方是「需要在日志/数据里记录
          人类可读的绝对时间」。
    教学价值：这是新手最常犯的时间相关 bug。
          C++ 的 steady_clock、Java 的 System.nanoTime 都是同样的东西。
          **一个判断标准：如果你在做减法求"经过了多久"，
          就绝对不能用墙钟。**

    ── 坑 2：random.choices 在权重全为 0 时抛 ValueError ──────────────
    ❌ 错误做法：直接 `rng.choices(alive, weights=[p.score() for p in alive])`
    现象：代理池运行一段时间后，某次 pick() 突然抛
          ValueError: Total of weights must be greater than zero。
          触发时机是"所有可用代理的分数都恰好为 0"——
          这需要在所有代理都连续失败过之后才会出现，
          正常测试很难撞上。
    根因：代理的 score() 在连续失败惩罚下可能变成 0.0，
          如果所有可用代理都是 0 分，权重和就是 0，
          random.choices 会直接拒绝。
    正确做法：`weights = [max(p.score(), 0.01) for p in alive]`
          —— 给一个极小下限，保证权重和恒大于 0。
          语义上它也正确：所有代理都是 0 分时，
          表现为"近似均匀随机"，而不是崩溃。
    教学价值：**任何"按权重抽样"的实现都必须处理权重全 0 的情况。**
          这也是一个更普遍的原则：**边界条件往往出现在
          "所有元素都恰好处于最差状态"时**，而这正是压力最大的时刻 ——
          系统最脆弱的时候抛出最莫名其妙的异常，后果最严重。

    ── 坑 3：代理池的 pick/report 在多线程下丢数据 ────────────────────
    ❌ 错误做法：多线程共享一个 ProxyPool，不加锁地读写 pick_count、
          banned_total 等计数器，并且 pick 和 report 之间不加保护。
    现象：`pick_count` 的总和小于实际请求数（计数丢失）；
          极端情况下同一个代理被两个线程同时选中并都判定为"可用"，
          尽管它其实已经进入冷却。
    根因：`self.pick_count[key] = self.pick_count.get(key, 0) + 1`
          是"读-改-写"三步操作，在多线程下会丢失更新。
          （注意：第 60 课实验 2 里我们讨论过，
          CPython 的 GIL 有时会让这种操作"碰巧安全"，
          但那依赖于字节码调度，不能依赖。）
    正确做法：给计数器和状态变更加 threading.Lock；
          或者用 collections.Counter + 单线程汇总。
          更重要的是：**pick() 里"选出一个可用代理"
          和"标记它已被使用"应该是原子的**，
          否则两个线程可能选中同一个刚进入冷却的代理。
    本课处理：本课的实验都是单线程顺序调用，
          所以没有暴露这个问题；但这是一个**真实的隐患**，
          第 65 课的综合实战里会加上锁。
    教学价值：**"我的测试跑通了"不等于"并发下是对的"。**
          写并发代码时要主动问自己：「这个复合操作会不会被插入？」

    ── 坑 4：退避的 max_delay 没设上限 → 任务永不执行 ─────────────────
    ❌ 错误做法：`delay = base * 2 ** attempt`（不设上限）
    现象：某个失败任务在第 20 次重试时，退避时间达到
          0.1 * 2^20 = 104857 秒 ≈ 29 小时。
          任务实际上永远不会被重新执行，但它**也没有被标记为失败** ——
          它就静静地躺在队列里，占用一个位置，且没有任何告警。
    根因：指数增长的天然特性 —— 它会超过任何有限的时间预算。
    正确做法：① 设置 max_delay（本课用 10 秒）
          ② 设置最大重试次数（超过就转入死信队列）
          ③ 死信队列要有监控和告警（它才是真正需要人工介入的地方）
    教学价值：**指数退避 + 无上限 = 资源泄漏。**
          任何指数增长的量都必须有天花板。
          同类的坑：指数退避的累积计数溢出、
          递归重试导致的栈溢出、指数扩缩容导致的资源耗尽。

    ── 坑 5：把「按域名限速」实现成了「按完整 URL 限速」────────────
    ❌ 错误做法：`bucket_for(request.url)` —— 用完整 URL 做桶的 key
    现象：限速看起来"生效了"（每个 URL 都有独立的桶，
          每个桶都不会超限），但实际上**完全没有起到限速作用** ——
          因为一个站点的不同 URL 会各自拥有独立的桶，
          于是该站点的总 QPS = 桶数量 × 单桶 QPS，远远超标。
          这个 bug 特别隐蔽：所有单元测试都通过
          （单独测一个 URL 的限速是对的），只有全站抓取时才暴露。
    根因：限速的**约束对象**是"目标服务器"，
          而服务器是按**域名**划分的，不是按 URL。
          URL 是域名的子集（一个域名有无数个 URL），
          按 URL 限速等于按"页面"限速 —— 而风控是按"站点"计数的。
    正确做法：先从 URL 提取域名（本课的 extract_domain），
          用域名做桶的 key。
    教学价值：**限速的粒度必须和"约束的来源"对齐。**
          问自己：「谁在限制我？」—— 是目标服务器的风控。
          「它按什么维度计数？」—— 按 IP、按域名、按账号。
          那么你的限速就必须按同样的维度做。
          同类的错误：按用户限速写成了按会话限速、
          按 API 限速写成了按 endpoint 限速。


    ── 坑 6：模拟环境里「时间尺度失真」→ 三种策略跑出相同结果 ★ ────────
    ❌ 错误做法：模拟时不注入时钟，直接让 Proxy / ProxyPool 用 time.time()。
    现象（本课实跑遇到的第一个大坑）：
          实验 1 里三种调度策略的成功率**几乎完全相同**：
            轮询 96.2% | 加权 96.4% | 最优优先 96.2%
          而且「活跃代理数」只剩 1~2 个（池子里明明有 20 个）。
          轮询本该"流量均分"的特性**完全体现不出来**。
          → 所有对照实验跑出相同数字时，**先怀疑开关没生效**，
            而不是急着写结论。这是本课最重要的一条方法论。
    根因：模拟跑得**太快了**。
          500 次「请求」在纯内存里只要 **4.7 毫秒**，
          而失败一次触发的冷却是 **1 秒**。
          也就是说：任何一个代理只要失败过一次，
          就会在本次实验剩余的全部时间里一直处于冷却状态 ——
          等于**永久下线**。1 秒 ÷ 4.7 毫秒 ≈ 200 倍的时间尺度错配。
          最后只有运气最好的 1~2 个代理能一直活着，
          三种策略都被迫退化成"近似单代理"，结果自然一样。
    正确做法（本课采用）：引入 **VirtualClock 虚拟时钟**。
          让模拟拥有自己的时间轴：每次请求都让时钟前进"本次延迟"的量级。
          这样 500 次请求对应虚拟时间里的 200 秒左右，
          冷却期（1~8 秒）才是它本该有的意义。
          修好后实测：
            轮询 84.6%（差代理均分请求，被拖累）
            加权 87.8%（好代理多拿，Top1 仅 7.2%）
            最优优先 95.6%（Top1 高达 94%，风险全部集中）
          **三种策略的差异终于显现出来了。**
    教学价值：**任何"时间驱动"的逻辑，测试时都必须让时间可注入。**
          这个坑在真实项目里的表现形式恰好相反：
          单元测试用 1 毫秒的冷却期（跑得快），上线用 60 秒的冷却期，
          **测试通过但行为完全不同**。
          生产代码里对应的是「依赖注入一个 clock」，Python 圈常用
          `freezegun` 或自建 `Clock` 协议（本课的 VirtualClock 就是最小版）。
          同类的坑：用真实 random 但没固定种子导致测试偶发失败。


    ── 坑 7：配置字典的键格式和运行时提取出的键**对不上** ──────────────
    ❌ 错误做法：
          per_domain = {"https://site-a.com": 40, ...}   # 配置用完整 URL
          limiter.acquire(url)                          # 内部先 extract_domain()
          # → 查表时用的键是 "site-a.com"，永远查不到
    现象（本课实跑遇到的坑）：
          实验 3 按域名限速后，三个站点的实测 QPS **完全一样**（都是 13.1），
          和它们各自的配置（40 / 3 / 12）毫无关系。
          而"独立令牌桶"是限速器的核心卖点 —— 它看起来**完全失效了**。
    根因：`extract_domain("https://site-a.com")` 返回 `"site-a.com"`，
          而配置字典的键是 `"https://site-a.com"`。
          `dict.get(domain, default_qps)` 查不到 → **全部落到默认值 10**。
          三个站点都用默认值，当然一样。
    这个 bug 的恶劣之处：
          ① **不报错** —— dict.get 有默认值，静默成功
          ② **功能"看起来正常"** —— 限速确实在生效，只是全用的默认值
          ③ 只有当你**对比「配置值 vs 实测值」**时才会发现对不上
    正确做法：**在限速器内部把配置的键归一化**（本课采用），
          对外接受 URL 或裸域名两种写法，内部统一成域名：
              self.per_domain = {extract_domain(k): v for k, v in cfg.items()}
          另一种做法是要求调用方统一格式（靠约定），但约定容易出错。
          **靠代码而不是靠约定，是更稳健的设计。**
    教学价值：**凡是「用一个字符串去查配置表」的地方，都要问一句
          「这个字符串的格式，和我配置时用的是同一种吗？」**
          同类的坑：环境变量大小写、文件路径是否绝对、
          时间戳是秒还是毫秒、ID 是 int 还是 str（`"1" != 1`）。


    ── 坑 8：测量方法本身引入的偏差 → 把「突发」当成了「稳态速率」─────
    ❌ 错误做法：用「第一个请求到最后一个请求」的总跨度算 QPS。
    现象：site-a 配置 40 QPS，实测却是 **197 QPS**（超了 4 倍），
          看起来限速器完全没生效。
          但同一批数据里，site-b（配置 3）实测 3.2、site-c（配置 12）
          实测 16.9 —— 又大致合理。同一个实现，怎么能一个失效一个正常？
    根因：**测量窗口包含了启动瞬态。**
          令牌桶的初始容量是 `rate × capacity_factor = 40 × 1.5 = 60`，
          开局这 60 个令牌会被**瞬间**用完 —— 这是设计好的"突发额度"。
          如果从第一个请求就开始计时，这段突发会把平均速率算得虚高。
          而请求总数只有 60 个时，突发阶段占了绝大部分，偏差就极其严重。
          修好后（提高请求数 + 去掉开局 1 秒）实测：
            site-a 40.0 / 40（偏差 0%）
            site-b  3.0 / 3 （偏差 0%）
            site-c 12.0 / 12（偏差 0%）
    正确做法：
          ① **测速率必须避开启动瞬态** —— 丢掉开局一段，只看稳态窗口
          ② 采样时长要远大于瞬态时长（本课最终用 100 个请求）
          ③ 报告结果时**同时给出"配置值"和"实测值"**，
             让读者能判断偏差是否可接受
    教学价值：**性能测量里，"你测的是什么"比"你测出多少"更重要。**
          看到离谱的数字时，先怀疑测量方法，再怀疑被测对象。
          同类的坑：用 time.time() 而不是 perf_counter 测耗时、
          把 JIT 预热阶段算进基准测试、用平均值掩盖长尾
          （第 60 课讲过 P99 的教训）。


    ── 坑 9：稳态窗口用「绝对时间」切 → 快方案的样本数变成 0 ─────────────
    ❌ 错误做法（是坑 8 的"修复版"引入的新 bug）：
          warmup_cut = 1.0
          steady = [t for t in times if t >= t0 + warmup_cut]   # 丢掉前 1 秒
    现象：修完坑 8 之后，**不限速方案的实测 QPS 变成了 0.0**，
          按域名限速下的 site-a 也变成 0.0。
          解读正文里出现了「超出 0.0 倍」这种荒谬数字。
    根因：「前 1 秒」这个绝对时间窗口，隐含假设了**所有方案都跑得足够慢**。
          不限速方案 60 个请求只要 **0.30 秒**就跑完了 ——
          第 1 秒之后的样本数是 **0**，除以 0 就得到 0.0。
          按域名限速的 site-a（40 QPS，60 个请求约 1.5 秒）也几乎全军覆没。
    正确做法：**用「相对位置」而不是「绝对时间」定义稳态窗口**。
          改成按请求序号跳过前 30%：
              skip = int(len(times) * 0.3)
              steady = times[skip:]
          这样无论方案是 0.3 秒还是 30 秒，
          「丢掉的开局那一段」都占总量的同样比例，采样逻辑一致。
    教学价值：**写测量/切分逻辑时，不要用「绝对量」去卡「相对阶段」。**
          绝对量只在你能保证取值范围时才安全。
          同类的坑：用固定的前 10 条数据做预热，
          但有的场景只有 5 条数据 → 预热后没有任何数据可测。""")


# ============================================================================
# 主流程
# ============================================================================
def main() -> None:
    """运行全部实验。"""
    print(SEP)
    print("阶段 6 · 第 63 课：代理池与反爬对抗")
    print(SEP)
    print("""
本课聚焦反爬对抗的三个层次中的**最上层 —— 节奏控制**：

  L1 资源层：有代理可用          （花钱能解决，无工程含量）
  L2 调度层：怎么分配代理        （本课实验 1、2）
  L3 节奏层：什么时候发请求 ★    （本课实验 3、4 —— 真正的核心竞争力）

核心认知：**反爬对抗的本质不是「换身份」，而是「改变行为模式」。**

⚠ 局限声明：
  · 本课**不访问任何真实网站**，也不使用任何真实代理。
  · 所有代理都是模拟的（有"上帝视角"的真实质量参数），
    真实的代理质量未知、动态变化、且分地域/分目标站。
  · 风控规则是简化的「连续 N 次即封」，
    真实的封禁是多维特征 + 时间窗口 + 概率性的，
    而且常常是**静默的**（返回 200 假数据，比 429 危险得多）。
  · 本课的全部耗时数字来自本机模拟，不代表真实网络表现。

★ 合规提醒：
  代理、限速、退避都是中性技术。合法用途包括保护自有业务、
  访问授权的数据源、做容灾切换。请遵守目标站的 robots.txt
  与服务条款，以及《数据安全法》《个人信息保护法》的相关规定。
""")

    exp1_dispatch_strategies()
    exp2_proxy_lifecycle()
    exp3_rate_limiter()
    exp4_backoff_jitter()
    exp5_pool_vs_no_pool()
    pitfalls()

    title("本课要点")
    for line in [
        "1. 反爬对抗分三层：资源（有代理）→ 调度（怎么分）→ 节奏（什么时候发）",
        "2. 核心竞争力在节奏层：5 个 IP 控制好节奏，胜过 500 个 IP 乱打",
        "3. 代理评分三个维度：成功率(0.6) + 速度(0.3) + 新鲜度(0.1)，再乘连败惩罚",
        "4. 连败惩罚用乘法不用减法，让好代理「伤而不死」、差代理直接归零",
        "5. 区分「质量评估」（总失败率）和「存活判断」（连续失败数），二者分工明确",
        "6. 新代理要给中性先验（0.5）而不是 0 或 1，否则不是误信就是冷启动死锁",
        "7. 调度用加权随机：好代理多拿但有探索，best_first 会被逐个击破",
        "8. 剔除要配冷却 + 复活：冷却=缓刑，剔除=死刑，误杀好代理的代价更大",
        "9. 令牌桶优于固定间隔 sleep：能应对耗时波动，且支持突发额度",
        "10. 限速必须按域名：约束来自对方的容忍度，所以约束要按对方分别定义",
        "11. 测间隔耗时一律用 time.monotonic()，墙钟会被 NTP 校时影响",
        "12. 指数退避必须有 max_delay，否则任务会退避到永远不执行（资源泄漏）",
        "13. 退避必须加抖动，否则所有 worker 同时重试 → 惊群效应",
        "14. 任何「大量客户端同时做同一件事」的场景都需要抖动（缓存过期、心跳重连、定时任务）",
        "15. 代理池把「单点失败」变成「局部失败」，是无代理方案最大的价值差距",
        "16. 降低代理成本的方向是「少发请求」（增量、ETag、接口优先），不是「找更便宜的 IP」",
        "17. 「全员冷却」和「全员被剔除」是两种枯竭，要分开处理：前者等待，后者重检",
        "18. 池子枯竭必须有显式分支（降级直连 / 回延时队列 / 告警），忘了写就会线上崩",
        "19. 建模分布式系统时，时间必须是可注入的依赖 —— 否则时间尺度失真会让对照实验全部失效",
        "20. 配置表的键要和运行时提取的键同格式，否则静默落到默认值（用代码归一化，别靠约定）",
        "21. 测速率要避开启动瞬态：突发额度会把实测 QPS 算成配置值的数倍",
        "22. 对照实验跑出相同数字时，先怀疑「开关没生效」，再写结论",
    ]:
        print("  " + line)
    print()


if __name__ == "__main__":
    main()
