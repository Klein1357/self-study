"""
阶段 3 · 3.10 阶段 2 vs 阶段 3 对比实测
======================================================
对应网页章节：#s3-9（毕业作业）

用同一个目标站点（books.toscrape.com，3 页 60 本书），
对比两代实现的差距：

  阶段 2 版：requests 串行 + BeautifulSoup，无重试/无断点/无统计
  阶段 3 版：httpx 异步 + parsel，含并发/限速/重试/断点/日志/CLI

运行：python3 38_stage2_vs_stage3.py
"""

from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import requests
from bs4 import BeautifulSoup
from parsel import Selector

BASE = "https://books.toscrape.com/"
LIST = BASE + "catalogue/page-{}.html"
PAGES = [1, 2, 3]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


# ============================================================
# 阶段 2 版实现（对照组）
# ============================================================
@dataclass
class Stage2Result:
    """阶段 2 实现的跑分。"""

    books: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    requests_made: int = 0
    errors: list[str] = field(default_factory=list)


def stage2_crawl() -> Stage2Result:
    """
    阶段 2 写法：requests 串行 + BeautifulSoup。

    典型特征（也是局限）：
      · 一次请求一个线程，等待时间完全浪费
      · 没有重试，一页失败整批就少了数据
      · 没有断点，中断就得从头来
      · 没有统计，跑完只有一句"完成"

    Returns:
        跑分结果。
    """
    r = Stage2Result()
    t0 = time.perf_counter()
    session = requests.Session()

    try:
        for page in PAGES:
            # 1. 抓列表页
            url = LIST.format(page)
            resp = session.get(url, headers={"User-Agent": UA}, timeout=(5, 20))
            resp.raise_for_status()
            r.requests_made += 1
            soup = BeautifulSoup(resp.text, "lxml")

            # 2. 逐个抓详情页（串行！这里是性能瓶颈）
            for a in soup.select("article.product_pod h3 a"):
                detail_url = BASE + "catalogue/" + a["href"]
                try:
                    d = session.get(detail_url, headers={"User-Agent": UA}, timeout=(5, 20))
                    d.raise_for_status()
                    r.requests_made += 1
                    dsoup = BeautifulSoup(d.text, "lxml")
                    title = dsoup.select_one("h1")
                    r.books.append(title.get_text(strip=True) if title else "?")
                except Exception as e:                  # noqa: BLE001
                    r.errors.append(f"{detail_url}: {type(e).__name__}")
    except Exception as e:                              # noqa: BLE001
        r.errors.append(f"致命错误：{type(e).__name__}: {e}")

    r.elapsed = time.perf_counter() - t0
    return r


# ============================================================
# 阶段 3 版实现
# ============================================================
@dataclass
class Stage3Result:
    """阶段 3 实现的跑分。"""

    books: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    requests_made: int = 0
    retried: int = 0
    errors: list[str] = field(default_factory=list)
    request_times: list[float] = field(default_factory=list)


async def stage3_crawl(concurrency: int = 8, rate: float = 6.0) -> Stage3Result:
    """
    阶段 3 写法：httpx 异步 + parsel + 并发控制 + 重试。

    Args:
        concurrency: 并发上限。
        rate: 每秒请求上限。

    Returns:
        跑分结果。
    """
    from spiderkit.fetcher import Fetcher, TokenBucket
    from spiderkit.config import Settings

    r = Stage3Result()
    cfg = Settings(
        concurrency=concurrency,
        rate_limit=rate,
        max_retries=3,
        base_url=BASE,
    )
    bucket = TokenBucket(rate=rate)
    t0 = time.perf_counter()

    async with Fetcher(cfg) as f:
        # 阶段 1：并发抓列表页
        async def list_page(page: int) -> list[str]:
            """抓列表页并返回详情页链接。"""
            html = await f.get(LIST.format(page))
            if not html:
                return []
            sel = Selector(text=html)
            return [
                BASE + "catalogue/" + h
                for h in sel.css("article.product_pod h3 a::attr(href)").getall()
            ]

        batches = await asyncio.gather(*(list_page(p) for p in PAGES))
        detail_urls = [u for b in batches for u in b]

        # 阶段 2：并发抓详情页，带限速
        async def detail(url: str) -> str | None:
            """抓详情页并提取标题。"""
            await bucket.acquire()
            ts = time.perf_counter()
            html = await f.get(url)
            r.request_times.append(time.perf_counter() - ts)
            if not html:
                return None
            sel = Selector(text=html)
            title = sel.css("h1::text").get()
            return title.strip() if title else None

        titles = await asyncio.gather(*(detail(u) for u in detail_urls))
        r.books = [t for t in titles if t]

        r.requests_made = f.stats.total
        r.retried = f.stats.retried

    r.elapsed = time.perf_counter() - t0
    return r


# ============================================================
# 对比输出
# ============================================================
def compare(s2: Stage2Result, s3: Stage3Result) -> None:
    """
    打印对比表。

    Args:
        s2: 阶段 2 结果。
        s3: 阶段 3 结果。
    """
    print("\n" + "=" * 76)
    print("对比结果")
    print("=" * 76)

    rows = [
        ("采集书籍数", f"{len(s2.books)}", f"{len(s3.books)}"),
        ("总耗时", f"{s2.elapsed:.1f} 秒", f"{s3.elapsed:.1f} 秒"),
        ("发出请求数", f"{s2.requests_made}", f"{s3.requests_made}"),
        ("平均每请求", f"{s2.elapsed / max(s2.requests_made, 1) * 1000:.0f} ms",
         f"{s3.elapsed / max(s3.requests_made, 1) * 1000:.0f} ms"),
        ("错误数", f"{len(s2.errors)}", f"{len(s3.errors)}"),
        ("重试次数", "不支持", f"{s3.retried}"),
    ]
    print(f"  {'指标':<16}{'阶段 2（同步）':<22}{'阶段 3（异步）'}")
    print("  " + "-" * 70)
    for name, a, b in rows:
        print(f"  {name:<16}{a:<22}{b}")

    speedup = s2.elapsed / s3.elapsed if s3.elapsed else 0
    print(f"\n  🚀 提速：{speedup:.1f}x")
    saved = s2.elapsed - s3.elapsed
    print(f"  ⏱  节省：{saved:.1f} 秒（{saved / 60:.1f} 分钟）")

    if s3.request_times:
        print(f"\n  阶段 3 单请求耗时分布（异步下呈长尾）：")
        print(f"    中位数 {statistics.median(s3.request_times) * 1000:.0f} ms")
        print(f"    最快   {min(s3.request_times) * 1000:.0f} ms")
        print(f"    最慢   {max(s3.request_times) * 1000:.0f} ms")
        print("    → 中位数远小于最慢值，说明网络延迟差异大；")
        print("      异步的价值就是把这条长尾重叠起来。")

    print("\n" + "=" * 76)
    print("不只是快：工程能力的差距")
    print("=" * 76)
    print(f"  {'能力':<20}{'阶段 2':<14}{'阶段 3'}")
    print("  " + "-" * 68)
    caps = [
        ("并发采集", "✗ 串行", "✓ 异步 + 限速"),
        ("失败重试", "✗ 无", "✓ 指数退避 + 抖动"),
        ("断点续爬", "✗ 中断即重来", "✓ SQLite 状态库"),
        ("错误分类", "✗ 全部吞掉", "✓ 区分可/不可重试"),
        ("日志追溯", "✗ print", "✓ 分级 + 轮转 + JSON"),
        ("配置管理", "✗ 硬编码", "✓ 环境变量 + 校验"),
        ("进度查询", "✗ 只能看终端", "✓ spiderkit status"),
        ("测试覆盖", "✗ 无", "✓ 55 个单测"),
        ("命令行工具", "✗ 改代码", "✓ typer CLI"),
    ]
    for name, a, b in caps:
        print(f"  {name:<20}{a:<14}{b}")

    print("\n  阶段 2 的代码能让这一次跑完。")
    print("  阶段 3 的代码能让每一次都跑完 —— 包括半夜挂了自动续上。")


async def main() -> None:
    """执行对比。"""
    print("=" * 76)
    print("阶段 2 vs 阶段 3 —— 同一目标站点的两代实现")
    print(f"目标：{BASE}  第 {PAGES} 页")
    print("=" * 76)

    print("\n▸ 阶段 2 版（requests 串行 + BeautifulSoup）运行中…")
    print("  （这一步要几分钟，串行抓 60 个详情页）")
    s2 = stage2_crawl()
    print(f"  完成：{len(s2.books)} 本，耗时 {s2.elapsed:.1f} 秒，"
          f"发出 {s2.requests_made} 个请求")

    print("\n▸ 阶段 3 版（httpx 异步 + parsel + 限速）运行中…")
    s3 = await stage3_crawl(concurrency=8, rate=6.0)
    print(f"  完成：{len(s3.books)} 本，耗时 {s3.elapsed:.1f} 秒，"
          f"发出 {s3.requests_made} 个请求")

    compare(s2, s3)

    # 数据一致性校验
    print("\n" + "=" * 76)
    print("数据一致性校验")
    print("=" * 76)
    a, b = set(s2.books), set(s3.books)
    print(f"  阶段 2 采集 {len(a)} 本，阶段 3 采集 {len(b)} 本")
    print(f"  两版共有   ：{len(a & b)} 本")
    print(f"  仅阶段 2 有 ：{len(a - b)} 本")
    print(f"  仅阶段 3 有 ：{len(b - a)} 本")

    if a == b:
        print("  ✓ 两版结果完全一致 —— 重构没有丢数据")
    else:
        print("\n  差异清单：")
        for t in sorted(a - b):
            print(f"    仅阶段 2：{t!r}")
        for t in sorted(b - a):
            print(f"    仅阶段 3：{t!r}")

        # 判断差异是否只是编码问题
        only2 = a - b
        only3 = b - a
        if len(only2) == len(only3) and _looks_like_mojibake(only2, only3):
            print("\n  🔍 诊断：这不是数据缺失，是【阶段 2 的编码 bug】。")
            print("     mojibake 形如 â\\x80\\x99，本该是弯引号 ’（U+2019）。")
            print("     原因：阶段 2 用 resp.text 时，requests 按 HTTP 头猜编码，")
            print("     把本该是 UTF-8 的页面按 latin-1 解了码。")
            print("     阶段 3 的 parsel 直接从 bytes 解析，绕开了这个问题。")
            print("\n     → 两版采集到的书籍【完全相同】，只是阶段 2 的标题有乱码字符。")
            print("     → 这正好是阶段 0 讲的『编码是字节到字符的映射规则』的实战案例。")


def _looks_like_mojibake(set_a: set[str], set_b: set[str]) -> bool:
    """
    判断两组差异是否只是编码问题（而非真的缺数据）。

    原理：把疑似乱码的字符串按 latin-1 编回字节再用 utf-8 解，
    如果能还原成集合 b 里的某一项，说明就是编码问题。

    Args:
        set_a: 阶段 2 在集合里多出的项。
        set_b: 阶段 3 在集合里多出的项。

    Returns:
        True 表示差异纯属编码问题。
    """
    recovered: set[str] = set()
    for s in set_a:
        try:
            recovered.add(s.encode("latin-1").decode("utf-8"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            recovered.add(s)
    return recovered == set_b


if __name__ == "__main__":
    import sys
    from pathlib import Path as _P

    # 把 spiderkit 加入 import 路径，方便直接运行本脚本
    _kit = _P(__file__).parent / "spiderkit"
    if str(_kit) not in sys.path:
        sys.path.insert(0, str(_kit))

    asyncio.run(main())
