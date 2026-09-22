"""
阶段 4 · 4.1 反爬全景图
======================================================
对应网页章节：#s4-1

本脚本是一个"反爬手段识别器"：
  给一个 HTTP 响应，自动分析对方用了哪些反爬手段。

为什么先做识别？因为对抗的前提是识别 ——
90% 的人卡住不是"不会破解"，而是"不知道对方用了什么"。

运行：python3 40_antibot_overview.py
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
# 一、反爬手段分类（这是阶段 4 的"地图"）
# ============================================================
class Layer(str, Enum):
    """反爬手段所属层次。"""

    TRANSPORT = "传输层"
    HEADER = "请求头层"
    SESSION = "会话层"
    CONTENT = "内容层"
    BEHAVIOR = "行为层"
    RATE = "频率层"


@dataclass(frozen=True)
class Technique:
    """一种反爬手段。"""

    name: str
    layer: Layer
    symptom: str
    detect: str
    counter: str
    difficulty: int          # 1-5
    section: str             # 对应的教程章节


# 这是阶段 4 的完整地图 —— 本阶段所有章节都在填这张表
TECHNIQUES: list[Technique] = [
    Technique(
        "TLS 指纹", Layer.TRANSPORT,
        "curl 能拿到数据，Python requests 拿到 403",
        "检查 ClientHello 的 JA3 指纹",
        "curl_cffi / tls-client 伪装浏览器指纹",
        4, "4.2",
    ),
    Technique(
        "HTTP/2 强制", Layer.TRANSPORT,
        "只能走 HTTP/2，HTTP/1.1 请求被拒绝",
        "响应头无 HTTP/1.1 特征，需 h2 支持",
        "httpx(http2=True) 或 curl_cffi",
        3, "4.2",
    ),
    Technique(
        "User-Agent 校验", Layer.HEADER,
        "返回 403，提示浏览器版本过低",
        "换 UA 后是否恢复正常",
        "用真实浏览器 UA，且与其余头部自洽",
        1, "4.2",
    ),
    Technique(
        "头部字段完整性", Layer.HEADER,
        "UA 对了但还 403",
        "对比浏览器请求，看缺了哪些头（Sec-Ch-Ua 等）",
        "补齐 Sec-Fetch-*、Sec-Ch-Ua-*、Accept-* 等",
        2, "4.2",
    ),
    Technique(
        "Referer 校验", Layer.HEADER,
        "直接访问详情页失败，从列表页跳转成功",
        "加上来源页地址试试",
        "按真实导航链路构造 Referer",
        1, "4.2",
    ),
    Technique(
        "Cookie 会话校验", Layer.SESSION,
        "直接请求接口返回未登录",
        "清空 Cookie 后是否还能拿到数据",
        "先请求首页拿 Cookie，再带 Cookie 请求接口",
        2, "4.3",
    ),
    Technique(
        "Token 签名", Layer.SESSION,
        "每次请求带上时间戳/token，过期即失效",
        "看 URL 或头部是否有动态参数",
        "还原签名算法",
        3, "4.3",
    ),
    Technique(
        "JS 动态渲染", Layer.CONTENT,
        "requests 拿到的 HTML 里没有数据",
        "对比浏览器 Elements 与 View Source",
        "Playwright 渲染，或找 XHR 接口",
        2, "4.4",
    ),
    Technique(
        "JS 混淆加密", Layer.CONTENT,
        "请求参数是一串看不懂的密文",
        "搜索参数名，定位加密函数",
        "扣代码 / 补环境执行",
        5, "4.4",
    ),
    Technique(
        "参数签名（sign）", Layer.CONTENT,
        "所有参数都对，但服务端返回签名错误",
        "搜索 sign / _signature / nonce",
        "找到签名函数并还原算法",
        4, "4.5",
    ),
    Technique(
        "字体反爬", Layer.CONTENT,
        "页面能看到数字，但 HTML 里是乱码字符",
        "查看源码里是否是私有区 Unicode",
        "解析字体文件映射表",
        5, "4.6",
    ),
    Technique(
        "CSS 偏移", Layer.CONTENT,
        "文本内容正确但位置错乱",
        "检查元素 style 里的 position/left",
        "解析 CSS 规则还原文案",
        4, "4.6",
    ),
    Technique(
        "验证码", Layer.BEHAVIOR,
        "关键操作前弹出验证码",
        "触发后响应要求验证",
        "OCR 识别 / 打码平台 / 行为模拟",
        4, "4.7",
    ),
    Technique(
        "滑块验证", Layer.BEHAVIOR,
        "出现拖动滑块验证",
        "无法用纯 HTTP 完成",
        "Playwright + 拟人轨迹",
        4, "4.7",
    ),
    Technique(
        "鼠标轨迹检测", Layer.BEHAVIOR,
        "行为异常被封（如瞬间完成鼠标移动）",
        "检查是否采集 mousemove 事件",
        "Playwright 模拟贝塞尔曲线轨迹",
        4, "4.7",
    ),
    Technique(
        "IP 限频", Layer.RATE,
        "请求几十次后开始 429 / 503",
        "统计返回 429 前的请求数",
        "降低频率 / 加代理池",
        2, "4.8",
    ),
    Technique(
        "IP 封禁", Layer.RATE,
        "所有请求都 403，换网络后恢复",
        "换 IP 是否恢复",
        "代理池轮换",
        3, "4.8",
    ),
]


def print_map() -> None:
    """打印反爬全景图。"""
    print("=" * 92)
    print("阶段 4 反爬全景图")
    print("=" * 92)

    for layer in Layer:
        items = [t for t in TECHNIQUES if t.layer is layer]
        if not items:
            continue
        print(f"\n▍{layer.value}")
        print(f"  {'手段':<18}{'难度':<6}{'典型症状':<38}{'对应章节'}")
        print("  " + "-" * 86)
        for t in items:
            stars = "★" * t.difficulty + "☆" * (5 - t.difficulty)
            print(f"  {t.name:<18}{stars:<6}{t.symptom[:34]:<38}{t.section}")

    print("\n" + "=" * 92)
    print("按难度排序：该从哪儿入手")
    print("=" * 92)
    print(f"  {'难度':<8}{'手段数':<8}{'说明'}")
    print("  " + "-" * 86)
    for level in range(1, 6):
        items = [t for t in TECHNIQUES if t.difficulty == level]
        if not items:
            continue
        note = {
            1: "看两眼就能解决，先查这里",
            2: "需要一点分析，但都能自己做",
            3: "需要理解协议细节或会调试",
            4: "需要会 JS 或浏览器自动化",
            5: "需要逆向功底，是接单溢价点",
        }[level]
        names = "、".join(t.name for t in items)
        print(f"  {'★' * level:<8}{len(items):<8}{note}")
        print(f"  {'':<16}{names}")


# ============================================================
# 二、诊断器：给响应自动判断用了哪些手段
# ============================================================
@dataclass
class Diagnosis:
    """诊断结果。"""

    url: str
    status: int = 0
    findings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def report(self) -> str:
        """生成诊断报告。"""
        lines = [
            "=" * 78,
            f"诊断报告：{self.url}",
            "=" * 78,
            f"  HTTP 状态：{self.status}",
        ]
        if self.findings:
            lines.append("\n  发现的线索：")
            for f in self.findings:
                lines.append(f"    · {f}")
        else:
            lines.append("\n  未发现明显反爬特征（可能是目标站本身宽松）")

        if self.suggestions:
            lines.append("\n  建议尝试：")
            for s in self.suggestions:
                lines.append(f"    → {s}")
        return "\n".join(lines)


# 常见的反爬指纹特征
ANTIBOT_MARKERS: list[tuple[str, str, str]] = [
    ("Just a moment", "Cloudflare 5 秒盾", "等待几秒后重试，或直接用 Playwright 过盾"),
    ("cf-browser-verification", "Cloudflare JS 挑战", "用 Playwright 渲染一次拿到 Cookie"),
    ("__cf_chl", "Cloudflare 挑战页", "同上"),
    ("Access Denied", "通用访问拒绝", "检查 UA / Referer / Cookie 是否完整"),
    ("您的访问过于频繁", "频率限制", "降低速率，或加代理池"),
    ("请开启 JavaScript", "强制 JS 渲染", "用 Playwright，或找 XHR 接口"),
    ("验证码", "验证码拦截", "OCR / 打码平台 / 行为模拟"),
    ("请稍后再试", "软性限流", "退避重试（带抖动）"),
    ("verify", "可能有验证环节", "查看是否有 /verify 接口"),
]

# 响应头里的反爬线索
HEADER_MARKERS: list[tuple[str, str, str]] = [
    ("x-iplb-request-id", "可能用了负载均衡 + 风控", "检查是否有 WAF 特征头"),
    ("cf-ray", "Cloudflare 前置", "需要处理 CF 的 JS 挑战或指纹"),
    ("x-cache", "CDN 缓存", "注意缓存可能返回旧数据"),
    ("set-cookie", "服务端下发会话", "下一步要带上这个 Cookie"),
    ("retry-after", "明确限流", "按 Retry-After 的值退避"),
    ("x-ratelimit", "有速率限制头", "读取限制值并据此调速"),
    ("server", "服务器类型", "据此判断可能的技术栈"),
]


def diagnose(resp: httpx.Response, url: str) -> Diagnosis:
    """
    分析一个响应，判断可能用到的反爬手段。

    Args:
        resp: HTTP 响应。
        url: 请求地址。

    Returns:
        Diagnosis 诊断结果。
    """
    d = Diagnosis(url=url, status=resp.status_code)
    body = resp.text

    # ---- 状态码线索 ----
    if resp.status_code == 403:
        d.findings.append("403 Forbidden —— 身份被拒（UA/指纹/Cookie 可能有问题）")
        d.suggestions.append("对比浏览器完整请求头，逐项排查差异")
        d.suggestions.append("检查是否被 IP 封禁（换个网络试试）")
    elif resp.status_code == 429:
        d.findings.append("429 Too Many Requests —— 触发了频率限制")
        ra = resp.headers.get("retry-after")
        if ra:
            d.findings.append(f"  服务端明确要求等待 {ra} 秒")
        d.suggestions.append("降低并发/速率，按 Retry-After 退避")
    elif resp.status_code == 503:
        d.findings.append("503 —— 可能是 WAF 拦截或服务端过载")
        d.suggestions.append("退避重试；若持续 503，检查请求指纹")

    # ---- 响应体线索 ----
    for marker, name, advice in ANTIBOT_MARKERS:
        if marker.lower() in body.lower():
            d.findings.append(f"响应体含『{marker}』→ 疑似 {name}")
            d.suggestions.append(advice)

    # ---- 响应头线索 ----
    header_names = {k.lower() for k in resp.headers}
    for marker, name, advice in HEADER_MARKERS:
        if marker in header_names:
            val = resp.headers.get(marker, "")[:60]
            d.findings.append(f"响应头 {marker}: {val} → {name}")

    # ---- 内容层线索 ----
    js_render_markers = ["<noscript", "window.__INITIAL_STATE__", "enable JavaScript"]
    for m in js_render_markers:
        if m.lower() in body.lower():
            d.findings.append(f"含『{m}』→ 可能需要 JS 渲染")
            d.suggestions.append("用 Playwright 渲染，或抓 XHR 接口")
            break

    # ---- 检查是否有动态参数（签名）----
    sign_patterns = [r"sign=", r"_signature=", r"nonce=", r"timestamp=", r"token="]
    found_signs = [p for p in sign_patterns if re.search(p, url, re.I)]
    if found_signs:
        d.findings.append(f"URL 含动态参数 {found_signs} → 可能有签名机制")
        d.suggestions.append("搜索该参数名，定位生成函数")

    # ---- 数据是否真的在 HTML 里 ----
    d.evidence["body_length"] = len(body)
    d.evidence["script_count"] = body.count("<script")
    if len(body) > 5000 and body.count("<script") > 10:
        d.findings.append(
            f"页面有 {body.count('<script')} 个 script 标签"
            f"（{len(body)} 字节）→ 数据可能由 JS 注入"
        )

    return d


# ============================================================
# 三、实测：对真实站点做诊断
# ============================================================
TARGETS = [
    ("https://books.toscrape.com/", "无防御的练习靶站（对照组）"),
    ("https://httpbin.org/status/403", "模拟 403"),
    ("https://httpbin.org/status/429", "模拟 429"),
]


def run_diagnosis() -> None:
    """对若干站点运行诊断器。"""
    print("\n" + "=" * 78)
    print("诊断器实测")
    print("=" * 78)

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/122.0.0.0 Safari/537.36"),
    }

    for url, desc in TARGETS:
        print(f"\n▸ 目标：{url}")
        print(f"  说明：{desc}")
        try:
            resp = httpx.get(url, headers=headers, timeout=20, follow_redirects=True)
            d = diagnose(resp, url)
            print()
            for line in d.report().splitlines()[3:]:
                print("  " + line)
        except Exception as e:                  # noqa: BLE001
            print(f"  请求失败：{type(e).__name__}: {e}")


# ============================================================
# 四、排查决策树
# ============================================================
DECISION_TREE = """
  抓不到数据？
      │
      ├─ 状态码 403 / 406
      │     ├─ 换真实 UA 后好了？        → 只是 UA 校验（难度 ★）
      │     ├─ 补齐 Sec-Fetch-* 后好了？ → 头部完整性（难度 ★★）
      │     ├─ 带 Cookie 后好了？        → 会话校验（难度 ★★）
      │     └─ 都不行 → 可能是 TLS 指纹或 IP 封禁（难度 ★★★★）
      │
      ├─ 状态码 429 / 503
      │     ├─ 降低频率后好了？          → 限频（难度 ★★）
      │     └─ 换 IP 后好了？            → IP 封禁（难度 ★★★）
      │
      ├─ 状态码 200 但数据是空的
      │     ├─ View Source 里就没有？    → JS 渲染（难度 ★★）
      │     │     ├─ 找 XHR 接口 → 直接请求接口（更优解）
      │     │     └─ 用 Playwright 渲染
      │     └─ Source 里有但选择器取不到 → 字体反爬 / CSS 偏移（难度 ★★★★★）
      │
      └─ 状态码 200，数据对，但请求发不出去
            └─ 参数有签名 → JS 逆向（难度 ★★★★）

  排查顺序原则：
    从简单到复杂。先花 10 分钟试完所有低难度手段，
    再决定要不要投入几天做逆向。多数"疑似逆向"其实是 UA 没配对。
"""


def main() -> None:
    """运行全部内容。"""
    print_map()
    print("\n" + "=" * 92)
    print("排查决策树")
    print("=" * 92)
    print(DECISION_TREE)

    run_diagnosis()

    print("\n" + "=" * 92)
    print("阶段 4 的学习心法")
    print("=" * 92)
    print("  1. 先识别，再对抗 —— 90% 的卡点是『不知道对方用了什么』")
    print("  2. 从易到难 —— 别一上来就想着逆向，先试完所有简单手段")
    print("  3. 找接口胜过渲染 —— 能直接调 XHR 接口，就别用浏览器渲染")
    print("  4. 逆向是最后手段 —— 成本高、易失效、法律风险也更高")
    print()
    print("  ⚠️  合规提醒：")
    print("     本阶段所有技术只应用于：① 自己的系统 ② 明确授权的测试")
    print("     ③ 公开数据且不违反 robots.txt 与服务条款的采集。")
    print("     绕过技术保护措施可能违反《网络安全法》《反不正当竞争法》，")
    print("     详见 4.9 合规红线。")


if __name__ == "__main__":
    main()
