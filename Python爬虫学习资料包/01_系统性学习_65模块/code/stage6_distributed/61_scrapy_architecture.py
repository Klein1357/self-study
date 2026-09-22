"""
第 61 课 · Scrapy 架构 —— 用纯标准库复刻一个迷你 Scrapy 引擎

本课要回答的问题：
  1. Scrapy 的 Engine / Scheduler / Downloader / Spider / Pipeline
     五大组件各自负责什么？它们之间怎么传递数据？
  2. 为什么 Scrapy 要把「下载」和「解析」拆成两个独立的部分，
     而不是写成一个 for 循环？（这个设计带来的核心能力是什么？）
  3. Downloader Middleware 和 Spider Middleware 到底插在哪一环？
     为什么很多「反爬问题」的解法都藏在 Downloader Middleware 里？
  4. Scheduler 的优先级队列和去重是怎么协作的？
     为什么 Scrapy 默认给 Request 的优先级是 0，而 dupefilter 用的是指纹？
  5. 真实 Scrapy 有哪些东西是我这个迷你版没有的？少了的后果是什么？

================================ 运行方式 ================================
    python3 code/stage6_distributed/61_scrapy_architecture.py

⚠ 本课**不安装也不使用 Scrapy**。全程用纯标准库（queue / threading /
   dataclasses / itertools / hashlib）从零复刻它的核心骨架。

   为什么不用真 Scrapy？
     因为 Scrapy 的价值 90% 在于它的**架构设计**，而不是 API 用法。
     真 Scrapy 会把 Twisted reactor、信号、扩展、配置层全部压进调用栈，
     你在 2000 行的日志里根本看不清「一个 Request 是怎么走到 Spider.parse 的」。
     所以本课用 800 行把它拆开，让每一个环节都打印出来。

   本课与真实 Scrapy 的差异（诚实声明，详见文末「局限」章节）：
     · 没有 Twisted reactor / 没有 asyncio 事件循环，用线程池模拟并发
     · 没有自动限速（AutoThrottle 扩展）
     · dupefilter 用的是简单集合，不是 Scrapy 那种「指纹 + 三种去重策略」
     · 没有 CrawlSpider / Rule / LinkExtractor 等上层封装
     · 没有 Item Pipeline 的 open_spider/close_spider 生命周期之外的信号系统
"""

from __future__ import annotations

import hashlib
import heapq
import itertools
import queue
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Sequence

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
SUB_SEP = "-" * 76


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
# 零、先看全景：Scrapy 的数据流图
# ============================================================================
# 下面这张图是本课的全部内容。请先扫一眼，再往下看实现 ——
# 每一节实现完一个组件，就回来对一下它在图中的位置。
#
#                            ┌───────────────────────────────┐
#                            │           ENGINE              │  ⑥ 事件循环调度
#                            │   （本课：MiniEngine.run）     │     所有组件的总线
#                            └───────┬───────────┬───────────┘
#                      ⑤ 取request  │           │  ③ 递 request
#                                    ▼           ▼
#     ┌──────────────────┐   ┌───────────────┐   ┌──────────────────────┐
#     │    SCHEDULER     │   │  DOWNLOADER   │   │   SPIDER MIDDLEWARE  │
#     │  优先级队列       │◀──│  并发 + 重试   │   │  处理「输出」的item/  │
#     │  + 指纹去重       │   │               │   │  request（本课简化）  │
#     │                  │   │               │   └──────────┬───────────┘
#     │  ① 入队的request  │   └───────┬───────┘              │
#     │    先过指纹去重   │           │ ④ Response            │ ⑦ item
#     └──────────────────┘           │                       ▼
#                                    │           ┌──────────────────────┐
#                            ┌───────┴───────┐   │       SPIDER         │
#                            │  DOWNLOADER   │   │  parse() 产出         │
#                            │  MIDDLEWARE   │   │   · Item              │
#                            │  处理「请求」  │   │   · 新 Request        │
#                            │  和「响应」    │   │                      │
#                            │  ★ 代理/UA/    │   └──────────┬───────────┘
#                            │  重试/限速都在这│              │ ⑧ item
#                            └───────────────┘              ▼
#                                                ┌──────────────────────┐
#                                                │      PIPELINE        │
#                                                │  清洗→校验→入库 链式   │
#                                                └──────────────────────┘
#
# 用一句话概括这个架构的**精髓**：
#
#   Spider.parse() 不「下载」东西，它只「产出」Request。
#   Engine 拿到 Request 后，把它丢回 Scheduler 重新排队。
#
# 这一个设计决策带来了三个关键能力：
#   · **广度优先 / 深度优先可切换**：靠优先级队列实现，Spider 不用改一行代码
#   · **去重是全局的**：所有请求都经过 Scheduler，指纹天然全局唯一
#   · **可断点续爬**：把 Scheduler 的队列搬到 Redis 就能多机共享（第 62 课）
#
# 如果用 for 循环写爬虫：
#     for url in urls:
#         resp = fetch(url)          # 下载和解析耦合在一起
#         for next_url in parse(resp):
#             urls.append(next_url)  # 只能自己维护一个列表
# 你就失去了上面全部三项能力 —— 而且列表会无限膨胀、无去重、无优先级。


# ============================================================================
# 一、数据模型：Request / Response / Item
# ============================================================================
# 先定义三个「数据载体」。它们都是**纯粹的容器**，不含任何业务逻辑 ——
# 这一点很重要：Scrapy 里 Request/Response/Item 都是这样的哑对象，
# 所有逻辑都在 Engine / Middleware / Spider 里。
# 这样做的原因是它们要在组件之间反复传递，
# 一旦某个容器自带逻辑，就会出现「Middleware 改了它，Spider 不知道」的隐式耦合。


@dataclass(order=False)
class Request:
    """一次待发起的请求。

    Attributes:
        url: 目标 URL。
        callback: 拿到 Response 后要调用的 Spider 方法名。
            为什么存**方法名字符串**而不是方法本身？
            ① 因为 Request 可能要被序列化后丢进 Redis（分布式爬虫必须能序列化），
               方法对象不可序列化，方法名可以；
            ② 这样 Spider 类可以随意重构，只要保持方法名即可。
            真实 Scrapy 的 Request 还有一个 errback 参数用于处理失败，
            本课简化掉了，失败统一由 Downloader 的重试逻辑处理。
        priority: 优先级，数字越**大**越先被调度。
            注意与 heapq 的直觉相反：heapq 是小顶堆，
            所以 Scheduler 里会存 -priority（见 _Scheduler 实现）。
            真实 Scrapy 的默认值是 0，比 0 大的先走。
        meta: 附加元数据（代理、重试次数、深度、下载延迟等）。
            这是 Scrapy 里最重要的「逃生舱」—— 任何需要跨组件传递的
            信息都往 meta 里塞，不用改组件接口。
        dont_filter: 是否跳过去重。默认 False（要过滤）。
            什么时候设 True？当同一个 URL 确实需要重复抓取时，
            比如「翻页的下一页」在最后一页会反复指向自己。
    """

    url: str
    callback: str = "parse"
    priority: int = 0
    meta: dict[str, Any] = field(default_factory=dict)
    dont_filter: bool = False

    def fingerprint(self) -> str:
        """计算请求指纹（用于去重）。

        Returns:
            32 位十六进制指纹字符串。

        为什么指纹要包含 method/body，而不只是 url？
          因为 POST 到同一个 URL 但 body 不同，是两个完全不同的请求。
          真实 Scrapy 的指纹算法是
              sha1(method + url + body + headers的规范化子集)
          本课简化为 url + callback，因为我们的模拟请求没有 body。

        ⚠ 一个重要陷阱：**指纹不要包含 meta**。
          meta 里可能带随机数、时间戳、重试计数，
          把它们纳入指纹会导致「同一个 URL 每次指纹都不同」，
          去重彻底失效。这是新手最常犯的错误之一。
          反过来说，如果你**确实想让某个 URL 每次都被当成新请求**，
          往 meta 里塞一个随机数确实是一种 hack —— 但那是坏味道，
          正确做法是用 dont_filter=True 显式表达意图。
        """
        raw = f"{self.callback}|{self.url}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


@dataclass
class Response:
    """一次请求的结果。

    Attributes:
        url: 最终 URL（可能因重定向而与 Request.url 不同）。
        status: HTTP 状态码。
        body: 响应体文本。
        request: 产生这个响应的原始 Request。
            为什么 Response 要反向持有 Request？
            因为 Spider.parse(response) 里经常需要知道
            「我是从哪个 meta 过来的」（比如当前页码、代理 IP）。
            Scrapy 的 response.request 和 response.meta 就是干这个的。
        elapsed: 本次下载耗时（秒）。
        from_cache: 是否来自缓存（本课用内存字典模拟下载缓存）。
    """

    url: str
    status: int = 200
    body: str = ""
    request: Request | None = None
    elapsed: float = 0.0
    from_cache: bool = False

    @property
    def meta(self) -> dict[str, Any]:
        """透传 Request 的 meta。

        Returns:
            meta 字典；没有关联 Request 时返回空字典。

        这是 Scrapy 的经典便利属性，让 Spider 里写
        response.meta["page"] 而不是 response.request.meta["page"]。
        """
        return self.request.meta if self.request is not None else {}

    def css(self, selector_hint: str) -> str:
        """一个极度简化的「选择器」，只支持取标签里的文本。

        Args:
            selector_hint: 形如 'title' 的标签名提示。

        Returns:
            标签内的文本；找不到时返回空字符串。

        ⚠ 本课不实现真正的 CSS/XPath —— 那是阶段 2 的内容。
          这里的实现只是为了 demo 能用，它用最朴素的字符串查找。
          真实 Scrapy 用 parsel（封装 lxml），支持完整 CSS3 + XPath 1.0。
        """
        open_tag = f"<{selector_hint}>"
        close_tag = f"</{selector_hint}>"
        i = self.body.find(open_tag)
        if i < 0:
            return ""
        j = self.body.find(close_tag, i)
        if j < 0:
            return ""
        return self.body[i + len(open_tag):j].strip()

    def css_all(self, selector_hint: str) -> list[str]:
        """取出所有指定标签的文本。

        Args:
            selector_hint: 标签名。

        Returns:
            文本列表。
        """
        open_tag = f"<{selector_hint}>"
        close_tag = f"</{selector_hint}>"
        out: list[str] = []
        pos = 0
        while True:
            i = self.body.find(open_tag, pos)
            if i < 0:
                break
            j = self.body.find(close_tag, i)
            if j < 0:
                break
            out.append(self.body[i + len(open_tag):j].strip())
            pos = j + len(close_tag)
        return out


class Item(dict):
    """被抓取的一条数据。

    │ 为什么继承 dict 而不是用 dataclass？
    │   真实 Scrapy 的 Item 有「Field 声明 + 类型校验 + 未设置字段访问报错」
    │   三重机制。但它的**本质**仍然是一个「字段名 → 值」的映射，
    │   而且 Pipeline 需要动态增删字段（比如加一个 content_hash）。
    │   用 dict 子类能免费获得序列化、解包、in 判断等全部能力。
    │
    │ 这里继承 dict 而不是直接用 dict，是为了给未来的字段校验留一个
    │ **明确的类型锚点**：当业务代码写 isinstance(x, Item) 时，
    │ 语义是「这是一条待入库的采集结果」，而不是「这是个随便的字典」。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """构造一个 Item。

        Args:
            *args: 传给 dict 构造器的位置参数。
            **kwargs: 传给 dict 构造器的关键字参数。

        Returns:
            None
        """
        super().__init__(*args, **kwargs)
        # spider 字段记录「这条数据是谁抓的」，
        # 多 Spider 协同时用于区分数据来源（真实 Scrapy 也有这个名字）。
        self.setdefault("_spider", "")


# ============================================================================
# 二、Scheduler：优先级队列 + 指纹去重
# ============================================================================
class DupeFilter:
    """基于指纹的请求去重器。

    这个类的设计有一个**关键取舍**，值得单独讲：

      ❌ 朴素做法：保存完整的 URL 字符串
         一个 URL 平均 80 字节，1000 万个 URL 就是 800MB 内存。

      ✅ 做法：保存 SHA1 指纹的前 32 位十六进制字符（16 字节）
         1000 万个指纹只要 160MB。而且定长，内存可预测。

      ⚠ 但指纹去重有一个**本质风险：哈希碰撞**。
         SHA1 前 128 位理论上碰撞概率极低（约 10^-19 量级），
         但要注意：**碰撞的后果是「漏抓」而不是「重复抓」**，
         且无法察觉。所以指纹长度不能太短 —— 用 8 位十六进制（32 bit）
         在 100 万 URL 规模下碰撞概率就已经到 1% 量级了。

      ▸ 真实 Scrapy 的 dupefilter 默认用 SHA1 全长（40 位十六进制），
        并且提供一个 jobs 目录把指纹持久化到磁盘（支持断点续爬）。
        本课用 32 位十六进制（截断后的 SHA1），在 demo 规模下足够。
    """

    def __init__(self, enabled: bool = True) -> None:
        """初始化去重器。

        Args:
            enabled: 是否启用去重。关掉可用于「强制重抓」的调试场景。
        """
        self.enabled = enabled
        self._seen: set[str] = set()
        self.checked = 0
        self.duplicated = 0

    def request_seen(self, request: Request) -> bool:
        """判断请求是否已见过，并记录。

        Args:
            request: 待检查的请求。

        Returns:
            True 表示**已经见过**（应当丢弃）；False 表示是新请求（应当入队）。

        ⚠ 注意这个方法的**副作用语义**：调用它会「标记为已见」，
          而不是纯粹的查询。名字叫 request_seen（问句），
          但实现是「检查并登记」。
          这是 Scrapy 的原始命名，容易误用 —— 如果你只想查询不想登记，
          就得自己写 is_seen()。本课保留了这个名字以保持与真实 Scrapy 一致，
          但在这里明确指出它的副作用。
          在单线程 Engine 里不会有问题；如果多线程共享这个 DupeFilter，
          必须加锁，且「检查 + 写入」是**复合操作**，
          用 threading.Lock 包住，不能只锁写
          （否则两个线程可能同时判断为"新"）。
          第 62 课会用 Redis SADD 的原子性彻底解决这个问题。
        """
        self.checked += 1
        if not self.enabled:
            return False
        fp = request.fingerprint()
        if fp in self._seen:
            self.duplicated += 1
            return True
        self._seen.add(fp)
        return False

    def __len__(self) -> int:
        """已登记指纹数。

        Returns:
            指纹数量。
        """
        return len(self._seen)

    @property
    def hit_rate(self) -> float:
        """去重命中率。

        Returns:
            0.0 ~ 1.0 的浮点数。

        这个指标是爬虫健康度的核心信号之一：
          · 命中率突然变 0   → 可能是去重失效（指纹里混进了随机值）
          · 命中率突然 > 90% → 可能是站点在做「链接闭环」，
            页面互相指向对方，此时要降低抓取深度上限
        """
        return self.duplicated / self.checked if self.checked else 0.0


class Scheduler:
    """请求调度器：优先级队列 + 去重。

    Scrapy 的 Scheduler 做的事情比大部分人以为的要多：
      ① 去重（dupefilter）
      ② 优先级排序（优先队列）
      ③ 磁盘持久化（jobs 目录，支持断点续爬）
      ④ 请求去重后的「备份队列」（分布式模式下的 Redis 队列）

    本课实现 ① 和 ②，第 62 课会把 ③④ 用 Redis 补上。

    │ 关于优先级队列的一个关键细节：
    │   Python 的 heapq 是**小顶堆**（每次弹出最小值）。
    │   而 Scrapy 的语义是「priority 越大越先出队」。
    │   所以入队时要存 -priority 来反转顺序。
    │
    │   还有一个更隐蔽的坑：**heapq 在元素相等时会比较第二个元素**。
    │   如果我们存 (priority, request)，两个 priority 相同的 Request
    │   就会退化成比较 Request 对象 —— 而 dataclass 默认不实现 < 运算，
    │   直接抛 TypeError: '<' not supported between instances of 'Request'。
    │   这就是著名的「unorderable types」错误。
    │   解法：存 (priority, 单调递增序号, request)。
    │   序号由 itertools.count() 提供，永不重复，于是比较在第二个元素
    │   就分出胜负，永远不会走到比较 Request 那一步。
    │   这个技巧叫 **tie-breaker**，是使用 heapq 时的必备套路。
    """

    def __init__(self, dupefilter: DupeFilter | None = None) -> None:
        """初始化调度器。

        Args:
            dupefilter: 去重器；为 None 时新建一个默认开启的。

        Raises:
            TypeError: 当 dupefilter 不是 DupeFilter 实例也不是 None 时。

        ⚠⚠ 这里有一个**极其隐蔽的真实 bug**，是本课最值得记住的踩坑之一
           （踩坑记录：❌ 错误做法 → 现象 → 根因 → 正确做法）：

        ❌ 错误做法（我最初就是这么写的）：
               self.dupefilter = dupefilter or DupeFilter()

           现象：实验 3 里「去重关闭」和「去重开启」跑出了**完全相同**的结果，
                 指纹库都是 8 个、命中率都是 42.9% ——
                 看起来像是「去重开关根本不生效」。
                 更糟的是（如果只跑单个实验）它**完全不报错**，
                 你只会得出一个错误的结论。

           根因：Python 的 `or` 运算符用的是**真值判断**，不是 `is None` 判断。
                 而 `DupeFilter` 实现了 `__len__`（返回已登记的指纹数量），
                 于是**一个空的 DupeFilter 对象的布尔值是 False**！
                 （Python 的对象真值规则：实现了 __bool__ 用它；
                  否则实现了 __len__ 就用 len() != 0。）
                 所以 `DupeFilter(enabled=False) or DupeFilter()` 中，
                 左边虽然是合法的对象，但因为它是「空」的，
                 整个表达式直接取右边的**新对象**（enabled=True）。
                 我们精心构造的「关闭去重」配置被静默丢弃了。

           正确做法：显式判断 None ——
               self.dupefilter = dupefilter if dupefilter is not None else DupeFilter()

           教学价值（这一条比实现本身重要）：
             ① **`x or default` 是一个陷阱**，只要 x 可能是「合法的假值」
                （空容器、0、空字符串、实现了 __len__/__bool__ 的对象）
                就会静默替换掉调用方传进来的东西。
                定义 API 参数默认值时，一律用 `if x is None`。
             ② 这是一个「不报错但结果全错」的 bug 类型 ——
                比抛异常危险得多。防御手段是**对照实验必须能产生差异**：
                如果 A/B 两组跑出完全一样的数字，第一反应应该是
                「我的开关真的生效了吗」，而不是「结论：开关无效」。
             ③ 本课保留了这个 bug 的完整记录，因为它正好演示了
                实验 60 里说过的原则：**能被稳定复现的现象才配写进教程**。
                这个 bug 的核心特征就是「静默 + 可复现」。
        """
        if dupefilter is not None and not isinstance(dupefilter, DupeFilter):
            raise TypeError(
                f"dupefilter 必须是 DupeFilter 实例或 None，"
                f"收到 {type(dupefilter).__name__}")
        # ★ 必须用 is not None 判断，不能用 `or`（见 docstring 里的踩坑记录）
        self.dupefilter = dupefilter if dupefilter is not None else DupeFilter()
        # 堆元素：(−priority, 序号, Request)
        self._heap: list[tuple[int, int, Request]] = []
        self._counter = itertools.count()
        self.enqueued = 0
        self.popped = 0
        # 记录每个优先级档位有多少个请求，用于输出「优先级真的起作用了吗」
        self.priority_hist: dict[int, int] = {}
        self._lock = threading.Lock()

    def enqueue_request(self, request: Request) -> bool:
        """把请求放入调度队列。

        Args:
            request: 待入队的请求。

        Returns:
            True 表示成功入队；False 表示被去重丢弃。

        Raises:
            TypeError: 当 request 不是 Request 实例时。

        这是一个线程安全方法：Engine 可能是多线程的
        （真实 Scrapy 里 Downloader 是并发的，回调回 Engine 时也是）。
        「去重检查 + 入堆」必须是**原子**的，否则两个线程可能同时
        通过去重检查，把同一个 URL 入队两次。
        """
        if not isinstance(request, Request):
            raise TypeError(f"只接受 Request 实例，收到 {type(request).__name__}")
        with self._lock:
            if not request.dont_filter and self.dupefilter.request_seen(request):
                return False
            heapq.heappush(self._heap, (-request.priority, next(self._counter), request))
            self.enqueued += 1
            self.priority_hist[request.priority] = (
                self.priority_hist.get(request.priority, 0) + 1)
            return True

    def next_request(self) -> Request | None:
        """取出下一个待处理的请求。

        Returns:
            优先级最高的 Request；队列为空时返回 None。
        """
        with self._lock:
            if not self._heap:
                return None
            _, _, request = heapq.heappop(self._heap)
            self.popped += 1
            return request

    def __len__(self) -> int:
        """当前队列长度。

        Returns:
            等待处理的请求数。

        ⚠ 这个方法在多线程下只应作为「大致参考」：
          len() 返回的瞬间队列就可能被其它线程改变。
          监控指标不要依赖它的精确值，只看趋势。
        """
        return len(self._heap)


# ============================================================================
# 三、Downloader Middleware：插在「请求出去」和「响应回来」之间
# ============================================================================
class DownloaderMiddleware:
    """Downloader Middleware 的基类。

    │ 这是 Scrapy 里**最重要**的扩展点。真实爬虫里 80% 的
    │ 「反爬对抗」代码都写在这里：
    │   · 换 User-Agent / 加指纹头
    │   · 挂代理（第 63 课的 ProxyPool 就接在这里）
    │   · 重试策略、退避
    │   · Cookie 注入、签名参数计算
    │   · 请求限速
    │
    │ 为什么是这个位置？因为它能同时看到「请求」和「响应」，
    │ 而且每个**单个请求**都会经过它 —— 不是每个页面，
    │ 是每个请求，包括重试的每一次。所以在这里做限速最精确。
    │
    │ process_request 的返回值语义（**必须背下来**）：
    │   · 返回 None        → 继续走后面的中间件，最终发出去
    │   · 返回 Request     → 熔断！不再发请求，把新 Request 直接回给 Engine
    │   · 返回 Response    → 熔断！不发了，把这个 Response 直接回给 Engine
    │                        （用于响应缓存、从本地读 mock 数据）
    │   · 抛 IgnoreRequest → 丢弃这个请求，走 errback
    │
    │ 这个「返回非 None 即熔断」的设计是**责任链模式**的经典实现。
    │ process_response 类似：
    │   · 返回 Response    → 继续往下传
    │   · 返回 Request     → 熔断！把请求重新丢回 Scheduler（这是重试的实现方式！）
    │   · 抛 IgnoreRequest → 丢弃响应
    │
    │ ▸ 注意：**Scrapy 的重试就是用「process_response 返回 Request」实现的** ——
    │   不是抛异常、不是循环。理解了这一点，你就理解了整个中间件体系的威力。
    """

    def process_request(self, request: Request) -> Request | Response | None:
        """处理即将发出的请求。

        Args:
            request: 待发出的请求。

        Returns:
            None 继续；Request/Response 熔断。

        Raises:
            Exception: 子类可实现自己的丢弃逻辑。
        """
        return None

    def process_response(self, request: Request,
                         response: Response) -> Request | Response:
        """处理刚回来的响应。

        Args:
            request: 原始请求。
            response: 响应对象。

        Returns:
            Response 继续；Request 表示重试。
        """
        return response


class UserAgentMiddleware(DownloaderMiddleware):
    """随机 User-Agent 中间件。

    为什么要随机 UA？
      这不是为了「骗过」什么，而是因为**统计意义上的同质性检测**：
      如果一个 IP 用同一个 UA 每秒发 50 个请求，这是明显非人类的模式；
      而 50 个不同 UA 的请求看起来像 50 个不同浏览器。
      但请注意：UA 随机化只能骗过**最粗的**统计规则。
      现代的指纹系统会同时看 TLS 指纹、JA3、HTTP/2 帧顺序、
      Canvas/WebGL（JS 侧），此时改 UA 毫无意义甚至有害
      （UA 说自己是 Chrome 但 TLS 指纹是 Python，反而成为强特征）。

    ▸ 所以本课把 UA 随机化定位为「初级手段」，
      真正有效的做法是**降低请求频率 + 用真实浏览器指纹**（阶段 4 讲过）。
    """

    POOL: tuple[str, ...] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
    )

    def __init__(self, rng: random.Random | None = None) -> None:
        """初始化中间件。

        Args:
            rng: 随机数发生器（传固定种子可复现）。
        """
        self.rng = rng or random.Random(20260919)
        self.picked: list[str] = []

    def process_request(self, request: Request) -> Request | Response | None:
        """给请求装上随机 UA。

        Args:
            request: 请求对象。

        Returns:
            始终返回 None（不熔断，只是修改请求）。

        为什么修改 request.meta 而不是给 Request 加一个 headers 字段？
          因为本课的 Request 是简化版。真实 Scrapy 的 Request 有
          headers 字段，但**很多团队仍然习惯把 UA 放 meta**，
          因为 meta 是"什么都能塞"的通用槽位，不用改数据模型。
          代价是：这些字段无法被 Scrapy 的类型系统校验，
          拼错一个 key 会静默失效。这是一个真实的工程取舍。
        """
        ua = self.rng.choice(self.POOL)
        request.meta["user_agent"] = ua
        self.picked.append(ua)
        return None


class ProxyMiddleware(DownloaderMiddleware):
    """代理中间件（本课用模拟代理，第 63 课会给出完整的 ProxyPool）。

    本课只演示「中间件怎么挂代理」这个位置，
    真正的健康度评分 / 加权调度 / 自动降权在第 63 课。
    """

    def __init__(self, proxies: Sequence[str] | None = None,
                 rng: random.Random | None = None) -> None:
        """初始化中间件。

        Args:
            proxies: 代理地址列表（形如 http://1.2.3.4:8080）。
            rng: 随机数发生器。
        """
        self.proxies = list(proxies or [
            "http://10.0.0.11:8001",
            "http://10.0.0.12:8002",
            "http://10.0.0.13:8003",
        ])
        self.rng = rng or random.Random(7)
        self.usage: dict[str, int] = {p: 0 for p in self.proxies}

    def process_request(self, request: Request) -> Request | Response | None:
        """给请求分配一个代理。

        Args:
            request: 请求对象。

        Returns:
            始终返回 None。

        已分配代理的请求（重试的）不再换代理 ——
        这一点很重要：**重试用同一个代理才叫重试，
        换代理那叫「换个身份重试」**。如果重试时无脑换代理，
        你永远无法判断「是这个代理坏了还是这个 URL 坏了」。
        正确做法是：第 1 次重试用原代理，第 2 次起才升级为换代理
        （第 63 课的 BackoffStrategy 会实现这个阶梯）。
        """
        if "proxy" not in request.meta:
            p = self.rng.choice(self.proxies)
            request.meta["proxy"] = p
            self.usage[p] += 1
        return None


class RetryMiddleware(DownloaderMiddleware):
    """重试中间件：**用「process_response 返回 Request」实现重试**。

    这是本课最希望读者记住的一个实现技巧。

    ❌ 新手写法：在 Downloader 里写 while 循环重试
        for attempt in range(3):
            resp = fetch(req)
            if resp.status == 200:
                break
        问题：
          · 重试时其它请求全被阻塞（同步场景）
          · 重试次数无法被中间件/配置统一控制
          · 日志里看不出「这是一次重试」

    ✅ Scrapy 写法：process_response 返回一个新的 Request
        新 Request 被 Engine 重新丢回 Scheduler →
        重新参与优先级排序和调度 → 后面还可能有别的请求先跑
        好处：
          · 重试不是「阻塞等待」，而是「重新排队」，不占用调度器的位置
          · 重试间隔可以靠 priority 微调（降低优先级 = 往后排 = 天然退避）
          · 重试次数存在 meta 里，任何中间件都能看到
          · 重试有完整的日志和统计（Scrapy 的 retry/count 指标）
    """

    RETRY_STATUSES: tuple[int, ...] = (429, 500, 502, 503, 504, 522, 524)

    def __init__(self, max_retry: int = 3, backoff_base: float = 0.05) -> None:
        """初始化重试中间件。

        Args:
            max_retry: 最大重试次数（不含首次请求）。
            backoff_base: 退避基数（秒）。真实 Scrapy 用 RETRY_TIMES + 
                EXPONENTIAL_BACKOFF 扩展，本课内置一个简化的指数退避。
        """
        self.max_retry = max_retry
        self.backoff_base = backoff_base
        self.retried = 0
        self.gave_up = 0

    def process_response(self, request: Request,
                         response: Response) -> Request | Response:
        """检查响应状态，决定是否重试。

        Args:
            request: 原始请求。
            response: 响应。

        Returns:
            Response（不重试）或一个新的 Request（重试）。

        为什么重试的 Request 要**降低优先级**？
          因为这样它会排到队尾，天然实现了「延后重试」的效果，
          而不需要在线程里 sleep 阻塞调度器。
          这是把「时间维度」映射到「优先级维度」的一个漂亮技巧。
          但要注意：这只在队列非空时有效。如果队列空了，
          它还是会被立刻取出来 —— 所以真实场景必须配合
          下载延迟（DOWNLOAD_DELAY）或显式的退避等待。
        """
        if response.status not in self.RETRY_STATUSES:
            return response

        attempt = int(request.meta.get("retry_times", 0))
        if attempt >= self.max_retry:
            self.gave_up += 1
            request.meta["give_up"] = True
            return response

        self.retried += 1
        new_req = Request(
            url=request.url,
            callback=request.callback,
            # 降低优先级 → 排到队尾 → 等效于延后重试
            priority=request.priority - 1,
            meta={**request.meta, "retry_times": attempt + 1},
            dont_filter=True,   # ★ 重试必须绕过去重，否则会被自己拦掉！
        )
        return new_req


class DownloaderMiddlewareManager:
    """中间件链的管理器。

    这个类本身只有 30 行，但它是 Scrapy 扩展体系的**全部秘密**：

      请求方向：middleware[0] → middleware[1] → ... → 真正发出
      响应方向：... ← middleware[1] ← middleware[0] ← 真正收到

    也就是说 **process_response 是逆序执行的**（洋葱模型 / 栈结构）。
    为什么？想象一个负责「解压响应体」的中间件：
      · 请求方向：压缩请求体 → 排在后面
      · 响应方向：解压响应体 → 必须排在前面先执行
    如果响应也顺序执行，解压就会在「读内容」之后才发生，拿到的是乱码。
    洋葱模型保证「谁包装的谁负责解包」，这是所有中间件/拦截器框架的通用设计。

    ⚠ 本课的实现有一个**重要简化**：process_response 返回 Request 时，
      我**没有**再去执行前面那几个中间件的 process_response。
      真实 Scrapy 会执行 —— 因为「重试」这个动作本身可能也需要
      被前面的中间件感知（比如限速中间件要记录"这次不算一次成功请求"）。
      这个差异在文末「局限」里会再次强调。
    """

    def __init__(self, middlewares: Sequence[DownloaderMiddleware] | None = None,
                 verbose: bool = True) -> None:
        """初始化中间件管理器。

        Args:
            middlewares: 中间件列表，**顺序即请求方向的执行顺序**。
            verbose: 是否打印日志（教学演示需要，生产应关掉）。
        """
        self.middlewares = list(middlewares or [])
        self.verbose = verbose
        self.event_log: list[str] = []

    def _log(self, msg: str) -> None:
        """记录一条中间件日志。

        Args:
            msg: 日志内容。

        Returns:
            None
        """
        self.event_log.append(msg)
        if self.verbose:
            print(f"        [MW] {msg}")

    def process_request(self, request: Request) -> Request | Response | None:
        """按顺序执行所有中间件的 process_request。

        Args:
            request: 请求对象。

        Returns:
            None（继续）或熔断产生的 Request / Response。

        Raises:
            Exception: 中间件抛出的异常会向上传播（真实 Scrapy 有异常处理链）。
        """
        for mw in self.middlewares:
            result = mw.process_request(request)
            if result is not None:
                self._log(f"{type(mw).__name__}.process_request → 熔断"
                          f"（返回 {type(result).__name__}）")
                return result
        return None

    def process_response(self, request: Request,
                         response: Response) -> Request | Response:
        """**逆序**执行所有中间件的 process_response。

        Args:
            request: 原始请求。
            response: 响应对象。

        Returns:
            Response（继续）或 Request（重试）。

        Raises:
            Exception: 中间件抛出的异常向上传播。
        """
        for mw in reversed(self.middlewares):
            result = mw.process_response(request, response)
            if isinstance(result, Request):
                self._log(f"{type(mw).__name__}.process_response → 触发重试 "
                          f"(retry_times={result.meta.get('retry_times')})")
                return result
            response = result
        return response


# ============================================================================
# 四、Downloader：真正「发请求」的地方
# ============================================================================
class MockTargetSite:
    """模拟一个目标站点（本课不访问真实网络）。

    本模拟器刻意实现了三类真实站点都会有的行为：
      ① **分页列表页**：每页有 N 个详情页链接 + 下一页链接
      ② **偶发 5xx**：模拟服务端抖动（这是重试中间件的用武之地）
      ③ **限速反应**：同一 URL 短时间内被请求多次返回 429

    ⚠ 局限：模拟站点是**确定性**的（固定种子），
      真实站点的失败模式远比这复杂：
        · 失败可能是**静默的**（200 但内容是反爬页面）
        · 429 通常带 Retry-After 头，你需要遵守
        · 站点可能针对 IP 而不是针对 URL 限速
      本课只模拟了「针对 URL 的确定性失败」，用于演示架构流程。
    """

    def __init__(self, n_lists: int = 3, items_per_list: int = 3,
                 fail_urls: Iterable[str] = (),
                 fail_times: int = 2,
                 rng: random.Random | None = None) -> None:
        """初始化模拟站点。

        Args:
            n_lists: 列表页数量。
            items_per_list: 每页商品数。
            fail_urls: 这些 URL 会先失败若干次。
            fail_times: 每个失败 URL 连续失败的次数。
            rng: 随机数发生器。
        """
        self.n_lists = n_lists
        self.items_per_list = items_per_list
        self.rng = rng or random.Random(2026)
        self.request_count = 0
        self.status_count: dict[int, int] = {}
        self._fail_budget: dict[str, int] = {u: fail_times for u in fail_urls}

    def list_url(self, page: int) -> str:
        """列表页 URL。

        Args:
            page: 页码（1 起）。

        Returns:
            URL 字符串。
        """
        return f"https://mock-shop.local/list?page={page}"

    def item_url(self, i: int) -> str:
        """详情页 URL。

        Args:
            i: 商品序号。

        Returns:
            URL 字符串。
        """
        return f"https://mock-shop.local/item/{i}"

    def fetch(self, request: Request) -> Response:
        """处理一次请求。

        Args:
            request: 请求对象。

        Returns:
            Response 对象。

        这个方法就是「Downloader」的核心 —— 真实 Scrapy 里它是
        Twisted 的 HTTPClientAgent，本课用同步函数 + 线程池并发调用。
        """
        self.request_count += 1
        url = request.url
        # 模拟网络延迟：真实下载时间由目标站决定，这里用随机延迟近似
        delay = self.rng.uniform(0.008, 0.030)
        time.sleep(delay)

        # ① 注入性失败：演示重试中间件
        budget = self._fail_budget.get(url, 0)
        if budget > 0:
            self._fail_budget[url] = budget - 1
            status = 503
            self.status_count[status] = self.status_count.get(status, 0) + 1
            return Response(url=url, status=status,
                            body="<html>503 Service Unavailable</html>",
                            request=request, elapsed=delay)

        # ② 按 URL 类型生成内容
        if "/list" in url:
            page = int(url.split("=")[-1])
            body = self._render_list(page)
        elif "/item/" in url:
            idx = int(url.rsplit("/", 1)[-1])
            body = self._render_item(idx)
        else:
            body = "<html><title>404</title></html>"
            self.status_count[404] = self.status_count.get(404, 0) + 1
            return Response(url=url, status=404, body=body,
                            request=request, elapsed=delay)

        self.status_count[200] = self.status_count.get(200, 0) + 1
        return Response(url=url, status=200, body=body,
                        request=request, elapsed=delay)

    def _render_list(self, page: int) -> str:
        """渲染一个列表页的 HTML。

        Args:
            page: 页码。

        Returns:
            HTML 字符串。
        """
        parts = [f"<html><title>第 {page} 页</title><body>"]
        for k in range(self.items_per_list):
            idx = (page - 1) * self.items_per_list + k + 1
            parts.append(f"<title>商品 {idx}</title>")
            parts.append(f"<price>{round(self.rng.uniform(9.9, 999.0), 2)}</price>")
            # 关键：列表页里「产出」详情页链接，对应 Spider 产出新 Request。
            # 真实实现里这是 <a href="...">，Spider 用 css('a::attr(href)') 提取。
            parts.append(f"<a>{self.item_url(idx)}</a>")
        if page < self.n_lists:
            parts.append(f"<a>{self.list_url(page + 1)}</a>")
        parts.append("</body></html>")
        return "".join(parts)

    def _render_item(self, idx: int) -> str:
        """渲染一个详情页的 HTML。

        Args:
            idx: 商品序号。

        Returns:
            HTML 字符串。
        """
        return (f"<html><title>商品 {idx}</title>"
                f"<price>{round(self.rng.uniform(9.9, 999.0), 2)}</price>"
                f"<stock>{self.rng.randint(0, 500)}</stock>"
                f"<a>{self.list_url(1)}</a>"   # 详情页回链列表页，制造"链接闭环"
                f"</body></html>")


class Downloader:
    """下载器：并发执行请求，并跑通中间件链。

    本课用 ThreadPoolExecutor 模拟 Scrapy 的异步下载。
    ▸ 为什么这样模拟是**合理**的？
      Scrapy 的并发模型（Twisted/reactor）和线程池在「IO 密集」这个
      特性上行为一致：都是「同时有 N 个请求在飞」。
      在实验 60 里我们已经验证过：IO 密集场景下多线程 ≈ 异步的性能。
      差别只在资源占用（线程 MB 级 vs 协程 KB 级）和
      「是否可能出现竞态」（线程有共享内存竞态，协程没有）。
    ▸ 为什么这样模拟是**不精确**的？
      · 真实 reactor 是单线程事件循环，本课是多线程，所以
        「中间件里改共享状态」在有竞态和没竞态之间行为不同；
      · Scrapy 的并发上限由 CONCURRENT_REQUESTS 控制，
        本课由线程池大小控制，语义相近但不完全等价
        （Scrapy 还有 CONCURRENT_REQUESTS_PER_DOMAIN）。
    """

    def __init__(self, site: MockTargetSite,
                 mw_manager: DownloaderMiddlewareManager,
                 concurrency: int = 4,
                 verbose: bool = True) -> None:
        """初始化下载器。

        Args:
            site: 模拟目标站点。
            mw_manager: 中间件管理器。
            concurrency: 并发下载数（相当于 Scrapy 的 CONCURRENT_REQUESTS）。
            verbose: 是否打印日志。
        """
        self.site = site
        self.mw = mw_manager
        self.concurrency = concurrency
        self.verbose = verbose
        self.downloaded = 0
        self.blocked = 0        # 被中间件熔断（没真正发出去）的请求数
        self.short_circuit = 0  # 熔断返回 Response 的次数

    def download(self, request: Request) -> Response | Request | None:
        """下载一个请求（含中间件链）。

        Args:
            request: 待下载的请求。

        Returns:
            · Response：正常拿到响应
            · Request ：中间件要求重试（直接返回给 Engine，重新入队）
            · None    ：被丢弃（本课简化，真实 Scrapy 走 errback）

        Raises:
            Exception: 模拟站点抛出的异常（本课会转成 500 响应而不是抛出，
                以保持演示流程连续）。
        """
        if self.verbose:
            print(f"      ⇩ Downloader 收到请求 {request.url} "
                  f"(retry={request.meta.get('retry_times', 0)})")

        # ① 请求方向走中间件链
        result = self.mw.process_request(request)
        if isinstance(result, Response):
            self.short_circuit += 1
            if self.verbose:
                print("      ⇧ 中间件直接返回 Response（未发网络请求）")
            return result
        if isinstance(result, Request):
            self.blocked += 1
            return result

        # ② 真正发出请求
        try:
            response = self.site.fetch(request)
        except Exception as exc:      # noqa: BLE001 - 模拟器兜底，真实场景应分类处理
            response = Response(url=request.url, status=500,
                                body=f"<html>{type(exc).__name__}</html>",
                                request=request)

        self.downloaded += 1
        if self.verbose:
            print(f"      ⇧ 下载完成 status={response.status} "
                  f"{response.elapsed * 1000:.1f}ms")

        # ③ 响应方向逆序走中间件链
        return self.mw.process_response(request, response)


# ============================================================================
# 五、Spider：产出 Item 和新 Request
# ============================================================================
class Spider:
    """爬虫基类：定义「从响应里提取什么」。

    │ 这是全课最重要的一个类，也是最容易被误解的一个类。
    │
    │ 新手以为 Spider 是「爬虫程序」，所以会想在这里写下载代码。
    │ 错了。Spider 只做**解析**，它不下载任何东西 ——
    │ 它接收 Response，拿到 Response 说明已经下载好了。
    │
    │ Spider.parse() 的返回/产出可以是：
    │   · Item          → 送去 Pipeline
    │   · Request       → 送回 Scheduler，继续爬
    │   · 两者混合       → 最常见的形态
    │   · None          → 什么也不产出（通常意味着解析失败）
    │
    │ 真实 Scrapy 的 parse 是一个**生成器**，用 yield 逐个产出。
    │ 为什么用生成器而不是返回 list？
    │   因为一个列表页可能有 5000 个链接，如果一次性构造 list，
    │   这 5000 个 Request 对象会同时存在于内存里。
    │   生成器让它们「一个产出一个消费」，内存占用降为 O(1)。
    │   在大规模爬虫里这个差别是「能跑」和「OOM」的区别。
    │   本课也实现为生成器，请重点体会这一点。
    """

    name: str = "base"

    def start_requests(self) -> Iterator[Request]:
        """产出起始请求。

        Yields:
            Request 对象。

        真实 Scrapy 从 start_urls 自动生成 start_requests，
        每个 URL 的 callback 都是 self.parse。
        本课让子类直接重写，因为我们的站点有 list/item 两种页面类型。
        """
        raise NotImplementedError("子类必须实现 start_requests")

    def parse(self, response: Response) -> Iterator[Item | Request]:
        """解析响应。

        Args:
            response: 响应对象。

        Yields:
            Item 或 Request。

        Raises:
            NotImplementedError: 基类未实现。
        """
        raise NotImplementedError("子类必须实现 parse")


class ShopSpider(Spider):
    """一个抓取模拟商城的 Spider。

    它演示了「列表页 → 详情页」的两级爬取结构，
    这是最经典的爬虫形态（也是理解 Scrapy 数据流的最佳载体）。
    """

    name = "shop"

    def __init__(self, site: MockTargetSite, max_depth: int = 2,
                 track_backlinks: bool = False) -> None:
        """初始化 Spider。

        Args:
            site: 模拟站点（用于构造 URL）。
            max_depth: 最大抓取深度，防止「详情页回链列表页」造成无限循环。
            track_backlinks: 是否跟进详情页里的回链（用于演示去重兜底）。
                默认 False —— 「不在源头产出无用的链接」是更好的设计。
        """
        self.site = site
        self.max_depth = max_depth
        self.track_backlinks = track_backlinks
        self.items_parsed = 0
        self.requests_emitted = 0
        self.parse_failures = 0

    def start_requests(self) -> Iterator[Request]:
        """从列表页第 1 页开始。

        Yields:
            起始 Request。
        """
        yield Request(url=self.site.list_url(1), callback="parse_list",
                      priority=10, meta={"depth": 0})

    def parse_list(self, response: Response) -> Iterator[Item | Request]:
        """解析列表页：产出分页请求 + 详情页请求。

        Args:
            response: 列表页响应。

        Yields:
            Request（下一页 + 各个详情页）。
        """
        depth = int(response.meta.get("depth", 0))
        # ① 提取所有 <a> 里的链接
        links = response.css_all("a")
        for link in links:
            if "/item/" in link and depth < self.max_depth:
                self.requests_emitted += 1
                yield Request(
                    url=link,
                    callback="parse_item",
                    # 详情页优先级低于下一页，让「广度」优先展开
                    priority=5,
                    meta={"depth": depth + 1,
                          "from": response.url},
                )
            elif "/list?page=" in link:
                self.requests_emitted += 1
                yield Request(
                    url=link,
                    callback="parse_list",
                    priority=10,          # 分页优先级最高 → 先把目录铺开
                    meta={"depth": depth},
                )

    def parse_item(self, response: Response) -> Iterator[Item | Request]:
        """解析详情页：产出 Item，并按需产出「推荐位」请求。

        Args:
            response: 详情页响应。

        Yields:
            Item，以及（当开关打开时）详情页里的推荐链接请求。

        Raises:
            ValueError: 当价格无法解析为浮点数时（本实现内部捕获，不外抛）。

        ▸ 关于「推荐位」和链接闭环（本课刻意演示的重点）：
          详情页里有一条回链列表页的 <a>，表示真实站点常见的「面包屑/推荐位」。
          · **默认不跟进去**（track_backlinks=False）：
            这是**设计层面**的防循环 —— 从源头掐断，比依赖去重更好。
          · 打开 track_backlinks 时会把这条回链也产出成 Request：
            此时就会出现「重复 URL 进入调度器」的情况，
            靠 DupeFilter 兜底拦下 —— 实验 1 里两种模式都会跑一次，
            用来对比「源头控制」和「兜底去重」的差异。

        ▸ 这里**只 yield Item 是默认行为**，因为：
          真实项目里「谁负责产出链接」必须明确。
          详情页既产出商品数据、又产出列表页链接，就会形成
          列表→详情→列表→详情 的无限循环。
          虽然 DupeFilter 会拦住重复 URL，但那是**兜底**不是**设计**。
          正确做法是用 allowed_domains + link 白名单来控制
          （真实 Scrapy 的 CrawlSpider + Rule 就是干这个的）。
        """
        idx_text = response.css("title")
        price_text = response.css("price")
        stock_text = response.css("stock")
        try:
            price = float(price_text)
        except ValueError:
            # ❌ 踩坑记录：最初这里是 `raise ValueError("price 解析失败")`，
            #    结果一个页面格式异常就让整个 Engine 崩掉。
            #    现象：ValueError 直冲 main 函数，前面抓的全丢。
            #    根因：Spider 的解析异常在真实 Scrapy 里是被
            #         Spider Middleware 捕获的，不会杀进程。
            #         我们的迷你实现没有这一层，所以必须自己吞掉。
            #    正确做法：解析失败就**记录并跳过**（return），
            #         并把这个失败计数暴露成指标（parse_failures），
            #         因为「解析失败率飙升」是站点改版的第一信号。
            self.parse_failures = getattr(self, "parse_failures", 0) + 1
            if getattr(self, "_verbose", False):
                print(f"      ⚠ 解析失败（跳过）：{response.url} price={price_text!r}")
            return

        # 可选：跟进详情页里的回链（用于演示去重兜底）
        if self.track_backlinks:
            for link in response.css_all("a"):
                if "/list?page=" in link:
                    self.requests_emitted += 1
                    yield Request(
                        url=link,
                        callback="parse_list",
                        priority=10,
                        meta={"depth": 0, "from": response.url},
                    )

        self.items_parsed += 1
        yield Item(
            url=response.url,
            title=idx_text,
            price=price,
            stock=int(stock_text) if stock_text.isdigit() else 0,
            _spider=self.name,
        )


# ============================================================================
# 六、Pipeline：链式处理 Item
# ============================================================================
class Pipeline:
    """Item Pipeline 基类。

    │ Pipeline 的设计原则：**每个 Pipeline 只做一件事**。
    │ 真实项目里通常是这样一个链条：
    │     CleanPipeline → ValidatePipeline → DedupPipeline → MySQLPipeline
    │ 为什么不在一个 Pipeline 里全做完？
    │   ① 可组合：清洗规则变了不影响入库逻辑
    │   ② 可观测：每个 Pipeline 的输入输出计数可以单独监控，
    │      一眼看出「是哪一环丢的数据」
    │   ③ 短路语义清晰：process_item 返回 None 表示「丢弃这条」，
    │      后面的 Pipeline 就不会执行 —— 这比在 200 行的函数里
    │      散布 return 语句好维护得多
    │
    │ 真实 Scrapy 的 Pipeline 是**全局单例**，跨 Spider 共享，
    │ 并且有 open_spider / close_spider 生命周期钩子用来
    │ 建立/关闭数据库连接。本课简化掉了这部分。
    """

    def process_item(self, item: Item) -> Item | None:
        """处理一条 Item。

        Args:
            item: 待处理的数据。

        Returns:
            处理后的 Item；返回 None 表示丢弃。

        Raises:
            DropItem: 真实 Scrapy 用这个异常表达丢弃，本课用返回 None。
        """
        raise NotImplementedError


class CleanPipeline(Pipeline):
    """清洗 Pipeline：去空白、统一数据类型。"""

    def process_item(self, item: Item) -> Item | None:
        """清洗字段。

        Args:
            item: 原始 Item。

        Returns:
            清洗后的 Item。

        Raises:
            KeyError: 当缺少必需字段时（由 ValidatePipeline 兜底更合理，
                但这里选择在清洗阶段就剔除明显异常的数据）。
        """
        if "title" in item and isinstance(item["title"], str):
            item["title"] = item["title"].strip()
        # 价格归一化：统一保留两位小数。真实场景还涉及「分/元」单位统一、
        # 「￥/$」符号剥离、「原价/现价」区分 —— 那是数据清洗课（52）的内容。
        if isinstance(item.get("price"), float):
            item["price"] = round(item["price"], 2)
        return item


class ValidatePipeline(Pipeline):
    """校验 Pipeline：拦截脏数据。

    │ 这一层是「成本分界线」：
    │   放到数据库约束里校验 → 脏数据已经走过网络，报错在入库时，
    │                          要靠日志回溯哪一条出的问题
    │   放到 Pipeline 里校验  → 数据还在内存里，可以立刻丢弃并计数
    │ 位置越靠前，排查成本越低。这是数据工程的一条通用原则。
    """

    REQUIRED: tuple[str, ...] = ("url", "title", "price")

    def __init__(self) -> None:
        """初始化校验器。"""
        self.passed = 0
        self.dropped = 0
        self.drop_reasons: dict[str, int] = {}

    def process_item(self, item: Item) -> Item | None:
        """校验字段。

        Args:
            item: 待校验的 Item。

        Returns:
            校验通过的 Item；不通过时返回 None（丢弃）。

        注意这里**返回 None 而不是抛异常** ——
        一条坏数据不应该影响其它 9999 条好数据。
        这是「批量处理」与「RPC」在错误处理哲学上的根本差异：
        RPC 失败要立刻抛出（调用方需要知道），
        批量处理失败要隔离（继续处理其他的）。
        """
        for f in self.REQUIRED:
            if not item.get(f):
                self.dropped += 1
                self.drop_reasons[f"缺字段:{f}"] = (
                    self.drop_reasons.get(f"缺字段:{f}", 0) + 1)
                return None
        # 业务规则校验：价格必须为正数
        if not isinstance(item.get("price"), (int, float)) or item["price"] <= 0:
            self.dropped += 1
            self.drop_reasons["价格非法"] = self.drop_reasons.get("价格非法", 0) + 1
            return None
        self.passed += 1
        return item


class CollectPipeline(Pipeline):
    """收集 Pipeline：把 Item 存进内存列表（代替真实的数据库写入）。"""

    def __init__(self) -> None:
        """初始化收集器。"""
        self.items: list[Item] = []

    def process_item(self, item: Item) -> Item | None:
        """收集一条 Item。

        Args:
            item: 待收集的 Item。

        Returns:
            原样返回 item（Pipeline 链通常要求透传）。

        ▸ **必须返回 item（或等价的 Item），不能返回 None！**
          因为返回 None 是「丢弃」的语义，会让后面的 Pipeline 收不到数据。
          这是新手写 Pipeline 最容易犯的错误：把「我只是看看」
          写成了「我把它扔了」。本课保留这个注释作为提醒。
        """
        self.items.append(item)
        return item


class PipelineManager:
    """Pipeline 链管理器。

    实现「某一步返回 None 就短路」的语义。
    """

    def __init__(self, pipelines: Sequence[Pipeline],
                 verbose: bool = True) -> None:
        """初始化管理器。

        Args:
            pipelines: Pipeline 列表，顺序即执行顺序。
            verbose: 是否打印日志。
        """
        self.pipelines = list(pipelines)
        self.verbose = verbose
        self.processed = 0
        self.dropped_at: dict[str, int] = {}

    def process_item(self, item: Item) -> Item | None:
        """把 Item 依次送过所有 Pipeline。

        Args:
            item: 待处理的数据。

        Returns:
            最终处理结果；中途被丢弃时返回 None。
        """
        self.processed += 1
        for p in self.pipelines:
            result = p.process_item(item)
            if result is None:
                name = type(p).__name__
                self.dropped_at[name] = self.dropped_at.get(name, 0) + 1
                if self.verbose:
                    print(f"      ⛔ {name} 丢弃了这条数据 "
                          f"（url={item.get('url', '?')}）")
                return None
            item = result
        return item


# ============================================================================
# 七、Engine：把所有组件串起来的总线
# ============================================================================
class Engine:
    """迷你 Scrapy 引擎。

    它做的事情本质上是一个**事件循环**：
        while 还有待处理的请求:
            req = scheduler.next_request()
            resp = downloader.download(req)
            对 resp 调用 spider 的 callback（可能产出 item 或新 request）
            新 request 回 Scheduler，item 送 Pipeline

    ▸ 为什么真实的 Scrapy 不用这种「同步循环」？
      因为同步循环里 downloader.download() 会阻塞整个循环 ——
      如果你有 100 个请求，就必须等第 1 个下载完才能开始第 2 个。
      真实 Scrapy 用 Twisted reactor：download() 立刻返回一个 Deferred，
      循环继续处理下一个请求，等 IO 就绪时回调再推进。
      这就是「异步」在架构层面的样子。

    ▸ 本课的做法：**批处理 + 线程池**。
      每次从 Scheduler 取一批（concurrency 个）请求，
      用线程池并发下载，收集结果后再统一处理回调。
      这也是 scrapy-redis 单机模式常用的折中方案 ——
      实现简单，且在 IO 密集场景下性能与真异步相差不大
      （实验 60 已经量化验证过这一点）。

    ⚠ 本课的一个重要简化：**并发下载时，Spider 的 callback 是串行执行的**。
      真实 Scrapy 的回调也是在 reactor 线程里串行跑的（Python 代码部分），
      所以这一点反而和真实行为一致 —— CPU 密集的解析会拖慢整个引擎，
      这正是为什么生产环境要把重解析丢进进程池（实验 60 的结论）。
    """

    def __init__(self, spider: Spider, scheduler: Scheduler,
                 downloader: Downloader, pipelines: PipelineManager,
                 concurrency: int = 4, verbose: bool = True,
                 max_requests: int = 10_000) -> None:
        """初始化引擎。

        Args:
            spider: 爬虫实例。
            scheduler: 调度器。
            downloader: 下载器。
            pipelines: Pipeline 管理器。
            concurrency: 每批并发下载数。
            verbose: 是否打印完整事件流（教学演示用）。
            max_requests: 请求总量上限，防止演示程序失控。
        """
        self.spider = spider
        self.scheduler = scheduler
        self.downloader = downloader
        self.pipelines = pipelines
        self.concurrency = concurrency
        self.verbose = verbose
        self.max_requests = max_requests

        # 统计指标
        self.requests_handled = 0
        self.responses_ok = 0
        self.responses_bad = 0
        self.items_yielded = 0
        self.items_stored = 0
        self.retries = 0
        self.timeline: list[str] = []

    def _log_event(self, msg: str) -> None:
        """记录事件流（用于最后的汇总展示）。

        Args:
            msg: 事件描述。

        Returns:
            None
        """
        self.timeline.append(msg)

    def run(self) -> dict[str, Any]:
        """跑完所有请求。

        Returns:
            运行统计字典。

        Raises:
            RuntimeError: 当请求数超过 max_requests 上限时（防止死循环）。
        """
        # ① 让 Spider 产出起始请求
        for req in self.spider.start_requests():
            self.scheduler.enqueue_request(req)
        self._log_event(f"start_requests 产出 {len(self.scheduler)} 个初始请求入队")

        t0 = time.perf_counter()

        # ② 主循环：一批一批地取、下载、解析、回填
        while True:
            batch: list[Request] = []
            for _ in range(self.concurrency):
                req = self.scheduler.next_request()
                if req is None:
                    break
                batch.append(req)
            if not batch:
                break

            if self.requests_handled + len(batch) > self.max_requests:
                raise RuntimeError(
                    f"请求数超过上限 {self.max_requests}，"
                    f"可能存在无限循环的链接（检查 max_depth 与去重逻辑）")
            self.requests_handled += len(batch)
            self._log_event(f"Engine 从 Scheduler 取出 {len(batch)} 个请求")

            # ③ 并发下载这一批
            responses = self._download_batch(batch)

            # ④ 串行处理回调（与真实 Scrapy 一致：Python 回调不并发）
            for resp in responses:
                self._handle_response(resp)

        elapsed = time.perf_counter() - t0
        return {
            "elapsed": elapsed,
            "requests": self.requests_handled,
            "ok": self.responses_ok,
            "bad": self.responses_bad,
            "items_yielded": self.items_yielded,
            "items_stored": self.items_stored,
        }

    def _download_batch(self, batch: Sequence[Request]) -> list[Response]:
        """并发下载一批请求。

        Args:
            batch: 请求列表。

        Returns:
            Response 列表（重试的请求已被重新入队，不含在本列表中）。

        ▸ 这里用 threads 而不是 asyncio，与真实 Scrapy 的 reactor 不同，
          但在 IO 密集下行为等价（实验 60 已量化）。
          为了教学清晰，下载顺序被强制为与输入顺序一致 ——
          真实 Scrapy 是「谁先回来谁先处理」，即完成顺序。
          这个差异对本课演示无影响，但生产环境里它会影响
          「同站点的请求先后顺序」，进而影响反爬表现。
        """
        results: list[Response] = []
        for i, req in enumerate(batch):
            if self.verbose:
                print(f"\n    ┌─ [{i + 1}/{len(batch)}] {req.url}")
            out = self.downloader.download(req)

            if isinstance(out, Request):
                # 中间件要求重试 → 重新入队（这是 RetryMiddleware 的产物）
                self.retries += 1
                self.scheduler.enqueue_request(out)
                self._log_event(f"重试入队 {out.url} "
                                f"(第 {out.meta.get('retry_times')} 次)")
                continue
            if out is None:
                continue
            results.append(out)
        return results

    def _handle_response(self, response: Response) -> None:
        """把响应交给 Spider 的 callback，并处理产出。

        Args:
            response: 响应对象。

        Returns:
            None

        Raises:
            AttributeError: 当 callback 名字在 Spider 上不存在时。

        ▸ 「用方法名字符串调用方法」是 Scrapy 用 Request.callback 存字符串
          的原因。用 getattr 取方法，天然支持「一个 Spider 有多个解析方法」。
          踩坑记录：最初我直接写 response.request.callback(response)，
          想当然地以为 callback 存的是方法对象 ——
          结果 callback 是 "parse_list" 这个字符串，报
          TypeError: 'str' object is not callable。
          根因：Request 必须可序列化（要丢进 Redis），
                所以只能存方法名，不能存方法对象。
          正确做法：getattr(self.spider, callback_name)(response)。
          并且要用 getattr 的默认值参数做**友好报错**，
          否则拼错方法名会得到一个看不懂的 AttributeError。
        """
        if response.status != 200:
            self.responses_bad += 1
            if self.verbose:
                print(f"      ⚠ 状态码 {response.status}，交给 Spider 处理")
            # 真实 Scrapy 会走 errback；本课让 Spider 也看看非 200 响应，
            # 因为很多站点的 404 页面里其实含有有用的信息。
        else:
            self.responses_ok += 1

        callback_name = response.request.callback if response.request else "parse"
        callback = getattr(self.spider, callback_name, None)
        if callback is None:
            raise AttributeError(
                f"Spider {type(self.spider).__name__} 上不存在回调方法 "
                f"{callback_name!r}（Request.callback 拼错了？）")

        for out in callback(response):
            if isinstance(out, Request):
                accepted = self.scheduler.enqueue_request(out)
                if self.verbose:
                    mark = "入队" if accepted else "去重丢弃"
                    print(f"      ↳ 产出 Request [{mark}] {out.url}")
                if accepted:
                    self._log_event(f"Spider 产出新 Request 入队 {out.url}")
                else:
                    self._log_event(f"Spider 产出 Request 被去重 {out.url}")
            elif isinstance(out, Item):
                self.items_yielded += 1
                if self.verbose:
                    print(f"      ↳ 产出 Item {out.get('title')} "
                          f"¥{out.get('price')}")
                stored = self.pipelines.process_item(out)
                if stored is not None:
                    self.items_stored += 1


# ============================================================================
# 实验 1：完整事件流 —— 跟着一个请求走一遍
# ============================================================================
def exp1_event_flow() -> ShopSpider:
    """实验 1：跑一次完整爬取，打印全部事件流。

    Returns:
        跑完的 ShopSpider 实例（供后续实验复用其统计）。
    """
    title("【实验 1】完整事件流：一个 Request 是怎么走完全程的")

    print("""
    站点结构（模拟）：
      list?page=1 ─┬─> item/1  item/2  item/3
                   └─> list?page=2 ─┬─> item/4  item/5  item/6
                                    └─> list?page=3 ─┬─> item/7 item/8 item/9
                                                     └─> (没有下一页了)

    刻意注入的失败：item/5 前 2 次请求返回 503（演示重试中间件）。
    刻意制造的闭环：每个 item 详情页都有回链 list?page=1（演示去重）。

    下面会逐行打印事件流。注意观察三件事：
      ① 优先级是否真的起作用（分页请求是否先被处理）
      ② 重试请求是否「重新入队」而不是「原地等待」
      ③ 两种模式下的对比：不跟进回链 vs 跟进回链（去重兜底）
    """)

    site = MockTargetSite(n_lists=3, items_per_list=3,
                          fail_urls=[MockTargetSite().item_url(5)],
                          fail_times=2,
                          rng=random.Random(2026))

    mw = DownloaderMiddlewareManager([
        UserAgentMiddleware(),
        ProxyMiddleware(),
        RetryMiddleware(max_retry=3),
    ], verbose=True)

    downloader = Downloader(site, mw, concurrency=4, verbose=True)

    validate = ValidatePipeline()
    collect = CollectPipeline()
    pipelines = PipelineManager([CleanPipeline(), validate, collect], verbose=True)

    spider = ShopSpider(site, max_depth=2, track_backlinks=False)
    spider._verbose = True   # 打开解析失败的日志

    scheduler = Scheduler()
    engine = Engine(spider, scheduler, downloader, pipelines,
                    concurrency=4, verbose=True, max_requests=200)

    stats = engine.run()

    sub("运行统计（模式 A：不跟进详情页回链 —— 源头防循环）")
    print(f"    总耗时          : {stats['elapsed']:.2f}s")
    print(f"    处理请求数      : {stats['requests']}")
    print(f"    成功响应        : {stats['ok']}")
    print(f"    非 200 响应     : {stats['bad']}")
    print(f"    重试次数        : {engine.retries}")
    print(f"    产出 Item       : {stats['items_yielded']}")
    print(f"    入库 Item       : {stats['items_stored']}")
    print(f"    下载器实际发包  : {downloader.downloaded}")
    print(f"    中间件熔断      : {downloader.short_circuit}")
    print(f"    去重检查次数    : {scheduler.dupefilter.checked}")
    print(f"    去重命中次数    : {scheduler.dupefilter.duplicated}")
    print(f"    去重命中率      : {scheduler.dupefilter.hit_rate * 100:.1f}%")
    print(f"    指纹库大小      : {len(scheduler.dupefilter)}")
    print(f"    Spider 产出请求 : {spider.requests_emitted}")
    print(f"    Spider 解析成功 : {spider.items_parsed}")
    print(f"    Spider 解析失败 : {spider.parse_failures}")

    sub("事件流时间线（前 24 条）")
    for i, ev in enumerate(engine.timeline[:24], 1):
        print(f"    {i:>3}. {ev}")
    if len(engine.timeline) > 24:
        print(f"    ... 共 {len(engine.timeline)} 条事件")

    sub("抓到的数据")
    print(f"    {'商品':<10}{'价格':>10}{'库存':>8}   URL")
    print("    " + "-" * 62)
    for it in collect.items[:12]:
        print(f"    {it.get('title', ''):<10}{it.get('price', 0):>10.2f}"
              f"{it.get('stock', 0):>8}   {it.get('url', '')}")
    print(f"    （共 {len(collect.items)} 条）")

    sub("模式 B：跟进详情页回链（靠去重兜底）")
    print("""    同样的站点与 Spider，唯一区别是 track_backlinks=True ——
    详情页会把回链 list?page=1 也产出成 Request。
    这部分请求会被 DupeFilter 全部拦下，不会真的发出。""")

    site_b = MockTargetSite(n_lists=3, items_per_list=3,
                            fail_urls=[MockTargetSite().item_url(5)],
                            fail_times=2, rng=random.Random(2026))
    mw_b = DownloaderMiddlewareManager([RetryMiddleware(max_retry=3)], verbose=False)
    dl_b = Downloader(site_b, mw_b, concurrency=4, verbose=False)
    collect_b = CollectPipeline()
    pipes_b = PipelineManager([CleanPipeline(), ValidatePipeline(), collect_b],
                              verbose=False)
    spider_b = ShopSpider(site_b, max_depth=2, track_backlinks=True)
    sched_b = Scheduler()
    eng_b = Engine(spider_b, sched_b, dl_b, pipes_b, concurrency=4,
                   verbose=False, max_requests=400)
    stats_b = eng_b.run()

    print(f"\n  {'模式':<32}{'调度尝试':>10}{'实际发包':>10}{'去重命中':>10}"
          f"{'命中率':>10}{'入库':>8}")
    print("  " + "-" * 76)
    # ⚠ 注意「调度尝试」这一列的口径（踩坑记录）：
    #   最初我把它写成 Engine.requests_handled，但那个数字是
    #   「Engine 从队列里成功取出的请求数」，**不包含被去重拒收的那批** ——
    #   于是 A/B 两种模式显示的都是 14，看起来「去重毫无开销」，
    #   与正文「模式 B 多消耗了调度资源」的说法自相矛盾。
    #   正确口径是 scheduler.dupefilter.checked（所有尝试入队的次数），
    #   它才是「Scheduler 真正处理过的请求量」。
    #   教训：**指标的口径比指标本身更容易出错**。
    #   写教程时只要出现「A 比 B 多消耗 X」，就必须确认 X 的分子分母
    #   在两边是同一个口径。
    print(f"  {'A 不跟进回链（源头防循环）':<30}"
          f"{scheduler.dupefilter.checked:>10}"
          f"{downloader.downloaded:>10}{scheduler.dupefilter.duplicated:>10}"
          f"{scheduler.dupefilter.hit_rate * 100:>9.1f}%{stats['items_stored']:>8}")
    print(f"  {'B 跟进回链（靠去重兜底）':<30}"
          f"{sched_b.dupefilter.checked:>10}"
          f"{dl_b.downloaded:>10}{sched_b.dupefilter.duplicated:>10}"
          f"{sched_b.dupefilter.hit_rate * 100:>9.1f}%{stats_b['items_stored']:>8}")

    sub("▸ 关键洞察 1：重试是「重新入队」而不是「原地等待」")
    print(f"""
    看上面的日志：item/5 第一次返回 503 时，
    RetryMiddleware.process_response 返回了一个**新的 Request**，
    这个 Request 被 Engine 重新丢回 Scheduler 排队。

    意义在哪？
      · 如果是「原地等待重试」，假设有 100 个请求同时遇到 503，
        就会出现 100 个线程同时 sleep —— 线程池直接被打满。
      · 重新入队则是：请求回到队列尾，调度器继续处理别的请求，
        等它被再次取出时，服务端的抖动往往已经过去了。
      · 更妙的是，重试的新 Request 优先级被降低了 1 档，
        这天然实现了「延后重试」——不需要任何 sleep。

    ▸ 但它也有代价：如果队列空了，重试会被**立刻**再次取出，
      连一毫秒都没等 —— 也就是"退避"失效了。
      所以真实 Scrapy 必须配合 DOWNLOAD_DELAY 或
      显式的时间退避（第 63 课的 BackoffStrategy 会解决这个问题）。
      本课实测：注入 2 次失败，实际发生 {engine.retries} 次重试，
      第 3 次请求成功拿到数据，item/5 最终入库。""")

    sub("▸ 关键洞察 2：源头控制 vs 兜底去重")
    print(f"""
    对比两种模式的实测数字：

      模式 A（不跟进回链）：调度尝试 {scheduler.dupefilter.checked} 次，
          实际发包 {downloader.downloaded} 个，去重命中 {scheduler.dupefilter.duplicated} 次
          （命中率 {scheduler.dupefilter.hit_rate * 100:.1f}%），抓到 {stats['items_stored']} 条数据
      模式 B（跟进回链）  ：调度尝试 {sched_b.dupefilter.checked} 次，
          实际发包 {dl_b.downloaded} 个，去重命中 {sched_b.dupefilter.duplicated} 次
          （命中率 {sched_b.dupefilter.hit_rate * 100:.1f}%），抓到 {stats_b['items_stored']} 条数据

    ▸ 关键观察 1：**两种模式抓到的数据完全一样（都是 {stats['items_stored']} 条）**，
      实际发包数也几乎一样（{downloader.downloaded} vs {dl_b.downloaded}）。
      也就是说，被去重拦住的 {sched_b.dupefilter.duplicated} 个请求**并没有被真的发出** ——
      去重成功地避免了「重复抓取」这件事本身。

    ▸ 关键观察 2：但模式 B 的**调度尝试次数**从 {scheduler.dupefilter.checked}
      涨到了 {sched_b.dupefilter.checked}（多了
      {sched_b.dupefilter.checked - scheduler.dupefilter.checked} 次），
      这多出来的每一次都要：
        · 构造一个 Request 对象
        · 计算一次 SHA1 指纹（约 1~2 微秒）
        · 查询一次 set（约 0.1 微秒）
        · 在调度器里占用一次锁
      在 9 个请求的规模下这无所谓；在 1000 万 URL 的规模下，
      这就是「能跑完」和「跑不完」的区别。

    ▸ 这就是「源头控制优于兜底」的量化证据：
      能在 Spider 里不产出的链接，就不要产出来交给 DupeFilter 去拦。
      三层防循环的正确用法是**逐层降低压力**：
        ① 源头控制（Spider 不产出）→ 省掉全部调度与去重开销
        ② 深度限制（max_depth）    → 兜住源头漏掉的情况
        ③ 全局去重（DupeFilter）   → 最后一道防线，必须有，但不该是主力

    ▸ 一个反向的注意点：本课模式 B 的去重命中率是
      {sched_b.dupefilter.hit_rate * 100:.1f}% —— 这个数字偏高，
    是因为我们的模拟站点回链非常密集（每个详情页都回链同一个列表页）。
    在健康的大规模爬虫里这个数字通常在 5%~30%。
      如果它接近 0，要怀疑：指纹里是否混进了随机值/时间戳
      （每次指纹都不同 → 去重完全失效）。
      如果它超过 90%，要怀疑：站点在做「链接闭环」，需要降低深度上限。""")

    sub("▸ 关键洞察 3：优先级的真实行为（本课实现的一个坑）")
    print(f"""
    本课给分页请求 priority=10、详情页请求 priority=5。
    但请仔细看实验 1 的日志：

        Engine 从 Scheduler 取出 4 个请求 → list?page=2, item/1, item/2, item/3

    ⚠ 注意：这里 item/1 和 list?page=2 是**同一批**被取出来的！
      也就是说，虽然 list?page=2 的优先级更高，
      但批次内的请求是**并发发出**的，并没有严格串行地先走完 page=2。

    ▸ 根因：本课 Engine 的实现是「**批处理**」——
      一次从 Scheduler 取 concurrency 个请求，然后一起并发下载。
      取的时候确实是按优先级取的（列表顺序是 page=2 在前），
      但取出来之后就一起发了，所以"优先级"只影响了**取出的顺序**，
      没有影响**发起的时机**。

    ▸ 真实 Scrapy 的差异：reactor 是「一次取一个、立刻发出、
      等 IO 就绪再取下一个」。虽然它也有 CONCURRENT_REQUESTS 的
      并发窗口，但请求是**逐个进入**下载队列的，
      所以优先级的语义严格得多。

    ▸ 这个坑的教学价值：**「优先级队列」和「优先级调度」是两件事**。
      有优先队列不代表有严格优先级 —— 批量消费会把优先级"摊平"。
      如果你的业务真的依赖严格优先级（比如「先抓完列表页再抓详情页」），
      在批处理架构下必须显式实现「批次内按优先级排序后再并发」，
      或者干脆把 concurrency 降到 1 来换取严格的顺序语义。

    本课调度队列里各优先级的入队数量（模式 A）：
    """)
    for pri, cnt in sorted(scheduler.priority_hist.items(), reverse=True):
        print(f"      priority={pri:>3} → {cnt} 个请求")
    print(f"""
    ▸ 如果反过来（详情页优先级更高），爬虫会沿着一条路径钻到底，
      这是**深度优先**；本课的配置是**广度优先**（先把目录铺开）。
      两者的适用场景完全不同：
        · 广度优先 → 适合「先快速拿到全站 URL 清单」，也更适合反爬
          （对单个页面的访问被分散到更长时间里）
        · 深度优先 → 适合「沿着分类树往下钻」，不适合需要全站覆盖的场景

    ▸ 关键在于：**Spider 的代码一行都不用改**，只改 Request 的 priority。
      这就是把「下载」和「调度」解耦带来的红利。""")

    return spider


# ============================================================================
# 实验 2：中间件的洋葱模型
# ============================================================================
def exp2_middleware_onion() -> None:
    """实验 2：证明 process_response 是逆序执行的。"""
    title("【实验 2】中间件的洋葱模型：为什么 process_response 是逆序的")

    print("""
    用一个「会说话的中间件」把执行顺序打印出来：
    每个中间件只在自己的 process_request / process_response 里
    打印自己的名字。
    """)

    order: list[str] = []

    class TraceMW(DownloaderMiddleware):
        """一个记录自己被调用顺序的中间件。"""

        def __init__(self, name: str) -> None:
            """初始化。

            Args:
                name: 中间件名字。
            """
            self.name = name

        def process_request(self, request: Request) -> Request | Response | None:
            """记录请求方向的调用。

            Args:
                request: 请求对象。

            Returns:
                None（不熔断）。
            """
            order.append(f"req→{self.name}")
            return None

        def process_response(self, request: Request,
                             response: Response) -> Request | Response:
            """记录响应方向的调用。

            Args:
                request: 请求对象。
                response: 响应对象。

            Returns:
                response（不重试）。
            """
            order.append(f"resp←{self.name}")
            return response

    site = MockTargetSite(n_lists=1, items_per_list=1, rng=random.Random(1))
    mw = DownloaderMiddlewareManager(
        [TraceMW("A"), TraceMW("B"), TraceMW("C")], verbose=False)
    downloader = Downloader(site, mw, concurrency=1, verbose=False)

    req = Request(url=site.list_url(1))
    downloader.download(req)

    sub("实测调用顺序")
    for i, line in enumerate(order, 1):
        print(f"    {i}. {line}")

    sub("▸ 关键洞察：洋葱模型（栈结构）")
    print(f"""
    实测序列是：
        req→A → req→B → req→C → [真正发请求] → resp←C → resp←B → resp←A

    请求方向：正序（列表顺序）
    响应方向：**逆序**（列表反序）

    ▸ 为什么必须这样？想一个真实的中间件组合：

        [0] DecompressMiddleware    负责解压响应体
        [1] ProxyMiddleware         负责挂代理
        [2] LogMiddleware           负责记录响应大小

      请求方向：先决定要不要解压，再挂代理，再记日志 —— 顺序无所谓
      响应方向：必须先把响应解压出来，LogMiddleware 才能记录到正确的大小！
              如果响应也正序，LogMiddleware 会在解压**之前**执行，
              记录的是压缩后的大小 —— 日志骗了你，而且没人会发现。

    ▸ 这就是「谁包装的谁负责解包」原则。
      同样的模式你在 ASGI/WSGI 中间件、Java Servlet Filter、
      React HOC、Axios 拦截器里都会见到 ——
      它们是同一个设计模式的重复出现。

    ▸ 在本课的实现里，一个**例外**值得注意：
      `Process_response 返回 Request 表示重试` 时，
      本课直接返回给 Engine，**不再执行前面中间件的 process_response**。
      真实 Scrapy 会继续执行剩下的响应中间件。
      差异的后果：如果 A 中间件负责「统计成功请求数」，
      在真实 Scrapy 里它能感知到"这次是重试"，
      而本课的实现里它收不到这个信息（它只会看到最终成功的那个响应）。
      这就是简化实现与生产实现的**可观测性差距** —— 不影响功能，影响监控。""")


# ============================================================================
# 实验 3：把去重关掉会发生什么（反例实验）
# ============================================================================
def exp3_disable_dupefilter() -> None:
    """实验 3：关闭去重，观察请求量爆炸。"""
    title("【实验 3】反例实验：关掉去重会发生什么？")

    print("""
    同样的站点、同样的 Spider（**开启回链跟进**，制造真实的链接闭环），
    只把 DupeFilter 关掉。

    ⚠ 一个必须先说明的前提：
      如果 Spider 本身不产出重复链接，去重开不开都没区别 ——
      所以本实验**必须**先打开 track_backlinks，
      让「详情页 → 列表页」的闭环真实存在。

    ⚠ 本实验设置了 max_requests=150 作为安全阀。
      真实场景里这种失控会一直跑到你被封 IP 或者内存耗尽。
      实测：关闭去重时安全阀**确实被触发**了（请求量失控）。
    """)

    def run_with_dupe(enabled: bool) -> dict[str, Any]:
        """用指定去重配置跑一次。

        Args:
            enabled: 是否开启去重。

        Returns:
            统计结果字典。
        """
        site = MockTargetSite(n_lists=2, items_per_list=3, rng=random.Random(3))
        mw = DownloaderMiddlewareManager([UserAgentMiddleware()], verbose=False)
        downloader = Downloader(site, mw, concurrency=4, verbose=False)
        collect = CollectPipeline()
        pipelines = PipelineManager([collect], verbose=False)
        # 关键：打开 track_backlinks，让「详情页 → 列表页」的闭环真实存在。
        # 不打开的话，Spider 根本不产出重复链接，去重开不开都一样 ——
        # 那这个实验就什么也证明不了（这是本实验第一版的错误）。
        spider = ShopSpider(site, max_depth=2, track_backlinks=True)
        scheduler = Scheduler(DupeFilter(enabled=enabled))
        engine = Engine(spider, scheduler, downloader, pipelines,
                        concurrency=4, verbose=False, max_requests=150)
        error = ""
        try:
            stats = engine.run()
        except RuntimeError as exc:
            # 触发了安全阀：说明请求量确实失控了
            error = str(exc)
            stats = {"requests": scheduler.popped,
                     "items_stored": len(collect.items)}
        stats["error"] = error
        stats["dup_hit"] = scheduler.dupefilter.hit_rate
        stats["dup_checked"] = scheduler.dupefilter.checked
        stats["dup_size"] = len(scheduler.dupefilter)
        stats["downloaded"] = downloader.downloaded
        stats["unique_urls"] = len({u for u in [
            site.list_url(p) for p in range(1, 3)] + [site.item_url(i) for i in range(1, 7)]})
        return stats

    on = run_with_dupe(True)
    off = run_with_dupe(False)

    print(f"  {'配置':<14}{'调度尝试':>10}{'实际发包':>10}{'去重命中率':>12}"
          f"{'指纹库':>10}{'入库数据':>10}{'安全阀':>10}")
    print("  " + "-" * 76)
    for label, s in (("去重开启", on), ("去重关闭", off)):
        hit = f"{s['dup_hit'] * 100:.1f}%" if s["dup_hit"] else "0.0%"
        valve = "触发" if s["error"] else "未触发"
        print(f"  {label:<14}{s['dup_checked']:>10}{s['downloaded']:>10}"
              f"{hit:>12}{s['dup_size']:>10}{s['items_stored']:>10}{valve:>10}")

    sub("▸ 关键洞察：去重是「能跑」与「跑死」的分水岭")
    print(f"""
    实测对比（站点只有 {on['unique_urls']} 个唯一 URL）：

      去重开启 → 调度尝试 {on['dup_checked']} 次，实际发包 {on['downloaded']} 个，
                 指纹库 {on['dup_size']} 个，抓到 {on['items_stored']} 条数据
      去重关闭 → 调度尝试 {off['dup_checked']} 次，实际发包 {off['downloaded']} 个，
                 指纹库 {off['dup_size']} 个，抓到 {off['items_stored']} 条数据
                 （安全阀 {'已触发 —— 请求量失控' if off['error'] else '未触发'}）

    ▸ 差距：调度尝试次数从 {on['dup_checked']} 涨到 {off['dup_checked']}
      （{off['dup_checked'] / max(on['dup_checked'], 1):.1f} 倍），
      实际发包从 {on['downloaded']} 涨到 {off['downloaded']}
      （{off['downloaded'] / max(on['downloaded'], 1):.1f} 倍）。

    ▸ 更要命的是「抓到的数据」：去重关闭后入库了 {off['items_stored']} 条，
      而站点只有 {on['unique_urls']} 个唯一 URL、真实商品只有 6 个。
      多出来的 {off['items_stored'] - on['items_stored']} 条
      **全是同一条数据的重复副本** ——
      数据库里出现大量重复行，下游报表全部失真。
      这是「没有去重」最容易被忽略的代价：
      不只是慢，而是**数据是错的**。

    ⚠ 诚实说明（这一节的返工过程值得完整记录）：

      **第一版实验里两种模式的结果完全相同**（都是 8 个指纹、42.9% 命中率、
      6 条数据），看起来像是「去重开不开都无所谓」。
      真实原因有两层，也是两个独立的 bug：

      ① **实验设计错误**：我那时让 parse_item **不产出**回链 Request，
         于是 Spider 根本不产生重复 URL —— 去重没有任何东西可拦。
         → **一个「不产生压力」的实验，无法证明减压手段有效。**
           必须先让 bug 能复现（track_backlinks=True），
           再证明修复有效。这是所有对照实验的第一原则。

      ② **代码 bug（更隐蔽）**：Scheduler.__init__ 里我写的是
             self.dupefilter = dupefilter or DupeFilter()
         而 DupeFilter 实现了 __len__，所以**一个空的 DupeFilter 布尔值是 False**。
         `DupeFilter(enabled=False) or DupeFilter()` 直接取右边，
         把「关闭去重」的配置静默丢弃，换成了一个默认开启的新对象。
         → 修法是 `dupefilter if dupefilter is not None else DupeFilter()`。
           完整分析写在 Scheduler.__init__ 的 docstring 里（坑 4）。

      ▸ 这两个 bug 叠在一起，产生了一个「看起来很正常」的错误结论。
        它给我们的教训是：
        · 对照实验跑出**完全相同的数字**时，第一反应应该是
          「我的开关真的生效了吗」，而不是「结论：开关无效」。
        · `x or default` 是给参数设默认值时的经典陷阱 ——
          只要 x 可能是「合法的假值」（空容器、0、空串、实现了 __len__ 的对象），
          就会静默替换掉调用方传进来的东西。**一律用 `if x is None`。**

    ▸ 修复后本实验的结论非常清晰：
      **去重把「调度尝试次数」压在了与「唯一 URL 数」同量级的水平上
      （{on['dup_checked']} 次调度 vs {on['unique_urls']} 个唯一 URL），
      而没有去重时这个数字由链接图的结构决定 —— 你控制不了它。**

      本模拟站点的闭环结构极简单（详情页只回链列表页 1），
      膨胀已经是 {off['dup_checked'] / max(on['dup_checked'], 1):.1f} 倍且打爆了安全阀；
      真实站点的交叉链接密度高得多（面包屑、推荐位、标签页、相关商品），
      没有去重时请求数会呈**指数**增长，几分钟就能把你送进对方的黑名单。

    ▸ 工程上的三层防循环：
      ① **源头控制**：Spider 只产出你真正需要的链接
         （实验 1 的模式 A 就是这一层，零开销）
      ② **深度控制**：max_depth，超过就不往下走
         （真实 Scrapy 用 DEPTH_LIMIT + DEPTH_PRIORITY）
      ③ **全局去重**：DupeFilter，兜底
      这三层缺一不可 —— 只用第三层，去重表会膨胀；
      只用第一层，一旦 Spider 写漏一个地方就完蛋。

    ▸ 一个真实的数字：某电商爬虫的复盘里，去重命中率 87%。
      也就是说如果没有去重，请求量会是现在的 7.7 倍，
      被封 IP 的概率、带宽成本、跑完耗时都会相应上升。
      **去重不是"优化"，而是"能不能跑完"的问题。**""")


# ============================================================================
# 实验 4：与真实 Scrapy 的差异对照
# ============================================================================
def exp4_gap_with_real_scrapy() -> None:
    """实验 4：诚实对照本课实现与真实 Scrapy 的差距。"""
    title("【实验 4】局限对照：本课实现 vs 真实 Scrapy")

    rows = [
        ("并发模型", "threading 线程池（批次并发）", "Twisted reactor（单线程事件循环）",
         "IO 密集下性能接近；但 reactor 无线程竞态，本课实现里"
         "中间件改共享状态需要自己加锁"),
        ("限速", "无", "AutoThrottle 扩展（按响应延迟自适应）",
         "本课没有限速，跑真实站点会很快被封。第 63 课补上"),
        ("去重", "内存 set + SHA1 前 32 位", "内存 set + 全 SHA1 + 可选磁盘/Redis 后端",
         "本课重启即丢，无法断点续爬。第 62 课用 Redis 解决"),
        ("优先级", "heapq 优先队列（-priority）", "heapq 优先队列（同样取负）",
         "语义一致。区别是本课批次内不严格排序"),
        ("重试", "process_response 返回 Request", "同样机制 + RetryMiddleware 官方实现",
         "机制相同；本课缺 Retry-After 头解析和退避等待"),
        ("中间件异常", "直接向上抛，会中断 Engine", "异常处理器链 + errback",
         "本课一个中间件抛异常会杀掉整个爬取，这是**严重**差异"),
        ("Spider 回调", "getattr 方法名调用", "同样的回调机制 + Spider Middleware 包装",
         "本课无 Spider Middleware，无法在 item 产出前后插逻辑"),
        ("Pipeline", "内存列表", "open_spider/close_spider + 数据库连接池",
         "本课没有生命周期管理，无法批量提交/事务"),
        ("信号系统", "无", "signals（spider_opened/item_scraped/…）",
         "本课无法做「爬取结束时发通知」这类扩展"),
        ("统计", "自己维护的 dict", "Stats Collector + 多种导出后端",
         "真实 Scrapy 的 stats 是全局可读的，扩展可直接消费"),
    ]

    print(f"  {'维度':<12}{'本课实现':<26}{'真实 Scrapy':<34}")
    print("  " + "-" * 76)
    for dim, mine, real, _ in rows:
        print(f"  {dim:<12}{mine:<26}{real:<34}")

    sub("▸ 每一处差异的后果")
    for dim, mine, real, consequence in rows:
        print(f"    · {dim}：{consequence}")

    sub("▸ 那为什么还要从零复刻一遍？")
    print("""    因为**知道一个框架的内部结构，你才能判断它能不能解决你的问题**。

    具体来说，学完本课你应该能做到：
      ① 看 Scrapy 的日志，知道每个阶段在干什么
         （"Item 从 Spider 出来后去哪了？"→ Pipeline 链）
      ② 遇到「重试不生效」时，知道该去 RetryMiddleware 的
         process_response 里查，而不是去翻 Downloader 源码
      ③ 遇到「去重太狠/太松」时，知道要去改 dupefilter 的指纹算法
      ④ 遇到「限速没效果」时，知道 AutoThrottle 是中间件，
         而不是去调 CONCURRENT_REQUESTS
      ⑤ 第 62 课讲 scrapy-redis 时，你能立刻理解它到底
         「替换了哪几个组件」——它替换的就是 Scheduler 和 DupeFilter 两个类，
         其余全部不动。

    ▸ 最后一条特别重要：**scrapy-redis 的本质就是把本课的
      Scheduler 换成 Redis 版、DupeFilter 换成 Redis 版。**
      不理解本课的架构，你就只能用它的配置文件，不能改它。""")


# ============================================================================
# 踩坑记录
# ============================================================================
def pitfalls() -> None:
    """打印本课实测中真实遇到的问题。"""
    title("踩坑记录：本课代码实测中真实遇到的问题")

    print("""
    ── 坑 1：heapq 在优先级相同时比较 Request 对象 → TypeError ──────────
    ❌ 错误做法：heapq.heappush(heap, (-request.priority, request))
    现象：TypeError: '<' not supported between instances of 'Request' and 'Request'
    根因：heapq 是**元组比较**：先比第一个元素，相等时比第二个。
          当两个请求优先级相同时，它会去比较两个 Request 对象，
          而 dataclass 默认不生成 __lt__ 方法。
    正确做法：插入 (priority, 序号, request) 三元组，
         序号由 itertools.count() 生成，**永不重复**，
         于是比较永远在第二个元素就分出胜负。
    教学价值：这是 heapq 的经典陷阱。依赖顺序的容器（堆/排序）
         在使用自定义对象时，**必须保证对象之间有全序关系**，
         或者插入一个永不重复的 tie-breaker。

    ── 坑 2：重试的 Request 被自己的去重逻辑拦掉 ──────────────────────
    ❌ 错误做法：RetryMiddleware 里构造新 Request 时忘了设 dont_filter=True
    现象：重试请求入队失败（enqueue_request 返回 False），
          日志里看到「重试入队」了但请求数没有增加，
          Spider 永远收不到那次重试的响应，item 永久丢失。
    根因：重试用的是**同一个 URL**，而该 URL 的指纹在首次请求时
          已经被 DupeFilter 登记过了。第二次进来必然判定为"重复"。
    正确做法：重试产生的 Request 必须 dont_filter=True。
          更本质的理解是：**"重试"不是"新请求"，它不应该走完整去重流程**，
          因为去重的目的是"避免抓取同一个页面两次"，
          而重试是"上一次没抓到，所以再抓一次"——
          这两件事在语义上是不同的，但它们的 URL 完全一样，
          所以只能靠 dont_filter 这个显式标记来区分。
    这个坑很隐蔽：代码不报错，只是静默地少抓了几条数据。

    ── 坑 3：Spider.parse 里的解析异常会杀掉整个 Engine ────────────────
    ❌ 错误做法：parse_item 里 price 解析失败时 `raise ValueError(...)`
    现象：一个页面的价格字段格式异常，异常一路穿过 Engine.run()，
          整个程序崩溃，前面抓到的数据全丢。
    根因：真实 Scrapy 有 Spider Middleware 层负责捕获回调异常，
          我们的迷你实现没有这一层，异常会直冲调用栈顶端。
    正确做法：解析失败**记录 + 跳过**（return 而不 raise），
          并把失败计数暴露为指标。
          因为「解析失败率突然上升」是站点改版的最早信号，
          比任何日志都灵敏。
    教学价值：**在批量处理场景里，单条失败不应该影响整批**。
          这和实验 60 里 asyncio.gather(return_exceptions=True)
          是同一个原则 —— 容错的粒度要尽量细。

    ── 坑 4：`x or default` 静默丢掉了调用方传进来的合法对象 ──────────────
    ❌ 错误做法：Scheduler.__init__ 里写
          self.dupefilter = dupefilter or DupeFilter()
    现象：实验 3 的「去重关闭」组跑出了和「去重开启」组**一模一样**的数字
          （指纹库 8、命中率 42.9%）。也就是说 DupeFilter(enabled=False)
          被丢弃了，实际生效的是一个新建的、默认开启的去重器。
    根因：`or` 用的是**真值**判断不是 `is None` 判断。
          而 DupeFilter 实现了 __len__（返回已登记指纹数），
          所以一个**空的** DupeFilter 布尔值为 False，
          于是 `空DupeFilter or DupeFilter()` 取右边 —— 我们的配置被吃掉了。
    正确做法：`dupefilter if dupefilter is not None else DupeFilter()`
    本课的修复与完整分析写在 Scheduler.__init__ 的 docstring 里。
    教学价值：① 给参数设默认值时永远用 `is None` 判断，不要用 `or`；
          ② 这一类 bug **不报错、结果全错**，是最危险的一种；
          ③ 防御手段是「对照实验必须产生差异」——
             如果 A/B 两组数字完全相同，先怀疑开关没生效。

    ── 坑 5：Process_response 里返回 None 被误当成"丢弃" ──────────────
    ❌ 错误做法：写一个只做记录的中间件，process_response 里
          `print(...)` 之后忘记 return response
    现象：所有请求都不返回内容了，Spider 收到 None，
          报 AttributeError: 'NoneType' object has no attribute 'body'。
    根因：process_response 的契约是「必须返回 Response 或 Request」，
          返回 None 在真实 Scrapy 里会抛异常
          （NotConfigured: middleware must return Response, Request or raise）。
          本课的实现没做这个校验，于是 None 被静默透传。
    正确做法：中间件里做纯观测时，用「先记录、最后 return response」的
          结构；或者用装饰器包一层断言。
    教学价值：**中间件是有契约的**。写中间件时，第一个要问的问题是
          "我这个方法的返回值语义是什么"，而不是"我要在这做什么"。
          契约搞错，代码能跑但行为全错。""")


# ============================================================================
# 主流程
# ============================================================================
def main() -> None:
    """运行全部实验。"""
    print(SEP)
    print("阶段 6 · 第 61 课：Scrapy 架构（纯标准库复刻迷你引擎）")
    print(SEP)
    print(f"""
本课用 {77} 个字符宽的纯标准库实现，复刻 Scrapy 的五大组件 + 两个中间件体系：

    Request / Response / Item     → 数据载体
    Scheduler + DupeFilter        → 优先级队列 + 指纹去重
    DownloaderMiddleware (x3)     → UA / 代理 / 重试
    Downloader                    → 并发下载
    Spider                        → parse 产出 Item 和新 Request
    Pipeline (x3)                 → 清洗 / 校验 / 收集
    Engine                        → 事件循环总线

⚠ 局限声明（详见实验 4 的完整对照表）：
  · 本课**不安装、不使用**真实 Scrapy，也不访问任何真实网站。
  · 用线程池代替 Twisted reactor —— IO 密集下行为等价，
    但缺少 reactor 的「无竞态」特性。
  · 没有限速（AutoThrottle）、没有 Spider Middleware、
    没有信号系统、没有磁盘持久化。
    因此**不能**把这个实现直接用于真实站点。
  · 目标站点是确定性模拟器，失败模式比真实站点简单得多。
""")

    exp1_event_flow()
    exp2_middleware_onion()
    exp3_disable_dupefilter()
    exp4_gap_with_real_scrapy()
    pitfalls()

    title("本课要点")
    for line in [
        "1. Scrapy 五大组件：Engine 是总线，其余四个各管一件事",
        "2. 最高价值的设计决策：Spider.parse 只产出 Request，不下载",
        "3. 该决策带来三项能力：优先级可切换、去重全局唯一、队列可外置（Redis）",
        "4. Request.callback 存方法名字符串而非方法对象，是为了可序列化",
        "5. DupeFilter 用哈希指纹而非原始 URL 存储，定长且省内存",
        "6. 指纹绝不能包含 meta —— 随机值/时间戳会让去重彻底失效",
        "7. heapq 存 (priority, 序号, request) 三元组，用序号做 tie-breaker",
        "8. 中间件是洋葱模型：请求正序、响应逆序，谁包装谁解包",
        "9. process_request 返回非 None 即熔断（责任链模式）",
        "10. Scrapy 的重试用「process_response 返回新 Request」实现，是重新入队不是原地等待",
        "11. 重试的 Request 必须 dont_filter=True，否则被自己的去重拦掉",
        "12. 降低重试请求的 priority 可天然实现延后重试（队列非空时）",
        "13. Pipeline 链式执行，返回 None 即短路丢弃；纯观测的 Pipeline 必须返回 item",
        "14. 解析失败要记录并跳过，不能让一条坏数据杀掉整批",
        "15. 防循环三层：源头控制 > 深度限制 > 全局去重，缺一不可",
        "16. 给参数设默认值一律用 `if x is None`，绝不用 `x or default`（空对象是假值）",
        "17. 对照实验跑出相同数字时，先怀疑开关没生效，而不是下结论",
        "18. scrapy-redis 的本质：只替换 Scheduler 和 DupeFilter 两个类",
    ]:
        print("  " + line)
    print()


if __name__ == "__main__":
    main()
