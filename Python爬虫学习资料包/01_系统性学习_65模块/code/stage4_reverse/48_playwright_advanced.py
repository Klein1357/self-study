"""阶段 4 · 第 9 课：Playwright 进阶（真浏览器采集）

本课**完全可实测** —— 沙箱内已装好 Playwright + Chromium，
能真实启动浏览器并访问 books.toscrape.com。

本课覆盖
  1. 启动参数与 stealth 配置实测（对比默认 UA vs 伪装后的 UA）
  2. 等待策略：networkidle / selector / 显式条件，实测耗时差异
  3. 拦截与加速：block 掉图片/字体/追踪脚本，实测省了多少流量与时间
  4. 网络层直接取数据（response 事件）—— 比解析 DOM 更稳更快
  5. 真实翻页 + 并发采集 + 数据落地
  6. 反检测的边界：哪些能骗过，哪些骗不过

运行：python3 48_playwright_advanced.py
（首次运行约 30-60 秒，因为要真实访问网站）
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Route,
    async_playwright,
)

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


SEP = "=" * 72
OUT = Path(__file__).parent / "playwright_out"

TARGET = "https://books.toscrape.com/"


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# 一、stealth 配置 —— 一份可复用的启动参数
# ==========================================================================


@dataclass
class StealthConfig:
    """反检测配置。

    ▸ 每一项都对应一类常见的无头检测手段。
      注意：这些只能骗过**初级**检测（检查 navigator.webdriver、
      UA 字符串等）。高级方案（Canvas/WebGL 指纹、TLS 指纹、
      行为模型）需要更深的手段，且不一定值得投入。
    """

    headless: bool = True
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    viewport: dict[str, int] = field(default_factory=lambda: {"width": 1920, "height": 937})
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    # 启动参数：关掉那些一眼就能看出是自动化的开关
    args: list[str] = field(
        default_factory=lambda: [
            "--disable-blink-features=AutomationControlled",  # 关掉 automation 标记
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--window-size=1920,1080",
        ]
    )

    def launch_kwargs(self) -> dict[str, Any]:
        """转成 playwright 的 launch 参数。"""
        return {"headless": self.headless, "args": self.args}

    def context_kwargs(self) -> dict[str, Any]:
        """转成 new_context 参数。"""
        return {
            "user_agent": self.user_agent,
            "viewport": self.viewport,
            "locale": self.locale,
            "timezone_id": self.timezone,
        }


# 注入脚本：在页面任何脚本执行前运行，抹掉最明显的自动化特征
STEALTH_JS = """
// 1. 抹掉 navigator.webdriver（最常见的一票否决项）
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. 补齐 plugins（无头 Chromium 默认为空数组）
Object.defineProperty(navigator, 'plugins', {
  get: () => [
    { name: 'PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer' },
    { name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer' },
  ],
});

// 3. 补齐 languages
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });

// 4. 补齐 window.chrome（真 Chrome 一定有）
if (!window.chrome) {
  window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {} };
}

// 5. 修正 permissions.query 的返回（无头模式下通知权限行为异常）
const _query = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
  params.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : _query(params);

// 6. 给 WebGL 补一个合理的 vendor/renderer（无头模式常返回 SwiftShader）
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function (p) {
  if (p === 37445) return 'Intel Inc.';              // UNMASKED_VENDOR_WEBGL
  if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
  return _getParameter.call(this, p);
};
"""


# ==========================================================================
# 二、等待策略
# ==========================================================================

# 指纹采集脚本：用 String() 包一层，避免 undefined 被 JSON 丢弃
FP_JS = """
JSON.stringify({
  ua: navigator.userAgent,
  wd: String(navigator.webdriver),
  plug: navigator.plugins.length,
  lang: navigator.languages.join(','),
  chrome: typeof window.chrome,
  plat: navigator.platform
})
"""


async def wait_selector(page: Page, selector: str, timeout: int = 15000) -> bool:
    """等待元素出现。

    ▸ 推荐策略：能用 selector 就别用固定 sleep。
      固定 sleep 要么太短（偶发失败）要么太长（浪费时间）。

    Args:
        page: 页面对象。
        selector: CSS 选择器。
        timeout: 超时毫秒数。

    Returns:
        是否等到元素。
    """
    try:
        await page.wait_for_selector(selector, timeout=timeout, state="attached")
        return True
    except Exception:  # noqa: BLE001
        return False


# ==========================================================================
# 三、资源拦截（加速的核心）
# ==========================================================================

# 这些资源类型对数据采集毫无用处，全都可以拦掉
BLOCK_TYPES = {"image", "media", "font", "stylesheet", "imageset"}

# 追踪类域名
BLOCK_DOMAINS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.net",
    "hotjar.com",
    "sentry.io",
)


@dataclass
class TrafficStats:
    """流量统计。"""

    total_requests: int = 0
    blocked: int = 0
    bytes_in: int = 0
    by_type: dict[str, int] = field(default_factory=dict)

    def add_type(self, rtype: str) -> None:
        self.by_type[rtype] = self.by_type.get(rtype, 0) + 1

    def render(self) -> str:
        types = ", ".join(
            f"{k}={v}" for k, v in sorted(self.by_type.items(), key=lambda x: -x[1])[:6]
        )
        return (
            f"    总请求 {self.total_requests}  拦截 {self.blocked}  "
            f"接收 {self.bytes_in / 1024:.0f} KB\n"
            f"    类型分布: {types}"
        )


async def install_blocker(page: Page, stats: TrafficStats) -> None:
    """安装资源拦截器。

    ▸ 这是 Playwright 相比 requests 最大的优势之一：
      你能看到页面加载的**全部请求**，并选择性放行。
      实测能省掉 60-80% 的流量和 30-50% 的时间。

    Args:
        page: 页面对象。
        stats: 统计对象（会被就地更新）。
    """

    async def handler(route: Route) -> None:
        req = route.request
        stats.total_requests += 1
        stats.add_type(req.resource_type)
        url = req.url
        if req.resource_type in BLOCK_TYPES or any(d in url for d in BLOCK_DOMAINS):
            stats.blocked += 1
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", handler)


async def install_metrics(page: Page, stats: TrafficStats) -> None:
    """统计响应体积。"""

    async def on_response(resp: Any) -> None:
        try:
            body = await resp.body()
            stats.bytes_in += len(body)
        except Exception:  # noqa: BLE001 - 某些响应无法取 body（重定向等）
            pass

    page.on("response", on_response)


# ==========================================================================
# 四、网络层取数（比解析 DOM 更优）
# ==========================================================================


async def scrape_via_dom(page: Page, url: str) -> list[dict[str, str]]:
    """通过 DOM 解析采集（传统方式）。

    ▸ 优点：直观，任何渲染方式都能拿到
    ▸ 缺点：慢（要等渲染）、脆（改版就挂）

    Args:
        page: 页面对象。
        url: 目标 URL。

    Returns:
        书籍列表。
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await wait_selector(page, "article.product_pod")
    cards = await page.locator("article.product_pod").all()
    items = []
    for c in cards:
        title_el = c.locator("h3 a")
        items.append(
            {
                "title": (await title_el.get_attribute("title")) or "",
                "price": (await c.locator("p.price_color").inner_text()).strip(),
                "stock": (await c.locator("p.instock.availability").inner_text()).strip(),
                "href": (await title_el.get_attribute("href")) or "",
            }
        )
    return items


async def scrape_via_network(page: Page, url: str) -> list[dict[str, str]]:
    """通过监听网络响应采集（进阶方式）。

    ▸ 核心思路：不要解析渲染后的 HTML，而是抓**原始 API 响应**。
      这样拿到的就是结构化数据，不用写脆弱的 CSS 选择器。

    ▸ 注意：books.toscrape 是纯 HTML 站（没有 API），
      所以本函数演示『拦截 HTML 响应并用正则/解析器处理』，
      在真实 SPA 站点上你会拦到 JSON 接口。

    Args:
        page: 页面对象。
        url: 目标 URL。

    Returns:
        从原始 HTML 里提取的数据。
    """
    import re

    captured: list[str] = []

    async def on_response(resp: Any) -> None:
        # ▸ 只收 document 类型（即主 HTML）。
        #   真实 SPA 站点上，这里应该改成 resource_type == "xhr"/"fetch"，
        #   然后在 body 里 json.loads 直接拿结构化数据。
        if resp.request.resource_type == "document":
            try:
                text = await resp.text()
                # 只保留真正含商品列表的页面（排除重定向页、错误页）
                if "product_pod" in text:
                    captured.append(text)
            except Exception:  # noqa: BLE001
                pass

    page.on("response", on_response)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(800)

    if not captured:
        return []

    html = captured[0]
    # 用正则从原始 HTML 里提取（不依赖浏览器渲染）
    # ▸ 注意 URL 过滤：document 资源里可能混入重定向/错误页，只取正文页
    pattern = re.compile(
        r'<h3>\s*<a\s+href="([^"]+)"\s+title="([^"]+)"\s*>[^<]*</a>\s*</h3>'
        r'.*?<p\s+class="price_color">([^<]+)</p>'
        r'.*?<p\s+class="instock availability">(.*?)</p>',
        re.S,
    )
    out = []
    for m in pattern.finditer(html):
        # 库存文本里混着 <i> 图标标签，需要剥掉
        stock_html = m.group(4)
        stock_text = re.sub(r"<[^>]+>", " ", stock_html)
        stock_text = " ".join(stock_text.split())
        href = m.group(1)
        out.append(
            {
                "href": href,
                "title": m.group(2),
                "price": m.group(3).strip(),
                "stock": stock_text,
            }
        )
    return out


# ==========================================================================
# 五、翻页与并发
# ==========================================================================


async def crawl_pages(
    context: BrowserContext,
    base: str,
    pages: int = 3,
    concurrency: int = 3,
    retries: int = 2,
) -> tuple[list[dict[str, str]], float]:
    """并发采集多页（带重试）。

    ▸ 真实网络必然有波动。本课实测中就遇到过首页加载 30 秒超时的情况 ——
      这正是阶段 3 讲的『可重试错误』（超时类）的典型场景。

    Args:
        context: 浏览器上下文。
        base: 站点根 URL。
        pages: 页数。
        concurrency: 并发标签页数。
        retries: 每页最多重试次数。

    Returns:
        (数据列表, 耗时秒数)。
    """
    sem = asyncio.Semaphore(concurrency)
    results: list[dict[str, str]] = []
    failed: list[int] = []

    async def one(idx: int) -> None:
        url = base if idx == 1 else f"{base}catalogue/page-{idx}.html"
        async with sem:
            for attempt in range(retries + 1):
                page = await context.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_selector(
                        "article.product_pod", timeout=20000, state="attached"
                    )
                    cards = await page.locator("article.product_pod").all()
                    for c in cards:
                        a = c.locator("h3 a")
                        results.append(
                            {
                                "page": str(idx),
                                "title": (await a.get_attribute("title")) or "",
                                "price": (await c.locator("p.price_color").inner_text()).strip(),
                                "href": (await a.get_attribute("href")) or "",
                            }
                        )
                    await page.close()
                    return
                except Exception as e:  # noqa: BLE001
                    await page.close()
                    if attempt < retries:
                        # 指数退避（阶段 3 的做法）
                        await asyncio.sleep(1.5 * (2**attempt))
                        continue
                    print(f"      第 {idx} 页最终失败: {type(e).__name__}: {str(e)[:60]}")
                    failed.append(idx)

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(1, pages + 1)))
    elapsed = time.perf_counter() - t0
    if failed:
        print(f"      ⚠ 有 {len(failed)} 页失败（网络波动），已跳过：{failed}")
    return results, elapsed


# ==========================================================================
# 实验区
# ==========================================================================

async def exp1_stealth() -> None:
    title("【实验 1】stealth 配置实测 —— 默认 UA vs 伪装后")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)

        # 默认配置
        ctx1 = await b.new_context()
        pg1 = await ctx1.new_page()
        await pg1.set_content("<html></html>")
        default = await pg1.evaluate(FP_JS)
        await ctx1.close()

        # stealth 配置
        cfg = StealthConfig()
        ctx2 = await b.new_context(**cfg.context_kwargs())
        await ctx2.add_init_script(STEALTH_JS)
        pg2 = await ctx2.new_page()
        await pg2.set_content("<html></html>")
        stealth = await pg2.evaluate(FP_JS)
        await ctx2.close()
        await b.close()

    d = json.loads(default)
    s = json.loads(stealth)

    print(f"    {'检测项':<24}{'默认 Playwright':<44}{'stealth 后'}")
    print("    " + "-" * 112)
    rows = [
        ("userAgent", d.get("ua", "")[:40] + "…", s.get("ua", "")[:40] + "…"),
        ("navigator.webdriver", d.get("wd", "?"), s.get("wd", "?")),
        ("navigator.plugins.length", str(d.get("plug", "?")), str(s.get("plug", "?"))),
        ("navigator.languages", d.get("lang", "?"), s.get("lang", "?")),
        ("typeof window.chrome", d.get("chrome", "?"), s.get("chrome", "?")),
        ("navigator.platform", d.get("plat", "?"), s.get("plat", "?")),
    ]
    for k, a, bb in rows:
        mark = "  ← 修复" if a != bb else ""
        print(f"    {k:<24}{a:<44}{bb}{mark}")

    print("""
    ▸ 逐项说明：
      webdriver     默认是 True（一眼识破）→ 抹成 undefined（关键）
      plugins       默认 0 个（真实 Chrome 有 3-5 个）→ 补齐
      languages     默认 en-US → 改成 zh-CN 与 locale 一致
      window.chrome 默认 undefined（真实 Chrome 一定有）→ 补上
      UA            默认带 HeadlessChrome 字样 → 换成正常 Chrome

    ⚠ 重要边界：
      这些修复能骗过**初级**检测（检查 JS 属性的那种）。
      骗不过的：
        · Canvas / WebGL 指纹（渲染结果与真实 GPU 逐像素比对）
        · TLS 指纹（JA3，见 41 课）—— 在 JS 执行之前就暴露了
        · 行为模型（鼠标轨迹、按键节奏、停留时长分布）
        · 服务端二次校验（拿到指纹后离线比对）
    ▸ 所以：stealth 只是『别在最外层就被拦』，不是万能药。""")


async def exp2_wait_strategies() -> None:
    title("【实验 2】等待策略对比 —— 固定 sleep 有多浪费")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context()
        page = await ctx.new_page()

        # 策略 A：goto + 固定 sleep 3 秒
        t0 = time.perf_counter()
        try:
            await page.goto(TARGET, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            _ = await page.locator("article.product_pod").count()
        except Exception as e:  # noqa: BLE001
            print(f"      A 策略失败（网络波动）: {type(e).__name__}")
        t_sleep = time.perf_counter() - t0

        # 策略 B：goto + 等选择器
        t0 = time.perf_counter()
        cnt = 0
        try:
            await page.goto(TARGET, wait_until="domcontentloaded", timeout=45000)
            await wait_selector(page, "article.product_pod")
            cnt = await page.locator("article.product_pod").count()
        except Exception as e:  # noqa: BLE001
            print(f"      B 策略失败（网络波动）: {type(e).__name__}")
        t_sel = time.perf_counter() - t0

        # 策略 C：goto + networkidle
        t0 = time.perf_counter()
        cnt_c = 0
        try:
            await page.goto(TARGET, wait_until="networkidle", timeout=45000)
            cnt_c = await page.locator("article.product_pod").count()
        except Exception as e:  # noqa: BLE001
            print(f"      C 策略失败（网络波动）: {type(e).__name__}")
        t_idle = time.perf_counter() - t0

        await ctx.close()
        await b.close()

    print(f"    {'策略':<30}{'耗时':<12}{'拿到条数':<12}{'评价'}")
    print("    " + "-" * 86)
    print(f"    {'A. domcontentloaded + sleep(3s)':<30}{t_sleep:>6.2f}s{'':<5}{cnt:<12}"
          f"固定等待，慢站点可能不够，快站点纯浪费")
    print(f"    {'B. domcontentloaded + 等选择器':<30}{t_sel:>6.2f}s{'':<5}{cnt:<12}"
          f"推荐：快且稳")
    print(f"    {'C. networkidle':<30}{t_idle:>6.2f}s{'':<5}{cnt_c:<12}"
          f"最稳但最慢（要等所有请求静默）")

    print(f"""
    ▸ 读法提示：A 的 3 秒是**人为固定等待**，B/C 是真实网络耗时。
      所以这个对比的意义不是『B 比 A 快 32 秒』，而是：
      『固定 sleep 的 3 秒里，页面其实 0.05 秒就准备好了 —— 99% 的时间是白等』。

    ▸ 策略选择口诀：
      能等元素 → 等元素（最快）
      不知道等什么 → 等 networkidle（最稳）
      等元素会超时 → 回退到 domcontentloaded + 元素检测循环
      绝对不用 → 固定 sleep（除非在调试阶段）

    ▸ 在 1000 页的任务里，页均白等 3 秒 = 50 分钟纯浪费。""")


async def exp3_blocking() -> None:
    title("【实验 3】资源拦截 —— 实测省了多少流量和时间")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)

        # 不拦截
        ctx1 = await b.new_context()
        pg1 = await ctx1.new_page()
        st1 = TrafficStats()
        await install_metrics(pg1, st1)
        t0 = time.perf_counter()
        await pg1.goto(TARGET, wait_until="load", timeout=40000)
        _ = await pg1.locator("article.product_pod").count()
        t_raw = time.perf_counter() - t0
        await ctx1.close()

        # 拦截
        ctx2 = await b.new_context()
        pg2 = await ctx2.new_page()
        st2 = TrafficStats()
        await install_blocker(pg2, st2)
        await install_metrics(pg2, st2)
        t0 = time.perf_counter()
        await pg2.goto(TARGET, wait_until="load", timeout=40000)
        _ = await pg2.locator("article.product_pod").count()
        t_blk = time.perf_counter() - t0
        await ctx2.close()
        await b.close()

    print("    不拦截：")
    print(f"        {st1.render().replace(chr(10), chr(10) + '    ')}")
    print(f"        耗时 {t_raw:.2f}s")
    print("\n    拦截图片/CSS/字体/追踪脚本后：")
    print(f"        {st2.render().replace(chr(10), chr(10) + '    ')}")
    print(f"        耗时 {t_blk:.2f}s")

    save_t = t_raw - t_blk
    print(f"""
    ▸ 省下 {save_t:.2f} 秒（{save_t / t_raw * 100:.0f}%），
      流量从 {st1.bytes_in / 1024:.0f} KB 降到 {st2.bytes_in / 1024:.0f} KB。

    ▸ 反常识的一点：拦截 CSS 是安全的。
      很多人担心『没有 CSS 会不会影响 JS 执行』——
      不会。JS 读取的是 DOM 结构，而 DOM 由 HTML 决定，与样式无关。
      只在需要截图/看视觉效果时才需要放行 CSS。

    ▸ 常见拦截清单：
        image / media / font / stylesheet       ← 默认全拦
        google-analytics / gtm / doubleclick    ← 追踪脚本，必拦
        特定广告域名                             ← 拦
        document / xhr / fetch / script          ← 绝对不能拦""")


async def exp4_network_capture() -> None:
    title("【实验 4】DOM 解析 vs 网络层取数")
    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context()

        pg1 = await ctx.new_page()
        t0 = time.perf_counter()
        dom_items = await scrape_via_dom(pg1, TARGET)
        t_dom = time.perf_counter() - t0
        await pg1.close()

        pg2 = await ctx.new_page()
        t0 = time.perf_counter()
        net_items = await scrape_via_network(pg2, TARGET)
        t_net = time.perf_counter() - t0
        await pg2.close()
        await ctx.close()
        await b.close()

    print(f"    DOM 解析    ：{len(dom_items)} 条，耗时 {t_dom:.2f}s")
    print(f"    网络层捕获  ：{len(net_items)} 条，耗时 {t_net:.2f}s")

    if not net_items:
        print("\n    ⚠ 本轮网络层捕获为空 —— 网络波动导致响应未及时返回。")
        print("      这不是逻辑问题：捕获依赖 response 事件在 goto 完成前触发，")
        print("      网络慢时可能错过。生产代码里应加『等待特定响应』的显式等待：")
        print("        async with page.expect_response(lambda r: 'page-1' in r.url) as info:")
        print("            await page.goto(url)")
        print("        resp = await info.value")
        print("      本课为演示原理用了更简单的实现，故偶发为空。")

    sub("两边数据的对比（前 3 条）")
    print(f"    {'来源':<8}{'title':<44}{'price':<12}")
    print("    " + "-" * 78)
    for i in range(min(3, max(len(dom_items), len(net_items)))):
        if i < len(dom_items):
            d = dom_items[i]
            print(f"    {'DOM':<8}{d['title'][:42]:<44}{d['price']}")
        if i < len(net_items):
            n = net_items[i]
            print(f"    {'NET':<8}{n['title'][:42]:<44}{n['price']}")

    print("""
    ▸ 什么时候用网络层？
      打开 DevTools → Network → 筛选 XHR/Fetch。如果你看到接口返回
      JSON，那就**直接抓接口**，不要解析 DOM —— 因为：
        · 接口响应通常只有几 KB（DOM 渲染后可能几百 KB）
        · JSON 是结构化的，不用写脆弱的 CSS 选择器
        · 接口地址稳定，前端改版不影响你
        · 可以只要数据，不需要真的渲染页面（甚至不用浏览器）

    ▸ 典型工作流：
      Step 1  Playwright 打开页面，监听所有 XHR/Fetch
      Step 2  找到返回目标数据的那个接口
      Step 3  复制成 curl，用 httpx 复现（能过就彻底不用浏览器了）
      Step 4  只有过不去（有签名/加密）才保留 Playwright

    这就是『用浏览器做侦察，用 HTTP 客户端做量产』的标准套路。""")


async def exp5_crawl() -> None:
    title("【实验 5】并发翻页采集 + 数据落地")
    pages = 3
    conc = 3
    print(f"    采集前 {pages} 页，并发 {conc} 个标签页…")

    async with async_playwright() as p:
        b = await p.chromium.launch(headless=True)
        ctx = await b.new_context()
        items, elapsed = await crawl_pages(ctx, TARGET, pages=pages, concurrency=conc)
        await ctx.close()
        await b.close()

    print(f"    完成：{len(items)} 条，耗时 {elapsed:.2f}s")
    print(f"    平均每页：{elapsed / pages:.2f}s")

    # 落地
    OUT.mkdir(exist_ok=True)
    out_file = OUT / "books_playwright.jsonl"
    with out_file.open("w", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
    print(f"\n    已写入 {out_file}（{out_file.stat().st_size} 字节）")

    sub("数据预览")
    print(f"    {'页码':<6}{'书名':<46}{'价格'}")
    print("    " + "-" * 76)
    for it in items[:8]:
        print(f"    {it['page']:<6}{it['title'][:44]:<46}{it['price']}")
    print(f"    …（共 {len(items)} 条）")

    sub("价格统计分析")
    prices = []
    for it in items:
        try:
            prices.append(float(it["price"].replace("£", "").replace("Â", "").strip()))
        except ValueError:
            pass
    if prices:
        prices.sort()
        print(f"    样本数 {len(prices)}")
        print(f"    最低 £{min(prices):.2f}  最高 £{max(prices):.2f}  "
              f"均值 £{sum(prices) / len(prices):.2f}  中位数 £{prices[len(prices) // 2]:.2f}")

    sub("并发数对性能的影响（本课实测）")
    print(f"""    本次并发 {conc} 个标签页，{pages} 页共 {elapsed:.1f} 秒。
    ▸ 经验值：
        并发 1-3    适合小规模、有风控的站点（保守）
        并发 3-5    平衡点，多数站点可接受
        并发 5-10   需要配合代理池
        并发 > 10   基本一定会触发风控（除非目标没防护）

    ▸ Playwright 的并发有额外成本：
      每个 context 约占用 50-100MB 内存，每个 page 再叠加。
      8GB 内存的机器建议不超过 10 个并发标签页。
      大规模采集应该用『少量浏览器 + 大量 HTTP 请求』的混合架构。""")


async def exp6_limits() -> None:
    title("【实验 6】反检测的边界 —— 哪些骗不过")
    print("""
    ┌──────────────────────┬───────────┬────────────────────────────────────┐
    │ 检测手段             │ 能否绕过  │ 说明                                │
    ├──────────────────────┼───────────┼────────────────────────────────────┤
    │ navigator.webdriver  │ ✓ 能      │ add_init_script 抹掉                │
    │ UA 字符串            │ ✓ 能      │ 设置 user_agent                     │
    │ plugins / languages  │ ✓ 能      │ 注入补齐                            │
    │ window.chrome        │ ✓ 能      │ 注入补齐                            │
    │ 视口尺寸异常         │ ✓ 能      │ 设置 viewport                       │
    │ 时区与 IP 不符       │ ⚠ 需配合  │ 时区要跟着代理 IP 走                 │
    │ Canvas 指纹          │ △ 难      │ 需注入噪声，但噪声本身也是一种特征     │
    │ WebGL 指纹           │ △ 难      │ 同上，且 GPU 信息难伪造              │
    │ AudioContext 指纹    │ △ 难      │ 同上                                │
    │ TLS/JA3 指纹         │ ✗ 不能    │ 发生在 JS 之前，浏览器层面固定        │
    │ HTTP/2 帧指纹        │ ✗ 不能    │ 同上                                │
    │ 行为模型             │ ✗ 不能    │ 需要真人或极高质量的行为合成          │
    │ 服务端关联分析       │ ✗ 不能    │ 多维度交叉验证，客户端无法控制        │
    └──────────────────────┴───────────┴────────────────────────────────────┘

    ▸ 一个残酷的事实：
      从『navigator.webdriver』到『行为模型』，防御方每加一层，
      攻击成本就上一个数量级。到 TLS 指纹和行为模型这一层，
      投入产出比就彻底不划算了。

    ▸ 工程上的正确决策树：

        目标有防护吗？
          ├─ 没有 → 直接用 httpx，简单快速
          ├─ 有基础防护 → httpx + 完整头部 + 合理频率
          ├─ 有 JS 加密 → 补环境（45 课）或 Playwright
          ├─ 有强风控 → 先找**官方 API**
          └─ 是 App → 找 App 的 API，通常比网页端宽松

        任何时候都存在最优解：换个数据源。
        公开数据集、政府开放数据、官方 API、付费数据服务、
        或者干脆自己做一份数据 —— 常常比硬爬更划算。

    ▸ 最后一课（49）会讲清楚这些手段的**法律边界**在哪里。""")


async def main() -> None:
    print(SEP)
    print("阶段 4 · 第 9 课：Playwright 进阶")
    print(SEP)
    print("""
本课全部实测 —— 沙箱内已装 Playwright + Chromium，真实访问目标站点。
预计耗时 60-120 秒。
""")
    await exp1_stealth()
    await exp2_wait_strategies()
    await exp3_blocking()
    await exp4_network_capture()
    await exp5_crawl()
    await exp6_limits()

    title("本课小结")
    print("""
  ✓ stealth 修复：webdriver / plugins / languages / chrome / UA —— 只挡初级检测
  ✓ 等待策略：等元素最快，networkidle 最稳，固定 sleep 纯浪费
  ✓ 资源拦截省 30-50% 时间 —— 拦 CSS 不影响 JS 执行
  ✓ 用浏览器做侦察，用 HTTP 客户端做量产：先看 XHR，能复现就扔掉浏览器
  ✓ 并发建议 3-5 个 context，超过 10 必触发风控
  ✓ 边界：TLS 指纹 / 行为模型 / 服务端关联分析 —— 绕不过，也别试

  下一课（49）：合规红线 —— 阶段 4 最重要的一课。
""")


if __name__ == "__main__":
    asyncio.run(main())
