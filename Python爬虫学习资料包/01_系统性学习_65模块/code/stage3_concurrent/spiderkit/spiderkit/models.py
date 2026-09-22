"""
spiderkit 数据模型与解析模块
============================
对应网页章节：#s2-5 #s3-9

解析与数据模型分离：
  · Book —— 纯数据模型（dataclass），不含解析逻辑
  · parse_list_page / parse_detail_page —— 纯函数，输入 HTML 输出模型
这样解析逻辑可以脱离网络单独测试（见 tests/）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from parsel import Selector

log = logging.getLogger("spiderkit.parser")

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}

# books.toscrape 的价格是 £xx.xx，且受 encoding 问题影响可能出现 Â£
PRICE_RE = re.compile(r"([\d.]+)")


# ============================================================
# 数据模型
# ============================================================
@dataclass
class Book:
    """
    书籍数据模型。

    所有字段都有明确类型和默认值，方便 CSV/JSON 导出与校验。
    """

    title: str
    price: Decimal = Decimal("0")
    currency: str = "GBP"
    rating: int = 0
    in_stock: bool = False
    stock_count: int = 0
    category: str = ""
    upc: str = ""
    tax: Decimal = Decimal("0")
    reviews: int = 0
    url: str = ""
    availability: str = ""

    def to_dict(self) -> dict[str, Any]:
        """
        转字典（Decimal 转 float，便于写 CSV/JSON）。

        Returns:
            可序列化字典。
        """
        d = asdict(self)
        d["price"] = float(self.price)
        d["tax"] = float(self.tax)
        return d

    @property
    def price_with_tax(self) -> Decimal:
        """含税价。"""
        return self.price + self.tax


@dataclass
class ListItem:
    """列表页上的条目（只需 title/price/url/rating 即可进一步抓详情）。"""

    title: str
    price: Decimal
    rating: int
    url: str


@dataclass
class ParseReport:
    """解析结果与统计。"""

    items: list[Any] = field(default_factory=list)
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# ============================================================
# 工具函数
# ============================================================
def fix_encoding(text: str) -> str:
    """
    修正 Â£ / Â 这类常见的 latin-1 误码。

    原理：UTF-8 的 £ 是 C2 A3 两个字节，被当作 latin-1 解码就得到 Â£。

    Args:
        text: 待修正文本。

    Returns:
        修正后的文本。
    """
    if "Â" not in text:
        return text
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text.replace("Â£", "£").replace("Â", "")


def parse_price(raw: str) -> Decimal:
    """
    从价格文本中提取数值。

    Args:
        raw: 形如 "£51.77" 或 "Â£51.77" 的文本。

    Returns:
        Decimal 价格；解析失败返回 Decimal('0')。
    """
    cleaned = fix_encoding(raw)
    m = PRICE_RE.search(cleaned)
    if not m:
        return Decimal("0")
    try:
        return Decimal(m.group(1))
    except InvalidOperation:
        return Decimal("0")


def parse_stock(raw_el: Selector | None) -> tuple[bool, int]:
    """
    解析库存信息。

    库存节点的结构是：
        <p class="instock availability"><i ...></i>\n In stock (22 available)</p>
    —— 文本被 <i> 切成碎片，必须拼接所有文本节点。

    Args:
        raw_el: parsel 的 Selector 或 SelectorList。

    Returns:
        (是否有货, 库存数量)
    """
    if raw_el is None:
        return False, 0
    # 关键：用 " " 连接，让碎片拼成完整句子
    text = " ".join(raw_el.css("::text").getall())
    text = fix_encoding(" ".join(text.split()))
    in_stock = "In stock" in text
    m = re.search(r"(\d+)\s+available", text)
    return in_stock, int(m.group(1)) if m else 0


# ============================================================
# 解析函数
# ============================================================
def parse_list_page(html: str, base_url: str) -> ParseReport:
    """
    解析列表页，提取所有条目的基本信息。

    Args:
        html: 列表页 HTML。
        base_url: 用于把相对链接转绝对链接。

    Returns:
        ParseReport，items 为 ListItem 列表。
    """
    from urllib.parse import urljoin

    report = ParseReport()
    sel = Selector(text=html)

    for pod in sel.css("article.product_pod"):
        try:
            a = pod.css("h3 a")
            title = (a.attrib.get("title") or "").strip()
            href = a.attrib.get("href") or ""
            price = parse_price("".join(pod.css("p.price_color ::text").getall()))
            star = (pod.css("p.star-rating::attr(class)").get() or "")
            rating_word = star.replace("star-rating", "").strip()
            rating = RATING_MAP.get(rating_word, 0)

            if not title or not href:
                report.skipped += 1
                continue

            report.items.append(ListItem(
                title=title,
                price=price,
                rating=rating,
                url=urljoin(base_url, href),
            ))
        except Exception as e:                      # noqa: BLE001
            report.skipped += 1
            report.errors.append(f"{type(e).__name__}: {e}")
            log.debug("列表页条目解析失败：%s", e)

    return report


def parse_detail_page(html: str, url: str = "", category: str = "") -> Book | None:
    """
    解析详情页，生成完整 Book 对象。

    Args:
        html: 详情页 HTML。
        url: 详情页地址。
        category: 所属分类（可从面包屑提取）。

    Returns:
        Book 对象；完全无法解析时返回 None。
    """
    sel = Selector(text=html)

    title = (sel.css("h1::text").get() or "").strip()
    if not title:
        log.debug("详情页无标题，跳过：%s", url)
        return None

    # 产品信息表：所有键值对都在 th/td 里
    info: dict[str, str] = {}
    for row in sel.css("table.table-striped tr"):
        key = (row.css("th::text").get() or "").strip()
        val = " ".join(" ".join(row.css("td ::text").getall()).split())
        if key:
            info[key] = val

    price = parse_price("".join(sel.css("p.price_color ::text").getall()))
    in_stock, stock_count = parse_stock(sel.css("p.instock.availability"))
    star = sel.css("p.star-rating::attr(class)").get() or ""
    rating = RATING_MAP.get(star.replace("star-rating", "").strip(), 0)

    # 分类：从面包屑取
    if not category:
        crumbs = sel.css("ul.breadcrumb li a::text").getall()
        category = crumbs[-1].strip() if crumbs else ""

    # 评论数：优先从信息表拿（比找兄弟节点可靠得多）
    reviews = 0
    if "Number of reviews" in info:
        m = re.search(r"(\d+)", info["Number of reviews"])
        reviews = int(m.group(1)) if m else 0

    return Book(
        title=title,
        price=price,
        currency="GBP",
        rating=rating,
        in_stock=in_stock,
        stock_count=stock_count,
        category=category,
        upc=info.get("UPC", ""),
        tax=parse_price(info.get("Tax", "0")),
        reviews=reviews,
        url=url,
        availability=info.get("Availability", ""),
    )


def extract_book_links(html: str, base_url: str) -> list[str]:
    """
    从详情页提取"同类推荐"链接。

    Args:
        html: 详情页 HTML。
        base_url: 基础地址。

    Returns:
        绝对 URL 列表。
    """
    from urllib.parse import urljoin

    sel = Selector(text=html)
    hrefs = sel.css("#promotions h3 a::attr(href)").getall()
    return [urljoin(base_url, h) for h in hrefs]
