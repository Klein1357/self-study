"""第 3 个爬虫：翻页 + 存文件 + 伪装请求头。

运行：python3 d03_pagination_csv.py

这是从"玩具"到"能用"的分水岭。真实采集任务几乎都要跨页，
结果要落成文件才能交付给客户。
"""

import csv
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# ---------- 配置 ----------
BASE_URL = "https://books.toscrape.com/"
CATALOG_URL = BASE_URL + "catalogue/page-{}.html"
# 一页 20 本，1000 本共 50 页
TOTAL_PAGES = 50
OUTPUT = Path("books.csv")
# 翻页间隔（秒）。这是爬虫的"礼貌"：不给对方服务器压力。
# 真实接单时，这个值通常设 1-3 秒，宁可慢也不能被封。
DELAY = 0.5

# ---------- 反爬应对之一：请求头 ----------
# 服务器靠 User-Agent 判断"你是浏览器还是脚本"。
# 不带这个头，requests 默认的 python-requests/2.x 会被一眼识破。
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------- 复用连接：性能关键 ----------
# 用 Session 而不是每次 requests.get()，底层会复用 TCP 连接，
# 几十页采集能快 2-3 倍。这是新手最容易忽略的性能优化。
session = requests.Session()
session.headers.update(HEADERS)

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


def parse_page(html: str) -> list[dict[str, Any]]:
    """解析一页 HTML，返回这一页所有书籍。

    Args:
        html: 页面 HTML 源码

    Returns:
        书籍字典列表，每个字典含书名/价格/评分/库存/详情页链接
    """
    soup = BeautifulSoup(html, "html.parser")
    page_books: list[dict[str, Any]] = []

    for book in soup.select("article.product_pod"):
        link = book.select_one("h3 a")["href"]
        page_books.append({
            "书名": book.select_one("h3 a")["title"],
            "价格(£)": float(
                book.select_one("p.price_color").text.replace("£", "").strip()
            ),
            "评分": RATING_MAP[book.select_one("p.star-rating")["class"][1]],
            "库存": book.select_one("p.instock.availability").text.strip(),
            # 相对链接要拼成绝对链接才能直接用
            "详情页": BASE_URL + "catalogue/" + link,
        })

    return page_books


def crawl_book_pages(total_pages: int = TOTAL_PAGES) -> list[dict[str, Any]]:
    """采集指定页数的书籍数据。

    Args:
        total_pages: 需要采集的页数

    Returns:
        所有页合并后的书籍列表
    """
    all_books: list[dict[str, Any]] = []

    for page in range(1, total_pages + 1):
        url = CATALOG_URL.format(page)
        print(f"[{page:>2}/{total_pages}] 正在采集 {url}")

        try:
            # timeout 是必须的！不加的话网络卡住，脚本会永远挂在那里。
            resp = session.get(url, timeout=15)
            # raise_for_status: 状态码不是 2xx 就抛异常，让下面的 except 接住
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding
        except requests.RequestException as exc:
            # 真实项目里，失败要记录并继续，而不是整个任务崩掉。
            print(f"    ✗ 请求失败：{exc}，跳过此页")
            continue

        page_books = parse_page(resp.text)
        all_books.extend(page_books)
        print(f"    ✓ 获取 {len(page_books)} 本，累计 {len(all_books)} 本")

        time.sleep(DELAY)  # 礼貌间隔

    return all_books


def save_to_csv(books: list[dict[str, Any]], path: Path) -> None:
    """把书籍列表写入 CSV 文件。

    Args:
        books: 书籍字典列表
        path: 输出文件路径
    """
    if not books:
        print("没有数据可保存")
        return

    # newline="" 是写 CSV 的标准做法，避免 Windows 下多出空行
    # encoding="utf-8-sig" 让 Excel 打开中文不乱码（BOM 头）
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(books[0].keys()))
        writer.writeheader()
        writer.writerows(books)

    print(f"\n已保存 {len(books)} 条数据到 {path.resolve()}")


if __name__ == "__main__":
    start = time.time()
    data = crawl_book_pages()
    save_to_csv(data, OUTPUT)
    print(f"总耗时 {time.time() - start:.1f} 秒")

    # 简单统计，交付时客户通常想看这种汇总
    if data:
        print(f"\n=== 数据概览 ===")
        print(f"书籍总数：{len(data)}")
        print(f"平均价格：£{sum(b['价格(£)'] for b in data) / len(data):.2f}")
        five_star = [b for b in data if b["评分"] == 5]
        print(f"五星好评书籍：{len(five_star)} 本")
