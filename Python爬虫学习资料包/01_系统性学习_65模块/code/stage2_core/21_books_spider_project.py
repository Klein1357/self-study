"""阶段 2 毕业作业：完整的电商站点采集器。

运行：python3 21_books_spider_project.py

这是本阶段的综合实战。相比速成版，这里增加了：
  · 进入详情页采集更多字段（主流程 + 子流程）
  · 用 dataclass 定义数据模型
  · parsel 解析（阶段 6 Scrapy 同款）
  · 图片链接完整化处理
  · 分类遍历（多入口采集）
  · 结构化日志与失败记录

采集目标：books.toscrape.com —— 开放的练习站
产出：books_full.csv / books_full.json

实测规模：50 页 × 20 本 = 1000 本书
"""

from __future__ import annotations

import csv
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from parsel import Selector

# ============================================================
# 配置
# ============================================================
BASE_URL = "https://books.toscrape.com/"
CATALOG_URL = urljoin(BASE_URL, "catalogue/page-{}.html")
TOTAL_PAGES = 50
DELAY = 0.3                      # 请求间隔（秒）
TIMEOUT = 15

WORK_DIR = Path(__file__).parent
OUTPUT_CSV = WORK_DIR / "books_full.csv"
OUTPUT_JSON = WORK_DIR / "books_full.json"

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("books")


# ============================================================
# 数据模型
# ============================================================
@dataclass
class Book:
    """一本书的完整数据模型。

    Args:
        title: 书名
        price: 价格（英镑）
        rating: 评分 1-5
        stock: 库存数量
        category: 分类
        upc: 商品唯一编码
        image_url: 封面图链接
        detail_url: 详情页链接
        tax: 税费
        reviews: 评论数
    """

    title: str
    price: float
    rating: int
    stock: int
    category: str
    upc: str
    image_url: str
    detail_url: str
    tax: float
    reviews: int

    @property
    def price_with_tax(self) -> float:
        """含税价格。"""
        return round(self.price + self.tax, 2)


# ============================================================
# 会话管理
# ============================================================
def build_session() -> requests.Session:
    """创建配置好的 Session。

    Returns:
        带伪装请求头的 Session
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def fetch(session: requests.Session, url: str, retries: int = 3) -> str | None:
    """带重试的页面抓取。

    ⚠️ 这里有个重要的编码细节：这个站点返回的响应头没有声明 charset，
    requests 会默认按 ISO-8859-1 解码，导致 £ 变成 Â£（乱码）。
    必须手动修正编码。

    Args:
        session: 复用的 Session
        url: 目标地址
        retries: 最大重试次数

    Returns:
        HTML 文本；失败返回 None
    """
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
            if resp.status_code == 404:
                logger.warning("404: %s", url)
                return None
            resp.raise_for_status()

            # ★ 关键：修正编码（阶段 0.4 讲过的原理在这里实战）
            # 这个站点的 HTML meta 里声明了 charset=UTF-8，
            # 但响应头没声明，所以 requests 猜错了。
            # 从 content 里检测实际编码更可靠。
            if resp.encoding in (None, "ISO-8859-1"):
                resp.encoding = resp.apparent_encoding or "utf-8"

            return resp.text
        except requests.RequestException as exc:
            wait = 2 ** (attempt - 1)
            logger.warning("请求失败(%d/%d): %s —— %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(wait)
    return None


# ============================================================
# 解析：列表页
# ============================================================
def parse_list_page(html: str, category: str = "") -> list[dict[str, Any]]:
    """解析列表页，提取每本书的概览信息和详情页链接。

    Args:
        html: 列表页 HTML
        category: 当前分类

    Returns:
        书籍概览列表（含 detail_url）
    """
    sel = Selector(text=html)
    items = []

    for pod in sel.css("article.product_pod"):
        # 详情页链接需要从相对路径转成绝对路径
        relative = pod.css("h3 a::attr(href)").get("")
        detail_url = urljoin(CATALOG_URL, relative)

        # 列表页的缩略图（比详情页的全图小）
        thumb = pod.css("img::attr(src)").get("")
        image_url = urljoin(CATALOG_URL, thumb)

        rating_word = pod.css("p.star-rating::attr(class)").get("")
        # class 形如 "star-rating Three"，用正则提取更稳健
        rating_match = re.search(r"star-rating\s+(\w+)", rating_word)
        rating = RATING_MAP.get(rating_match.group(1), 0) if rating_match else 0

        # ★ 库存解析的坑：文本被 <i> 标签切成了碎片
        #   HTML 实际是：<p class="instock"><i class="icon-ok"></i>\n  In stock\n</p>
        #   所以 ::text 返回 ['\n  ', '\n  \n    In stock\n  \n'] —— 第一段是空白！
        #   解决办法：用 getall() 拿全部文本，拼起来再正则匹配
        stock_text = " ".join(pod.css("p.instock.availability ::text").getall())
        stock_match = re.search(r"(\d+)\s+available", stock_text)
        if stock_match:
            stock = int(stock_match.group(1))
        elif "In stock" in stock_text:
            # 有些情况下（比如详情页）没有具体数量，但有货
            stock = 1
        else:
            stock = 0

        items.append({
            "title": pod.css("h3 a::attr(title)").get("").strip(),
            "price": pod.css("p.price_color::text").get("").strip(),
            "rating": rating,
            "stock": stock,
            "image_url": image_url,
            "detail_url": detail_url,
            "category": category,
        })

    return items


# ============================================================
# 解析：详情页（补充字段）
# ============================================================
def parse_detail_page(html: str) -> dict[str, Any]:
    """解析详情页，提取列表页没有的字段。

    这里演示一个重要的实战技巧：详情页的字段通常存放在
    <table> 里，形如 <th>UPC</th><td>abc123</td>。
    需要把"表头和值配对"提取出来。

    ★ 同时你会发现：库存和评论数其实也能从这个表格里拿到，
      比在列表页用正则抠文本可靠得多。这是"优先进表格"的实战体现。

    Args:
        html: 详情页 HTML

    Returns:
        补充字段字典
    """
    sel = Selector(text=html)

    # 方法：拿到所有表头，再拿到所有值，按下标配对
    keys = sel.css("table.table-striped th::text").getall()
    values = sel.css("table.table-striped td::text").getall()
    info = dict(zip(keys, values))

    # ★ 评论数直接从表格拿（比找兄弟节点可靠）
    reviews = int(info.get("Number of reviews", "0") or 0)

    # ★ 库存也从这个表格里拿，格式 "In stock (22 available)"
    avail_text = info.get("Availability", "")
    avail_match = re.search(r"\((\d+)", avail_text)

    return {
        "upc": info.get("UPC", ""),
        "tax": _parse_money(info.get("Tax", "0")),
        "reviews": reviews,
        # 表格里的库存数量（比列表页解析更准确）
        "stock_detail": int(avail_match.group(1)) if avail_match else None,
        # 含税价（可以和我们自己算的对比）
        "price_incl_tax": _parse_money(info.get("Price (incl. tax)", "0")),
    }


def _parse_money(text: str) -> float:
    """从货币文本里提取数字。

    Args:
        text: 如 "£0.00"

    Returns:
        浮点数；无法解析时返回 0.0
    """
    match = re.search(r"[\d.]+", text or "")
    return float(match.group()) if match else 0.0


def _parse_price(text: str) -> float:
    """解析列表页的价格文本。

    Args:
        text: 如 "£51.77"

    Returns:
        浮点数
    """
    return _parse_money(text)


# ============================================================
# 主流程
# ============================================================
def crawl(detail: bool = False, max_pages: int = TOTAL_PAGES) -> list[Book]:
    """采集主流程。

    Args:
        detail: 是否进入详情页采集更多字段（会慢很多）
        max_pages: 最多采集页数

    Returns:
        Book 列表
    """
    session = build_session()
    books: list[Book] = []
    failed: list[str] = []

    for page in range(1, max_pages + 1):
        url = CATALOG_URL.format(page)
        logger.info("列表页 %d/%d", page, max_pages)

        html = fetch(session, url)
        if html is None:
            failed.append(url)
            continue

        items = parse_list_page(html)
        logger.info("  解析出 %d 本", len(items))

        for item in items:
            extra: dict[str, Any] = {"upc": "", "tax": 0.0, "reviews": 0,
                                     "stock_detail": None}

            # 可选：进入详情页
            if detail:
                dhtml = fetch(session, item["detail_url"])
                if dhtml:
                    extra = parse_detail_page(dhtml)
                time.sleep(DELAY)

            # ★ 详情页的库存更准确，有就用它
            final_stock = (
                extra.get("stock_detail")
                if extra.get("stock_detail") is not None
                else item["stock"]
            )

            try:
                books.append(Book(
                    title=item["title"],
                    price=_parse_price(item["price"]),
                    rating=item["rating"],
                    stock=final_stock,
                    category=item["category"] or "未分类",
                    upc=extra["upc"],
                    image_url=item["image_url"],
                    detail_url=item["detail_url"],
                    tax=extra["tax"],
                    reviews=extra["reviews"],
                ))
            except (ValueError, TypeError) as exc:
                logger.warning("构建失败，跳过《%s》: %s", item["title"], exc)

        time.sleep(DELAY)

    if failed:
        logger.warning("以下页面采集失败，共 %d 个：", len(failed))
        for f in failed:
            logger.warning("  %s", f)

    return books


# ============================================================
# 导出
# ============================================================
def export(books: list[Book]) -> None:
    """导出结果到 CSV 和 JSON。

    Args:
        books: 书籍列表
    """
    if not books:
        logger.warning("没有数据可导出")
        return

    # --- CSV ---
    fields = list(asdict(books[0]).keys())
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(b) for b in books)

    # --- JSON（含统计）---
    payload = {
        "采集时间": datetime.now().isoformat(),
        "统计": {
            "总数": len(books),
            "平均价格": round(sum(b.price for b in books) / len(books), 2),
            "总货值": round(sum(b.price * b.stock for b in books), 2),
            "五星数量": sum(1 for b in books if b.rating == 5),
            "有库存数量": sum(1 for b in books if b.stock > 0),
        },
        "数据": [asdict(b) for b in books],
    }
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("已导出: %s", OUTPUT_CSV)
    logger.info("已导出: %s", OUTPUT_JSON)


# ============================================================
# 入口
# ============================================================
def main() -> None:
    """主入口。"""
    print("=" * 66)
    print("阶段 2 毕业作业：完整电商采集器")
    print("=" * 66)
    print()

    start = time.time()

    # 默认只采 3 页做演示；把 detail=True 且 max_pages=50 就是全量采集
    books = crawl(detail=True, max_pages=3)

    elapsed = time.time() - start
    print()
    print(f"采集完成：{len(books)} 本书，耗时 {elapsed:.1f} 秒")
    print()

    if books:
        print("前 5 本预览（注意 tax/upc/reviews 来自详情页）：")
        print(f"  {'书名':<42} {'价格':>8} {'含税':>8} {'评分':>4} {'UPC':<14}")
        print("  " + "-" * 80)
        for b in books[:5]:
            title = b.title[:40]
            print(f"  {title:<42} £{b.price:>7.2f} £{b.price_with_tax:>7.2f} "
                  f"{'★' * b.rating:<4} {b.upc:<14}")

        print()
        print("统计：")
        print(f"  平均价格:  £{sum(b.price for b in books) / len(books):.2f}")
        print(f"  有库存:    {sum(1 for b in books if b.stock > 0)} 本")
        print(f"  最高评分:  {'★' * max(b.rating for b in books)}")

    export(books)

    print()
    print("=" * 66)
    print("提示：把 main() 里的 max_pages 改成 50，就是完整 1000 本采集")
    print("=" * 66)


if __name__ == "__main__":
    main()
