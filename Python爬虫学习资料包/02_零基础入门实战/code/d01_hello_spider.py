"""第 1 个爬虫：把网页内容取回来。

运行：python3 d01_hello_spider.py

这个脚本只做一件事——拿到一个网页的 HTML 源码。
它是所有爬虫的起点，先跑通它，建立"我能控制网络请求"的实感。
"""

import requests  # 第三方库，负责"发请求"这件事

# 1) 目标网址。这里用 books.toscrape.com——一个专门给人练爬虫的假书店，
#    作者自己开放的，随便爬，不会有任何法律风险。
#    ⚠️ 学爬虫的第一课不是技术，是选对靶子。
url = "https://books.toscrape.com/"

# 2) 发一个 GET 请求。requests.get() 返回的不是网页内容本身，
#    而是一个 Response 对象，里面装着状态码、响应头、正文等一大堆信息。
response = requests.get(url, timeout=10)

# 3) 先看状态码。200 代表成功，404 是页面不存在，403 是被拒绝，503 是被限流。
print("状态码:", response.status_code)

# 4) 再看编码。这一步很多人会漏，导致中文变成乱码。
#    requests 会自动猜测编码，但有时猜错（尤其 GBK 网站），需要手动指定。
response.encoding = response.apparent_encoding
print("实际使用的编码:", response.encoding)

# 5) text 属性才是真正的 HTML 源码（字符串形式）
html = response.text
print("HTML 总长度:", len(html), "字符")
print("-" * 50)
print("前 300 个字符预览：")
print(html[:300])
