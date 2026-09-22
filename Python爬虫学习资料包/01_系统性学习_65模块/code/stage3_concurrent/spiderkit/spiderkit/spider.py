"""
spiderkit 爬虫主编排模块
========================
对应网页章节：#s3-4 #s3-6 #s3-9

把 Fetcher / StateStore / Parser / Writer 串成完整流程：
  列表页 → 详情页 URL → 详情页 → 落盘，全程带断点续爬与优雅退出。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from typing import Any

from .config import Settings
from .fetcher import Fetcher
from .models import Book, parse_detail_page, parse_list_page
from .state import StateStore
from .writer import BatchWriter, write_failures, write_json

log = logging.getLogger("spiderkit.spider")


class BooksSpider:
    """
    books.toscrape.com 采集器。

    流程：
      1. 生成列表页 URL（设置里指定范围）
      2. 并发抓列表页，解析出详情页链接 → 写入 state（pending）
      3. 从 state 领取任务，并发抓详情页 → 解析 → 落盘 → 标记 done
      4. 中断时可随时 kill，重启后从 state 恢复

    Attributes:
        settings: 配置。
        state: 状态存储。
        stop: 优雅退出标志。
    """

    LIST_URL = "catalogue/page-{}.html"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state = StateStore(settings.state_db)
        self.stop = False
        self._signal_count = 0

    # ---------------- 信号处理 ----------------
    def install_signal_handlers(self) -> None:
        """安装 Ctrl-C 处理器：第一次优雅退出，第二次强制退出。"""

        def handler(signum: int, frame: Any) -> None:
            """信号回调。"""
            self._signal_count += 1
            if self._signal_count == 1:
                log.warning("收到中断信号，正在优雅退出…（再按一次强制退出）")
                self.stop = True
            else:
                log.error("强制退出")
                raise KeyboardInterrupt

        for sig in (signal.SIGINT, signal.SIGTERM):
            # 非主线程无法注册信号（测试时会遇到），忽略即可
            with contextlib.suppress(ValueError):
                signal.signal(sig, handler)

    # ---------------- 第一阶段：收集详情页 URL ----------------
    async def collect_urls(self, fetcher: Fetcher) -> int:
        """
        抓取列表页，把详情页 URL 写入状态库。

        Args:
            fetcher: 抓取器。

        Returns:
            新收录的 URL 数量。
        """
        pages = range(self.settings.start_page, self.settings.max_pages + 1)
        log.info("阶段 1：抓取 %d 个列表页", len(pages))

        async def one(page: int) -> list[str]:
            """抓一个列表页并返回详情页 URL。"""
            url = self.settings.base_url + self.LIST_URL.format(page)
            html = await fetcher.get(url)
            if not html:
                log.warning("列表页 %d 抓取失败", page)
                return []
            report = parse_list_page(html, url)
            log.info("列表页 %d：解析出 %d 个条目（跳过 %d）",
                     page, len(report.items), report.skipped)
            return [it.url for it in report.items]

        results = await asyncio.gather(*(one(p) for p in pages))
        urls: list[str] = []
        for batch in results:
            urls.extend(batch)

        # 顺带补充"同类推荐"链接，扩大覆盖面
        added = self.state.seed(urls)
        log.info("阶段 1 完成：收录 %d 个详情页 URL（新增 %d）", len(urls), added)
        return added

    # ---------------- 第二阶段：抓详情页 ----------------
    async def crawl_details(self, fetcher: Fetcher, writer: BatchWriter) -> int:
        """
        从状态库领取任务并抓详情页。

        Args:
            fetcher: 抓取器。
            writer: 输出写入器。

        Returns:
            本轮成功写入的条数。
        """
        batch_size = self.settings.concurrency * 2
        written = 0
        in_flight: list[str] = []

        while not self.stop:
            urls = self.state.claim_batch(batch_size, include_failed=False)
            if not urls:
                log.info("没有待处理任务")
                break
            in_flight = urls

            async def one(u: str) -> tuple[str, Book | None]:
                """抓单个详情页。"""
                html = await fetcher.get(u)
                if not html:
                    return u, None
                # 解析是 CPU 密集但很快，直接同步调用即可
                return u, parse_detail_page(html, u)

            results = await asyncio.gather(*(one(u) for u in urls))

            for url, book in results:
                if book is None:
                    self.state.mark_failed(url, "抓取或解析失败")
                else:
                    self.state.mark_done(url, book.to_dict())
                    writer.write(book.to_dict())
                    written += 1

            in_flight = []
            stats = self.state.stats()
            log.info("进度 %s", stats)

        if in_flight and self.stop:
            released = self.state.release(in_flight)
            log.warning("优雅退出：释放 %d 条进行中的任务回待处理队列", released)

        return written

    # ---------------- 主流程 ----------------
    async def run(self, skip_collect: bool = False) -> dict[str, Any]:
        """
        执行完整采集流程。

        Args:
            skip_collect: 跳过列表页抓取（用于续爬）。

        Returns:
            运行摘要。
        """
        t0 = time.monotonic()
        self.install_signal_handlers()

        # 启动时先处理悬空任务 —— 断点续爬的关键一步
        self.state.reset_stale()

        self.settings.output_dir.mkdir(parents=True, exist_ok=True)

        async with Fetcher(self.settings) as fetcher:
            if not skip_collect:
                await self.collect_urls(fetcher)
            else:
                log.info("跳过列表页抓取（续爬模式）")

            out_path = self.settings.output_dir / "books.csv"
            writer = BatchWriter(out_path, fmt="csv")
            fieldnames = list(Book("").to_dict().keys())
            writer.open(fieldnames)

            try:
                written = await self.crawl_details(fetcher, writer)
            finally:
                writer.close()

        # 汇总输出
        results = self.state.results()
        stats = self.state.stats()
        failures = self.state.failures()

        json_path = self.settings.output_dir / "books.json"
        summary = {
            "总数": len(results),
            "平均价格": round(
                sum(float(r.get("price", 0)) for r in results) / len(results), 2
            ) if results else 0.0,
            "有库存": sum(1 for r in results if r.get("in_stock")),
            "五星数量": sum(1 for r in results if r.get("rating") == 5),
            "分类数": len({r.get("category") for r in results if r.get("category")}),
        }
        write_json(json_path, results, {"统计": summary})

        if failures:
            write_failures(self.settings.output_dir / "failures.csv", failures)

        elapsed = time.monotonic() - t0
        log.info("─" * 60)
        log.info("采集完成：%s", stats)
        log.info("本轮写入：%d 条", written)
        log.info("抓取统计：%s", fetcher.stats.report())
        log.info("总耗时：%.1f 秒", elapsed)

        return {
            "stats": stats,
            "written": written,
            "fetch": fetcher.stats.report(),
            "summary": summary,
            "elapsed": elapsed,
            "failures": len(failures),
        }

    def close(self) -> None:
        """释放资源。"""
        self.state.close()
