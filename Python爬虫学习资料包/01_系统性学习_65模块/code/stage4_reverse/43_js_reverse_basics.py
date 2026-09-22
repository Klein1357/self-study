"""阶段 4 · 第 4 课：JS 逆向基础（浏览器调试 + 调用栈 + 断点）

运行说明
--------
本课分两部分：

  A 部分（可实测）：用纯 Python 复刻逆向的『方法论』——
     栈回溯、调用图、Hook 打点、参数溯源。这些都是逆向时的核心动作，
     只是把 JS 引擎换成了一个模拟器，逻辑完全一致。

  B 部分（需浏览器，本课不实测）：真实浏览器 DevTools 的操作步骤，
     以清单 + 截图位说明的形式给出，代码为 Playwright 加载页面的骨架。

    ★ 环境局限：本沙箱未安装 Playwright 浏览器内核（约 150MB 下载），
      也无可供逆向的带加密目标站（逆向第三方网站加密参数可能违反其
      ToS，本课程不提供此类目标）。因此 B 部分只给方法与骨架，不实际执行。
      请在你自己的机器上按步骤操作。

运行：python3 43_js_reverse_basics.py
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

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


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# A 部分：逆向方法论（模拟器版，可实测）
# ==========================================================================

# --------------------------------------------------------------------------
# 核心概念 1：栈帧与调用栈
# --------------------------------------------------------------------------
# 逆向的本质动作是回答一个问题：『这个参数 x 是从哪来的？』
# 答案藏在调用栈里。JS 里断点命中后你会看到 Call Stack，Python 里
# 就是 traceback。下面的 tiny 调用图模拟器把这件事显式化。


@dataclass
class Frame:
    """一个栈帧。"""

    func: str          # 函数名
    args: dict[str, Any]  # 入参快照
    ret: Any = None    # 返回值（返回时回填）
    depth: int = 0


class CallTracer:
    """极简调用栈追踪器：模拟 DevTools 的 Call Stack 面板。

    用法类似 JS 里打断点后单步执行，看每一步的入参与返回值。
    """

    def __init__(self) -> None:
        self.stack: list[Frame] = []
        self.history: list[tuple[int, str, dict[str, Any], Any]] = []

    def enter(self, func: str, **args: Any) -> None:
        f = Frame(func=func, args=args, depth=len(self.stack))
        self.stack.append(f)
        pad = "  " * f.depth
        print(f"{pad}→ {func}({self._fmt(args)})")

    def leave(self, ret: Any) -> Any:
        f = self.stack.pop()
        f.ret = ret
        self.history.append((f.depth, f.func, f.args, ret))
        pad = "  " * f.depth
        print(f"{pad}← {f.func} 返回 {self._fmt(ret)}")
        return ret

    @staticmethod
    def _fmt(v: Any) -> str:
        if isinstance(v, str):
            return f'"{v[:40]}{"…" if len(v) > 40 else ""}"'
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}: {CallTracer._fmt(x)}" for k, x in list(v.items())[:3]) + "}"
        return str(v)

    def traceback_str(self) -> str:
        """生成调用栈文本，等价于 JS 的 new Error().stack。

        注意：这里从 history 重建，因为断点命中的瞬间栈还没弹出。
        """
        lines: list[str] = []
        for depth, func, args, _ret in self.history:
            pad = "  " * depth
            a = ", ".join(f"{k}={v!r}" for k, v in args.items())
            lines.append(f"{pad}    at {func} ({a})")
        return "\n".join(lines) or "    at <root>"


# --------------------------------------------------------------------------
# 核心概念 2：参数溯源 —— 从一个签名反推需要哪些输入
# --------------------------------------------------------------------------
# 目标站常见做法：sign = md5(f"{a}&{b}&{c}&{secret}")
# 逆向要做的是：给定若干次真实请求（a, b, c 已知，sign 已知），
# 猜出拼接顺序与 secret。这叫『已知明文攻击』的工程化版本。


@dataclass
class ObservedRequest:
    """一次抓包观测到的请求。"""

    params: dict[str, Any]
    sign: str


def brute_force_sign_template(
    observations: list[ObservedRequest],
    candidates: list[str],
    secrets: list[str],
) -> tuple[str, str] | None:
    """暴力枚举拼接模板与 secret，返回命中的 (模板, secret)。

    这是逆向中最朴素也最有效的一招：当你怀疑是 md5 拼接时，
    用几次真实样本把模板和盐值枚举出来。

    Args:
        observations: 真实抓包样本（参数 + 签名）。
        candidates: 参与签名的参数名候选列表（顺序会被枚举）。
        secrets: 盐值候选。

    Returns:
        命中则返回 (模板字符串, secret)，否则 None。
    """
    if not observations:
        return None
    from itertools import permutations

    # 用第一个样本缩小范围，再用剩余样本验证（避免误报）
    first = observations[0]
    n = min(len(candidates), len(first.params))
    for n_use in range(n, 0, -1):
        for perm in permutations(candidates, n_use):
            for secret in secrets:
                tpl = "&".join(f"{{{k}}}" for k in perm)
                if secret:
                    tpl_full = tpl + "&" + secret
                else:
                    tpl_full = tpl
                if _render_and_hash(tpl, first.params, secret) != first.sign:
                    continue
                # 用剩余样本验证
                ok = all(
                    _render_and_hash(tpl, o.params, secret) == o.sign
                    for o in observations[1:]
                )
                if ok:
                    return tpl, secret
    return None


def _render_and_hash(tpl: str, params: dict[str, Any], secret: str) -> str:
    parts = [tpl.format(**params)]
    if secret:
        parts.append(secret)
    raw = "&".join(parts)
    return hashlib.md5(raw.encode()).hexdigest()


# --------------------------------------------------------------------------
# 核心概念 3：Hook 打点 —— 不改源码观察函数调用
# --------------------------------------------------------------------------
# JS 里 Hook 的经典写法：
#   const _md5 = md5; md5 = function(x){ console.log("md5 入参", x); return _md5(x); }
# 目的是不读混淆代码也能看到『真正的入参』。
# Python 里就是 monkey patch，逻辑一模一样。


class HookedFuncs:
    """模拟被 Hook 的加密函数集合。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], Any]] = []

    def md5(self, data: str) -> str:
        return hashlib.md5(data.encode()).hexdigest()

    def aes_encrypt(self, data: str, key: str) -> str:
        # 模拟：真实场景是 AES-CBC，这里用可复现的伪加密
        return hashlib.sha256((data + key).encode()).hexdigest()[:32]

    def btoa(self, data: str) -> str:
        import base64
        return base64.b64encode(data.encode()).decode()


def install_hooks(obj: HookedFuncs, names: list[str]) -> None:
    """给对象的指定方法打 Hook，记录入参与返回值。

    Args:
        obj: 目标对象。
        names: 要 Hook 的方法名列表。
    """
    for name in names:
        original = getattr(obj, name)

        def make_wrapper(fn: Callable[..., Any], fname: str) -> Callable[..., Any]:
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                ret = fn(*args, **kwargs)
                obj.calls.append((fname, args, ret))
                print(f"    [HOOK] {fname}({', '.join(repr(a)[:34] for a in args)})")
                print(f"           └─ 返回 {str(ret)[:50]}")
                return ret

            return wrapper

        setattr(obj, name, make_wrapper(original, name))


# --------------------------------------------------------------------------
# 核心概念 4：混淆代码的常见形态与对抗
# --------------------------------------------------------------------------

OBFUSCATION_FORMS: list[tuple[str, str, str]] = [
    ("字符数组化", "const _0x1a2b = ['sign', 'md5', 'timestamp']", "把字符串抽到数组，用下标访问，搜索关键字失效"),
    ("控制流平坦化", "switch(_0x9f[i++]){case '0': …; case '1': …}", "打乱执行顺序，无法顺序阅读"),
    ("十六进制/Unicode 编码", "var a = '\\x73\\x69\\x67\\x6e'", "肉眼不可读，但可被格式化工具还原"),
    ("死代码注入", "if(false){/* 大量垃圾 */}", "增大体积，干扰阅读"),
    ("字符串拼接还原", "_0x1a2b[0] + _0x1a2b[3]", "运行时才拼出真实字符串"),
    ("eval / Function 动态执行", "eval(_0x1a2b[7])", "静态分析失效，需动态 Hook"),
]

DEAL_WITH: list[tuple[str, str]] = [
    ("格式化", "JS Beautify / Prettier —— 让代码可读，第一步永远是这个"),
    ("还原字符串数组", "找数组定义处，写脚本把所有下标替换回字面量"),
    ("去掉控制流平坦化", "ast 分析 + 还原 switch 为顺序执行（工具：deflat、babel 插件）"),
    ("搜索入口", "在 Sources 面板搜 'sign' / 'md5' / 'encrypt' / 'window' 关键字"),
    ("XHR 断点", "在 Network 里对目标请求右键 → Break on → XHR/fetch，直接停在发起处"),
    ("Hook 兜底", "混淆再狠，最终也要调用 CryptoJS/md5/JSON.stringify，Hook 那一层即可"),
]


# ==========================================================================
# 实验区
# ==========================================================================

def exp1_call_stack() -> None:
    title("【实验 1】调用栈追踪（模拟 DevTools 的 Call Stack）")
    print("场景：目标站 sign 参数的生成链路，我们要看清每一步。")

    t = CallTracer()

    def get_params(book_id: str) -> dict[str, Any]:
        t.enter("getParams", book_id=book_id)
        ts = str(int(time.time() * 1000))
        client = "web"
        return t.leave({"id": book_id, "ts": ts, "client": client})

    def build_sign(params: dict[str, Any], secret: str) -> str:
        t.enter("buildSign", params=params, secret=secret)
        raw = f"{params['ts']}{params['id']}{params['client']}{secret}"
        hashed = hash_md5(raw)
        return t.leave(hashed)

    def hash_md5(raw: str) -> str:
        t.enter("md5", raw=raw)
        return t.leave(hashlib.md5(raw.encode()).hexdigest())

    params = get_params("BK10086")
    sign = build_sign(params, "s3cr3t_k3y")
    print(f"\n  最终请求：params={params}")
    print(f"            sign={sign}")
    print("\n  断点命中时看到的调用栈（自上而下 = 从浅到深）：")
    print(t.traceback_str())
    print("\n  ▸ 逆向启示：Call Stack 告诉你『谁调用了谁』，")
    print("    配合 Scope 面板看局部变量，就能确定 raw 字符串的拼法。")
    print(f"\n  本次共记录 {len(t.history)} 次函数进出。")


def exp2_brute_force_sign() -> None:
    title("【实验 2】签名模板暴力枚举（已知明文攻击）")
    print("场景：抓包拿到 4 次请求，参数和 sign 都有，要反推签名算法。")

    SECRET = "abc123def456"
    real_params = [
        {"id": "1001", "ts": "1699999999001", "client": "web"},
        {"id": "1002", "ts": "1699999999002", "client": "web"},
        {"id": "1003", "ts": "1699999999003", "client": "web"},
        {"id": "1004", "ts": "1699999999004", "client": "web"},
    ]

    def make_sign(p: dict[str, Any]) -> str:
        # 真实算法：md5(id=...&ts=...&client=...&abc123def456)
        raw = f"id={p['id']}&ts={p['ts']}&client={p['client']}&{SECRET}"
        return hashlib.md5(raw.encode()).hexdigest()

    observed = [
        ObservedRequest(params=p, sign=make_sign(p)) for p in real_params
    ]
    sub("抓到的样本")
    for o in observed[:4]:
        print(f"    params={o.params}\n      sign={o.sign}")

    sub("开始暴力枚举（参数名候选 × 排列 × 盐值候选）")
    candidates = ["id", "ts", "client"]
    secrets = ["", SECRET, "secret", "key"]

    # 注意：真实算法是 key=value 形式并且带 & 前缀，
    # 我们上面的模板生成器用的是『纯值拼接』，所以这里手工补一个
    # 更贴近真实的枚举：加上 "k=v" 形式。
    hit = None
    from itertools import permutations

    for perm in permutations(candidates):
        for secret in secrets:
            kv = "&".join(f"{k}={{{k}}}" for k in perm)
            tpl = kv + ("&" + secret if secret else "")
            ok = True
            for o in observed:
                raw = tpl.replace("{", "").replace("}", "")
                raw = "&".join(f"{k}={o.params[k]}" for k in perm)
                if secret:
                    raw += "&" + secret
                if hashlib.md5(raw.encode()).hexdigest() != o.sign:
                    ok = False
                    break
            if ok:
                hit = (kv, secret)
                break
        if hit:
            break

    if hit:
        tpl, secret = hit
        print(f"\n  ✓ 命中！")
        print(f"    模板：md5({tpl})")
        print(f"    盐值：{secret!r}")
        print(f"    验证：4/4 样本全部匹配")
    else:
        print("\n  ✗ 未命中，说明候选集合不够（真实逆向中需要更多猜测）")

    sub("为什么这招有效")
    print("    1. 签名通常是『确定性函数』：同样入参 → 同样签名，可枚举")
    print("    2. 参数个数一般不超过 6 个，排列组合在可接受范围")
    print("    3. 盐值常常硬编码在 JS 里，先读代码拿到再枚举模板，效率更高")
    print("    ⚠ 局限：如果算法是 AES/RSA 或含随机数 nonce，本方法失效，")
    print("       必须回到 JS 层做真实执行（见 45 课『补环境』）。")


def exp3_hook() -> None:
    title("【实验 3】Hook 打点（不读混淆代码也能拿到真实入参）")
    print("场景：JS 被重度混淆，但最终一定要调用加密函数——Hook 那一层。")

    api = HookedFuncs()
    print("\n  目标代码（模拟混淆后的调用链）里隐藏了三个加密调用，")
    print("  我们不读它的源码，直接对函数打 Hook：")

    sub("安装 Hook")
    install_hooks(api, ["md5", "aes_encrypt", "btoa"])
    print("    [HOOK] 已挂载 3 个函数")

    sub("运行目标逻辑（模拟混淆代码在跑）")
    ts = "1699999999001"
    raw = f"user=10086&ts={ts}"
    api.md5(raw)
    enc = api.aes_encrypt(raw, "0123456789abcdef")
    api.btoa(enc + ":" + ts)

    sub("Hook 捕获结果")
    for i, (name, args, ret) in enumerate(api.calls, 1):
        print(f"    {i}. {name}")
        print(f"       入参：{[str(a)[:44] for a in args]}")
        print(f"       返回：{str(ret)[:60]}")
    print("\n  ▸ 逆向启示：混淆可以改『怎么算』，但改不了『调用了什么 API』。")
    print("    Hook 是绕过混淆最省力的路径，优先于硬啃混淆代码。")
    print("    JS 里的具体写法：")
    print('      const _md5 = CryptoJS.MD5;')
    print('      CryptoJS.MD5 = function(x){ console.log("MD5入参:", x); return _md5(x); };')
    print("\n  ⚠ 局限：Hook 必须在目标代码执行『之前』注入。")
    print("     若代码在页面加载瞬间自执行，需要在 DevTools 里用")
    print("     Sources → Overrides / Snippet 提前注入。")


def exp4_obfuscation() -> None:
    title("【实验 4】混淆形态速查与对抗策略")
    sub("常见混淆形态（6 种）")
    print(f"    {'形态':<22}{'特征':<44}{'目的'}")
    print("    " + "-" * 100)
    for form, feat, purpose in OBFUSCATION_FORMS:
        print(f"    {form:<22}{feat:<44}{purpose}")

    sub("对抗策略（按推荐顺序）")
    for i, (step, desc) in enumerate(DEAL_WITH, 1):
        print(f"    {i}. {step}")
        print(f"       {desc}")

    sub("模拟：字符串数组还原")
    arr = ["s", "i", "g", "n", "_", "k", "e", "y"]
    idx_code = "[0][1][2][3][4][5][6][7]"
    print(f"    混淆代码：_0x1a2b{idx_code}")
    idxs = idx_code.replace("][", " ").strip("[]").split()
    restored = "".join(arr[int(i)] for i in idxs)
    print(f"    还原后：  '{restored}'")
    print("\n  ▸ 这一步用脚本批处理，几百个下标几秒钟还原完，不要手工看。")


def part_b_browser() -> None:
    title("【B 部分】真实浏览器逆向操作清单（本沙箱不实测）")
    print("""
★ 环境局限说明
  本沙箱未安装 Playwright 浏览器内核，也没有可供逆向的目标站
  （对第三方站点逆向其加密参数可能违反 ToS）。故以下为操作清单，
  请在你自己的机器上执行。附带的 Playwright 骨架代码仅供参考，
  本课不运行它。

────────────────────────────────────────────────────────────
Step 1  打开 DevTools（F12）→ Network 面板 → 勾选 Preserve log
Step 2  触发一次目标请求（点击按钮 / 翻页）
Step 3  在请求列表里找到携带 sign/token 的那个 XHR/fetch
Step 4  右键 → Copy → Copy as cURL，先确认能用 curl 复现请求
Step 5  Sources 面板 → 全局搜索（Ctrl+Shift+F）关键字：
          sign  /  encrypt  /  md5  /  _sign  /  X-Sign
Step 6  在可疑函数的首行左侧行号点一下，下断点
Step 7  重新触发请求 → 代码停住 → 看右侧 Scope（局部变量）
          ▸ 这里能看到 raw 字符串的真实拼法
Step 8  看 Call Stack（调用栈）逐层向上，找到最外层入口
Step 9  若无从下手：Network → 目标请求右键 → Break on → XHR/fetch
          ▸ 直接从『发起请求的那一行』往回推，命中率最高
Step 10 用 Console 直接调用被 Hook 的函数验证猜测：
          > _md5("id=1&ts=2&secret")
Step 11 把关键函数抠出来 → 进入 45 课『补环境』
────────────────────────────────────────────────────────────

Playwright 加载页面的骨架（仅供你本机参考，本课不执行）：

    import asyncio
    from playwright.async_api import async_playwright

    async def main() -> None:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=False)  # 必须非 headless
            page = await browser.new_page()
            await page.goto("https://example.com")
            # 在页面加载前注入 Hook
            await page.add_init_script(
                "window.__logs = [];"
                "const _m = window.md5;"
                "window.md5 = function(x){ window.__logs.push(x); return _m(x); };"
            )
            await page.click("#submit")
            logs = await page.evaluate("window.__logs")
            print(logs)
            await browser.close()

    asyncio.run(main())

────────────────────────────────────────────────────────────
""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 4 课：JS 逆向基础")
    print(SEP)
    print("""
本课目标：掌握逆向的四个核心动作
  1. 调用栈回溯 —— 回答『参数从哪来』
  2. 参数溯源   —— 反推签名算法
  3. Hook 打点  —— 绕过混淆拿真实入参
  4. 混淆对抗   —— 还原可读代码

A 部分用 Python 模拟器实测方法论；B 部分给真实浏览器操作清单。
""")
    exp1_call_stack()
    exp2_brute_force_sign()
    exp3_hook()
    exp4_obfuscation()
    part_b_browser()

    title("本课小结")
    print("""
  ✓ 逆向不是『读懂混淆代码』，而是『找到关键函数并让它自己说话』
  ✓ 优先级：Hook > XHR 断点 > 关键字搜索 > 啃混淆代码
  ✓ 签名算法大多可枚举（md5 拼接型），先试着暴力还原
  ✓ 遇到 AES/RSA/nonce，才需要真正的 JS 执行环境 → 下一课

  下一课（44）：加密参数深度分析 —— MD5/AES/RSA 的 Python 实现与
              真实签名对齐验证。
""")


if __name__ == "__main__":
    random.seed(42)
    main()
