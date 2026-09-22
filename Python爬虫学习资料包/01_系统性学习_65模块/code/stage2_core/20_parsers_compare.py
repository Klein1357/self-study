"""阶段 2 · 三大解析器性能与用法对比。

运行：python3 20_parsers_compare.py

目的：让你亲手感受 BeautifulSoup / lxml / parsel 的差异，
      知道什么时候该用哪个。

结论预告（实测数据在下面）：
  · parsel 最快，且是 Scrapy 的底层，学会它等于为阶段 6 铺路
  · lxml 次之，XPath 支持最好
  · BeautifulSoup 最慢，但容错性最强（脏 HTML 也能解析）
  · 学习阶段用 BeautifulSoup 最友好，生产环境建议 parsel
"""

import time
from typing import Any

import requests
from bs4 import BeautifulSoup
from lxml import etree
from parsel import Selector

URL = "https://books.toscrape.com/"


# ============================================================
# 准备：先抓一次页面，后续都用同一份 HTML 做对比
# ============================================================
print("=" * 70)
print("阶段 2 · 解析器对比实验")
print("=" * 70)
print()

resp = requests.get(URL, timeout=15)
resp.encoding = resp.apparent_encoding
HTML = resp.text
print(f"已获取测试页面，HTML 长度: {len(HTML):,} 字符")
print()


# ============================================================
# 解析器 1：BeautifulSoup
# ============================================================
def parse_with_bs4(html: str) -> list[dict[str, Any]]:
    """用 BeautifulSoup 解析。

    特点：
      · 语法最友好，容错性最强（遇到不规范 HTML 也不崩）
      · 支持 CSS 选择器
      · 速度最慢

    Args:
        html: 页面 HTML

    Returns:
        书籍列表
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for item in soup.select("article.product_pod"):
        results.append({
            "title": item.select_one("h3 a")["title"],
            "price": item.select_one("p.price_color").text.strip(),
            # 用 .get() 更安全：属性不存在时返回 None 而不是报错
            "link": item.select_one("h3 a").get("href"),
        })

    return results


# ============================================================
# 解析器 2：lxml（XPath）
# ============================================================
def parse_with_lxml(html: str) -> list[dict[str, Any]]:
    """用 lxml 解析（XPath 语法）。

    特点：
      · 速度快（C 语言实现）
      · XPath 功能强大（能向上找父节点、按位置筛选）
      · 对不规范 HTML 的容错性较差

    Args:
        html: 页面 HTML

    Returns:
        书籍列表
    """
    # lxml 需要字节流或正确处理编码，用 html.fromstring
    tree = etree.HTML(html)
    results = []

    for item in tree.xpath('//article[@class="product_pod"]'):
        # XPath 的 .//  表示"从当前节点往下找"
        # [1] 表示取第一个匹配（XPath 下标从 1 开始！）
        title_nodes = item.xpath(".//h3/a/@title")
        price_nodes = item.xpath(".//p[@class='price_color']/text()")
        link_nodes = item.xpath(".//h3/a/@href")

        results.append({
            "title": title_nodes[0] if title_nodes else None,
            "price": price_nodes[0].strip() if price_nodes else None,
            "link": link_nodes[0] if link_nodes else None,
        })

    return results


# ============================================================
# 解析器 3：parsel（Scrapy 同款）
# ============================================================
def parse_with_parsel(html: str) -> list[dict[str, Any]]:
    """用 parsel 解析。

    特点：
      · 同时支持 CSS 和 XPath
      · 速度快（底层就是 lxml）
      · ★ Scrapy 的默认解析器 —— 学会它，阶段 6 零成本切换
      · .get() 取第一个，.getall() 取全部，语义清晰

    Args:
        html: 页面 HTML

    Returns:
        书籍列表
    """
    sel = Selector(text=html)
    results = []

    for item in sel.css("article.product_pod"):
        results.append({
            "title": item.css("h3 a::attr(title)").get(),
            "price": item.css("p.price_color::text").get("").strip(),
            "link": item.css("h3 a::attr(href)").get(),
        })

    return results


# ============================================================
# 性能对比
# ============================================================
print("-" * 70)
print("性能对比（每个解析器跑 50 次，取平均）")
print("-" * 70)
print()

parsers = [
    ("BeautifulSoup", parse_with_bs4),
    ("lxml (XPath)", parse_with_lxml),
    ("parsel (CSS)", parse_with_parsel),
]

ROUNDS = 50
timings: dict[str, float] = {}

for name, func in parsers:
    # 先跑一次预热（避免首次导入开销影响结果）
    func(HTML)

    start = time.perf_counter()
    for _ in range(ROUNDS):
        result = func(HTML)
    elapsed = (time.perf_counter() - start) / ROUNDS * 1000

    timings[name] = elapsed
    print(f"  {name:<18} {elapsed:>7.2f} ms/次    解析出 {len(result)} 条")

print()

# 以 parsel 为基准算相对速度
base = timings["parsel (CSS)"]
print("  相对速度（以 parsel 为基准，倍数越小越快）：")
for name, t in sorted(timings.items(), key=lambda x: x[1]):
    ratio = t / base
    bar = "█" * int(ratio * 12)
    print(f"    {name:<18} {ratio:>5.2f}x  {bar}")

print()


# ============================================================
# 三种解析器的语法对照表
# ============================================================
print("=" * 70)
print("语法对照：同一个需求，三种写法")
print("=" * 70)
print()

soup = BeautifulSoup(HTML, "html.parser")
tree = etree.HTML(HTML)
sel = Selector(text=HTML)

comparisons = [
    {
        "需求": "所有书籍容器",
        "BeautifulSoup": 'soup.select("article.product_pod")',
        "lxml/XPath": 'tree.xpath(\'//article[@class="product_pod"]\')',
        "parsel": 'sel.css("article.product_pod")',
        "验证": lambda: (
            len(soup.select("article.product_pod")),
            len(tree.xpath('//article[@class="product_pod"]')),
            len(sel.css("article.product_pod")),
        ),
    },
    {
        "需求": "第一本书的标题（属性）",
        "BeautifulSoup": 'soup.select_one("h3 a")["title"]',
        "lxml/XPath": 'tree.xpath("//h3/a/@title")[0]',
        "parsel": 'sel.css("h3 a::attr(title)").get()',
        "验证": lambda: (
            soup.select_one("h3 a")["title"],
            tree.xpath("//h3/a/@title")[0],
            sel.css("h3 a::attr(title)").get(),
        ),
    },
    {
        "需求": "所有价格文本",
        "BeautifulSoup": '[p.text for p in soup.select("p.price_color")]',
        "lxml/XPath": 'tree.xpath(\'//p[@class="price_color"]/text()\')',
        "parsel": 'sel.css("p.price_color::text").getall()',
        "验证": lambda: (
            len([p.text for p in soup.select("p.price_color")]),
            len(tree.xpath('//p[@class="price_color"]/text()')),
            len(sel.css("p.price_color::text").getall()),
        ),
    },
    {
        "需求": "所有书籍的链接",
        "BeautifulSoup": '[a["href"] for a in soup.select("h3 a")]',
        "lxml/XPath": 'tree.xpath("//h3/a/@href")',
        "parsel": 'sel.css("h3 a::attr(href)").getall()',
        "验证": lambda: (
            len([a["href"] for a in soup.select("h3 a")]),
            len(tree.xpath("//h3/a/@href")),
            len(sel.css("h3 a::attr(href)").getall()),
        ),
    },
]

for i, comp in enumerate(comparisons, 1):
    print(f"【{i}】{comp['需求']}")
    print(f"  BeautifulSoup:  {comp['BeautifulSoup']}")
    print(f"  lxml / XPath:   {comp['lxml/XPath']}")
    print(f"  parsel:         {comp['parsel']}")
    try:
        counts = comp["验证"]()
        print(f"  → 三者结果数量/值: {counts}")
    except Exception as e:
        print(f"  → 验证失败: {e}")
    print()


# ============================================================
# XPath 独有能力：CSS 做不到的事情
# ============================================================
print("=" * 70)
print("XPath 的独门绝技（CSS 选择器做不到）")
print("=" * 70)
print()

print("""CSS 选择器有个根本限制：它只能"向下"找（找子节点、后代节点），
无法"向上"找父节点。而 XPath 可以。

典型场景：你想抓"所有包含'科幻'标签的书籍的分类名"，
         但分类名在标签的【父节点】上 —— 这时必须用 XPath。

代码示例：

  # ❌ CSS 做不到：无法从子节点回到父节点
  # sel.css("...::parent")  ← 不存在这种语法

  # ✅ XPath 可以：/.. 表示父节点
  tree.xpath('//a[text()="科幻"]/../..')

常用 XPath 轴（axis）：
  ..            父节点
  ancestor::    所有祖先
  following-sibling::  后面的兄弟节点
  preceding-sibling::  前面的兄弟节点
  parent::      父节点（同 ..）

常用筛选：
  [@class="xxx"]      属性等于
  [contains(@class,"x")]  属性包含（处理多 class 很有用）
  [text()="内容"]      文本等于
  [position()=1]      位置筛选（或直接 [1]）
  [last()]            最后一个
""")

# 实测：用 XPath 找父节点
print("实测演示：从某个价格元素向上找它的书籍容器")
price_node = tree.xpath('//p[@class="price_color"]')[0]
print(f"  找到价格节点: <{price_node.tag} class={price_node.get('class')}>")

# 向上找 3 层，找到 article
article = price_node.xpath("./ancestor::article[1]")
if article:
    title = article[0].xpath(".//h3/a/@title")[0]
    print(f"  向上找到书籍容器，标题是: {title}")
    print("  → 这就是 XPath 的 ancestor 轴，CSS 无法替代")


print()
print("=" * 70)
print("选择建议")
print("=" * 70)
print("""
┌──────────────────┬─────────────────────────────────────────────┐
│ 场景             │ 推荐                                        │
├──────────────────┼─────────────────────────────────────────────┤
│ 学习阶段         │ BeautifulSoup（语法友好、容错强、报错清晰）  │
│ 一般生产项目     │ parsel（快、API 清晰、与 Scrapy 一致）       │
│ 需要向上查找     │ lxml 或 parsel 的 XPath                     │
│ 处理脏 HTML      │ BeautifulSoup（lxml 遇到畸形 HTML 可能丢内容）│
│ 已有 Scrapy 项目 │ parsel（Scrapy 内置，无需额外引入）          │
│ 极致性能         │ lxml 直接操作（跳过 BeautifulSoup 封装）     │
└──────────────────┴─────────────────────────────────────────────┘

本课程后续主要用：
  · 阶段 2 教学：BeautifulSoup（因为语法最直观，报错最友好）
  · 阶段 3 工程化：parsel（性能更好，为 Scrapy 铺路）
  · 阶段 6 Scrapy：parsel（内置）
""")
