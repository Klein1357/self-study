"""第 5 个爬虫：抓取 JS 动态渲染的页面（Playwright）。

运行：python3 d05_dynamic_playwright.py（首次需 playwright install chromium）

为什么需要这个？
  requests 只能拿到服务器返回的原始 HTML。
  但很多现代网站的数据是浏览器执行 JavaScript 之后才渲染出来的，
  requests 抓回来只有一堆空壳，看不到内容。
  这时候必须用真实浏览器内核去跑。

判断信号：F12 里能看到内容，requests 抓回来却是空的 —— 十有八九是动态页面。
"""

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def crawl_dynamic_page(url: str) -> str:
    """用无头浏览器抓取动态渲染后的 HTML。

    Args:
        url: 目标地址

    Returns:
        渲染完成后的完整 HTML
    """
    async with async_playwright() as p:
        # headless=True 表示不弹出可见窗口（后台运行）
        # 调试时改成 False，能看到浏览器实际操作过程，非常有用
        browser = await p.chromium.launch(headless=True)

        # 用 context 而非直接 page，可以统一设置视口、UA、语言等
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = await context.new_page()

        # wait_until="networkidle" 表示等到网络请求基本停止，
        # 通常意味着页面数据已经加载完毕。这是抓动态页的关键。
        await page.goto(url, wait_until="networkidle", timeout=30000)

        # 页面结构复杂时，可以显式等某个元素出现，比死等更可靠
        # await page.wait_for_selector("div.product", timeout=10000)

        # 顺手截图，交付时给客户看"我确实抓到了"，很有说服力
        shot = Path("screenshot.png")
        await page.screenshot(path=shot, full_page=True)

        # 3) 拿到渲染后的完整 HTML —— 这一步才是 Playwright 的价值所在，
        #    这里的内容是 JS 执行完毕后的最终状态
        html = await page.content()

        await context.close()
        await browser.close()

    # 标题解析是纯 CPU 操作，不涉及 IO，所以放在 async 块外面用同步函数处理
    print(f"页面标题: {extract_title(html)}")
    print(f"HTML 长度: {len(html)} 字符（对比 requests 抓回来通常只有几 KB）")
    print(f"截图已保存: {shot.resolve()}")
    return html


def extract_title(html: str) -> str:
    """从 HTML 中提取 <title> 文本。

    Args:
        html: 页面 HTML 源码

    Returns:
        标题文本，无标题时返回 "(无标题)"
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("title")
    return tag.text.strip() if tag else "(无标题)"


if __name__ == "__main__":
    # 拿一个明确的动态站点做演示
    asyncio.run(crawl_dynamic_page("https://books.toscrape.com/"))
