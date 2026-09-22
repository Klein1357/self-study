"""阶段 0.2 + 0.4 · 网络分层与 DNS：一次请求到底经过了什么。

运行：python3 02_network_layers.py

这一节回答一个根本问题：
  在浏览器输入网址、按下回车，到看到页面，中间到底发生了什么？

大部分教程会略过这段直接教 requests。但理解了它，你就能解释：
  - 为什么有时候是"DNS 解析失败"而不是"连接失败"
  - 为什么 HTTPS 比 HTTP 慢一点（多了一次握手）
  - 为什么代理能帮你隐藏身份
"""

import socket
import ssl
import time


# ============================================================
# 实验 1：TCP/IP 四层模型
# ============================================================
print("=" * 66)
print("实验 1：数据是如何被层层打包的")
print("=" * 66)

print("""
你发出的一个请求，并不是"一整块"飞过去的，而是被层层封装：

  ┌─────────────────────────────────────────────────┐
  │ 应用层    HTTP 报文（GET / HTTP/1.1 ...）        │ ← 你写的内容
  ├─────────────────────────────────────────────────┤
  │ 传输层    TCP 头 + 数据（加端口号、序号）        │ ← 保证可靠
  ├─────────────────────────────────────────────────┤
  │ 网络层    IP 头 + 数据（加 IP 地址）             │ ← 负责寻址
  ├─────────────────────────────────────────────────┤
  │ 链路层    帧头 + 数据（加 MAC 地址）             │ ← 物理传输
  └─────────────────────────────────────────────────┘

每一层加上自己的"信封"，对方收到后再一层层拆开。

对爬虫的意义：
  · 应用层 → requests / 状态码 / Cookie  ← 你 90% 的时间在这里
  · 传输层 → TCP 连接、端口、超时        ← 理解 timeout 和连接复用
  · 网络层 → IP、代理                    ← 理解代理为什么能换身份
  · 链路层 → 网卡、MAC                   ← 基本不用关心
""")


# ============================================================
# 实验 2：DNS 解析 —— 把域名变成 IP
# ============================================================
print("=" * 66)
print("实验 2：DNS 解析（第一步永远是它）")
print("=" * 66)

print("你的电脑不认识 'books.toscrape.com'，只认识 IP 地址。")
print("DNS 的工作就是把域名翻译成 IP。\n")

domains = [
    "books.toscrape.com",
    "www.python.org",
    "github.com",
]

print(f"{'域名':<28} {'解析出的 IP':<20} {'耗时'}")
print("-" * 66)
for domain in domains:
    start = time.perf_counter()
    try:
        ip = socket.gethostbyname(domain)
        elapsed = (time.perf_counter() - start) * 1000
        print(f"{domain:<28} {ip:<20} {elapsed:.0f} ms")
    except socket.gaierror as e:
        print(f"{domain:<28} {'解析失败':<20} {e}")

print()
print("→ 一个域名可以对应多个 IP（负载均衡）。")
print("  完整的 DNS 查询过程：")
print("    浏览器缓存 → 系统缓存 → hosts 文件 → 本地 DNS 服务器 → 根域名服务器")
print("  这个链路里任何一环出问题，都会表现为「DNS 解析失败」。")
print()
print("  爬虫常见报错 'Failed to resolve' / 'Name or service not known'")
print("  就是卡在这一步 —— 和 HTTP 无关，是域名根本没解析出来。")


# ============================================================
# 实验 3：TCP 三次握手
# ============================================================
print()
print("=" * 66)
print("实验 3：TCP 三次握手（建立连接）")
print("=" * 66)

print("""
拿到 IP 后，还要和对方建立 TCP 连接。这个过程叫"三次握手"：

  客户端 ──── SYN（我想连你） ──────────→ 服务器
  客户端 ←─── SYN + ACK（好，我也想知道你） ─ 服务器
  客户端 ──── ACK（收到，开始吧） ───────→ 服务器

为什么要三次？
  两次不够：服务器发出 SYN+ACK 后，无法确认对方是否收到。
  三次才能让【双方】都确认"我的发送能力和你的接收能力都正常"。

对爬虫的意义：
  · 每次建立连接都有成本（约 20-100ms）
  · 这就是为什么用 Session 复用连接能快 2-3 倍 —— 省掉了反复握手
""")

# 实测握手耗时
host = "books.toscrape.com"
print(f"实测到 {host}:443 建立 TCP 连接的耗时：")
print()
times = []
for i in range(3):
    start = time.perf_counter()
    with socket.create_connection((host, 443), timeout=10) as s:
        elapsed = (time.perf_counter() - start) * 1000
        times.append(elapsed)
        print(f"  第 {i+1} 次: {elapsed:.0f} ms")
avg = sum(times) / len(times)
print(f"\n  平均: {avg:.0f} ms")
if avg < 5:
    print("  ⚠️ 耗时接近 0，通常说明当前环境走了本地代理/网关，")
    print("     或距离目标服务器极近。真实公网环境下，一次握手")
    print("     通常要 20-100ms —— 你在自己电脑上跑这个脚本会看到真实值。")
print("  → 每次请求都重建连接的话，光握手就要花这些时间。")
print("    用 Session 复用连接 = 把这部分成本省掉。")


# ============================================================
# 实验 4：TLS 握手 —— HTTPS 多出来的那一步
# ============================================================
print()
print("=" * 66)
print("实验 4：TLS 握手（HTTPS 比 HTTP 多的环节）")
print("=" * 66)

print("""
TCP 连接建好后，如果地址是 https://，还要再做一次 TLS 握手：

  1. 客户端发送：支持的加密套件列表 + 随机数
  2. 服务器回复：选定套件 + 随机数 + 数字证书
  3. 客户端验证证书（是不是真的、有没有过期、域名对不对）
  4. 双方用交换的信息算出相同的对称密钥
  5. 之后的数据都用这个密钥加密传输

这就是"加密"的本质：用非对称加密安全地协商出对称密钥，
再用对称加密传数据（因为对称加密快得多）。

对爬虫的意义：
  · 你在浏览器里看到的 🔒 就是这个
  · 证书验证失败会报 SSLError
  · 『TLS 指纹』是一种高级反爬手段 —— 通过握手的特征
    识别你是不是真实浏览器（requests 和 Chrome 的握手细节不同）
""")

# 实测 TLS 握手，并查看证书信息
print(f"实测：查看 {host} 的 TLS 证书信息\n")
try:
    context = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname=host) as ssock:
            cert = ssock.getpeercert()
            print(f"  协议版本:   {ssock.version()}")
            print(f"  加密算法:   {ssock.cipher()[0]}")
            print(f"  证书主体:   {dict(x[0] for x in cert.get('subject', []))}")
            print(f"  颁发机构:   {dict(x[0] for x in cert.get('issuer', []))}")
            print(f"  有效期至:   {cert.get('notAfter')}")
except Exception as e:
    print(f"  获取失败: {e}")

print()
print("  → 这些信息都是服务器在 TLS 握手时主动提供的。")
print("    浏览器会验证证书是否可信，不可信就弹出红色警告。")
print("    requests 用的是一套独立的证书链（certifi），这就是")
print("    为什么有时浏览器能开、requests 却报 SSLError。")


# ============================================================
# 实验 5：完整链路总结
# ============================================================
print()
print("=" * 66)
print("实验 5：完整链路——从输入网址到看到页面")
print("=" * 66)

print("""
① DNS 解析
   域名 → IP
   失败表现: "Name or service not known"

② TCP 三次握手
   建立到目标 IP:443 的连接
   失败表现: ConnectionTimeout / ConnectionRefused

③ TLS 握手（仅 HTTPS）
   协商加密、验证证书
   失败表现: SSLError / CERTIFICATE_VERIFY_FAILED

④ 发送 HTTP 请求
   应用层发送 GET / HTTP/1.1 ...

⑤ 服务器处理并返回响应
   状态码 + 响应头 + 响应体

⑥ 客户端解析响应
   requests → response.text
   浏览器 → 解析 HTML、加载 CSS/JS、执行 JS、渲染

⑦ 关键区别：浏览器还会执行第 6 步里的 JavaScript
   ┌──────────────────────────────────────────┐
   │ 这一步就是"动态页面"的来源               │
   │ 数据由 JS 在客户端生成，原始 HTML 里没有 │
   │ 这就是为什么 requests 抓不到，          │
   │ 而 Playwright（真实浏览器）可以          │
   └──────────────────────────────────────────┘
""")

print("=" * 66)
print("本节要点回顾")
print("=" * 66)
print("""
1. 数据被 TCP/IP 四层层层封装，应用层是你主要关注的
2. DNS 把域名解析成 IP —— 失败时是"解析失败"，不是 HTTP 问题
3. TCP 三次握手建立连接，有成本 → Session 复用来省
4. HTTPS 多一次 TLS 握手，用非对称加密协商出对称密钥
5. 证书验证可能失败（SSLError），因为 requests 和浏览器用不同证书链
6. 浏览器会执行 JS，这是动态页面抓不到的根本原因
""")
