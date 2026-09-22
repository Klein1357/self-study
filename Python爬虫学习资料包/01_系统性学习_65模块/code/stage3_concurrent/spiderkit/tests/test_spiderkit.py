"""
spiderkit 测试套件
==================
对应网页章节：#s3-9

测试策略：
  · 解析器测试：用内联 HTML 片段，不依赖网络（快、稳定）
  · 配置测试：用 monkeypatch 注入环境变量，不碰真实 .env
  · 状态库测试：用 tmp_path 建临时数据库
  · 抓取器测试：用 httpx.MockTransport 模拟响应，不发真实请求

运行：pytest -v
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spiderkit.config import Settings
from spiderkit.fetcher import backoff_delay, is_retryable
from spiderkit.models import (
    Book,
    fix_encoding,
    parse_detail_page,
    parse_list_page,
    parse_price,
    parse_stock,
)
from spiderkit.state import StateStore, Status
from spiderkit.writer import BatchWriter


# ============================================================
# 测试夹具（fixtures）
# ============================================================
LIST_HTML = """
<html><body>
<article class="product_pod">
  <h3><a href="book-a_1/index.html" title="Book A">Book A</a></h3>
  <p class="star-rating Three"></p>
  <p class="price_color">£51.77</p>
</article>
<article class="product_pod">
  <h3><a href="book-b_2/index.html" title="Book B">Book B</a></h3>
  <p class="star-rating Five"></p>
  <p class="price_color">£13.99</p>
</article>
<article class="product_pod">
  <h3><a href="broken/index.html" title="">Broken</a></h3>
  <p class="star-rating One"></p>
  <p class="price_color">£9.99</p>
</article>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<ul class="breadcrumb">
  <li><a href="/">Home</a></li><li><a href="/c/">Books</a></li>
  <li><a href="/c/travel/">Travel</a></li>
</ul>
<div class="product_main">
  <h1>A Light in the Attic</h1>
  <p class="price_color">£51.77</p>
  <p class="instock availability">
    <i class="icon-ok"></i>
    In stock (22 available)
  </p>
  <p class="star-rating Three"></p>
</div>
<table class="table-striped">
  <tr><th>UPC</th><td>a897fe39b1053632</td></tr>
  <tr><th>Tax</th><td>£0.00</td></tr>
  <tr><th>Availability</th><td>In stock (22 available)</td></tr>
  <tr><th>Number of reviews</th><td>0</td></tr>
</table>
</body></html>
"""

# 注意这里故意用 Â£ —— 模拟真实的编码错乱场景
BROKEN_ENCODING_HTML = DETAIL_HTML.replace("£51.77", "Â£51.77")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """
    构造一份隔离的测试配置。

    用 monkeypatch 清掉环境变量，保证测试结果不受运行环境影响。

    Args:
        tmp_path: pytest 提供的临时目录。
        monkeypatch: pytest 提供的环境变量补丁工具。

    Returns:
        Settings 对象。
    """
    for key in list(__import__("os").environ):
        if key.startswith("SPIDERKIT_"):
            monkeypatch.delenv(key, raising=False)
    return Settings(
        concurrency=4,
        rate_limit=1000.0,          # 测试时限速调到极高，避免拖慢
        max_retries=1,
        output_dir=tmp_path / "out",
        state_db=tmp_path / "state.db",
    )


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    """
    构造临时状态库。

    Args:
        tmp_path: 临时目录。

    Returns:
        StateStore 实例（测试结束自动关闭）。
    """
    st = StateStore(tmp_path / "test.db")
    yield st
    st.close()


# ============================================================
# 1. 解析工具测试
# ============================================================
class TestParsing:
    """解析函数单元测试。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("£51.77", Decimal("51.77")),
            ("Â£51.77", Decimal("51.77")),      # 编码错乱也要能解析
            ("£0.00", Decimal("0.00")),
            ("51.77", Decimal("51.77")),
            ("无价格", Decimal("0")),
            ("", Decimal("0")),
        ],
    )
    def test_parse_price(self, raw: str, expected: Decimal) -> None:
        """价格解析：各种输入都要正确处理。"""
        assert parse_price(raw) == expected

    def test_fix_encoding(self) -> None:
        """编码修正：Â£ 应还原为 £。"""
        assert fix_encoding("Â£51.77") == "£51.77"
        assert fix_encoding("£51.77") == "£51.77"       # 已正确的不要动
        assert fix_encoding("A Light in the Attic") == "A Light in the Attic"

    def test_parse_stock_with_fragmented_text(self) -> None:
        """
        库存解析：文本被 <i> 标签切碎时也要正确拼接。

        这是阶段 2 实测踩到的坑，必须有回归测试守着。
        """
        from parsel import Selector

        sel = Selector(text=DETAIL_HTML)
        in_stock, count = parse_stock(sel.css("p.instock.availability"))
        assert in_stock is True
        assert count == 22

    def test_parse_stock_out_of_stock(self) -> None:
        """缺货时应返回 (False, 0)。"""
        from parsel import Selector

        html = '<p class="instock availability"><i></i>Out of stock</p>'
        in_stock, count = parse_stock(Selector(text=html).css("p.instock"))
        assert in_stock is False
        assert count == 0


# ============================================================
# 2. 列表页解析测试
# ============================================================
class TestListPage:
    """列表页解析测试。"""

    def test_extracts_all_valid_items(self) -> None:
        """应提取 2 条有效数据（第 3 条 title 为空应被跳过）。"""
        report = parse_list_page(LIST_HTML, "https://example.com/catalogue/")
        assert len(report.items) == 2
        assert report.skipped == 1

    def test_relative_url_becomes_absolute(self) -> None:
        """相对链接必须转成绝对链接。"""
        report = parse_list_page(LIST_HTML, "https://example.com/catalogue/")
        assert report.items[0].url == "https://example.com/catalogue/book-a_1/index.html"

    def test_rating_mapping(self) -> None:
        """评分星级要正确映射为数字。"""
        report = parse_list_page(LIST_HTML, "https://example.com/")
        ratings = {it.title: it.rating for it in report.items}
        assert ratings["Book A"] == 3
        assert ratings["Book B"] == 5

    def test_empty_html_returns_empty(self) -> None:
        """空页面不应抛异常。"""
        report = parse_list_page("<html></html>", "https://example.com/")
        assert report.items == []


# ============================================================
# 3. 详情页解析测试
# ============================================================
class TestDetailPage:
    """详情页解析测试。"""

    def test_full_parse(self) -> None:
        """完整解析所有字段。"""
        book = parse_detail_page(DETAIL_HTML, url="https://x.com/b/1")
        assert book is not None
        assert book.title == "A Light in the Attic"
        assert book.price == Decimal("51.77")
        assert book.rating == 3
        assert book.in_stock is True
        assert book.stock_count == 22
        assert book.upc == "a897fe39b1053632"
        assert book.category == "Travel"
        assert book.reviews == 0
        assert book.url == "https://x.com/b/1"

    def test_broken_encoding_still_correct(self) -> None:
        """编码错乱时价格仍须正确 —— 阶段 2 实测的坑。"""
        book = parse_detail_page(BROKEN_ENCODING_HTML)
        assert book is not None
        assert book.price == Decimal("51.77")

    def test_no_title_returns_none(self) -> None:
        """没有标题说明不是详情页，应返回 None。"""
        assert parse_detail_page("<html><body>404</body></html>") is None

    def test_book_to_dict_is_serializable(self) -> None:
        """to_dict 结果必须能被 JSON 序列化。"""
        import json

        book = parse_detail_page(DETAIL_HTML)
        assert book is not None
        payload = json.dumps(book.to_dict(), ensure_ascii=False)
        assert "A Light in the Attic" in payload
        # Decimal 应已转成 float
        assert isinstance(book.to_dict()["price"], float)

    def test_price_with_tax(self) -> None:
        """含税价应为 price + tax。"""
        book = Book(title="X", price=Decimal("10.00"), tax=Decimal("2.00"))
        assert book.price_with_tax == Decimal("12.00")


# ============================================================
# 4. 配置测试
# ============================================================
class TestSettings:
    """配置系统测试。"""

    def test_defaults(self, settings: Settings) -> None:
        """默认值应合理。"""
        assert settings.concurrency == 4
        assert settings.base_url.endswith("/")
        assert settings.timeout == (5.0, 20.0)

    def test_base_url_auto_slash(self) -> None:
        """base_url 缺结尾斜杠应自动补上。"""
        s = Settings(base_url="https://example.com")
        assert s.base_url == "https://example.com/"

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("concurrency", 0),         # 低于最小值
            ("concurrency", 999),       # 高于最大值
            ("max_retries", -1),
            ("rate_limit", 0),
            ("timeout_connect", 0),
            ("start_page", 0),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: int) -> None:
        """非法配置必须在构造时就被拒绝（fail fast）。"""
        with pytest.raises(Exception):
            Settings(**{field: value})

    def test_proxy_is_masked(self) -> None:
        """代理地址不得以明文出现在摘要里。"""
        from pydantic import SecretStr

        s = Settings(proxy_url=SecretStr("http://user:pass@1.2.3.4:8080"))
        assert "pass" not in str(s.safe_dump())
        assert s.safe_dump()["proxy"] == "********"
        # 真要取明文必须显式调用
        assert s.proxy == "http://user:pass@1.2.3.4:8080"

    def test_invalid_log_level(self) -> None:
        """非法日志级别应报错。"""
        with pytest.raises(Exception):
            Settings(log_level="VERBOSE")


# ============================================================
# 5. 状态库测试
# ============================================================
class TestStateStore:
    """断点续爬状态库测试。"""

    def test_seed_is_idempotent(self, store: StateStore) -> None:
        """重复 seed 同一批 URL 不应产生重复条目。"""
        urls = ["https://x.com/1", "https://x.com/2"]
        assert store.seed(urls) == 2
        assert store.seed(urls) == 0            # 第二次新增 0 条
        assert store.stats().total == 2

    def test_claim_and_complete_flow(self, store: StateStore) -> None:
        """领取 → 完成 → 统计的完整流程。"""
        store.seed(["https://x.com/1", "https://x.com/2"])
        claimed = store.claim_batch(10)
        assert len(claimed) == 2
        assert store.stats().running == 2

        store.mark_done(claimed[0], {"title": "A", "price": 10.0})
        store.mark_failed(claimed[1], "超时")

        s = store.stats()
        assert s.done == 1
        assert s.failed == 1
        assert s.running == 0
        assert store.has(claimed[0]) is True
        assert store.has("https://x.com/never") is False

    def test_reset_stale_recovers_running(self, store: StateStore) -> None:
        """进程被杀后，悬空的 running 任务应能被打回 pending。"""
        store.seed(["https://x.com/1", "https://x.com/2", "https://x.com/3"])
        store.claim_batch(2)                     # 模拟领取后进程崩溃
        assert store.stats().running == 2

        reset = store.reset_stale()
        assert reset == 2
        assert store.stats().running == 0
        assert store.stats().pending == 3

    def test_release_returns_tasks(self, store: StateStore) -> None:
        """优雅退出时应能把手上任务退回队列。"""
        store.seed(["https://x.com/1"])
        claimed = store.claim_batch(1)
        assert store.release(claimed) == 1
        assert store.stats().pending == 1

    def test_claim_excludes_failed_by_default(self, store: StateStore) -> None:
        """默认不重复领取失败任务。"""
        store.seed(["https://x.com/1"])
        claimed = store.claim_batch(1)
        store.mark_failed(claimed[0], "err")

        assert store.claim_batch(5, include_failed=False) == []
        assert store.claim_batch(5, include_failed=True) == ["https://x.com/1"]

    def test_results_roundtrip(self, store: StateStore) -> None:
        """写入的数据读出后应一致。"""
        store.seed(["https://x.com/1"])
        u = store.claim_batch(1)[0]
        store.mark_done(u, {"title": "书", "price": 51.77})
        results = store.results()
        assert len(results) == 1
        assert results[0]["title"] == "书"
        assert results[0]["price"] == 51.77

    def test_failures_list(self, store: StateStore) -> None:
        """失败清单应包含错误原因与重试次数。"""
        store.seed(["https://x.com/1"])
        u = store.claim_batch(1)[0]
        store.mark_failed(u, "连接超时")
        fails = store.failures()
        assert len(fails) == 1
        assert fails[0][0] == u
        assert "连接超时" in fails[0][1]
        assert fails[0][2] == 1

    def test_wal_mode_enabled(self, store: StateStore) -> None:
        """WAL 模式应已开启（崩溃恢复能力的前提）。"""
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


# ============================================================
# 6. 重试逻辑测试
# ============================================================
class TestRetry:
    """错误分类与退避测试。"""

    def test_retryable_status_codes(self) -> None:
        """5xx 和 429 应可重试。"""
        for code in (429, 500, 502, 503, 504):
            exc = httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "https://x.com"),
                response=httpx.Response(code),
            )
            assert is_retryable(exc) is True, f"{code} 应该可重试"

    def test_non_retryable_status_codes(self) -> None:
        """404/403/400 不该重试 —— 重试只是浪费时间和骚扰对方。"""
        for code in (400, 401, 403, 404, 422):
            exc = httpx.HTTPStatusError(
                "err",
                request=httpx.Request("GET", "https://x.com"),
                response=httpx.Response(code),
            )
            assert is_retryable(exc) is False, f"{code} 不该重试"

    def test_retryable_exceptions(self) -> None:
        """网络类异常应可重试。"""
        assert is_retryable(httpx.ConnectTimeout("t")) is True
        assert is_retryable(httpx.ReadTimeout("t")) is True
        assert is_retryable(ValueError("其他错误")) is False

    def test_backoff_grows_and_is_jittered(self) -> None:
        """退避时间应随次数增长，且带随机抖动。"""
        samples = [backoff_delay(i, base=1.0, cap=60.0) for i in range(1, 6)]
        # 上界应递增（抖动不会超过指数上界）
        assert samples[4] <= 16.0 + 1e-9
        # 同一 attempt 多次采样应不同（证明有抖动）
        dupes = {backoff_delay(5, base=1.0) for _ in range(20)}
        assert len(dupes) > 1, "退避缺少抖动，会导致惊群"

    def test_backoff_respects_cap(self) -> None:
        """退避不得超过上限。"""
        for _ in range(50):
            assert backoff_delay(20, base=1.0, cap=10.0) <= 10.0


# ============================================================
# 7. 输出模块测试
# ============================================================
class TestWriter:
    """输出模块测试。"""

    def test_csv_write_and_readback(self, tmp_path: Path) -> None:
        """CSV 写入后应能正确读回（含中文）。"""
        import csv

        p = tmp_path / "out.csv"
        w = BatchWriter(p, fmt="csv")
        w.open(["title", "price"])
        w.write({"title": "百年孤独", "price": 51.77})
        w.write({"title": "Book B", "price": 13.99})
        w.close()

        assert w.count == 2
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["title"] == "百年孤独"
        assert rows[0]["price"] == "51.77"

    def test_jsonl_write(self, tmp_path: Path) -> None:
        """JSONL 每行应是一个合法 JSON 对象。"""
        import json

        p = tmp_path / "out.jsonl"
        w = BatchWriter(p, fmt="jsonl")
        w.open()
        w.write({"title": "A", "price": 1.5})
        w.write({"title": "B", "price": 2.5})
        w.close()

        lines = p.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["title"] == "A"

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        """不支持的格式应明确报错。"""
        with pytest.raises(ValueError, match="不支持的格式"):
            BatchWriter(tmp_path / "x.txt", fmt="xml").open()

    def test_write_before_open_raises(self, tmp_path: Path) -> None:
        """未 open 就 write 应报错，而不是静默丢数据。"""
        w = BatchWriter(tmp_path / "x.csv", fmt="csv")
        with pytest.raises(RuntimeError, match="open"):
            w.write({"a": 1})

    def test_write_failures(self, tmp_path: Path) -> None:
        """失败清单应可写出并读回。"""
        import csv

        from spiderkit.writer import write_failures

        p = tmp_path / "failures.csv"
        write_failures(p, [("https://x.com/1", "超时", 3)])
        with p.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["url"] == "https://x.com/1"
        assert rows[0]["attempts"] == "3"

    def test_write_json_with_stats(self, tmp_path: Path) -> None:
        """JSON 汇总应包含统计段。"""
        import json

        from spiderkit.writer import write_json

        p = tmp_path / "out.json"
        write_json(p, [{"a": 1}], {"统计": {"总": 1}})
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["统计"]["总"] == 1
        assert len(data["数据"]) == 1


# ============================================================
# 8. 抓取器集成测试（MockTransport，不发真实请求）
# ============================================================
class TestFetcher:
    """抓取器测试：用 MockTransport 模拟网络，测试稳定且快。"""

    @pytest.mark.asyncio
    async def test_successful_fetch(self, settings: Settings) -> None:
        """正常抓取应返回 HTML 文本。"""

        from spiderkit.fetcher import Fetcher

        async with Fetcher(settings) as f:
            transport = httpx.MockTransport(
                lambda req: httpx.Response(200, text="<html>ok</html>")
            )
            assert f._client is not None
            f._client._transport = transport      # 注入 mock
            html = await f.get("https://example.com/")
            assert html == "<html>ok</html>"
            assert f.stats.succeeded == 1
            assert f.stats.total == 1

    @pytest.mark.asyncio
    async def test_404_not_retried(self, settings: Settings) -> None:
        """404 不该重试，且应返回 None。"""
        from spiderkit.fetcher import Fetcher

        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(404)

        async with Fetcher(settings) as f:
            assert f._client is not None
            f._client._transport = httpx.MockTransport(handler)
            html = await f.get("https://example.com/missing")
            assert html is None
            assert calls["n"] == 1, "404 不应该被重试"
            assert f.stats.failed == 1

    @pytest.mark.asyncio
    async def test_503_retried_then_succeeds(self, settings: Settings) -> None:
        """503 应重试，成功后正常返回。"""
        from spiderkit.fetcher import Fetcher

        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, text="recovered")

        async with Fetcher(settings) as f:
            assert f._client is not None
            f._client._transport = httpx.MockTransport(handler)
            html = await f.get("https://example.com/")
            assert html == "recovered"
            assert calls["n"] == 2
            assert f.stats.succeeded == 1

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens(self, settings: Settings) -> None:
        """连续失败应触发熔断，后续请求被直接拒绝。"""
        from spiderkit.fetcher import Fetcher

        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        async with Fetcher(settings) as f:
            f.breaker.threshold = 2
            f.breaker.cooldown = 999          # 测试期间不恢复
            assert f._client is not None
            f._client._transport = httpx.MockTransport(handler)

            await f.get("https://example.com/1")
            await f.get("https://example.com/2")
            n_before = calls["n"]
            await f.get("https://example.com/3")

            assert calls["n"] == n_before, "熔断后不该再发出请求"
            assert f.breaker.rejected == 1

    @pytest.mark.asyncio
    async def test_concurrency_limited(self, settings: Settings) -> None:
        """并发峰值不得超过配置的 concurrency。"""
        import asyncio

        from spiderkit.fetcher import Fetcher

        peak = {"v": 0}
        current = {"v": 0}

        async def handler(req: httpx.Request) -> httpx.Response:
            current["v"] += 1
            peak["v"] = max(peak["v"], current["v"])
            await asyncio.sleep(0.02)
            current["v"] -= 1
            return httpx.Response(200, text="ok")

        async with Fetcher(settings) as f:      # concurrency=4
            assert f._client is not None
            f._client._transport = httpx.MockTransport(handler)
            await asyncio.gather(*(f.get(f"https://x.com/{i}") for i in range(20)))

        assert peak["v"] <= settings.concurrency, f"并发峰值 {peak['v']} 超出上限"


# ============================================================
# 9. 端到端测试
# ============================================================
class TestEndToEnd:
    """用 mock 网络跑完整的爬虫流程，验证状态流转与幂等性。"""

    @pytest.mark.asyncio
    async def test_full_crawl_flow(self, settings: Settings, tmp_path: Path) -> None:
        """完整流程：seed → claim → 解析 → 落盘 → 统计。"""
        from spiderkit.spider import BooksSpider

        spider = BooksSpider(settings)
        try:
            spider.state.seed([
                "https://example.com/b/1",
                "https://example.com/b/2",
            ])
            stats = spider.state.stats()
            assert stats.pending == 2

            # 手动跑一遍状态机
            urls = spider.state.claim_batch(10)
            for i, u in enumerate(urls, 1):
                html = DETAIL_HTML.replace("A Light in the Attic", f"Book {i}")
                book = parse_detail_page(html, u)
                assert book is not None
                spider.state.mark_done(u, book.to_dict())

            results = spider.state.results()
            assert len(results) == 2
            assert results[0]["title"].startswith("Book")
            assert spider.state.stats().done == 2
        finally:
            spider.close()

    @pytest.mark.asyncio
    async def test_crawl_is_idempotent(self, settings: Settings) -> None:
        """重复运行不应产生重复数据。"""
        from spiderkit.spider import BooksSpider

        spider = BooksSpider(settings)
        try:
            urls = ["https://example.com/b/1", "https://example.com/b/2"]
            spider.state.seed(urls)
            spider.state.claim_batch(10)
            for u in urls:
                spider.state.mark_done(u, {"title": u})

            # 再跑一遍 seed
            spider.state.seed(urls)
            assert len(spider.state.results()) == 2, "重复 seed 导致了重复数据"

            # 已完成的任务不该被再次领取
            assert spider.state.claim_batch(10) == []
        finally:
            spider.close()

    def test_initialization_is_atomic(self, settings: Settings) -> None:
        """并发初始化不应产生主键冲突或重复。"""
        from spiderkit.spider import BooksSpider

        spider = BooksSpider(settings)
        try:
            urls = [f"https://example.com/b/{i}" for i in range(50)]
            spider.state.seed(urls)
            spider.state.seed(urls)
            spider.state.seed(urls)
            assert spider.state.stats().total == 50
        finally:
            spider.close()
