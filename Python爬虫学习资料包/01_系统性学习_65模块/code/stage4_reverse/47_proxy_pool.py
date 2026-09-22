"""阶段 4 · 第 8 课：代理池 —— 检测、评分与调度

本课可实测（全部离线可跑）：
  · 代理池的完整生命周期：采集 → 检测 → 评分 → 调度 → 淘汰
  · 用本地模拟代理服务器实测『检测器』（不依赖外网代理）
  · 评分算法的实测对比：朴素轮询 vs 加权调度
  · 连通性/匿名度/速度 三个维度的检测逻辑

★ 现实提醒
  本课不使用任何第三方代理服务，也不提供代理购买渠道。
  代理的合法使用场景：自有 IP 池、企业出口、合规的云函数多出口。
  用代理绕过他人站点的访问控制可能违反服务条款与法律。

运行：python3 47_proxy_pool.py
"""

from __future__ import annotations

import json
import random
import socket
import statistics
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

SEP = "=" * 72


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# 一、代理基础模型
# ==========================================================================

# 代理类型速查
PROXY_TYPES: list[tuple[str, str, str, str]] = [
    ("透明代理", "Transparent", "T1", "服务端能看到你的真实 IP（X-Forwarded-For）"),
    ("匿名代理", "Anonymous", "T2", "隐藏真实 IP，但会暴露『我用了代理』"),
    ("高匿代理", "Elite", "T3", "既隐藏真实 IP，也不暴露代理身份"),
    ("数据中心", "Datacenter", "D1", "云厂商 IP 段，便宜快，但极易被识别"),
    ("住宅代理", "Residential", "R1", "真实家庭宽带 IP，贵，但难识别"),
    ("移动代理", "Mobile", "M1", "4G/5G 基站 IP，最贵，最难封"),
]


@dataclass
class Proxy:
    """一个代理节点。"""

    host: str
    port: int
    scheme: str = "http"
    username: str | None = None
    password: str | None = None
    kind: str = "datacenter"

    # 运行时质量指标（由检测器填充）
    ok_count: int = 0
    fail_count: int = 0
    total_latency: float = 0.0
    last_check: float = 0.0
    banned_until: float = 0.0     # 被目标站封禁的恢复时间
    consec_fail: int = 0

    @property
    def key(self) -> str:
        return f"{self.host}:{self.port}"

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{self.username}:{self.password}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def success_rate(self) -> float:
        total = self.ok_count + self.fail_count
        return self.ok_count / total if total else 1.0

    @property
    def avg_latency(self) -> float:
        return self.total_latency / self.ok_count if self.ok_count else 99.0

    @property
    def is_banned(self) -> bool:
        return time.time() < self.banned_until

    def score(self, now: float | None = None) -> float:
        """综合评分（0-100）：成功率 × 速度 × 新鲜度 × 惩罚。

        ▸ 评分是调度器的唯一依据。设计要点：
          - 成功率权重最高（0.6）—— 请求失败的成本远高于慢
          - 延迟归一化到 0-1（200ms 得满分，5s 得 0 分）
          - 连续失败重罚，避免『偶尔成功了就把分数拉回来』
          - 被目标站封禁的节点直接归零
        """
        now = now or time.time()
        if self.is_banned:
            return 0.0

        rate = self.success_rate
        lat = max(0.0, min(1.0, (5.0 - self.avg_latency) / 4.8))  # 200ms→1.0, 5s→0
        freshest = max(0.0, 1.0 - (now - self.last_check) / 600)  # 10 分钟衰减到 0
        penalty = 1.0 / (1.0 + self.consec_fail * 0.5)
        raw = (rate * 0.6 + lat * 0.25 + freshest * 0.15) * penalty
        return round(raw * 100, 2)

    def render(self) -> str:
        flag = "⛔被封" if self.is_banned else "  "
        return (
            f"    {flag}{self.key:<22}{self.kind:<12}"
            f"成功 {self.ok_count:>3}/{self.ok_count + self.fail_count:<3}"
            f"  延迟 {self.avg_latency * 1000:>6.0f}ms  "
            f"连败 {self.consec_fail}  评分 {self.score():>5.1f}"
        )


# ==========================================================================
# 二、检测器：连通性 / 匿名度 / 速度
# ==========================================================================


@dataclass
class CheckResult:
    """一次代理检测的结果。"""

    proxy_key: str
    alive: bool
    latency: float
    anonymity: str          # transparent / anonymous / elite / unknown
    leaked_ip: str | None
    error: str = ""
    status: int = 0


class ProxyChecker:
    """代理检测器。

    ▸ 检测三项：
      1. 连通性  —— 能否正常建立连接并拿到 200
      2. 匿名度  —— 发送一个回显请求，看服务端能否看到我的真实 IP
      3. 速度    —— 从『发起请求』到『收到响应』的耗时

    ▸ 本课为了可离线实测，用一个**本地模拟代理服务器**做检测目标。
      真实场景下把 `echo_url` 换成一个回显服务即可
      （例如你自己部署的 httpbin）。
    """

    def __init__(self, echo_url: str, real_ip: str = "203.0.113.42") -> None:
        self.echo_url = echo_url
        self.real_ip = real_ip

    def check(self, proxy: Proxy) -> CheckResult:
        """检测一个代理。

        Args:
            proxy: 待检测的代理。

        Returns:
            CheckResult 检测结果。
        """
        t0 = time.perf_counter()
        try:
            resp = self._http_via_proxy(proxy, self.echo_url)
        except Exception as e:  # noqa: BLE001 - 任何异常都视为不可用
            return CheckResult(
                proxy_key=proxy.key,
                alive=False,
                latency=time.perf_counter() - t0,
                anonymity="unknown",
                leaked_ip=None,
                error=f"{type(e).__name__}: {e}",
            )
        latency = time.perf_counter() - t0
        body = resp.get("body", "")
        headers = resp.get("headers", {})

        anonymity, leaked = self._judge_anonymity(headers, body)
        return CheckResult(
            proxy_key=proxy.key,
            alive=resp.get("status") == 200,
            latency=latency,
            anonymity=anonymity,
            leaked_ip=leaked,
            status=resp.get("status", 0),
        )

    def _judge_anonymity(
        self, headers: dict[str, str], body: str
    ) -> tuple[str, str | None]:
        """判定匿名度。

        ▸ 判定依据：回显服务告诉我们『它看到了什么』。
          · 出现真实 IP            → transparent（透明，泄露身份）
          · 出现 Via / X-Forwarded-For → anonymous（匿名，但暴露代理身份）
          · 两者都没有             → elite（高匿）

        ▸ 重要区别：
          · 检查**响应头**：代理软件自己加的标记（Via、X-Proxy-Id）
          · 检查**响应体**：回显服务报告它从请求里看到了什么
          两者都要看，只看一处会漏判。
        """
        low = {k.lower(): v for k, v in headers.items()}
        header_blob = json.dumps(low, ensure_ascii=False)
        body_low = body.lower()

        # 1) 真实 IP 泄露 → 透明代理（头或体里任何一处出现都算）
        if self.real_ip in header_blob or self.real_ip in body:
            return "transparent", self.real_ip

        # 2) 出现代理标记 → 匿名
        markers = ("via", "x-forwarded-for", "x-proxy-id", "forwarded", "proxy-connection")
        if any(m in low for m in markers):
            return "anonymous", None
        # 回显体的 headers 里出现这些 key 也算暴露
        if any(f'"{m}"' in body_low for m in markers):
            return "anonymous", None

        return "elite", None

    @staticmethod
    def _http_via_proxy(proxy: Proxy, url: str) -> dict[str, Any]:
        """通过代理发起一个 HTTP 请求。

        ▸ 实现说明：这里用 socket 手写代理协议，是为了让你看清
          『代理到底做了什么』。生产中用 httpx 的 proxies 参数即可。

        Args:
            proxy: 代理节点。
            url: 目标 URL。

        Returns:
            含 status / headers / body 的字典。

        Raises:
            ConnectionError: 无法连接代理。
            TimeoutError: 超时。
        """
        u = urlparse(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or 80
        path = u.path or "/"

        s = socket.create_connection((proxy.host, proxy.port), timeout=3.0)
        s.settimeout(3.0)
        req = (
            f"GET {url} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"User-Agent: ProxyChecker/1.0\r\n"
            f"X-Real-IP: {u.hostname}\r\n"
            f"Connection: close\r\n\r\n"
        )
        s.sendall(req.encode())

        buf = b""
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > 65536:
                break
        s.close()

        text = buf.decode("utf-8", errors="replace")
        head, _, body = text.partition("\r\n\r\n")
        lines = head.split("\r\n")
        status = 0
        if lines and lines[0].startswith("HTTP/"):
            try:
                status = int(lines[0].split()[1])
            except (IndexError, ValueError):
                status = 0
        headers: dict[str, str] = {}
        for line in lines[1:]:
            k, _, v = line.partition(":")
            if k:
                headers[k.strip()] = v.strip()
        return {"status": status, "headers": headers, "body": body}


# ==========================================================================
# 三、模拟代理服务器（用于离线实测检测器）
# ==========================================================================


def start_mock_proxies(
    specs: list[tuple[str, float, str]], base_port: int = 18080
) -> tuple[list[Proxy], list[HTTPServer], list[threading.Thread]]:
    """启动一组模拟代理服务器。

    Args:
        specs: [(名称, 模拟延迟秒, 行为)]，行为可选 normal / leak / via / dead / ban。
        base_port: 起始端口。

    Returns:
        (代理对象列表, 服务器列表, 线程列表)。
    """
    proxies: list[Proxy] = []
    servers: list[HTTPServer] = []
    threads: list[threading.Thread] = []

    for i, (name, delay, behavior) in enumerate(specs):
        port = base_port + i
        # ★ 关键：用默认参数把 delay/behavior 绑定到当前迭代，
        #   否则所有 Handler 类共享最后一轮的闭包值（Python 经典陷阱）
        spec_delay, spec_behavior = delay, behavior

        class Handler(BaseHTTPRequestHandler):
            """模拟一个『回显型』代理。"""

            _delay = spec_delay
            _behavior = spec_behavior

            def log_message(self, *args: Any) -> None:  # 静默
                pass

            def do_GET(self) -> None:
                if self._behavior == "ban":
                    self.send_response(403)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "25")
                    self.end_headers()
                    self.wfile.write(b"403 Forbidden - IP banned")
                    return
                if self._behavior == "dead":
                    self.send_response(502)
                    self.send_header("Content-Length", "11")
                    self.end_headers()
                    self.wfile.write(b"bad gateway")
                    return

                time.sleep(self._delay)

                # 模拟真实代理的回显行为：
                #   normal → 什么都不额外暴露（高匿）
                #   leak   → 把真实 IP 写进 X-Forwarded-For（透明代理）
                #   via    → 只加一个 Via 头（匿名但暴露代理身份）
                seen: dict[str, str] = {"origin": "127.0.0.1"}
                if self._behavior == "leak":
                    seen["x-forwarded-for"] = "203.0.113.42"
                    seen["via"] = "1.1 mock"
                elif self._behavior == "via":
                    seen["via"] = "1.1 mock-proxy"

                payload = json.dumps({"args": {}, "headers": seen, "url": self.path})
                data = payload.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        srv = HTTPServer(("127.0.0.1", port), Handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        servers.append(srv)
        threads.append(th)

        kind = {"leak": "transparent", "via": "anonymous"}.get(behavior, "datacenter")
        proxies.append(Proxy(host="127.0.0.1", port=port, kind=kind))

    time.sleep(0.15)  # 等服务器就绪
    return proxies, servers, threads


# ==========================================================================
# 四、代理池调度器
# ==========================================================================


@dataclass
class PoolStats:
    """调度统计。"""

    total: int = 0
    alive: int = 0
    dead: int = 0
    banned: int = 0
    avg_score: float = 0.0


class ProxyPool:
    """代理池。

    ▸ 调度策略是本课重点。三种策略实测对比：
      · round_robin —— 轮询，实现最简单，但会一直用到快挂掉的节点
      · weighted    —— 按评分加权随机，自动偏向优质节点
      · best_first  —— 永远选最高分，容易把单个节点打爆
    """

    def __init__(self, proxies: list[Proxy], strategy: str = "weighted") -> None:
        self.proxies = proxies
        self.strategy = strategy
        self._rr = 0
        self._lock = threading.Lock()
        self.pick_log: list[str] = []

    @property
    def alive(self) -> list[Proxy]:
        """当前可用（未封禁）的代理。"""
        return [p for p in self.proxies if not p.is_banned]

    def pick(self) -> Proxy | None:
        """按策略挑一个代理。

        Returns:
            选中的代理，池空则 None。
        """
        with self._lock:
            cands = self.alive
            if not cands:
                return None
            if self.strategy == "round_robin":
                p = cands[self._rr % len(cands)]
                self._rr += 1
            elif self.strategy == "best_first":
                p = max(cands, key=lambda x: x.score())
            else:  # weighted
                weights = [max(x.score(), 0.1) for x in cands]
                p = random.choices(cands, weights=weights, k=1)[0]
            self.pick_log.append(p.key)
            return p

    def report(self, proxy: Proxy, ok: bool, latency: float = 0.0) -> None:
        """回填一次使用结果。

        Args:
            proxy: 使用的代理。
            ok: 是否成功。
            latency: 本次耗时（秒）。
        """
        if ok:
            proxy.ok_count += 1
            proxy.total_latency += latency
            proxy.consec_fail = 0
        else:
            proxy.fail_count += 1
            proxy.consec_fail += 1
            # 连续 3 次失败 → 暂时下线，60 秒后重试
            if proxy.consec_fail >= 3:
                proxy.banned_until = time.time() + 60

    def stats(self) -> PoolStats:
        """池的整体统计。"""
        scores = [p.score() for p in self.proxies]
        return PoolStats(
            total=len(self.proxies),
            alive=len(self.alive),
            dead=sum(1 for p in self.proxies if p.ok_count == 0),
            banned=sum(1 for p in self.proxies if p.is_banned),
            avg_score=statistics.mean(scores) if scores else 0.0,
        )

    def render(self) -> str:
        return "\n".join(p.render() for p in self.proxies)


# ==========================================================================
# 实验区
# ==========================================================================

def exp1_types() -> None:
    title("【实验 1】代理类型速查 —— 你要的到底是哪一种")
    print(f"    {'类型':<12}{'英文':<16}{'等级':<8}{'特征'}")
    print("    " + "-" * 92)
    for cn, en, level, feat in PROXY_TYPES:
        print(f"    {cn:<12}{en:<16}{level:<8}{feat}")

    sub("价格与可用性的权衡（市场大致区间）")
    print("""    ┌────────────┬──────────────┬──────────────┬──────────────────┐
    │ 类型       │ 单价区间      │ 可用率        │ 适用场景          │
    ├────────────┼──────────────┼──────────────┼──────────────────┤
    │ 数据中心   │ ¥0.5-2/IP/月 │ 30-70%       │ 无风控站点、内网   │
    │ 住宅       │ ¥15-50/GB    │ 85-95%       │ 有 IP 风控的站点   │
    │ 移动       │ ¥40-120/GB   │ 90-98%       │ 强风控、App 接口   │
    │ 自建隧道   │ 服务器成本    │ 依上游        │ 企业合规出口       │
    └────────────┴──────────────┴──────────────┴──────────────────┘

    ▸ 关键洞察：**可用率比单价重要得多**。
      5 元买 100 个 30% 可用率的 IP，实际有效成本是 166 元/100 个可用 IP。
      不如花 30 元买 20 个 90% 可用率的。""")

    sub("★ 合规提醒")
    print("""    · 免费公开代理：99% 不可用，且**可能被用来窃取你的请求内容**
      （包括 Cookie、Token、账号密码）—— 生产环境绝对不要用
    · 代理服务商的合规性需要你自己核实
    · 用技术手段隐藏身份去绕过站点的访问控制，可能违反：
        - 目标站的服务条款（民事违约）
        - 《反不正当竞争法》（若涉及商业竞争）
        - 《刑法》第 285 条（非法获取计算机信息系统数据）
    ▸ 一句话：代理是中性工具，关键是**你用它访问什么、获得了什么**。""")


def exp2_checker() -> None:
    title("【实验 2】检测器实测 —— 用本地模拟代理验证检测逻辑")
    print("""    启动 6 个本地模拟代理，覆盖各种真实会遇到的状况：

      端口 18080  正常，延迟 20ms     → 期望 elite
      端口 18081  正常，延迟 400ms    → 期望 elite（慢）
      端口 18082  泄露真实 IP         → 期望 transparent
      端口 18083  带 Via 头           → 期望 anonymous
      端口 18084  返回 502            → 期望 dead
      端口 18085  返回 403（被封）     → 期望 dead""")

    specs = [
        ("fast", 0.02, "normal"),
        ("slow", 0.40, "normal"),
        ("leaky", 0.05, "leak"),
        ("via", 0.05, "via"),
        ("dead", 0.00, "dead"),
        ("banned", 0.00, "ban"),
    ]
    proxies, servers, threads = start_mock_proxies(specs)
    checker = ProxyChecker(echo_url="http://127.0.0.1/echo", real_ip="203.0.113.42")

    print(f"\n    {'代理':<22}{'状态':<10}{'HTTP':<8}{'延迟':<12}{'匿名度':<16}{'泄露'}")
    print("    " + "-" * 92)
    results: list[CheckResult] = []
    for p in proxies:
        r = checker.check(p)
        results.append(r)
        # 把检测结果回填到代理对象（这正是生产中的做法）
        if r.alive:
            p.ok_count += 1
            p.total_latency += r.latency
            p.last_check = time.time()
        else:
            p.fail_count += 1
            p.last_check = time.time()
        status = "✓ 存活" if r.alive else "✗ 不可用"
        print(f"    {r.proxy_key:<22}{status:<10}{r.status:<8}"
              f"{r.latency * 1000:>6.0f}ms{'':<4}{r.anonymity:<16}{r.leaked_ip or '-'}")

    sub("匿名度判定逻辑解读")
    print("""    transparent（透明）：响应里出现了我的真实 IP 203.0.113.42
        → 说明代理把你的真实 IP 写进了 X-Forwarded-For
        → 目标站一眼看出你在用代理，且知道你是谁

    anonymous（匿名）：响应里有 Via / X-Proxy-Id 等头
        → 隐藏了真实 IP，但暴露了『我经过了代理』
        → 目标站知道要防你，会提高风控等级

    elite（高匿）：什么都没出现
        → 目标站看来你就是个普通客户端
        → 这是我们想要的""")

    sub("清理模拟服务器")
    for s in servers:
        s.shutdown()
    print(f"    已关闭 {len(servers)} 个模拟服务器")

    return results


def exp3_scoring() -> None:
    title("【实验 3】评分算法 —— 为什么不能只看成功率")

    @dataclass
    class Case:
        name: str
        ok: int
        fail: int
        lat: float
        consec: int
        banned: bool

    cases = [
        Case("A 稳定快速", 95, 5, 0.18, 0, False),
        Case("B 稳定但慢", 95, 5, 1.80, 0, False),
        Case("C 高成功但很慢", 99, 1, 3.50, 0, False),
        Case("D 中等成功快速", 70, 30, 0.15, 0, False),
        Case("E 刚连续失败 2 次", 80, 20, 0.30, 2, False),
        Case("F 被目标站封禁", 100, 0, 0.10, 0, True),
    ]

    now = time.time()
    print(f"    {'节点':<20}{'成功率':<10}{'延迟':<12}{'连败':<8}{'评分':<10}{'备注'}")
    print("    " + "-" * 92)
    scored: list[tuple[str, float]] = []
    for c in cases:
        p = Proxy(host="10.0.0.1", port=1)
        p.ok_count = c.ok
        p.fail_count = c.fail
        p.total_latency = c.lat * c.ok
        p.consec_fail = c.consec
        p.last_check = now
        p.banned_until = now + 60 if c.banned else 0.0
        s = p.score(now)
        scored.append((c.name, s))
        note = ""
        if c.banned:
            note = "封禁中 → 直接 0 分"
        elif c.lat > 2:
            note = "慢，但比失败强"
        elif c.consec >= 2:
            note = "惩罚系数生效"
        print(f"    {c.name:<20}{p.success_rate * 100:>6.1f}%{'':<3}"
              f"{c.lat * 1000:>7.0f}ms{'':<4}{c.consec:<8}{s:<10.2f}{note}")

    sub("评分的关键设计点")
    print("""    1. **成功率权重 0.6** —— 一次失败要重试（可能触发风控），
       而慢 1 秒只是浪费时间。失败的代价远高于慢。

    2. **延迟做归一化**：200ms 满分，5s 零分。
       线性的 min-max 比 sigmoid 更直观，也够用。

    3. **新鲜度衰减**：10 分钟没检测的节点分数掉到 0。
       防止『曾经很好但现在早就挂了』的节点继续被选中。

    4. **连败惩罚系数 1/(1+0.5n)**：连败 2 次 → ×0.5，连败 4 次 → ×0.33。
       避免『偶然成功一次就把平均成功率拉回来』。

    5. **封禁直接归零**：这是硬约束，不做任何加权。""")

    sub("实测：不同策略的节点使用分布")
    print("    模拟 3000 次调度，看三种策略分别把流量分给了谁。\n")

    random.seed(42)
    pool_proxies = []
    for c in cases[:5]:  # 排除被封禁的
        p = Proxy(host=c.name[0], port=1)
        p.ok_count, p.fail_count = c.ok, c.fail
        p.total_latency = c.lat * c.ok
        p.consec_fail = c.consec
        p.last_check = now
        pool_proxies.append(p)

    for strategy in ("round_robin", "weighted", "best_first"):
        pool = ProxyPool(pool_proxies, strategy=strategy)
        pool.pick_log.clear()
        for _ in range(3000):
            pool.pick()
        from collections import Counter

        cnt = Counter(pool.pick_log)
        total = sum(cnt.values())
        dist = "  ".join(
            f"{k}:{v / total * 100:4.1f}%" for k, v in sorted(cnt.items())
        )
        print(f"    {strategy:<14}{dist}")

    print("""
    ▸ 解读：
      round_robin  流量完全均分 —— 对优质节点不公平，慢节点也没被淘汰
      weighted     向高分倾斜 —— 优者多得，是推荐策略
      best_first   全部打到 1 个节点 —— 该节点 IP 消耗极快，很快被封
                   这是新手最常见的错误

    ▸ 生产建议：weighted + 每次使用后回填结果。
      再叠加一个『同域限速』（同一目标站每个 IP 每分钟最多 N 次）。""")


def exp4_lifecycle() -> None:
    title("【实验 4】代理池完整生命周期模拟")
    print("    模拟一个 20 节点的池，跑 500 次请求，观察质量演化。\n")

    random.seed(7)
    proxies = []
    for i in range(20):
        p = Proxy(host=f"10.0.{i // 256}.{i % 256}", port=8000 + i)
        # 池子的典型分布：60% 好、30% 一般、10% 差
        r = random.random()
        p.kind = "residential" if r < 0.3 else "datacenter"
        p._quality = 0.95 if r < 0.6 else (0.75 if r < 0.9 else 0.3)  # type: ignore[attr-defined]
        p._latency = random.uniform(0.15, 0.4)  # type: ignore[attr-defined]
        p.last_check = time.time()
        proxies.append(p)

    pool = ProxyPool(proxies, strategy="weighted")
    total = 500
    for i in range(total):
        p = pool.pick()
        if p is None:
            print(f"    第 {i} 次：池已空（所有节点都被封禁）")
            break
        ok = random.random() < p._quality  # type: ignore[attr-defined]
        pool.report(p, ok, p._latency if ok else 0.0)  # type: ignore[attr-defined]
        # 每 100 次做一次健康检查
        if (i + 1) % 100 == 0:
            st = pool.stats()
            alive = pool.alive
            top = sorted(alive, key=lambda x: -x.score())[:3]
            print(f"    第 {i + 1:>3} 次后：可用 {st.alive:>2}/20  "
                  f"封禁 {st.banned:>2}  平均分 {st.avg_score:>5.1f}  "
                  f"Top3: " + ", ".join(f"{t.key}({t.score():.0f})" for t in top))

    sub("最终池状态")
    print(pool.render())

    sub("生命周期总结")
    print("""    采集 → 检测 → 入池 → 调度 → 回填 → 淘汰

    ┌──────────┬─────────────────────────────────────────────┐
    │ 阶段     │ 关键动作                                     │
    ├──────────┼─────────────────────────────────────────────┤
    │ 采集     │ 自有 IP / 服务商 API 拉取 / 内网出口枚举       │
    │ 检测     │ 连通性 + 匿名度 + 延迟，三合一并发检测          │
    │ 入池     │ 淘汰可用率 < 60% 的，打上类型标签              │
    │ 调度     │ weighted 随机 + 同域限速 + 会话粘性            │
    │ 回填     │ 每次请求后立即更新成功率/延迟/连败计数           │
    │ 淘汰     │ 连败 3 次下线；定时（如 5 分钟）全量重检          │
    │ 扩容     │ 可用数低于阈值时自动拉新                        │
    └──────────┴─────────────────────────────────────────────┘

    ▸ 最难的部分不是实现，而是**检测的准确性**：
      很多代理在国内可用、到目标站就被封（地域风控），
      所以检测的目标 URL 应该就是你要爬的目标站，而不是 google.com。""")

    sub("★ 会话粘性 —— 容易被忽略的坑")
    print("""    有些站点要求『整个会话用同一个 IP』（登录 → 加购 → 下单）。
    这时不能每次请求都换代理，而要维护『会话 → 代理』的绑定：

      session_proxy_map = {}
      def get_proxy(session_id: str) -> Proxy:
          if session_id in session_proxy_map:
              p = session_proxy_map[session_id]
              if not p.is_banned:
                  return p
          p = pool.pick()
          session_proxy_map[session_id] = p
          return p

    ▸ 配套要点：会话绑定后，该 IP 的『消耗配额』要单独计数，
      否则 20 个会话都绑到同一个优质 IP 上，它会秒封。""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 8 课：代理池")
    print(SEP)
    print("""
代理池是规模化的必要条件。本课用本地模拟代理实测完整生命周期，
不依赖任何外部代理服务。

★ 代理是中性工具，请用于自有系统或已授权场景。
""")
    exp1_types()
    exp2_checker()
    exp3_scoring()
    exp4_lifecycle()

    title("本课小结")
    print("""
  ✓ 代理分三层匿名度：透明 / 匿名 / 高匿 —— 目标只有高匿
  ✓ 可用率比单价重要：30% 可用率的便宜 IP，实际成本更贵
  ✓ 免费公开代理会窃取你的 Cookie/Token，生产禁用
  ✓ 检测三要素：连通性 + 匿名度 + 延迟；检测目标应为真实目标站
  ✓ 评分 = 成功率 0.6 + 速度 0.25 + 新鲜度 0.15，再乘连败惩罚
  ✓ 调度用 weighted，不要用 best_first（会打爆单个 IP）
  ✓ 会话粘性：同一会话必须绑定同一 IP

  下一课（49）：合规红线 —— 这是整个阶段 4 最重要的一课。
""")


if __name__ == "__main__":
    main()
