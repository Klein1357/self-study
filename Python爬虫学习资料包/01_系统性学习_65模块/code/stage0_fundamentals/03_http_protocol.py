"""阶段 0.3 · HTTP 协议：把看不见的请求变成看得见的报文。

运行：python3 03_http_protocol.py

爬虫天天在发 HTTP 请求，但大多数人从没见过请求长什么样。
这个脚本用最原始的方式（socket）手写一个 HTTP 请求，
让你亲眼看到协议本身 —— 之后用 requests 时，你就知道
它到底替你做了什么。

理解了协议，你才能解释：
  - 为什么加 User-Agent 就能骗过反爬
  - Cookie 到底是怎么传的
  - 状态码从哪来
"""

import socket
import ssl
import time

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



def fetch_json(url: str, *, headers: dict | None = None,
               timeout: int = 15, retries: int = 3) -> dict | None:
    """请求一个返回 JSON 的接口，失败时重试；彻底失败则返回 None。

    为什么这节课需要这个函数（一段真实的踩坑记录）：
      本课用 httpbin.org 做演示，但它是公开的免费服务，实测 10 次请求里
      会出现 1 次 502、偶尔耗时飙到 11 秒。一旦返回 502，
      响应体就不是 JSON 而是 HTML 错误页，于是：

          resp = requests.get(url, timeout=15).json()
          # requests.exceptions.JSONDecodeError:
          #   Expecting value: line 1 column 1 (char 0)

      这个报错极具迷惑性 —— 它看起来像「JSON 解析写错了」，
      实际根因是「服务器给我返回了非 JSON 内容」，也就是网络问题。

      这是爬虫最常见的失败模式之一：**外部依赖一定会失败**。
      真实爬虫必须区分「我的代码错了」和「对方今天不舒服」，
      并对后者做重试 + 降级，而不是让整个程序崩掉。

    这里刻意演示三个工业级做法：
      1. 只重试可恢复的错误（超时、5xx、429），4xx 立刻放弃 —— 重试没意义
      2. 退避重试（1s → 2s），避免把已经吃力的服务压得更死
      3. 降级而不是崩溃：返回 None，让调用方决定怎么继续

    Args:
        url: 请求地址。
        headers: 自定义请求头。
        timeout: 单词请求超时秒数。
        retries: 最大尝试次数。

    Returns:
        解析成功的 dict；重试耗尽仍失败则返回 None。
    """
    import requests

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout)
            # 5xx 与 429 属于「对方暂时不行」，值得再试；
            # 4xx（除 429）是「你请求有问题」，重试一万次也一样
            if resp.status_code >= 500 or resp.status_code == 429:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            return resp.json()
        except Exception as exc:  # 网络错误、超时、JSON 解析失败都归这类
            if attempt == retries:
                print(f"  ⚠ 接口不可用（{type(exc).__name__}），已重试 {retries} 次，跳过本节实测")
                return None
            delay = 2 ** (attempt - 1)          # 1s, 2s —— 指数退避
            print(f"  ⚠ 第 {attempt} 次失败（{type(exc).__name__}），{delay}s 后重试")
            time.sleep(delay)
    return None


# ============================================================
# 实验 1：手写 HTTP 请求（不借助任何库）
# ============================================================
print("=" * 64)
print("实验 1：用 socket 手写一个 HTTP 请求")
print("=" * 64)

# HTTP 请求的本质：一段有格式的纯文本，通过 TCP 发出去
request_text = (
    "GET / HTTP/1.1\r\n"              # 请求行：方法 + 路径 + 版本
    "Host: books.toscrape.com\r\n"    # 必需：目标主机
    "User-Agent: Mozilla/5.0 (Learn/1.0)\r\n"
    "Accept: */*\r\n"
    "Connection: close\r\n"           # 告诉服务器：发完就关连接
    "\r\n"                            # 空行 = 请求头结束！
)

print("一个 HTTP 请求长这样（√ 就是用纯文本拼出来的）：")
print("-" * 64)
print(request_text.replace("\r\n", "\n"), end="")
print("-" * 64)
print()
print("关键点：")
print("  1. 换行必须用 \\r\\n（CRLF），不能用 \\n —— HTTP 协议规定")
print("  2. 最后必须有一个【空行】，表示请求头结束")
print("  3. 请求头和请求体之间就是这个空行分隔")

# 真正发出去（用 HTTPS，所以要包一层 SSL）
print()
print("正在通过 TCP + TLS 发送...")
context = ssl.create_default_context()
with socket.create_connection(("books.toscrape.com", 443), timeout=15) as sock:
    with context.wrap_socket(sock, server_hostname="books.toscrape.com") as ssock:
        ssock.sendall(request_text.encode("utf-8"))

        # 接收响应（分块读，直到服务器关闭连接）
        chunks = []
        while True:
            data = ssock.recv(4096)
            if not data:
                break
            chunks.append(data)
            if sum(len(c) for c in chunks) > 3000:  # 只取前 3000 字节做演示
                break

raw = b"".join(chunks).decode("utf-8", errors="replace")

print("服务器返回的原始响应（前 1200 字符）：")
print("-" * 64)
print(raw[:1200])
print("-" * 64)


# ============================================================
# 实验 2：解析响应报文的结构
# ============================================================
print()
print("=" * 64)
print("实验 2：响应报文由三部分组成")
print("=" * 64)

# 响应也一样：状态行 + 响应头 + 空行 + 响应体
head, _, body = raw.partition("\r\n\r\n")
lines = head.split("\r\n")
status_line = lines[0]
headers = lines[1:]

print("① 状态行（协议版本 + 状态码 + 说明）:")
print(f"   {status_line}")
print()
print("② 响应头（服务器告诉你的各种元信息）:")
for h in headers[:8]:
    if ":" in h:
        k, _, v = h.partition(":")
        print(f"   {k.strip():<28} {v.strip()[:45]}")
print()
print("③ 空行（分隔符）")
print()
print(f"④ 响应体（真正的页面内容，{len(body)} 字符）")
print(f"   {body[:100]}...")

print()
print("→ 对比一下：你在 requests 里写的")
print("     resp.status_code  → 就是状态行的 200/404")
print("     resp.headers      → 就是上面那些响应头")
print("     resp.text         → 就是响应体解码后的文本")
print("  现在你知道这些属性背后的东西了。")


# ============================================================
# 实验 3：为什么 User-Agent 能骗过反爬
# ============================================================
print()
print("=" * 64)
print("实验 3：User-Agent 为什么能骗过反爬")
print("=" * 64)

print("""
服务器收到的请求里，User-Agent 是客户端自报家门的字段：

  浏览器发的:   Mozilla/5.0 (Windows NT 10.0; Win64; x64) ...
  requests 默认: python-requests/2.32.5

服务器一看："哦，是 Python 脚本，八成是爬虫" → 返回 403。

它凭什么判断？就凭这个字段。
而这个字段【完全由客户端自己填写】，服务器无法验证真伪。

所以加了浏览器 UA 就能过 —— 不是"破解"了什么，
只是把自己报成了另一个身份。这也说明：
反爬的第一道防线很弱，因为它依赖客户端的诚实。
""")

# 实测：对比两种 UA 的响应
import requests

url = "https://httpbin.org/user-agent"  # 这个接口会回显你的 UA
print("实测验证（httpbin.org 会原样返回你的 User-Agent）：")
print("（注意：httpbin.org 是公开免费服务，偶尔 502/超时，此处做了重试 + 降级）")

default_resp = fetch_json(url)
if default_resp:
    print(f"  requests 默认 UA: {default_resp['user-agent'][:60]}")
else:
    print("  requests 默认 UA: python-requests/x.y.z（本次未取到实测值）")

custom_resp = fetch_json(
    url,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"},
)
if custom_resp:
    print(f"  伪装后的 UA:      {custom_resp['user-agent'][:60]}")
else:
    print("  伪装后的 UA:      Mozilla/5.0 ... Chrome/120.0.0.0 Safari/537.36（本次未取到实测值）")

print()
print("→ 服务器看到的就是这两串完全不同的身份标识。")


# ============================================================
# 实验 4：状态码是服务器的态度
# ============================================================
print()
print("=" * 64)
print("实验 4：亲手触发各种状态码")
print("=" * 64)

# 用 httpbin 提供的测试端点，故意制造不同状态码
test_cases = [
    ("正常访问", "https://httpbin.org/status/200"),
    ("资源不存在", "https://httpbin.org/status/404"),
    ("无权限", "https://httpbin.org/status/403"),
    ("服务器错误", "https://httpbin.org/status/500"),
]

print(f"{'场景':<12} {'地址后缀':<32} {'状态码'}")
print("-" * 60)
for name, test_url in test_cases:
    # 期望的状态码：正常访问要 200，其余三个是故意制造的 4xx/5xx
    expected = int(test_url.rsplit("/", 1)[-1])
    got = None
    for attempt in range(1, 4):
        try:
            r = requests.get(test_url, timeout=15)
            if r.status_code == expected:
                got = r.status_code
                break
            # 期望 500 却真收到 500 也算成功 —— 但这里要处理的是
            # 「期望 200 却收到 502」这种情况：那是 httpbin 自己挂了
            if attempt == 3:
                got = r.status_code
        except requests.RequestException:
            if attempt == 3:
                got = None
        if attempt < 3:
            time.sleep(2 ** (attempt - 1))

    if got is None:
        print(f"{name:<12} {test_url.replace('https://httpbin.org',''):<32} 请求失败")
    elif got == expected:
        print(f"{name:<12} {test_url.replace('https://httpbin.org',''):<32} {got}")
    else:
        # 不掩盖异常：如实报告并说明这是对方服务的问题
        print(f"{name:<12} {test_url.replace('https://httpbin.org',''):<32} {got} "
              f"（期望 {expected}，httpbin 服务不稳定）")

print()
print("→ 五大类状态码的含义：")
print("   1xx  信息提示（很少见到）")
print("   2xx  成功        ← 你想要的 200")
print("   3xx  重定向      ← requests 会自动跟随")
print("   4xx  客户端错误  ← 你请求错了（403/404/429）")
print("   5xx  服务端错误  ← 对方坏了（可以重试）")
print()
print("  关键区分：4xx 是「你做错了」，重试通常没用；")
print("            5xx 是「对方出问题了」，值得重试。")


# ============================================================
# 实验 5：Cookie 如何在请求间传递
# ============================================================
print()
print("=" * 64)
print("实验 5：Cookie —— 服务器如何「记住」你")
print("=" * 64)

print("""
HTTP 是【无状态】协议：每个请求都是独立的，服务器不记得你上次是谁。

那登录状态怎么维持？靠 Cookie：
  1. 你第一次登录，服务器发一个 Set-Cookie 响应头
  2. 浏览器把它存下来
  3. 之后每个请求都自动带上 Cookie 请求头
  4. 服务器读到 Cookie，就知道"还是刚才那个人"

爬虫要用 Session 而不是单独 get，核心原因就在这：
""")

print("代码对比：")
print("-" * 64)
print("""# ❌ 每次都是"新人"，Cookie 不共享，无法维持登录
requests.get(url1)
requests.get(url2)     # 服务器不认识你

# ✅ Session 会自动管理 Cookie
session = requests.Session()
session.get(url1)      # 服务器发 Cookie，Session 自动保存
session.get(url2)      # Session 自动带上 Cookie
""")
print("-" * 64)

# 实测：验证 Cookie 的存取
print("实测：让 httpbin 设置一个 Cookie，看 Session 是否记住")
session = requests.Session()
try:
    # 这一步只需「服务器发出 Set-Cookie」，不必解析响应体，
    # 因此刻意不用 fetch_json（那会把 502 误判为失败）
    session.get(
        "https://httpbin.org/cookies/set/token/abc123",
        timeout=15,
        allow_redirects=True,
    )
    print(f"  Session 里保存的 Cookie: {dict(session.cookies)}")

    echo_resp = fetch_json("https://httpbin.org/cookies")
    if echo_resp:
        print(f"  下次请求自动带上的 Cookie: {echo_resp.get('cookies')}")
    else:
        print("  下次请求自动带上的 Cookie: {'token': 'abc123'}（本次未取到实测值）")
    print()
    print("→ 你什么都没手动做，Session 全自动处理了。")
except Exception as exc:
    print(f"  ⚠ 跳过本项实测（{type(exc).__name__}）")
    print("    httpbin.org 是公开免费服务，偶发 502/超时属正常，")
    print("    不影响对 Cookie 机制的理解：Session 会在后续请求里自动回传 Cookie。")


print()
print("=" * 64)
print("本节要点回顾")
print("=" * 64)
print("""
1. HTTP 请求就是一段有格式的纯文本：请求行 + 头部 + 空行 + 响应体
2. 换行必须用 \\r\\n，头部结束用一个空行标记
3. 响应结构：状态行 + 响应头 + 空行 + 响应体
4. User-Agent 是客户端自报身份，服务器无法验证 → 所以能伪装
5. 状态码：2xx 成功 / 3xx 重定向 / 4xx 你错了 / 5xx 对方错了
6. HTTP 无状态，靠 Cookie 维持会话；requests 的 Session 自动管理
""")
