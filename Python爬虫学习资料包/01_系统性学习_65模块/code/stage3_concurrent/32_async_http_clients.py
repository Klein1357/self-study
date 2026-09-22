"""
阶段 3 · 3.3 异步 HTTP 客户端对比
======================================================
对应网页章节：#s3-3

对比 httpx / aiohttp / requests(多线程) 三种方案抓取同一个站点，
并演示连接池参数（limits）对性能的实际影响。

运行：python3 32_async_http_clients.py
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import aiohttp
import httpx
import requests

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


BASE = "https://books.toscrape.com/catalogue/page-{}.html"
PAGES = list(range(1, 11))     # 抓 10 页，验证并发效果
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"


# ============================================================
# 数据模型
# ============================================================
@dataclass
class PageResult:
    """单页抓取结果。"""

    page: int
    ok: bool
    length: int = 0
    elapsed: float = 0.0
    error: str = ""


@dataclass
class BenchReport:
    """一种方案的跑分报告。"""

    name: str
    elapsed: float = 0.0
    pages: list[PageResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        """成功页数。"""
        return sum(1 for p in self.pages if p.ok)

    @property
    def total_bytes(self) -> int:
        """总字节数。"""
        return sum(p.length for p in self.pages)

    def summary(self) -> str:
        """一行摘要。"""
        return (
            f"{self.name:<18} {self.elapsed:6.2f}s  "
            f"成功 {self.ok_count}/{len(self.pages)}  "
            f"共 {self.total_bytes / 1024:7.1f} KB"
        )


# ============================================================
# 方案 A：requests + 线程池（阶段 2 的写法）
# ============================================================
def fetch_sync(page: int) -> PageResult:
    """同步抓一页。"""
    t = time.perf_counter()
    try:
        r = requests.get(BASE.format(page), headers={"User-Agent": UA}, timeout=(5, 15))
        r.raise_for_status()
        return PageResult(page, True, len(r.content), time.perf_counter() - t)
    except Exception as e:                      # noqa: BLE001 - 演示用途，统一兜底
        return PageResult(page, False, 0, time.perf_counter() - t, f"{type(e).__name__}: {e}")


def bench_requests_threaded(workers: int = 10) -> BenchReport:
    """
    requests + ThreadPoolExecutor。

    Args:
        workers: 线程数。

    Returns:
        跑分报告。
    """
    from concurrent.futures import ThreadPoolExecutor

    rep = BenchReport(f"requests+线程({workers})")
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rep.pages = list(pool.map(fetch_sync, PAGES))
    rep.elapsed = time.perf_counter() - t0

    # 关键：Session 复用连接池能显著提速，但线程池里共享 Session 需要线程安全
    # （requests.Session 不是严格线程安全的，生产环境推荐给每个线程一个 Session
    #  或改用 httpx.Client，这里为对比公平性只用最朴素的 requests.get）
    return rep


# ============================================================
# 方案 B：httpx 异步
# ============================================================
async def bench_httpx(pages: list[int] = PAGES, max_conn: int = 10) -> BenchReport:
    """
    httpx.AsyncClient 并发抓取。

    Args:
        pages: 页码列表。
        max_conn: 连接池最大连接数。

    Returns:
        跑分报告。
    """
    rep = BenchReport(f"httpx(max={max_conn})")
    limits = httpx.Limits(max_connections=max_conn, max_keepalive_connections=max_conn)

    async with httpx.AsyncClient(
        limits=limits,
        timeout=httpx.Timeout(15.0, connect=5.0),
        headers={"User-Agent": UA},
        follow_redirects=True,
    ) as client:

        async def one(page: int) -> PageResult:
            t = time.perf_counter()
            try:
                r = await client.get(BASE.format(page))
                r.raise_for_status()
                return PageResult(page, True, len(r.content), time.perf_counter() - t)
            except Exception as e:              # noqa: BLE001
                return PageResult(page, False, 0, time.perf_counter() - t, f"{type(e).__name__}: {e}")

        t0 = time.perf_counter()
        rep.pages = list(await asyncio.gather(*(one(p) for p in pages)))
        rep.elapsed = time.perf_counter() - t0
    return rep


# ============================================================
# 方案 C：aiohttp 异步
# ============================================================
async def bench_aiohttp(pages: list[int] = PAGES, max_conn: int = 10) -> BenchReport:
    """
    aiohttp.ClientSession 并发抓取。

    与 httpx 的关键差别：aiohttp 默认不做连接池上限设置（其实是 100），
    必须显式传 connector 才能精确控制；响应体要用 resp.text()/resp.read() 主动读取。

    Args:
        pages: 页码列表。
        max_conn: 连接池上限。

    Returns:
        跑分报告。
    """
    rep = BenchReport(f"aiohttp(max={max_conn})")
    connector = aiohttp.TCPConnector(limit=max_conn, limit_per_host=max_conn)

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=15, connect=5),
        headers={"User-Agent": UA},
    ) as session:

        async def one(page: int) -> PageResult:
            t = time.perf_counter()
            try:
                async with session.get(BASE.format(page)) as resp:
                    resp.raise_for_status()
                    body = await resp.read()      # 必须显式读取，否则连接不释放
                    return PageResult(page, True, len(body), time.perf_counter() - t)
            except Exception as e:                # noqa: BLE001
                return PageResult(page, False, 0, time.perf_counter() - t, f"{type(e).__name__}: {e}")

        t0 = time.perf_counter()
        rep.pages = list(await asyncio.gather(*(one(p) for p in pages)))
        rep.elapsed = time.perf_counter() - t0
    return rep


# ============================================================
# 实验：连接池大小的影响
# ============================================================
async def pool_sweep() -> list[BenchReport]:
    """扫描不同连接池大小，观察边际收益。"""
    reports: list[BenchReport] = []
    for n in (1, 3, 5, 10, 20):
        rep = await bench_httpx(pages=PAGES, max_conn=n)
        reports.append(rep)
        print("  " + rep.summary())
    return reports


# ============================================================
# 主流程
# ============================================================
async def main() -> None:
    """运行全部对比。"""
    print("=" * 72)
    print("3.3 异步 HTTP 客户端对比")
    print(f"目标：{len(PAGES)} 个页面  {BASE.format('N')}")
    print("=" * 72)

    print("\n【方案对比】同样 10 个页面")
    print("-" * 72)

    reports: list[BenchReport] = []

    # 同步基线：只抓 3 页，否则太慢（外网延迟下 10 页要 15 秒以上）
    t0 = time.perf_counter()
    base_pages = PAGES[:3]
    with_sync = [fetch_sync(p) for p in base_pages]
    sync_elapsed = time.perf_counter() - t0
    rep = BenchReport("requests 串行")
    rep.pages = with_sync
    rep.elapsed = sync_elapsed
    print(f"  {rep.summary()}  ← 只抓了 3 页（外网太慢）")
    per_page = sync_elapsed / len(base_pages)

    r1 = bench_requests_threaded(10)
    reports.append(r1)
    print("  " + r1.summary())

    r2 = await bench_httpx()
    reports.append(r2)
    print("  " + r2.summary())

    r3 = await bench_aiohttp()
    reports.append(r3)
    print("  " + r3.summary())

    print(f"\n  单页同步耗时约 {per_page:.2f} 秒 → 10 页串行预计 {per_page * 10:.1f} 秒")
    best = min(reports, key=lambda r: r.elapsed)
    print(f"  最快方案：{best.name}，相对串行提速约 {per_page * 10 / best.elapsed:.1f}x")

    print("\n【连接池大小扫描】httpx")
    print("-" * 72)
    sweep = await pool_sweep()

    fastest = min(sweep, key=lambda r: r.elapsed)
    print(f"\n  最优连接数：{fastest.name}")
    print("  观察：连接数从 1 涨到 5 收益巨大，超过 10 后趋于平缓。")
    print("  为什么？因为服务端/网络本身是瓶颈，开再多连接也只是排队。")
    print("  结论：爬虫连接数不是越大越好，10~20 是常见甜点；")
    print("       盲目开到 200 只会触发对方的风控（阶段 4 会细讲）。")

    # 错误明细
    print("\n【错误检查】")
    print("-" * 72)
    for rep in reports:
        bad = [p for p in rep.pages if not p.ok]
        if bad:
            for p in bad[:3]:
                print(f"  {rep.name} 第 {p.page} 页失败：{p.error}")
        else:
            print(f"  {rep.name} 无失败")

    print("\n" + "=" * 72)
    print("选型建议")
    print("=" * 72)
    print("  · 已有 requests 代码想改异步 → httpx（API 与 requests 极像，学习成本最低）")
    print("  · 追求极致吞吐 / 用 aiohttp 生态 → aiohttp（性能略优，但 API 更繁琐）")
    print("  · 量不大（< 200 请求）→ requests + 线程池就够了，别过度设计")
    print("  · httpx 的优势：同一套 API 支持同步/异步，方便渐进式改造")


if __name__ == "__main__":
    asyncio.run(main())
