"""阶段 4 · 第 6 课：JS 补环境（Node.js 执行目标 JS 片段）

本课可实测 —— Node.js v22.13.1 已就绪，配套的两个 JS 文件就在
同目录的 js/ 子目录里：
    js/target_sign.js   模拟目标站（含浏览器环境检测 + 混淆 + 签名）
    js/browser_env.js   补环境沙箱（把浏览器全局对象补进 Node）

为什么要补环境？
----------------
逆向到某个阶段你会遇到这种情况：
  · 签名算法是 AES + 随机 nonce，无法枚举（44 课的方法失效）
  · 混淆太重，抠代码要花三天
  · 算法依赖一堆浏览器 API（navigator / screen / Intl）

这时候最省力的做法不是『读懂它』，而是『让它跑起来』——
在你的 Node 进程里补出浏览器环境，把目标站那段 JS 原样执行，
直接拿它的返回值。

★ 学习边界声明
  本课使用**自己编写的模拟目标**，不针对任何真实网站。
  补环境技术本身是通用的 JS 工程手段（浏览器兼容层、SSR 渲染都在用），
  但把它用于绕过他人站点的访问控制可能违反服务条款与相关法律。
  请把它用在：自有系统、已获授权、公开教学靶场（如各大自建靶场）。

运行：python3 45_js_sandbox.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

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
JS_DIR = Path(__file__).parent / "js"
NODE = shutil.which("node")


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


@dataclass
class NodeResult:
    """一次 Node 调用的结果。"""

    ok: bool
    stdout: str
    stderr: str
    returncode: int

    def first_line(self) -> str:
        line = self.stdout.strip().splitlines()
        return line[0] if line else "(无输出)"


def run_node(script: str, timeout: int = 30) -> NodeResult:
    """在 js/ 目录下执行一段 Node 脚本。

    Args:
        script: Node 源码。
        timeout: 超时秒数。

    Returns:
        NodeResult 结果对象。
    """
    proc = subprocess.run(
        [NODE, "-e", script],
        cwd=str(JS_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return NodeResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


# ==========================================================================
# 实验区
# ==========================================================================

def exp0_environment() -> None:
    title("【实验 0】环境探明")
    print(f"    Node 可执行文件: {NODE}")
    if NODE is None:
        print("    ✗ 未找到 Node，本课无法运行（请安装 Node.js）")
        return
    r = run_node("console.log(process.version); console.log(process.platform);")
    print(f"    Node 版本: {r.stdout.strip().splitlines()[0]}")
    print(f"    平台:      {r.stdout.strip().splitlines()[-1]}")
    print(f"\n    js/ 目录内容:")
    for f in sorted(JS_DIR.iterdir()):
        print(f"      {f.name:<24}{f.stat().st_size:>7} 字节")


def exp1_plain_fails() -> None:
    title("【实验 1】纯 Node 执行目标 JS —— 必然失败")
    print("""    目标站 JS 里写了这几行：
        var w = window.innerWidth;
        var ua = navigator.userAgent;
        var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    Node 里没有 window / navigator，一碰就炸。""")

    r = run_node(textwrap.dedent("""
        const t = require('./target_sign.js');
        try {
          console.log('RESULT:' + t.buildSign('1001', 1699999999));
        } catch (e) {
          console.log('ERRTYPE:' + e.constructor.name);
          console.log('ERRMSG:' + e.message);
        }
    """))
    print(f"\n    执行结果：")
    for line in r.stdout.strip().splitlines():
        if line.startswith("ERRTYPE:"):
            print(f"      ✗ 错误类型: {line[8:]}")
        elif line.startswith("ERRMSG:"):
            print(f"      ✗ 错误信息: {line[7:]}")
        elif line.startswith("RESULT:"):
            print(f"      结果: {line[7:]}")
    if r.stderr.strip():
        print(f"      stderr: {r.stderr.strip()[:200]}")

    print("\n    ▸ 这就是『补环境』要解决的第一类问题：缺全局对象。")
    print("      还有第二类更隐蔽的问题 —— 对象存在但值不合理，")
    print("      目标站会用这些值做指纹校验（见实验 3）。")


def exp2_patch_env() -> None:
    title("【实验 2】补环境后执行 —— 成功拿到签名")
    print("    载入补环境沙箱 browser_env.js：")
    print("      · window / navigator / document / location / screen")
    print("      · Intl（时区）、localStorage、btoa/atob、requestAnimationFrame")
    print("      · CryptoJS.MD5（本课用 Node crypto 代实现）")

    r = run_node(textwrap.dedent("""
        const fs = require('fs');
        const { createEnv } = require('./browser_env.js');

        const env = createEnv();
        env.run(fs.readFileSync('target_sign.js', 'utf8'));

        const fp = env.run('JSON.stringify(getEnvFingerprint())');
        const sign = env.run('buildSign("1001", 1699999999)');
        console.log('FP:' + fp);
        console.log('SIGN:' + sign);
    """))

    if not r.ok:
        print(f"\n    ✗ 失败：{r.stderr.strip()[:400]}")
        return

    fp = sign = ""
    for line in r.stdout.strip().splitlines():
        if line.startswith("FP:"):
            fp = line[3:]
        elif line.startswith("SIGN:"):
            sign = line[5:]

    data = json.loads(fp)
    print(f"\n    ✓ 补环境成功，目标 JS 认为自己在浏览器里：")
    print(f"        User-Agent   : {data['ua'][:58]}…")
    print(f"        语言         : {data['lang']}")
    print(f"        平台         : {data['platform']}")
    print(f"        视口         : {data['screen']}")
    print(f"        时区         : {data['timezone']}")
    print(f"        插件数       : {data['pluginCount']}")
    print(f"        被识别为机器人: {data['isBot']}")
    print(f"\n    ✓ 签名结果: {sign}")
    print("\n    ▸ 这个签名是目标 JS **自己算出来的**，我一行算法都没复刻。")
    print("      这就是补环境的核心价值：用执行代替阅读。")


def exp3_fingerprint_traps() -> None:
    title("【实验 3】指纹陷阱 —— 补了对象但值不合理会怎样")
    print("    目标站常做这类自洽性检查（41 课讲过 HTTP 头，这里是 JS 侧）：\n")

    TRAPS: list[tuple[str, str, str, str]] = [
        ("User-Agent 自相矛盾",
         "'Windows NT 10.0' 但 platform 是 'Linux x86_64'",
         "UA 声明 Windows，platform 却报 Linux"),
        ("视口为 0",
         "innerWidth = 0",
         "真实浏览器视口不可能是 0，典型的无头默认值"),
        ("webdriver 标志",
         "navigator.webdriver = true",
         "Playwright/Selenium 默认暴露，一票否决"),
        ("插件列表为空",
         "navigator.plugins.length === 0",
         "桌面 Chrome 至少有 PDF Viewer"),
        ("语言与 UA 不匹配",
         "UA 是 en-US 但 language 是 zh-CN",
         "跨国 CDN 出口常见，会触发二次验证"),
        ("缺少 chrome 对象",
         "typeof window.chrome === 'undefined'",
         "真 Chrome 一定有 window.chrome.runtime"),
        ("时区与 IP 不符",
         "时区 Asia/Shanghai 但代理 IP 在美国",
         "代理池场景高频翻车点"),
    ]
    print(f"    {'陷阱':<20}{'特征':<42}{'为何暴露'}")
    print("    " + "-" * 104)
    for name, feat, why in TRAPS:
        print(f"    {name:<20}{feat:<42}{why}")

    sub("实测：同一个目标，三种环境下的指纹差异")
    r = run_node(textwrap.dedent("""
        const fs = require('fs');
        const { createEnv, makeNavigator, makeWindow } = require('./browser_env.js');
        const code = fs.readFileSync('target_sign.js', 'utf8');

        // 环境 A：本课默认（自洽）
        const a = createEnv();
        a.run(code);
        console.log('A:' + a.run('JSON.stringify(getEnvFingerprint())'));

        // 环境 B：故意造矛盾（UA 说 Windows，platform 说 Linux）
        const navB = makeNavigator();
        navB.platform = 'Linux x86_64';
        const b = createEnv({ navigator: navB });
        b.run(code);
        console.log('B:' + b.run('JSON.stringify(getEnvFingerprint())'));

        // 环境 C：0 视口 + webdriver=true（典型无头默认值）
        const navC = makeNavigator();
        navC.webdriver = true;
        const c = createEnv({
          navigator: navC,
          innerWidth: 0,
          innerHeight: 0,
        });
        c.run(code);
        console.log('C:' + c.run('JSON.stringify(getEnvFingerprint())'));
    """))

    envs: dict[str, dict] = {}
    for line in r.stdout.strip().splitlines():
        if line[:2] in ("A:", "B:", "C:"):
            envs[line[0]] = json.loads(line[2:])

    labels = {"A": "自洽环境", "B": "UA/platform 矛盾", "C": "无头默认值"}
    print(f"    {'环境':<20}{'platform':<16}{'视口':<14}{'webdriver':<12}{'签名'}")
    print("    " + "-" * 96)
    for k in ("A", "B", "C"):
        d = envs.get(k, {})
        print(f"    {labels[k]:<20}{d.get('platform', '?'):<16}"
              f"{d.get('screen', '?'):<14}{str(d.get('isBot', '?')):<12}")

    print("\n    ▸ 注意：B 和 C 的**签名仍然能算出来**，")
    print("      但目标站拿到这个签名后，会在服务端做二次校验，")
    print("      发现指纹不合理 → 判定为爬虫 → 返回假数据或直接封禁。")
    print("      ⚠ 这是补环境最容易被忽略的一点：")
    print("        『能跑出结果』≠『结果会被接受』。")

    sub("自洽性检查器（Python 侧实现）")
    ok, issues = check_fingerprint_consistency(envs.get("B", {}))
    print(f"    对 B 环境的检查结果：{'✓ 通过' if ok else '✗ 发现问题'}")
    for i in issues:
        print(f"      · {i}")

    ok_a, issues_a = check_fingerprint_consistency(envs.get("A", {}))
    print(f"\n    对 A 环境的检查结果：{'✓ 通过' if ok_a else '✗ 发现问题'}")
    for i in issues_a:
        print(f"      · {i}")


def check_fingerprint_consistency(fp: dict) -> tuple[bool, list[str]]:
    """检查 JS 侧环境指纹是否自洽。

    Args:
        fp: 由 getEnvFingerprint() 返回的字典。

    Returns:
        (是否通过, 问题列表)。
    """
    issues: list[str] = []
    ua = fp.get("ua", "")
    plat = fp.get("platform", "")
    screen = fp.get("screen", "")

    if "Windows" in ua and "Linux" in plat:
        issues.append(f"UA 声明 Windows 但 platform={plat!r}")
    if "Macintosh" in ua and "Win" in plat:
        issues.append(f"UA 声明 macOS 但 platform={plat!r}")
    if "Chrome" in ua and "Safari" not in ua:
        issues.append("Chrome 的 UA 必须同时包含 Safari 字样（Chrome 基于 WebKit）")
    if screen in ("0x0", "0x937"):
        issues.append(f"视口尺寸不合理：{screen!r}")
    if fp.get("isBot"):
        issues.append("navigator.webdriver = true（自动化工具标志）")
    if fp.get("pluginCount", 1) == 0:
        issues.append("navigator.plugins 为空（桌面 Chrome 至少有 PDF Viewer）")

    return (len(issues) == 0), issues


def exp4_minimal_patch() -> None:
    title("【实验 4】最小补环境法 —— 缺什么补什么")
    print("""    实际工作中不必一次性补全整个 DOM。推荐流程：

      Step 1  把目标 JS 丢进 Node 直接跑 → 报错
      Step 2  按报错信息，缺什么补什么（只补用到的那几个）
      Step 3  反复跑，直到不再报错
      Step 4  此时打印它真正用到的环境变量清单，固化下来

    下面演示这个『渐进式补环境』过程。""")

    r = run_node(textwrap.dedent("""
        const fs = require('fs');
        const vm = require('vm');
        const code = fs.readFileSync('target_sign.js', 'utf8');

        const sandbox = { console, Intl, JSON, Math, Date, String, Number, Object,
                          Array, Error, RegExp, parseInt, parseFloat };
        sandbox.CryptoJS = { MD5: (s) => ({ toString: () =>
          require('crypto').createHash('md5').update(String(s), 'utf8').digest('hex') }) };

        const rounds = [];

        function stubFor(name) {
          if (name === 'navigator') {
            return { userAgent: 'Mozilla/5.0 Chrome/131.0.0.0',
                     language: 'zh-CN', platform: 'Win32',
                     plugins: [], webdriver: false };
          }
          if (name === 'screen') return { width: 1920, height: 1080 };
          return {};
        }

        for (let i = 1; i <= 8; i++) {
          sandbox.globalThis = sandbox;
          if (sandbox.window === undefined) sandbox.window = sandbox;
          const ctx = vm.createContext(sandbox);
          let err = null;
          let phase = 'load';
          try {
            vm.runInContext(code, ctx, { timeout: 3000 });
            phase = 'call';
            vm.runInContext('buildSign("1001", 1699999999)', ctx, { timeout: 3000 });
          } catch (e) {
            err = e.message;
          }
          rounds.push({ round: i, phase: phase, error: err,
                        patched: Object.keys(sandbox).length });

          if (err === null) break;

          // 从错误信息里抠出缺失的标识符名
          const m = err.match(/([A-Za-z_$][\\w$]*) is not defined/);
          const missing = m ? m[1] : '';
          if (missing && !(missing in sandbox)) {
            sandbox[missing] = (missing === 'window') ? sandbox : stubFor(missing);
            rounds[rounds.length - 1].missing = missing;
          } else {
            rounds[rounds.length - 1].missing = '';
          }
        }

        console.log('ROUNDS:' + JSON.stringify(rounds));
    """))

    for line in r.stdout.strip().splitlines():
        if not line.startswith("ROUNDS:"):
            continue
        rounds = json.loads(line[7:])
        print(f"\n    {'轮次':<10}{'阶段':<8}{'错误信息':<30}{'本轮补的':<14}{'全局键数'}")
        print("    " + "-" * 96)
        for rd in rounds:
            err = rd.get("error") or "（无错误，成功）"
            miss = rd.get("missing") or "-"
            flag = "✓" if rd.get("error") is None else " "
            phase = "载入" if rd.get("phase") == "load" else "调用"
            print(f"    {flag} 第 {rd['round']} 轮{'':<2}{phase:<8}{err[:28]:<30}{miss:<14}{rd['patched']}")

    print("""
    ▸ 观察：错误信息本身就告诉你缺什么。补环境的本质是
      『照着错误信息填空』，不需要提前读完整份 DOM 规范。
      本例 3 轮就补完了 —— 真实目标通常需要 10-30 轮。

    ▸ 实用工具（真实项目中会用到）：
        · npm i crypto-js        —— 真 CryptoJS，比本课的手写版可靠
        · npm i jsdom            —— 完整的 DOM 实现，省去手补 document
        · pyexecjs               —— Python 直接调 JS（本课用 subprocess 替代）
        · node --experimental-vm-modules —— 需要 ESM 时的开关

    ⚠ 局限与风险：
        1. 补出来的环境**永远不等于真浏览器**，高级风控能识别
           （Canvas/WebGL 指纹、行为轨迹、TLS 指纹都在 JS 之外）
        2. 目标 JS 一旦更新，补环境脚本就得跟着改，维护成本高
        3. 一些站点会把关键逻辑放进 WebAssembly 或 Service Worker，
           补环境难度陡增
        4. 本课只演示方法论，请勿用于未授权的目标""")


def exp5_python_bridge() -> None:
    title("【实验 5】Python ↔ Node 桥接 —— 生产可用的批量签名")
    print("    签名函数必须能被高频调用（每个请求一次），")
    print("    所以不能每次起一个 node 进程（启动开销约 50ms）。")
    print("    正确做法：Node 常驻，用 stdin/stdout 做 JSON 行协议。\n")

    bridge = JS_DIR / "sign_bridge.js"
    bridge.write_text(textwrap.dedent("""\
        // 常驻签名服务：读一行 JSON，回一行 JSON
        const readline = require('readline');
        const fs = require('fs');
        const { createEnv } = require('./browser_env.js');

        const env = createEnv();
        env.run(fs.readFileSync('target_sign.js', 'utf8'));

        const rl = readline.createInterface({ input: process.stdin });
        rl.on('line', (line) => {
          if (!line.trim()) return;
          let out;
          try {
            const req = JSON.parse(line);
            const sign = env.run(
              `buildSign(${JSON.stringify(String(req.id))}, ${Number(req.ts)})`
            );
            out = { ok: true, id: req.id, ts: req.ts, sign: sign };
          } catch (e) {
            out = { ok: false, error: e.message };
          }
          process.stdout.write(JSON.stringify(out) + '\\n');
        });
    """), encoding="utf-8")
    print(f"    已生成桥接脚本：{bridge.name}（{bridge.stat().st_size} 字节）")

    sub("启动常驻进程并批量签名")

    @dataclass
    class Bridge:
        """Node 常驻签名进程的封装。

        Attributes:
            proc: 子进程。
            calls: 已处理的请求数。
        """

        proc: subprocess.Popen
        calls: int = field(default=0)

        def sign(self, item_id: str, ts: int) -> dict:
            """请求一次签名。

            Args:
                item_id: 商品/资源 ID。
                ts: 时间戳。

            Returns:
                Node 返回的字典。
            """
            payload = json.dumps({"id": item_id, "ts": ts}, ensure_ascii=False)
            assert self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(payload + "\n")
            self.proc.stdin.flush()
            line = self.proc.stdout.readline()
            self.calls += 1
            return json.loads(line)

        def close(self) -> None:
            """关闭进程。"""
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)

    proc = subprocess.Popen(
        [NODE, str(bridge)],
        cwd=str(JS_DIR),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    b = Bridge(proc=proc)

    import time

    t0 = time.perf_counter()
    results = []
    for i in range(200):
        results.append(b.sign(str(1000 + i), 1699999999 + i))
    elapsed = time.perf_counter() - t0
    b.close()

    ok_count = sum(1 for x in results if x.get("ok"))
    print(f"    批量签名 200 次：成功 {ok_count}/200，耗时 {elapsed:.3f} 秒")
    print(f"    平均每次：{elapsed / 200 * 1000:.2f} ms")

    sub("样本结果")
    for x in results[:3]:
        print(f"      id={x['id']}  ts={x['ts']}  sign={x['sign']}")
    print("    …")
    for x in results[-2:]:
        print(f"      id={x['id']}  ts={x['ts']}  sign={x['sign']}")

    sub("对比：每次都起新进程 vs 常驻")
    t0 = time.perf_counter()
    for i in range(20):
        run_node(
            "const fs=require('fs');const {createEnv}=require('./browser_env.js');"
            "const e=createEnv();e.run(fs.readFileSync('target_sign.js','utf8'));"
            f"e.run('buildSign(\"{1000 + i}\", {1699999999 + i})');"
        )
    cold = time.perf_counter() - t0
    print(f"    冷启动模式 20 次：{cold:.3f} 秒（{cold / 20 * 1000:.1f} ms/次）")
    print(f"    常驻模式   200 次：{elapsed:.3f} 秒（{elapsed / 200 * 1000:.2f} ms/次）")
    speedup = (cold / 20) / (elapsed / 200)
    print(f"\n    ▸ 常驻模式快 {speedup:.0f} 倍。在高频爬取场景下，")
    print("      这个差异决定了你能否跑满目标 QPS。")

    sub("工程化封装建议")
    print("""    生产中把 Bridge 包成一个支持并发安全的单例：

      class Signer:
          def __init__(self, script: str, workers: int = 4):
              # 开多个 Node 进程组成池，避免单进程成为瓶颈
              self.pool = [Bridge(...) for _ in range(workers)]

          async def sign(self, item_id: str, ts: int) -> str:
              # 用 asyncio.to_thread 包住同步 IO，配合阶段 3 的信号量
              ...

    注意点：
      · 保持 JSON 行协议 —— 不要用 pickle/protobuf，调试困难
      · 记录 seq 号防止响应错配（并发时 stdout 可能乱序）
      · Node 进程崩溃要能自动重启（阶段 3 的熔断器思路适用）
      · 密钥/盐值不要写在 JS 文件里，通过环境变量注入""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 6 课：JS 补环境")
    print(SEP)
    print("""
核心思路：当算法无法复刻时，与其读懂 JS，不如让它跑起来。
本课用真实的 Node.js 沙箱实测补环境全流程。

★ 本课使用自编模拟目标，不针对真实网站。
""")
    exp0_environment()
    if NODE is None:
        print("\n✗ 未找到 Node，后续实验跳过。")
        return
    exp1_plain_fails()
    exp2_patch_env()
    exp3_fingerprint_traps()
    exp4_minimal_patch()
    exp5_python_bridge()

    title("本课小结")
    print("""
  ✓ 补环境 = 让目标 JS 在 Node 里以为自己跑在浏览器
  ✓ 渐进式补法：直接跑 → 按报错缺什么补什么，不必读完整 DOM 规范
  ✓ 『能算出签名』≠『签名会被接受』—— 指纹自洽性才是关键
  ✓ 桥接协议用 JSON 行 + 常驻进程，高频场景必需
  ✓ 局限：Canvas/WebGL 指纹、TLS 指纹、行为轨迹都在 JS 之外，
       补环境解决不了 —— 那需要 Playwright 真浏览器（48 课）

  下一课（47）：验证码 —— 图形 OCR 与滑块轨迹模拟。
""")


if __name__ == "__main__":
    main()
