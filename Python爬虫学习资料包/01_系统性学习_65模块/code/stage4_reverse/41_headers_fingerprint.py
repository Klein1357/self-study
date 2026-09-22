"""
阶段 4 · 4.2 请求头与浏览器指纹
======================================================
对应网页章节：#s4-2

本脚本实测：
  1. 不同 UA 得到的不同响应（含被识别为爬虫的 UA）
  2. 现代浏览器请求头的完整清单（缺哪些会被识别）
  3. 头部字段的"自洽性"检查 —— 为什么伪造要成套伪造
  4. TLS 指纹（JA3）原理演示 + curl_cffi 方案
  5. 请求头随机化模板（生产可用）

运行：python3 41_headers_fingerprint.py
"""

from __future__ import annotations

import hashlib
import json
import random
import ssl
from dataclasses import dataclass, field

import httpx

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



# ============================================================
# 一、UA 实测：不同 UA 得到什么
# ============================================================
@dataclass
class UACase:
    """一个 UA 测试用例。"""

    name: str
    ua: str
    expect: str = ""


UA_CASES: list[UACase] = [
    UACase(
        "未设置 UA（httpx 默认）",
        "",
        "多数站点会直接判定为爬虫 —— 这是最容易暴露的一条",
    ),
    UACase(
        "python-requests 默认",
        "python-requests/2.32.5",
        "几乎等于在头上写『我是爬虫』",
    ),
    UACase(
        "curl 默认",
        "curl/8.5.0",
        "同样是明显的脚本特征",
    ),
    UACase(
        "Chrome 122 真实 UA",
        ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
        "最常见的伪装选择",
    ),
    UACase(
        "Safari 17 (macOS)",
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
         "(KHTML, like Gecko) Version/17.2.1 Safari/605.1.15"),
        "注意：Safari 不会有 Sec-Ch-Ua 系列头，混用会露馅",
    ),
]

# httpbin 会回显收到的头部，方便我们观察服务端看到了什么
ECHO_URL = "https://httpbin.org/headers"


def test_user_agents() -> None:
    """实测不同 UA 下服务端看到的内容。"""
    print("=" * 84)
    print("【实验 1】不同 UA 的实测对比")
    print("=" * 84)

    for case in UA_CASES:
        headers = {}
        if case.ua:
            headers["User-Agent"] = case.ua
        try:
            r = httpx.get(ECHO_URL, headers=headers, timeout=20)
            seen = r.json().get("headers", {}).get("User-Agent", "(服务端未收到 UA 头)")
        except Exception as e:                  # noqa: BLE001
            seen = f"请求失败：{type(e).__name__}"

        print(f"\n▸ {case.name}")
        print(f"   发送： {case.ua or '(不设置)'}")
        print(f"   服务端看到： {seen}")
        if case.expect:
            print(f"   ⚠️  {case.expect}")

    print("\n" + "-" * 84)
    print("关键认知：服务端能看到的远不止 UA。下面是 httpbin 回显的完整头部：")
    try:
        r = httpx.get(ECHO_URL, timeout=20, headers={"User-Agent": UA_CASES[3].ua})
        for k, v in sorted(r.json().get("headers", {}).items()):
            print(f"    {k}: {v}")
    except Exception as e:                      # noqa: BLE001
        print(f"    获取失败：{e}")


# ============================================================
# 二、现代浏览器的完整请求头清单
# ============================================================
BROWSER_HEADERS: dict[str, str] = {
    # ---- 基础 ----
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8,"
               "application/signed-exchange;v=b3;q=0.7"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",

    # ---- Chrome 特有的客户端提示头（Client Hints）----
    # 缺了这些头，很多风控系统会立刻判定为非浏览器
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',

    # ---- Fetch 元数据（区分导航请求 vs 资源请求）----
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",

    # ---- 其他 ----
    "Upgrade-Insecure-Requests": "1",
    "Connection": "keep-alive",
    "Cache-Control": "max-age=0",
}

# 从列表页跳到详情页时，这些字段会变化
NAVIGATION_VARIANTS: dict[str, dict[str, str]] = {
    "直接访问首页/地址栏输入": {
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Referer": "(不发送)",
    },
    "从站内列表页点击": {
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Referer": "https://example.com/list",
    },
    "从搜索引擎跳转": {
        "Sec-Fetch-Site": "cross-site",
        "Sec-Fetch-User": "?1",
        "Referer": "https://www.google.com/",
    },
    "Ajax/XHR 请求": {
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "(不发送)",
        "Referer": "https://example.com/page",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
    },
}


def print_header_checklist() -> None:
    """打印完整请求头清单与自洽性规则。"""
    print("\n" + "=" * 84)
    print("【实验 2】现代浏览器的完整请求头（Chrome 122 / Windows）")
    print("=" * 84)
    print()
    for k, v in BROWSER_HEADERS.items():
        print(f"  {k}:")
        print(f"      {v}")

    print("\n" + "=" * 84)
    print("【实验 3】自洽性：不同导航场景下头部的变化")
    print("=" * 84)
    for scenario, variants in NAVIGATION_VARIANTS.items():
        print(f"\n▸ {scenario}")
        for k, v in variants.items():
            print(f"    {k:<20} {v}")


# ============================================================
# 三、自洽性检查器
# ============================================================
@dataclass
class ConsistencyIssue:
    """一条自洽性问题。"""

    level: str          # "严重" / "警告"
    field: str
    message: str


def check_consistency(headers: dict[str, str]) -> list[ConsistencyIssue]:
    """
    检查请求头是否自洽 —— 这是伪造成败的关键。

    风控系统不只看"有没有某个头"，更看"这些头之间是否矛盾"。
    例：UA 说是 Chrome 122，却没有 Sec-Ch-Ua 头 → 矛盾 → 判定为脚本。

    Args:
        headers: 请求头字典（键比较时忽略大小写）。

    Returns:
        问题列表。
    """
    h = {k.lower(): v for k, v in headers.items()}
    issues: list[ConsistencyIssue] = []

    ua = h.get("user-agent", "")

    # ---- 规则 1：Chrome 必须有 Sec-Ch-Ua ----
    if "chrome/" in ua.lower() and "sec-ch-ua" not in h:
        issues.append(ConsistencyIssue(
            "严重", "Sec-Ch-Ua",
            "UA 声明是 Chrome，但没有 Sec-Ch-Ua 头。"
            "Chrome 93+ 默认发送该头，缺失会立刻暴露。"
        ))

    # ---- 规则 2：Sec-Ch-Ua 版本要与 UA 里的版本一致 ----
    if "sec-ch-ua" in h and "chrome/" in ua.lower():
        import re
        ua_ver = re.search(r"chrome/(\d+)", ua, re.I)
        ch_ver = re.search(r'"Chromium";v="(\d+)"', h["sec-ch-ua"])
        if ua_ver and ch_ver and ua_ver.group(1) != ch_ver.group(1):
            issues.append(ConsistencyIssue(
                "严重", "Sec-Ch-Ua",
                f"UA 说 Chrome {ua_ver.group(1)}，但 Sec-Ch-Ua 说 {ch_ver.group(1)}。"
                "版本不一致是最容易被抓的矛盾。"
            ))

    # ---- 规则 3：Windows 的 Platform 必须是 "Windows" ----
    if "windows" in ua.lower() and "sec-ch-ua-platform" in h:
        if h["sec-ch-ua-platform"] != '"Windows"':
            issues.append(ConsistencyIssue(
                "严重", "Sec-Ch-Ua-Platform",
                f"UA 是 Windows，但 Platform 是 {h['sec-ch-ua-platform']}。"
            ))

    # ---- 规则 4：Safari 不该有 Sec-Ch-Ua ----
    if "safari" in ua.lower() and "chrome" not in ua.lower() and "sec-ch-ua" in h:
        issues.append(ConsistencyIssue(
            "严重", "Sec-Ch-Ua",
            "Safari 不发送 Sec-Ch-Ua 系列头。给 Safari 的 UA 配 Chrome 的提示头，"
            "是典型的『拼凑伪造』特征。"
        ))

    # ---- 规则 5：有 Sec-Fetch-Mode: navigate 时应该有 Sec-Fetch-User ----
    if h.get("sec-fetch-mode") == "navigate" and "sec-fetch-user" not in h:
        issues.append(ConsistencyIssue(
            "警告", "Sec-Fetch-User",
            "导航请求通常带 Sec-Fetch-User: ?1，缺失可能被识别。"
        ))

    # ---- 规则 6：XHR 请求不该有 Sec-Fetch-User ----
    if h.get("sec-fetch-mode") == "cors" and "sec-fetch-user" in h:
        issues.append(ConsistencyIssue(
            "警告", "Sec-Fetch-User",
            "XHR/CORS 请求不带 Sec-Fetch-User。这里带了，场景不自洽。"
        ))

    # ---- 规则 7：Accept-Encoding 不支持 br 却在宣称 Chrome ----
    ae = h.get("accept-encoding", "")
    if "chrome" in ua.lower() and ae and "br" not in ae:
        issues.append(ConsistencyIssue(
            "警告", "Accept-Encoding",
            "Chrome 默认支持 br（Brotli），这里没声明。httpx 默认不会自动加 br，"
            "需要装 brotli 并显式声明。"
        ))

    # ---- 规则 8：Connection: keep-alive 在 HTTP/2 里不存在 ----
    if "connection" in h:
        issues.append(ConsistencyIssue(
            "警告", "Connection",
            "HTTP/2 里没有 Connection 头（httpx 用 http2=True 时会自动处理）。"
            "如果目标站走 HTTP/2，手动设这个头反而异常。"
        ))

    return issues


def demo_consistency() -> None:
    """演示自洽性检查器。"""
    print("\n" + "=" * 84)
    print("【实验 4】自洽性检查器：找出伪造头部的破绽")
    print("=" * 84)

    cases: list[tuple[str, dict[str, str]]] = [
        ("场景 A：naive 伪造（只改 UA）", {
            "User-Agent": UA_CASES[3].ua,
        }),
        ("场景 B：拼接错误（Safari UA + Chrome 提示头）", {
            "User-Agent": UA_CASES[4].ua,
            "Sec-Ch-Ua": '"Chromium";v="122", "Google Chrome";v="122"',
            "Sec-Ch-Ua-Platform": '"Windows"',
        }),
        ("场景 C：版本不一致", {
            "User-Agent": UA_CASES[3].ua,
            "Sec-Ch-Ua": '"Chromium";v="120", "Google Chrome";v="120"',
            "Sec-Ch-Ua-Platform": '"Windows"',
        }),
        ("场景 D：完整且自洽（正确示范）", dict(BROWSER_HEADERS)),
    ]

    for name, hdrs in cases:
        issues = check_consistency(hdrs)
        print(f"\n▸ {name}")
        if not issues:
            print("   ✓ 未发现问题，头部自洽")
        else:
            for i in issues:
                icon = "🚨" if i.level == "严重" else "⚠️ "
                print(f"   {icon} [{i.level}] {i.field}")
                print(f"        {i.message}")


# ============================================================
# 四、TLS 指纹（JA3）原理
# ============================================================
def compute_ja3_like() -> None:
    """
    演示 JA3 指纹的计算原理。

    说明：真实的 JA3 需要从 TLS ClientHello 报文里提取字段。
    Python 的 ssl 模块把它封装了，无法直接拿到。
    这里用"如果能拿到字段会怎么算"的方式演示原理，
    同时展示为什么 Python 默认 TLS 栈会被识别。
    """
    print("\n" + "=" * 84)
    print("【实验 5】TLS 指纹（JA3）原理")
    print("=" * 84)

    print("""
  JA3 指纹的计算方式：
    从 TLS ClientHello 报文里提取 5 个字段，拼成字符串后取 MD5：

      TLSVersion,CipherSuites,Extensions,EllipticCurves,EllipticCurvePointFormats

    例：771,4865-4866-4867-49195-49199,0-23-65281-10-11-35-16-5-13-18-51-45-43-27,
        29-23-24,0
    → md5 →  51c64c77e60f3980eea90869b68c58a8

  为什么这个能识别爬虫：
    · Chrome / Firefox / Safari / curl / Python-requests 的
      ClientHello 报文各不相同 → JA3 各不相同
    · 服务端拿到 JA3 一查表：「这是 Python requests」→ 直接拒绝
    · 关键是：这个指纹在【TCP 握手之后、HTTP 之前】就暴露了，
      所以你改多少 HTTP 头都没用。
""")

    # 用 ssl 模块展示本机 Python 的 TLS 实测参数
    ctx = ssl.create_default_context()
    print("  本机 Python 的 TLS 配置（部分可观测信息）：")
    print(f"    最高协议版本 : {ctx.maximum_version.name}")
    print(f"    最低协议版本 : {ctx.minimum_version.name}(默认)")
    try:
        ciphers = ctx.get_ciphers()
        names = [c["name"] for c in ciphers[:12]]
        print(f"    默认加密套件 : {len(ciphers)} 个，前 12 个：")
        for n in names:
            print(f"      {n}")
    except Exception as e:                      # noqa: BLE001
        print(f"    获取套件失败：{e}")

    # 用字段拼一个"类 JA3"字符串演示哈希过程
    fake_fields = (
        "771,"
        "4865-4866-4867-49195-49199-49196-49200-52393-52392-49171-49172-"
        "156-157-47-53-10-51-45-43-13-11-35-16-5-18,"
        "0-23-65281-10-11-35-16-5-13-18-51-45-43-27-21,"
        "29-23-24-25-256-257-258-259,0"
    )
    fingerprint = hashlib.md5(fake_fields.encode()).hexdigest()
    print(f"\n  演示：拼出字段串后取 MD5")
    print(f"    字段串（截断）：{fake_fields[:70]}…")
    print(f"    JA3 指纹      ：{fingerprint}")
    print("\n  → 服务端只要维护一张『指纹 → 客户端类型』对照表，")
    print("    就能在 HTTP 请求发出前就判定你是不是浏览器。")

    print("\n" + "-" * 84)
    print("  应对方案：curl_cffi（伪装浏览器 TLS 指纹）")
    print("-" * 84)
    print("""
  # pip install curl_cffi
  from curl_cffi import requests as cffi_requests

  # impersonate 参数直接复用浏览器的完整指纹（TLS + HTTP/2 + 头部顺序）
  resp = cffi_requests.get(
      url,
      impersonate="chrome122",     # 可选 chrome120/chrome124/firefox133/safari17_0
      timeout=20,
  )
  print(resp.status_code, len(resp.text))

  实测行为（本环境未安装 curl_cffi，此处为用法说明）：
    · curl_cffi 的 ClientHello 与真实 Chrome 逐字节一致
    · 连 HTTP/2 的 SETTINGS 帧顺序、头部大小写都会模仿
    · 比手写 headers 的伪装层级更深 —— 它解决的是"HTTP 之前"的问题
""")


# ============================================================
# 五、生产可用的请求头工厂
# ============================================================
@dataclass
class HeaderFactory:
    """
    请求头工厂：生成自洽、可随机、带粘性的浏览器请求头。

    设计要点：
      · '粘性' —— 同一个会话内 UA 与 Sec-Ch-Ua 必须一致，不能每次请求都换
      · '自洽' —— 所有字段由同一份浏览器档案派生，不手工拼凑
      · '场景化' —— 导航请求/XHR 请求的 Sec-Fetch-* 不同

    Attributes:
        profile: 当前浏览器档案。
        platform: 操作系统。
    """

    PROFILES: list[dict[str, str]] = field(default_factory=lambda: [
        {
            "browser": "chrome",
            "version": "122",
            "ua_platform": "Windows NT 10.0; Win64; x64",
            "ch_platform": "Windows",
            "mobile": "?0",
        },
        {
            "browser": "chrome",
            "version": "121",
            "ua_platform": "Macintosh; Intel Mac OS X 10_15_7",
            "ch_platform": "macOS",
            "mobile": "?0",
        },
    ])
    profile: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """初始化时随机选一个档案（模拟"换浏览器"）。"""
        if not self.profile:
            self.profile = random.choice(self.PROFILES)

    def user_agent(self) -> str:
        """生成与档案一致的 UA。"""
        p = self.profile
        if p["browser"] == "chrome":
            return (f"Mozilla/5.0 ({p['ua_platform']}) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{p['version']}.0.0.0 Safari/537.36")
        raise ValueError(f"未支持的浏览器：{p['browser']}")

    def build(
        self,
        referer: str | None = None,
        mode: str = "navigate",
        accept: str | None = None,
    ) -> dict[str, str]:
        """
        构建一套自洽的请求头。

        Args:
            referer: 来源页地址。None 表示直接导航。
            mode: 'navigate'（页面）或 'cors'（XHR）。
            accept: 覆盖 Accept 头（XHR 常用 application/json）。

        Returns:
            请求头字典。
        """
        p = self.profile
        ua = self.user_agent()
        ver = p["version"]

        headers: dict[str, str] = {
            "User-Agent": ua,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            # 客户端提示头：与 UA 版本严格一致
            "Sec-Ch-Ua": (f'"Chromium";v="{ver}", "Not(A:Brand";v="24", '
                          f'"Google Chrome";v="{ver}"'),
            "Sec-Ch-Ua-Mobile": p["mobile"],
            "Sec-Ch-Ua-Platform": f'"{p["ch_platform"]}"',
        }

        if mode == "navigate":
            headers.update({
                "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                           "image/avif,image/webp,image/apng,*/*;q=0.8"),
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            })
        else:                                   # cors / XHR
            headers.update({
                "Accept": accept or "application/json, text/plain, */*",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
            })

        # Sec-Fetch-Site 与 Referer 必须匹配
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        else:
            headers["Sec-Fetch-Site"] = "none"

        return headers


def demo_header_factory() -> None:
    """演示请求头工厂。"""
    print("\n" + "=" * 84)
    print("【实验 6】请求头工厂：自动生成自洽头部")
    print("=" * 84)

    # 同一工厂实例多次 build —— UA 保持一致（粘性）
    factory = HeaderFactory()
    print(f"\n  随机选中的浏览器档案：{factory.profile['browser']} "
          f"{factory.profile['version']} / {factory.profile['ch_platform']}")

    print("\n▸ 场景一：直接访问页面（无 Referer）")
    h1 = factory.build()
    for k, v in h1.items():
        print(f"    {k}: {v[:72]}")

    print("\n▸ 场景二：从列表页点进详情页")
    h2 = factory.build(referer="https://example.com/catalogue/")
    print(f"    Referer: {h2['Referer']}")
    print(f"    Sec-Fetch-Site: {h2['Sec-Fetch-Site']}")

    print("\n▸ 场景三：XHR 接口请求")
    h3 = factory.build(referer="https://example.com/detail/1", mode="cors")
    print(f"    Accept: {h3['Accept']}")
    print(f"    Sec-Fetch-Mode: {h3['Sec-Fetch-Mode']}")
    print(f"    Sec-Fetch-Dest: {h3['Sec-Fetch-Dest']}")
    print(f"    （注意：XHR 不带 Sec-Fetch-User）")

    # 自洽性验证
    print("\n▸ 自洽性验证")
    for name, hdrs in [("页面导航", h1), ("详情页", h2), ("XHR", h3)]:
        issues = [i for i in check_consistency(hdrs) if i.level == "严重"]
        status = "✓ 自洽" if not issues else f"✗ 发现 {len(issues)} 个严重问题"
        print(f"    {name:<12} {status}")
        for i in issues:
            print(f"        {i.field}: {i.message[:60]}")

    print("\n▸ 粘性验证：连续 5 次生成，UA 应保持一致")
    uas = {factory.user_agent() for _ in range(5)}
    print(f"    生成了 {len(uas)} 个不同的 UA：{'✓ 有粘性（正确）' if len(uas) == 1 else '✗ 不稳定'}")

    print("\n▸ 对比：如果每次新建工厂（错误做法）")
    uas2 = {HeaderFactory().user_agent() for _ in range(5)}
    print(f"    生成了 {len(uas2)} 个不同的 UA —— 同一会话里来回换 UA 是明显异常")


# ============================================================
# 主流程
# ============================================================
def main() -> None:
    """运行全部实验。"""
    test_user_agents()
    print_header_checklist()
    demo_consistency()
    compute_ja3_like()
    demo_header_factory()

    print("\n" + "=" * 84)
    print("本节要点总结")
    print("=" * 84)
    print("""
  1. 只改 UA 是最初级的伪装。现代风控看的是【整套头部是否自洽】。

  2. 三条最容易被抓的矛盾：
       · UA 说 Chrome，但没有 Sec-Ch-Ua
       · UA 版本与 Sec-Ch-Ua 版本不一致
       · Sec-Fetch-Site 与 Referer 对不上

  3. 头部要有【粘性】：同一会话内不能来回换 UA。
     正确做法是用一个 HeaderFactory 实例，而不是每次请求都随机。

  4. TLS 指纹（JA3）在 HTTP 之前就暴露了，
     改 HTTP 头解决不了 —— 需要 curl_cffi 这类方案。

  5. 排查顺序：
       ① 换成真实 UA 试试
       ② 补齐 Sec-Ch-Ua / Sec-Fetch-* 系列
       ③ 加上正确的 Referer
       ④ 还不行 → 检查 TLS 指纹或 IP

  ⚠️  合规提醒：以上技术仅用于授权测试与公开数据采集。
      伪造指纹本身不违法，但用它突破技术保护措施获取非公开数据可能违法。
""")


if __name__ == "__main__":
    main()
