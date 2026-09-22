"""
阶段 4 · 4.3 Cookie 与会话管理
======================================================
对应网页章节：#s4-3

本脚本实测：
  1. Cookie 的完整生命周期（Set-Cookie → 存储 → 回传）
  2. Cookie 属性详解（Domain / Path / Expires / HttpOnly / Secure / SameSite）
  3. 会话保持：requests.Session vs httpx.Client vs 手动管理
  4. Token 类会话：Bearer / JWT 的结构与过期处理
  5. 登录态持久化的正确做法（不硬编码、可刷新）
  6. 会话失效的自动检测与重建

运行：python3 42_cookie_session.py
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path

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


WORK = Path("/root/.codebuddy/artifact/spider_systematic/session_demo")
WORK.mkdir(parents=True, exist_ok=True)


# ============================================================
# 一、Cookie 属性详解
# ============================================================
@dataclass(frozen=True)
class CookieAttr:
    """Cookie 属性的含义与爬虫相关性。"""

    name: str
    meaning: str
    crawler_note: str


COOKIE_ATTRS: list[CookieAttr] = [
    CookieAttr(
        "Domain", "该 Cookie 属于哪个域",
        "子域不共享（除 .example.com 这种写法）。跨子域登录态要特别注意",
    ),
    CookieAttr(
        "Path", "该 Cookie 在哪些路径下发送",
        "默认是当前路径。/admin 下发的 Cookie 不会发给 /api",
    ),
    CookieAttr(
        "Expires / Max-Age", "过期时间",
        "会话 Cookie（不设此项）关浏览器即失效；爬虫要保存到期时间并在过期前刷新",
    ),
    CookieAttr(
        "HttpOnly", "禁止 JS 读取",
        "爬虫无影响（我们不走 JS），但说明这是安全敏感 Cookie，通常是登录态",
    ),
    CookieAttr(
        "Secure", "只在 HTTPS 下发送",
        "用 http:// 请求会丢 Cookie —— 这是个常见坑",
    ),
    CookieAttr(
        "SameSite", "跨站请求策略（Strict/Lax/None）",
        "只影响浏览器行为。爬虫直接用 HTTP 客户端不受限制，"
        "但这意味着服务端可能用别的机制校验来源",
    ),
]


def print_cookie_attrs() -> None:
    """打印 Cookie 属性表。"""
    print("=" * 84)
    print("【表 1】Cookie 属性详解")
    print("=" * 84)
    print(f"  {'属性':<22}{'含义':<30}{'对爬虫的意义'}")
    print("  " + "-" * 80)
    for a in COOKIE_ATTRS:
        print(f"  {a.name:<22}{a.meaning:<30}{a.crawler_note}")


# ============================================================
# 二、解析真实 Set-Cookie 头
# ============================================================
SAMPLE_SET_COOKIE = [
    "sessionid=abc123def456; Domain=.example.com; Path=/; "
    "Expires=Wed, 09 Jun 2027 10:18:14 GMT; HttpOnly; Secure; SameSite=Lax",

    "csrf_token=xyz789; Path=/; Max-Age=3600; SameSite=Strict",

    "tracking_id=t-99887; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT",
]


@dataclass
class ParsedCookie:
    """解析后的 Cookie。"""

    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: str = ""
    max_age: int | None = None
    http_only: bool = False
    secure: bool = False
    same_site: str = ""

    @property
    def expired(self) -> bool:
        """是否已被标记为删除（Expires 在过去）。"""
        if not self.expires:
            return False
        try:
            dt = datetime.strptime(self.expires, "%a, %d %b %Y %H:%M:%S %Z")
            return dt.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
        except ValueError:
            return False

    @property
    def is_session_cookie(self) -> bool:
        """是否会话 Cookie（浏览器关闭即失效）。"""
        return not self.expires and self.max_age is None

    def flags(self) -> list[str]:
        """返回安全标志列表。"""
        out = []
        if self.http_only:
            out.append("HttpOnly")
        if self.secure:
            out.append("Secure")
        if self.same_site:
            out.append(f"SameSite={self.same_site}")
        return out


def parse_set_cookie(header: str) -> ParsedCookie:
    """
    解析一条 Set-Cookie 头。

    Args:
        header: Set-Cookie 头的值。

    Returns:
        ParsedCookie 对象。
    """
    sc = SimpleCookie()
    sc.load(header)
    name = next(iter(sc))
    morsel = sc[name]

    max_age: int | None = None
    if morsel["max-age"]:
        try:
            max_age = int(morsel["max-age"])
        except ValueError:
            max_age = None

    return ParsedCookie(
        name=name,
        value=morsel.value,
        domain=morsel["domain"] or "(当前域)",
        path=morsel["path"] or "/",
        expires=morsel["expires"],
        max_age=max_age,
        http_only=bool(morsel["httponly"]),
        secure=bool(morsel["secure"]),
        same_site=morsel["samesite"],
    )


def demo_parse_cookies() -> None:
    """演示 Cookie 解析。"""
    print("\n" + "=" * 84)
    print("【实验 1】解析真实的 Set-Cookie 头")
    print("=" * 84)

    for raw in SAMPLE_SET_COOKIE:
        c = parse_set_cookie(raw)
        print(f"\n▸ 原始头（截断）：{raw[:70]}…")
        print(f"    名称/值   : {c.name} = {c.value}")
        print(f"    Domain    : {c.domain}")
        print(f"    Path      : {c.path}")
        if c.expires:
            print(f"    Expires   : {c.expires}")
        if c.max_age is not None:
            print(f"    Max-Age   : {c.max_age} 秒")
        print(f"    安全标志  : {', '.join(c.flags()) or '(无)'}")
        print(f"    会话 Cookie: {'是' if c.is_session_cookie else '否'}")
        if c.expired:
            print("    ⚠️  Expires 在过去 → 这是服务端在【删除】该 Cookie")

    print("""
  从爬虫视角看这三条 Cookie：
    · sessionid  → 关键！这是登录态，必须保存并在后续请求带上
    · csrf_token → 通常需要从页面里取出来，再放进请求头/参数
    · tracking_id → Expires 在过去，说明服务端在下发"删除指令"，忽略即可
""")


# ============================================================
# 三、会话保持的三种方式
# ============================================================
def demo_session_managers() -> None:
    """对比三种会话管理方式。"""
    print("\n" + "=" * 84)
    print("【实验 2】会话保持：三种方式对比")
    print("=" * 84)

    print("""
▸ 方式一：手动管理 Cookie（能看清原理，生产不推荐）

    cookies = {}
    r = httpx.get("https://example.com/login", ...)
    cookies.update(r.cookies)               # 手动收集
    r2 = httpx.get("https://example.com/data", cookies=cookies, ...)

  问题：
    · 要自己处理 Domain / Path 匹配
    · 要自己处理过期
    · 要自己处理同一域名多个 Cookie 的情况
    → 出错概率高，且看不出错在哪

▸ 方式二：Session / Client（推荐）

    # requests（同步）
    with requests.Session() as s:
        s.get("https://example.com/login", ...)    # Cookie 自动存入
        r = s.get("https://example.com/data", ...)  # Cookie 自动带上
        print(s.cookies)                            # 随时可查看

    # httpx（同步或异步）
    with httpx.Client() as c:
        c.get("https://example.com/login", ...)
        r = c.get("https://example.com/data", ...)

  优势：
    · Cookie 自动存取，自动处理 Domain/Path/过期
    · 自动复用 TCP 连接（性能）
    · 可以随时导出/导入 Cookie，便于持久化

▸ 方式三：外部持久化（长时间运行必用）

    # 保存
    import json
    data = [{"name": c.name, "value": c.value, "domain": c.domain,
             "path": c.path} for c in client.cookies.jar]
    Path("cookies.json").write_text(json.dumps(data))

    # 加载
    for item in json.loads(Path("cookies.json").read_text()):
        client.cookies.set(item["name"], item["value"],
                           domain=item["domain"], path=item["path"])

  用途：爬虫重启后不用重新登录
""")


# ============================================================
# 四、实测：用 httpbin 验证 Cookie 来回
# ============================================================
def demo_cookie_roundtrip() -> None:
    """用 httpbin 实测 Cookie 的完整流程。"""
    print("\n" + "=" * 84)
    print("【实验 3】实测 Cookie 完整往返（httpbin）")
    print("=" * 84)

    with httpx.Client(timeout=20) as client:
        # 步骤 1：服务端下发 Cookie
        print("\n▸ 步骤 1：请求 /cookies/set，服务端下发 Cookie")
        try:
            r1 = client.get(
                "https://httpbin.org/cookies/set",
                params={"sessionid": "abc123", "role": "user"},
                follow_redirects=False,
            )
            set_cookies = r1.headers.get_list("set-cookie")
            print(f"    状态码：{r1.status_code}（302 重定向 —— 服务端下发了 Cookie）")
            print(f"    Set-Cookie 头：")
            for sc in set_cookies:
                print(f"      {sc}")
            print(f"\n    client 自动存储的 Cookie：")
            for c in client.cookies.jar:
                print(f"      {c.name} = {c.value}  (domain={c.domain}, path={c.path})")

        except Exception as e:                  # noqa: BLE001
            print(f"    请求失败：{type(e).__name__}: {e}")

        # 步骤 2：请求时自动带上
        print("\n▸ 步骤 2：请求 /cookies，观察服务端收到了什么")
        try:
            r2 = client.get("https://httpbin.org/cookies")
            seen = r2.json().get("cookies", {})
            print(f"    服务端收到：{seen}")
            print("    ✓ 无需手动传参，Client 自动带上了 Cookie")
        except Exception as e:                  # noqa: BLE001
            print(f"    请求失败：{type(e).__name__}: {e}")

        # 步骤 3：手动设置 Cookie
        print("\n▸ 步骤 3：手动设置一个 Cookie 再请求")
        client.cookies.set("injected", "manual-value", domain="httpbin.org")
        try:
            r3 = client.get("https://httpbin.org/cookies")
            print(f"    服务端收到：{r3.json().get('cookies', {})}")
        except Exception as e:                  # noqa: BLE001
            print(f"    请求失败：{type(e).__name__}: {e}")

        # 步骤 4：导出 / 导入（持久化演示）
        print("\n▸ 步骤 4：导出 Cookie 供下次使用")
        data = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in client.cookies.jar
        ]
        p = WORK / "cookies.json"
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"    已写入 {p.name}（{len(data)} 条）")
        for item in data:
            print(f"      {item['name']} = {item['value']}")

        print("\n▸ 步骤 5：新 Client 导入 Cookie，验证可复用")
        with httpx.Client(timeout=20) as fresh:
            for item in json.loads(p.read_text(encoding="utf-8")):
                fresh.cookies.set(item["name"], item["value"],
                                  domain=item["domain"], path=item["path"])
            try:
                r5 = fresh.get("https://httpbin.org/cookies")
                print(f"    新客户端服务端看到：{r5.json().get('cookies', {})}")
                print("    ✓ 会话已恢复，无需重新登录")
            except Exception as e:              # noqa: BLE001
                print(f"    请求失败：{type(e).__name__}: {e}")


# ============================================================
# 五、Token 类会话
# ============================================================
@dataclass
class TokenInfo:
    """Token 解析结果。"""

    raw: str
    kind: str = "unknown"       # jwt / opaque
    header: dict = field(default_factory=dict)
    payload: dict = field(default_factory=dict)
    expires_at: float = 0.0

    @property
    def is_expired(self) -> bool:
        """是否已过期。"""
        return self.expires_at > 0 and time.time() > self.expires_at

    @property
    def seconds_left(self) -> float:
        """剩余有效秒数。"""
        return max(0.0, self.expires_at - time.time()) if self.expires_at else -1


def parse_token(token: str) -> TokenInfo:
    """
    解析 Token（不校验签名，仅看结构 —— 爬虫通常只需要看结构）。

    Args:
        token: Token 字符串（可能带 Bearer 前缀）。

    Returns:
        TokenInfo 对象。
    """
    token = token.removeprefix("Bearer ").strip()
    info = TokenInfo(raw=token)

    parts = token.split(".")
    if len(parts) != 3:
        info.kind = "opaque（不透明 Token，服务端自行管理）"
        return info

    info.kind = "jwt"

    def b64decode(seg: str) -> dict:
        """解码 JWT 的一段。"""
        seg += "=" * (-len(seg) % 4)        # 补齐 padding
        try:
            return json.loads(base64.urlsafe_b64decode(seg))
        except Exception:                   # noqa: BLE001
            return {}

    info.header = b64decode(parts[0])
    info.payload = b64decode(parts[1])
    if "exp" in info.payload:
        info.expires_at = float(info.payload["exp"])
    return info


def demo_token() -> None:
    """演示 Token 解析与过期判断。"""
    print("\n" + "=" * 84)
    print("【实验 4】Token 类会话（爬虫里的第二种登录态）")
    print("=" * 84)

    # 构造一个演示用 JWT（用当前时间做 exp，便于观察）
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": "user_10086",
        "name": "爬虫用户",
        "iat": now,
        "exp": now + 3600,          # 1 小时后过期
        "scope": ["read:books", "read:user"],
    }

    def b64(obj: dict) -> str:
        """URL-safe base64 编码（去掉 padding）。"""
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    demo_jwt = f"{b64(header)}.{b64(payload)}.FakeSignatureNotVerified"

    print(f"\n▸ 演示 Token（前三段共 {len(demo_jwt)} 字符，签名部分是假的）")
    print(f"    {demo_jwt[:80]}…")

    info = parse_token(demo_jwt)
    print(f"\n▸ 解析结果")
    print(f"    类型   : {info.kind}")
    print(f"    Header : {info.header}")
    print(f"    Payload: {json.dumps(info.payload, ensure_ascii=False)}")
    if info.expires_at:
        exp_dt = datetime.fromtimestamp(info.expires_at, timezone.utc)
        print(f"    过期时间: {exp_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"    剩余    : {info.seconds_left:.0f} 秒")
        print(f"    是否过期: {'是' if info.is_expired else '否'}")

    print("\n▸ 对比：不透明 Token")
    opaque = parse_token("eyJhbGciOiJIUzI1NiJ9")
    print(f"    类型   : {opaque.kind}")

    print("""
  爬虫要做的三件事：
    1. 请求时带上：Authorization: Bearer <token>
    2. 定时检查 exp 字段，提前刷新（别等过期才刷）
    3. 刷新失败时（返回 401）自动重新登录

  注意：JWT 的 payload 是 base64 编码【不是加密】，任何人都能解开看内容。
  → 所以不要试图从 Token 里"破解"什么，它本来就给你看。
""")

    # 过期检测演示
    print("▸ 过期检测演示")
    for delta, label in [(3600, "1 小时后过期"), (30, "30 秒后过期"), (-100, "已过期 100 秒")]:
        expired_payload = {**payload, "exp": now + delta}
        t = parse_token(f"{b64(header)}.{b64(expired_payload)}.sig")
        need_refresh = t.seconds_left < 300        # 提前 5 分钟刷新
        print(f"    {label:<16} 剩余 {t.seconds_left:>7.0f}s  "
              f"过期={t.is_expired!s:<5}  需要刷新={'是 ←' if need_refresh else '否'}")

    print("\n  → 关键：不要等 is_expired 为 True 才刷新，那时请求已经失败了。")
    print("    实践做法是设一个提前量（比如 5 分钟），剩余时间不足就提前换新的。")


# ============================================================
# 六、会话管理器（生产可用）
# ============================================================
class SessionManager:
    """
    会话管理器：自动维持登录态、检测失效、重建会话。

    解决三个实际问题：
      1. 会话过期了不知道 → 每次响应检查是否被踢回登录页
      2. 会话存在内存里，重启就没了 → 持久化到文件
      3. 会话在并发下被多线程/协程竞争 → 加锁保护刷新过程

    Attributes:
        store_path: 会话持久化路径。
        session: 当前会话建立时间。
    """

    # 会话失效的典型信号
    INVALID_SIGNALS = (
        "请先登录", "登录已过期", "未登录", "请重新登录",
        "login required", "unauthorized", "session expired",
    )

    def __init__(self, store_path: Path) -> None:
        self.store_path = store_path
        self.cookies: dict[str, str] = {}
        self.token: str = ""
        self.token_expires_at: float = 0.0
        self.created_at: float = 0.0
        self.rebuild_count: int = 0
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        """从磁盘加载会话。"""
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            self.cookies = data.get("cookies", {})
            self.token = data.get("token", "")
            self.token_expires_at = data.get("token_expires_at", 0.0)
            self.created_at = data.get("created_at", 0.0)
        except (json.JSONDecodeError, OSError):
            # 会话文件损坏时不要崩，重建即可
            self.cookies, self.token = {}, ""

    def save(self) -> None:
        """保存会话到磁盘。"""
        payload = {
            "cookies": self.cookies,
            "token": self.token,
            "token_expires_at": self.token_expires_at,
            "created_at": self.created_at,
            "saved_at": time.time(),
        }
        self.store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------- 会话状态 ----------
    def is_token_expiring(self, margin: float = 300.0) -> bool:
        """
        Token 是否即将过期。

        Args:
            margin: 提前量（秒）。

        Returns:
            True 表示该刷新了。
        """
        if not self.token:
            return False
        return time.time() + margin >= self.token_expires_at

    def is_valid(self) -> bool:
        """会话是否还有效。"""
        if not self.cookies and not self.token:
            return False
        return not (self.token and self.is_token_expiring(0))

    def looked_like_logged_out(self, body: str) -> bool:
        """
        判断响应是否表明会话失效。

        Args:
            body: 响应体。

        Returns:
            True 表示疑似掉登录。
        """
        low = body.lower()
        return any(sig.lower() in low for sig in self.INVALID_SIGNALS)

    # ---------- 会话生命周期 ----------
    def login(self) -> None:
        """
        建立新会话（真实项目里这里是发登录请求）。

        这里模拟服务端行为：下发 Cookie + 有效期 1 小时的 Token。
        """
        self.rebuild_count += 1
        self.created_at = time.time()
        self.cookies = {
            "sessionid": f"sess_{self.rebuild_count}_{int(time.time())}",
            "csrf_token": f"csrf_{hash(time.time()) % 100000:05d}",
        }
        self.token = f"Bearer token_v{self.rebuild_count}"
        self.token_expires_at = time.time() + 3600
        print(f"      [会话] 登录成功（第 {self.rebuild_count} 次）"
              f" cookie={self.cookies['sessionid']}")

    def refresh_if_needed(self) -> bool:
        """
        需要时刷新会话。

        Returns:
            True 表示执行了刷新。
        """
        if self.is_token_expiring(margin=300):
            print(f"      [会话] Token 剩余 {self.token_expires_at - time.time():.0f}s "
                  f"不足 5 分钟，主动刷新")
            self.login()
            self.save()
            return True
        return False

    def reset(self) -> None:
        """会话失效时重建。"""
        print("      [会话] 检测到会话失效，重建中…")
        self.login()
        self.save()


def demo_session_manager() -> None:
    """演示会话管理器的生命周期。"""
    print("\n" + "=" * 84)
    print("【实验 5】会话管理器：自动维持、检测失效、持久化")
    print("=" * 84)

    store = WORK / "session.json"
    if store.exists():
        store.unlink()

    print("\n▸ 首次启动：无会话，执行登录")
    sm = SessionManager(store)
    print(f"    初始状态：is_valid={sm.is_valid()}")
    sm.login()
    sm.save()
    print(f"    登录后：is_valid={sm.is_valid()}，已持久化")

    print("\n▸ 模拟进程重启：从磁盘恢复会话")
    sm2 = SessionManager(store)
    print(f"    恢复的 Cookie：{sm2.cookies}")
    print(f"    恢复的 Token：{sm2.token}")
    print(f"    is_valid={sm2.is_valid()}  ✓ 无需重新登录")

    print("\n▸ 检测会话失效信号")
    tests = [
        ("<html><body>欢迎回来，用户</body></html>", "正常页面"),
        ("<html><body>请先登录后查看</body></html>", "掉登录页"),
        ('{"error": "session expired"}', "接口报会话过期"),
    ]
    for body, label in tests:
        hit = sm2.looked_like_logged_out(body)
        print(f"    {label:<20} → 疑似失效：{hit}")

    print("\n▸ Token 即将过期时自动刷新")
    sm2.token_expires_at = time.time() + 200      # 只剩 200 秒
    refreshed = sm2.refresh_if_needed()
    print(f"    刷新执行：{refreshed}")
    print(f"    刷新后剩余：{sm2.token_expires_at - time.time():.0f}s")

    print("\n▸ 会话失效后重建")
    sm2.reset()
    print(f"    重建次数：{sm2.rebuild_count}")

    print("""
  这个管理器的三个关键设计：

    1. 【主动刷新】而不是被动等待
       提前 5 分钟刷新，避免"请求发出去才发现过期"。

    2. 【持久化】让重启不丢登录态
       对于需要登录才能采集的站点，重新登录往往要过验证码，
       能省一次就省一次。

    3. 【失效检测】兜底
       即使有主动刷新，也可能因为服务端策略变化而失效。
       检查响应体里的特征词是最低成本的检测方式。
""")


# ============================================================
# 主流程
# ============================================================
def main() -> None:
    """运行全部实验。"""
    print_cookie_attrs()
    demo_parse_cookies()
    demo_session_managers()
    demo_cookie_roundtrip()
    demo_token()
    demo_session_manager()

    print("\n" + "=" * 84)
    print("本节要点总结")
    print("=" * 84)
    print("""
  1. Cookie 有三类，用途完全不同：
       会话 Cookie（无 Expires）→ 关浏览器失效，但爬虫里照样能用
       持久 Cookie（有 Expires）→ 注意过期时间
       Expires 在过去          → 服务端在下发删除指令，忽略

  2. 用 Session / Client 管理 Cookie，不要手动拼：
       自动处理 Domain / Path / 过期 / 连接复用

  3. Token 类会话要看 exp 字段，并且【提前】刷新：
       剩余 < 5 分钟就换新的，不要等 is_expired

  4. 长时间运行必须持久化会话：
       存到文件，重启后加载，避免重复登录（可能要过验证码）

  5. 代码里绝不硬编码 Cookie：
       会过期、会泄露、换环境就失效

  ⚠️  合规提醒：
      登录态采集涉及账号安全与用户隐私，务必确认：
      · 你是否有权使用该账号进行自动化访问
      · 服务条款是否允许自动化
      · 采集的数据是否涉及个人信息（涉及则受《个人信息保护法》约束）
""")


if __name__ == "__main__":
    main()
