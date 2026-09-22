"""第 6 个爬虫：并发采集，把 1 小时压到 5 分钟。

运行：python3 d06_concurrent.py

单线程爬虫的瓶颈不是网络，是"等待"。
每次请求要等 0.5 秒，1000 个页面就是 500 秒纯等待，CPU 全程在睡觉。
并发就是让这些等待重叠起来。

本脚本演示：线程池（适合 IO 密集的 requests 场景，最容易上手）。
⚠️ 并发不等于更快是无限的 —— 并发数开太大等于 DDoS，会被封。
   经验值：5-10 个并发，配合随机延迟，是接单的稳妥区间。
"""

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

import requests
from bs4 import BeautifulSoup

BASE = "https://books.toscrape.com/catalogue/page-{}.html"
TOTAL_PAGES = 20
MAX_WORKERS = 5  # 并发数：先从 5 开始，被限流就降到 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

RATING_MAP = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}


def fetch_and_parse(page: int) -> tuple[int, list[dict[str, Any]]]:
    """抓取并解析单页，供线程池调用。

    Args:
        page: 页码

    Returns:
        (页码, 该页书籍列表) 元组
    """
    url = BASE.format(page)
    # ⚠️ 线程安全：每个线程用独立的 Session，
    #    共享 Session 在高并发下可能出问题。
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding
    except requests.RequestException as exc:
        print(f"  ✗ 第 {page} 页失败：{exc}")
        return page, []

    soup = BeautifulSoup(resp.text, "html.parser")
    books = []
    for item in soup.select("article.product_pod"):
        books.append({
            "page": page,
            "title": item.select_one("h3 a")["title"],
            "price": float(
                item.select_one("p.price_color").text.replace("£", "").strip()
            ),
            "rating": RATING_MAP[item.select_one("p.star-rating")["class"][1]],
        })

    # 每个线程随机睡一下，打散请求节奏，比固定间隔更不容易被识别
    time.sleep(random.uniform(0.2, 0.6))
    return page, books


def crawl_concurrent(total_pages: int = TOTAL_PAGES) -> list[dict[str, Any]]:
    """并发采集多页。

    Args:
        total_pages: 总页数

    Returns:
        所有书籍数据
    """
    all_books: list[dict[str, Any]] = []

    # ThreadPoolExecutor 管理线程池，max_workers 控制同时进行的任务数
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # submit 把任务丢进池子，立刻返回 Future（可以理解为"未来的结果凭据"）
        futures = {
            executor.submit(fetch_and_parse, p): p
            for p in range(1, total_pages + 1)
        }

        # as_completed：谁先做完谁先处理，不用按提交顺序干等
        for future in as_completed(futures):
            page, books = future.result()
            all_books.extend(books)
            print(f"  ✓ 第 {page:>2} 页完成，{len(books)} 本")

    return all_books


if __name__ == "__main__":
    print("=== 单线程基准 ===")
    start = time.time()
    # 只跑 3 页做对比，避免太久
    for p in range(1, 4):
        fetch_and_parse(p)
    single_time = time.time() - start
    print(f"3 页单线程耗时：{single_time:.1f} 秒\n")

    print(f"=== 并发（{MAX_WORKERS} 线程）采集 {TOTAL_PAGES} 页 ===")
    start = time.time()
    data = crawl_concurrent()
    conc_time = time.time() - start

    print(f"\n采集 {TOTAL_PAGES} 页共 {len(data)} 本，耗时 {conc_time:.1f} 秒")
    est_single = conc_time * MAX_WORKERS * 0.85  # 粗略估算单线程所需时间
    print(f"估算单线程需 {est_single:.0f} 秒，提速约 {est_single / conc_time:.1f} 倍")

    if data:
        avg = sum(b["price"] for b in data) / len(data)
        print(f"平均价格：£{avg:.2f}")
