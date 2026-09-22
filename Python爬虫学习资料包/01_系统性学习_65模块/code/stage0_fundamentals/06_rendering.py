"""阶段 0.6 · 浏览器渲染流程：为什么动态页面抓不到。

运行：python3 06_rendering.py

这是阶段 0 最重要的一个实验。它用一个真实例子，
让你彻底搞懂"动态页面"到底是什么意思：

  同一个网址，requests 抓不到数据，浏览器却能看到。
  为什么？因为浏览器多干了一件事：执行 JavaScript。

理解了这一点，你才能真正明白 Playwright 存在的意义，
而不是把它当成一个"更厉害的 requests"。
"""

import re

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


URL = "https://quotes.toscrape.com/js/"


# ============================================================
# 实验 1：对比「原始响应」与「浏览器渲染后」的差异
# ============================================================
print("=" * 68)
print("实验 1：同一个网址，两种截然不同的结果")
print("=" * 68)

print(f"目标网址: {URL}")
print("这个网站有两个版本：")
print("  /        → 服务端直接返回 HTML（静态）")
print("  /js/     → 页面是空壳，数据由 JavaScript 填充（动态）")
print()
print("正在用 requests 抓取 /js/ 版本……")
print()

resp = requests.get(URL, timeout=15)
resp.encoding = resp.apparent_encoding
raw_html = resp.text

print(f"拿到 HTML，长度 {len(raw_html)} 字符")
print()
print("这个 HTML 的 <body> 部分长这样：")
print("-" * 68)

# 提取 body 部分展示
body_match = re.search(r"<body[^>]*>(.*?)</body>", raw_html, re.S | re.I)
body = body_match.group(1) if body_match else raw_html

# 去掉 script 里的内容，只留结构，让对比更清晰
body_visible = re.sub(r"<script[^>]*>.*?</script>", "<!--[这里是一大段 JS 代码]-->",
                      body, flags=re.S | re.I)
print(body_visible[:700].strip())
print("-" * 68)


# ============================================================
# 实验 2：关键证据 —— 数据在不在原始 HTML 里
# ============================================================
print()
print("=" * 68)
print("实验 2：关键证据")
print("=" * 68)

# 页面上应该显示的名言（浏览器里能看到）
expected_quotes = [
    "The world as we have created it is a process of our thinking",
    "It is our choices, Harry, that show what we truly are",
    "There are only two ways to live your life",
]

print("浏览器里明明能看到这些名言：")
for i, q in enumerate(expected_quotes, 1):
    print(f'  {i}. "{q}..."')
print()
print("它们在你抓到的原始 HTML 里以【可见元素】的形式存在吗？")
print("（注意：这里检查的是它们有没有出现在 HTML 标签结构中）")
print()

# 关键：检查数据是否出现在可见的 DOM 元素里，而不是 script 里
# 把 <script> 部分剥离后再检查
html_without_script = re.sub(r"<script[^>]*>.*?</script>", "", raw_html, flags=re.S | re.I)

for i, q in enumerate(expected_quotes, 1):
    in_visible = q in html_without_script
    in_script = q in raw_html and not in_visible
    if in_visible:
        note = "在可见 HTML 中 ✓（这是静态页面）"
    elif in_script:
        note = "只在 <script> 里找到 ⚠️"
    else:
        note = "完全找不到 ✗（真·动态加载）"
    print(f'  {i}. {note}')

print()
print("→ 这里出现了一个很有意思的中间情况：")
print()
print("  quotes.toscrape.com/js 这个页面，数据被【内联】写在")
print("  <script> 标签里的一个 JS 变量中：")
print('      var data = [ {"tags": [...], "author": {...}, ...}, ... ]')
print()
print("  它没有发起额外的网络请求，但数据也不在 HTML 元素里。")
print("  requests 能拿到这段文字，但需要额外解析 JSON 才能用。")
print()
print("  这说明『动态页面』其实是三种不同的情况：")
print("    A. 数据在可见 HTML 里          → requests 直接用")
print("    B. 数据内联在 <script> 里      → requests + 提取 JS 变量")
print("    C. 数据由 JS 异步请求后填充    → 抓接口 或 Playwright")


# ============================================================
# 实验 3：数据到底从哪来？—— 看 JS 在请求什么
# ============================================================
print()
print("=" * 68)
print("实验 3：数据其实来自另一个请求")
print("=" * 68)

print("既然 HTML 里没有，JS 是从哪拿到数据的？")
print("答案：JS 自己又发了一个请求去取数据。")
print()
print("在原始 HTML 里找 JS 的痕迹：")

# 找出页面引用的 JS 文件
scripts = re.findall(r'<script[^>]+src="([^"]+)"', raw_html)
print(f"  页面加载了 {len(scripts)} 个外部 JS 文件：")
for s in scripts:
    print(f"    · {s}")

# 找出内联 script 里的线索
inline_scripts = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", raw_html, re.S)
print(f"  另有 {len(inline_scripts)} 段内联脚本")
if inline_scripts:
    snippet = inline_scripts[0].strip()[:200]
    print(f"  第一段内联脚本的内容片段：")
    print(f"    {snippet}")

print()
print("→ 在真实网站上，你可以这样找到数据的真正来源：")
print()
print("   ① 打开 F12 → Network 面板")
print("   ② 筛选 Fetch/XHR（只看数据接口请求）")
print("   ③ 刷新页面，观察有哪些请求")
print("   ④ 逐个点开看 Response —— 找到返回 JSON 数据的那个")
print("   ⑤ 那个请求的 URL，就是你可以直接抓的接口")
print()
print("  这是抓动态页面【最高效】的方法：")
print("  绕开浏览器渲染，直接请求数据接口，速度提升 10 倍以上。")


# ============================================================
# 实验 4：实操——从内联 JS 变量里提取数据
# ============================================================
print()
print("=" * 68)
print("实验 4：实操——直接提取内联 JS 里的数据")
print("=" * 68)

print("针对刚才的『情况 B』（数据内联在 script 里），")
print("其实不需要 Playwright，只要把那段 JS 变量抠出来当 JSON 解析即可。")
print()

import json

# 找到 var data = [...] 这一段
match = re.search(r"var\s+data\s*=\s*(\[.*?\]);", raw_html, re.S)
if match:
    try:
        data = json.loads(match.group(1))
        print(f"✓ 成功提取并解析出 {len(data)} 条数据")
        print()
        print("前 3 条示例：")
        for i, item in enumerate(data[:3], 1):
            text = item.get("text", "")[:58]
            author = item.get("author", {}).get("name", "?")
            tags = ", ".join(item.get("tags", []))
            print(f"  {i}. \"{text}...\"")
            print(f"     作者: {author}")
            print(f"     标签: {tags}")
            print()
        print("→ 整个过程只用了 requests + 正则 + json，没有任何浏览器。")
        print("  速度快、资源省 —— 这就是为什么『先分析再动手』很重要。")
    except json.JSONDecodeError as e:
        print(f"✗ JSON 解析失败: {e}")
        print("  （真实项目中，JS 变量往往不是标准 JSON，")
        print("    可能带有单引号、尾逗号、函数调用等，需要额外清洗）")
else:
    print("没有找到 var data = [...] 结构")

print()
print("⚠️ 但要注意：这种做法很脆弱。")
print("   JS 代码稍一改动，你的正则就失效了。")
print("   而且如果数据需要经过复杂计算才能得到，还是得用 Playwright。")


# ============================================================
# 实验 5：用 Playwright 验证——真实浏览器就能拿到
# ============================================================
print()
print("=" * 68)
print("实验 5：用真实浏览器验证")
print("=" * 68)

print("如果用真实浏览器内核（Playwright）访问同一个地址，")
print("它会执行 JS、完成渲染，然后我们就能拿到数据了。")
print()
print("正在启动 Chromium……（首次可能稍慢）")

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(URL, wait_until="networkidle", timeout=30000)

        # 等元素真正出现（比 sleep 可靠）
        page.wait_for_selector("div.quote", timeout=15000)

        # 顺便演示：用浏览器直接提取结构化数据，非常方便
        quotes = page.eval_on_selector_all(
            "div.quote",
            """els => els.map(el => ({
                text: el.querySelector('.text').innerText,
                author: el.querySelector('.author').innerText,
                tags: [...el.querySelectorAll('.tag')].map(t => t.innerText)
            }))"""
        )

        rendered_html = page.content()
        browser.close()

    print(f"  渲染后 HTML 长度: {len(rendered_html)} 字符")
    print(f"  （对比 requests 抓到的 {len(raw_html)} 字符）")
    print()
    print(f"  ✓ 浏览器里直接提取到 {len(quotes)} 条结构化数据：")
    for i, q in enumerate(quotes[:3], 1):
        print(f"    {i}. {q['text'][:52]}... — {q['author']}")
    print()
    print("  → 注意这个用法：page.eval_on_selector_all() 可以直接在")
    print("    浏览器里执行 JS 提取数据，比抓 HTML 回来再解析更直接。")

except Exception as e:
    print(f"  运行失败: {e}")
    print("  请确认已安装: pip install playwright && playwright install chromium")


# ============================================================
# 实验 5：三个层次的应对策略（重要）
# ============================================================
print()
print("=" * 68)
print("实验 5：面对动态页面，你有三个选择（按优先级）")
print("=" * 68)

print("""
┌─ 方案 A：直接抓数据接口（最优） ──────────────────────────┐
│ 思路：JS 从某个接口拿数据，你也直接请求那个接口            │
│ 方法：F12 → Network → Fetch/XHR → 找到返回 JSON 的请求     │
│ 优点：快（毫秒级）、省资源、稳定                           │
│ 缺点：需要分析接口，可能遇到签名/加密参数（阶段 4 解决）    │
│ 适用：绝大多数场景 ★ 优先尝试这个                          │
└───────────────────────────────────────────────────────────┘

┌─ 方案 B：用 Playwright 模拟浏览器 ────────────────────────┐
│ 思路：让真实浏览器帮你渲染，然后取结果                     │
│ 方法：page.goto() → wait_for_selector() → page.content()  │
│ 优点：所见即所得，不需要分析接口                           │
│ 缺点：慢（秒级）、吃内存、并发能力弱                        │
│ 适用：接口难分析、或需要复杂交互（登录/点击/滚动）          │
└───────────────────────────────────────────────────────────┘

┌─ 方案 C：分析 JS 源码还原逻辑 ────────────────────────────┐
│ 思路：读 JS 代码，理解它怎么请求、怎么加密                 │
│ 方法：断点调试、格式化代码、跟踪调用栈                     │
│ 优点：最彻底，能应对最强防御                               │
│ 缺点：成本最高，需要 JS 基础（阶段 4 专项学习）            │
│ 适用：前两个方案都不行时的终极手段                         │
└───────────────────────────────────────────────────────────┘

⚠️ 新手最常见的错误：一遇到动态页面就上 Playwright。
   正确做法是先看 Network 面板 —— 很多时候方案 A 就能解决，
   而且比 Playwright 快 10 倍以上。
""")

print("=" * 68)
print("本节要点回顾")
print("=" * 68)
print("""
1. 浏览器收到 HTML 后，还要解析、加载资源、执行 JS，才能显示完整页面
2. requests 只拿到"原始 HTML"，不执行 JS —— 这就是动态页面抓不到的原因
3. 动态页面的数据通常来自一个单独的 XHR/Fetch 接口
4. 找到那个接口直接抓，比用浏览器渲染快得多（方案 A）
5. Playwright 用真实浏览器内核，能拿到渲染后的结果（方案 B）
6. 三个方案的优先级：抓接口 > Playwright > 逆向 JS
""")
