"""第 2 个爬虫：从 HTML 里把数据抠出来。

运行：python3 d02_parse_books.py

上一个脚本我们拿到了 HTML 字符串，但那是给人看的，
计算机要的是结构化的数据（书名、价格、评分）。
这个脚本演示怎么把 HTML 变成 Python 能用的列表和字典。
"""

import requests
from bs4 import BeautifulSoup

url = "https://books.toscrape.com/"
response = requests.get(url, timeout=10)
response.encoding = response.apparent_encoding

# 1) 把 HTML 字符串包装成 BeautifulSoup 对象。
#    "html.parser" 是 Python 自带的解析器，不用额外安装。
#    也可以换 "lxml"，速度更快（本环境已预装）。
soup = BeautifulSoup(response.text, "html.parser")

# 2) 找到所有书。每本书是一个 <article class="product_pod"> 标签。
#    select() 用的是 CSS 选择器语法，跟你在浏览器 F12 里用的一样。
books = soup.select("article.product_pod")
print(f"这一页共找到 {len(books)} 本书")
print("-" * 60)

results = []  # 用来装最终结果

for book in books:
    # 3) 书名藏在 <h3><a title="书名"> 的 title 属性里
    #    ["title"] 就是取属性的值，属性和标签的区别：属性在尖括号里面
    title = book.select_one("h3 a")["title"]

    # 4) 价格在 <p class="price_color"> 的文本里，取出来是 "£51.77" 这种
    price_text = book.select_one("p.price_color").text
    # 去掉英镑符号和首尾空格，转成小数
    price = float(price_text.replace("£", "").strip())

    # 5) 评分藏在 class 里，形如 class="star-rating Three"
    #    ["class"] 返回的是列表，比如 ['star-rating', 'Three']
    rating_class = book.select_one("p.star-rating")["class"]
    rating_word = rating_class[1]  # 第二个元素才是真正的评分词

    # 6) 英文评分词转成数字，方便后面排序
    rating_map = {"One": 1, "Two": 2, "Three": 3, "Four": 4, "Five": 5}
    rating = rating_map[rating_word]

    # 7) 库存状态
    stock = book.select_one("p.instock.availability").text.strip()

    # 8) 组装成字典。字典是爬虫最重要的数据结构：
    #    一个字段一个 key，将来存 CSV / Excel / 数据库都是一行一条
    results.append({
        "书名": title,
        "价格(£)": price,
        "评分": rating,
        "库存": stock,
    })

# 9) 打印前 5 条看看效果
for i, item in enumerate(results[:5], start=1):
    print(f"{i}. {item['书名'][:45]:<47} £{item['价格(£)']:<7} {'★' * item['评分']}")

print("-" * 60)

# 10) 顺手做个小分析：最贵和最便宜的书
expensive = max(results, key=lambda x: x["价格(£)"])
cheap = min(results, key=lambda x: x["价格(£)"])
print(f"最贵：《{expensive['书名']}》 £{expensive['价格(£)']}")
print(f"最便宜：《{cheap['书名']}》 £{cheap['价格(£)']}")
print(f"平均价格：£{sum(b['价格(£)'] for b in results) / len(results):.2f}")
