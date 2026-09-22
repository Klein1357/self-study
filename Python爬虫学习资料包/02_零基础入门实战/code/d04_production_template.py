"""接单级爬虫模板：可直接改造成交付项目。

运行：python3 d04_production_template.py

这个脚本演示一个"能拿去接单"的爬虫应该具备什么：
  1. 重试机制     —— 网络抖动自动重来，而不是让任务失败
  2. 断点续爬     —— 中途挂了，重跑不用从头再来
  3. 结构化日志   —— 出问题能查到是哪一条、为什么
  4. 数据校验     —— 脏数据不进结果集，交付质量有保障
  5. 去重         —— 避免同一本书重复入库
  6. 优雅退出     —— Ctrl+C 时保存已有成果

关键理念：客户付钱买的不是"能跑一次的脚本"，
而是"能稳定跑完、出问题能定位、结果可信"的解决方案。
"""

import csv
import json
import logging
import random
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ==================== 1. 日志配置 ====================
# 别再用 print 调试生产脚本了。logging 带时间戳和级别，
# 出问题时能直接定位到"什么时候、什么级别、发生了什么"。
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("spider")


# ==================== 2. 数据结构定义 ====================
@dataclass
class Book:
    """一本书的标准化数据模型。

    用 dataclass 而非裸字典的好处：
      - 字段名写错会立刻报错，不会静默产出脏数据
      - 类型清晰，团队协作不用猜
      - 转 CSV / JSON 都很方便
    """

    title: str
    price: float
    rating: int
    stock: int
    url: str

    # 唯一标识，用于去重。price 变化不算新书，所以用 url
    @property
    def key(self) -> str:
        return self.url


# ==================== 3. 配置集中管理 ====================
@dataclass
class Config:
    """所有可调参数集中在一处，改配置不用翻遍全文。"""

    base_url: str = "https://books.toscrape.com/"
    catalog_tpl: str = "https://books.toscrape.com/catalogue/page-{}.html"
    total_pages: int = 10
    delay_min: float = 0.3
    delay_max: float = 0.8
    max_retries: int = 3
    timeout: int = 15
    output_dir: Path = Path("output")
    checkpoint_file: Path = field(default_factory=lambda: Path("checkpoint.json"))


CONFIG = Config()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


# ==================== 4. 带重试的请求封装 ====================
def fetch(
    session: requests.Session,
    url: str,
    *,
    max_retries: int = CONFIG.max_retries,
) -> str | None:
    """带指数退避重试的页面抓取。

    Args:
        session: 复用的 requests 会话
        url: 目标地址
        max_retries: 最大重试次数

    Returns:
        页面 HTML 文本；全部重试失败则返回 None
    """
    for attempt in range(1, max_retries + 1):
        try:
            resp = session.get(url, timeout=CONFIG.timeout)

            # 429 = 请求太频繁，429/5xx 值得重试；404 重试没意义
            if resp.status_code == 404:
                logger.warning("404 不存在：%s", url)
                return None
            resp.raise_for_status()

            resp.encoding = resp.apparent_encoding
            return resp.text

        except requests.RequestException as exc:
            # 指数退避：1s → 2s → 4s，再叠加随机抖动避免"惊群"
            wait = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
            logger.warning(
                "请求失败(%d/%d)：%s —— %s，%.1fs 后重试",
                attempt, max_retries, url, exc, wait,
            )
            if attempt < max_retries:
                time.sleep(wait)

    logger.error("重试耗尽，放弃：%s", url)
    return None


# ==================== 5. 解析 + 数据校验 ====================
def parse_books(html: str, page_url: str) -> list[Book]:
    """解析页面，只返回通过校验的书籍。

    Args:
        html: 页面 HTML
        page_url: 页面地址，用于拼接详情页链接

    Returns:
        校验通过的 Book 列表
    """
    soup = BeautifulSoup(html, "html.parser")
    books: list[Book] = []

    for item in soup.select("article.product_pod"):
        try:
            # 库存文本形如 "In stock (22 available)"，正则抠出数字
            stock_text = item.select_one("p.instock.availability").text
            import re
            stock_match = re.search(r"(\d+)", stock_text)
            stock = int(stock_match.group(1)) if stock_match else 0

            book = Book(
                title=item.select_one("h3 a")["title"].strip(),
                price=float(
                    item.select_one("p.price_color").text.replace("£", "").strip()
                ),
                rating=RATING_MAP[item.select_one("p.star-rating")["class"][1]],
                stock=stock,
                url=CONFIG.base_url + "catalogue/" + item.select_one("h3 a")["href"],
            )

            # 数据校验：价格必须是正数、书名不能为空。
            # 脏数据在源头拦掉，比交付后被客户发现强一万倍。
            if book.price <= 0 or not book.title:
                logger.warning("数据异常，丢弃：%s", book.title)
                continue

            books.append(book)

        except (AttributeError, KeyError, ValueError) as exc:
            # 页面结构变了，或者某个字段缺失。记录但不停机。
            logger.warning("解析失败，跳过一条：%s", exc)
            continue

    return books


# ==================== 6. 断点续爬 ====================
class Checkpoint:
    """记录已完成的页码，支持中断后继续。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.done_pages: set[int] = set()
        self.books: dict[str, Book] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘恢复进度。"""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.done_pages = set(raw.get("done_pages", []))
            # Book 是 dataclass，恢复时要重新构造
            self.books = {
                k: Book(**v) for k, v in raw.get("books", {}).items()
            }
            logger.info(
                "恢复进度：已完成 %d 页，已存 %d 本书",
                len(self.done_pages), len(self.books),
            )
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("断点文件损坏，从零开始：%s", exc)

    def save(self) -> None:
        """把进度写到磁盘。"""
        payload = {
            "done_pages": sorted(self.done_pages),
            "books": {k: asdict(v) for k, v in self.books.items()},
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add(self, page: int, books: list[Book]) -> None:
        """记录一页的结果。

        Args:
            page: 页码
            books: 该页解析出的书籍
        """
        self.done_pages.add(page)
        # 用字典的 key 天然去重：同一 URL 的书只会留一份
        for b in books:
            self.books[b.key] = b

    @property
    def is_complete(self) -> bool:
        return len(self.done_pages) >= CONFIG.total_pages


# ==================== 7. 优雅退出 ====================
class GracefulExit:
    """捕获 Ctrl+C，先把进度存好再退出。"""

    def __init__(self, checkpoint: Checkpoint) -> None:
        self.checkpoint = checkpoint
        self.triggered = False
        signal.signal(signal.SIGINT, self._handler)

    def _handler(self, signum: int, frame: Any) -> None:
        logger.info("收到中断信号，正在保存进度……")
        self.checkpoint.save()
        logger.info("进度已保存，下次运行将自动续爬")
        self.triggered = True
        sys.exit(0)


# ==================== 8. 主流程 ====================
def main() -> None:
    """爬虫主入口。"""
    CONFIG.output_dir.mkdir(exist_ok=True)
    checkpoint = Checkpoint(CONFIG.checkpoint_file)
    _guard = GracefulExit(checkpoint)  # 注册信号处理

    session = requests.Session()
    session.headers.update(HEADERS)

    for page in range(1, CONFIG.total_pages + 1):
        if page in checkpoint.done_pages:
            logger.info("第 %d 页已完成，跳过", page)
            continue

        url = CONFIG.catalog_tpl.format(page)
        logger.info("采集第 %d/%d 页", page, CONFIG.total_pages)

        html = fetch(session, url)
        if html is None:
            continue

        books = parse_books(html, url)
        checkpoint.add(page, books)
        checkpoint.save()  # 每页都存，最坏情况只丢一页
        logger.info("  本页 %d 本，累计 %d 本", len(books), len(checkpoint.books))

        time.sleep(random.uniform(CONFIG.delay_min, CONFIG.delay_max))

    export(checkpoint.books.values())


def export(books: Any) -> None:
    """导出结果到 CSV 和 JSON 两种格式。

    Args:
        books: Book 可迭代对象
    """
    book_list = list(books)
    if not book_list:
        logger.warning("无数据可导出")
        return

    csv_path = CONFIG.output_dir / "books.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(book_list[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(b) for b in book_list)

    json_path = CONFIG.output_dir / "books.json"
    json_path.write_text(
        json.dumps(
            [asdict(b) for b in book_list], ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )

    logger.info("导出完成：%s / %s", csv_path, json_path)
    logger.info("总计 %d 本书", len(book_list))


if __name__ == "__main__":
    main()
