"""
第 55 课 · 可视化与调度 —— 让爬虫自己跑起来

================================ 学习目标 ================================
1. 掌握用 Plotly 画出「能说明问题」的图，而不只是「好看的图」
2. 理解爬虫监控的四个关键指标（覆盖率、成功率、速度、健康度）
3. 掌握 APScheduler 的三种触发器与任务持久化
4. 掌握「爬虫任务」的调度模式：定时、依赖触发、事件驱动
5. 建立监控告警闭环：发现问题 → 通知人 → 有据可查

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/55_viz_schedule.py

依赖：plotly 6.5.2、apscheduler 3.11.3、pandas、matplotlib（均已安装）
生成的图表会保存到 code/stage5_data_engineering/output/ 目录。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

# ============================================================================
# 认知框架：可视化不是「最后画个图」，而是「把数据变成决策」
# ============================================================================
# 新手做可视化：把数据 df.plot() 一下，截个图发出去。
# 老手做可视化：先问「这张图要让谁做什么决定？」
#
# 三个实用原则：
#
#   ① **一图一结论**。一张图只回答一个问题。
#      想在一张图里塞进所有信息，结果是谁都看不懂。
#
#   ② **选择合适的图型**：
#        趋势     → 折线图（时间在 X 轴）
#        对比     → 柱状图 / 分组柱状图
#        构成     → 堆叠柱状图 / 饼图（类别 ≤ 5 个才用饼图）
#        分布     → 直方图 / 箱线图
#        相关     → 散点图
#        排名     → 横向条形图（类别名长时比柱状图好读）
#
#   ③ **给运维看的图要能一眼看出「现在正常吗」**：
#      加阈值线、加颜色分区（绿/黄/红），而不是只给原始曲线。


# ============================================================================
# 一、爬虫监控指标体系
# ============================================================================
# 爬虫项目最常被忽略的一件事：**没有监控**。
# 结果就是任务静默失败了三天，你才发现数据没更新。
#
# 需要监控的四个维度：
#
# ┌────────────────┬──────────────────────────────────────────────┐
# │ 覆盖率 Coverage │ 该抓的抓到了多少？                             │
# │                │ 指标：发现 URL 数 / 成功入库数 / 发现率         │
# │                │ 异常信号：发现 URL 数突然下降 → 列表页改版了    │
# ├────────────────┼──────────────────────────────────────────────┤
# │ 成功率 Success  │ 抓取请求里多少成功了？                         │
# │                │ 指标：200 比例 / 各状态码分布 / 重试次数         │
# │                │ 异常信号：403/429 上升 → 被反爬盯上了           │
# ├────────────────┼──────────────────────────────────────────────┤
# │ 速度 Speed      │ 跑得多快？                                     │
# │                │ 指标：QPS / 平均延迟 / P95 延迟                 │
# │                │ 异常信号：延迟突然升高 → 被限速了               │
# ├────────────────┼──────────────────────────────────────────────┤
# │ 健康度 Health   │ 数据质量如何？                                 │
# │                │ 指标：字段解析成功率 / 空值率 / 校验通过率       │
# │                │ 异常信号：某字段空值率飙升 → 页面结构变了        │
# └────────────────┴──────────────────────────────────────────────┘
#
# **最重要的是「变化率」而不是「绝对值」**：
#   成功率 95% 是好是坏？如果昨天是 99%，那就是异常；
#   如果一直是 95%，那可能是这个站点的正常水平。
#   所以监控要看趋势，并且需要历史基线。


@dataclass
class CrawlMetrics:
    """一次爬虫运行的核心指标快照。

    对应上面四个监控维度，每个维度取 1-2 个最关键的数字。
    指标不在多而在**每个都能驱动一个决策**。
    """

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None

    # 覆盖率
    urls_discovered: int = 0        # 列表页发现的 URL 总数
    urls_fetched: int = 0           # 实际发起的请求数
    urls_succeeded: int = 0         # 成功拿到内容的
    urls_new: int = 0               # 其中内容是新/有变化的

    # 成功率
    status_counts: dict[int, int] = field(default_factory=dict)
    retry_count: int = 0

    # 速度
    latencies_ms: list[float] = field(default_factory=list)

    # 健康度
    parse_success: int = 0          # 字段解析成功的记录数
    parse_total: int = 0            # 尝试解析的记录数

    @property
    def success_rate(self) -> float:
        """请求成功率。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.urls_succeeded / self.urls_fetched if self.urls_fetched else 0.0

    @property
    def coverage(self) -> float:
        """覆盖率（成功抓取 / 发现的总数）。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.urls_succeeded / self.urls_discovered if self.urls_discovered else 0.0

    @property
    def parse_rate(self) -> float:
        """字段解析成功率。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.parse_success / self.parse_total if self.parse_total else 0.0

    @property
    def qps(self) -> float:
        """平均每秒请求数。

        Returns:
            QPS 值。
        """
        if not self.finished_at:
            return 0.0
        secs = (self.finished_at - self.started_at).total_seconds()
        return self.urls_fetched / secs if secs > 0 else 0.0

    @property
    def avg_latency(self) -> float:
        """平均延迟（毫秒）。

        Returns:
            毫秒数。
        """
        return float(np.mean(self.latencies_ms)) if self.latencies_ms else 0.0

    @property
    def p95_latency(self) -> float:
        """P95 延迟（毫秒）。

        Returns:
            毫秒数。

        为什么看 P95 而不是平均值？
          平均值会被大量快请求拉低。假设 95 个请求 100ms、5 个请求 10s，
          平均只有 595ms —— 看起来很正常，但实际有 5% 的用户体验极差。
          P95 会告诉你「最慢的那 5% 有多慢」，这才是真实体验。
          这是性能监控的行业标准做法。
        """
        return float(np.percentile(self.latencies_ms, 95)) if self.latencies_ms else 0.0

    @property
    def duration(self) -> float:
        """运行时长（秒）。

        Returns:
            秒数。
        """
        if not self.finished_at:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()

    def to_row(self) -> dict[str, Any]:
        """转成一行数据，便于构建 DataFrame。

        Returns:
            指标字典。
        """
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "duration_s": round(self.duration, 2),
            "urls_discovered": self.urls_discovered,
            "urls_fetched": self.urls_fetched,
            "urls_succeeded": self.urls_succeeded,
            "urls_new": self.urls_new,
            "success_rate": round(self.success_rate, 4),
            "coverage": round(self.coverage, 4),
            "parse_rate": round(self.parse_rate, 4),
            "qps": round(self.qps, 2),
            "avg_latency_ms": round(self.avg_latency, 1),
            "p95_latency_ms": round(self.p95_latency, 1),
            "retry_count": self.retry_count,
            "http_200": self.status_counts.get(200, 0),
            "http_404": self.status_counts.get(404, 0),
            "http_403": self.status_counts.get(403, 0),
            "http_429": self.status_counts.get(429, 0),
            "http_5xx": sum(v for k, v in self.status_counts.items() if 500 <= k < 600),
        }


def simulate_runs(n_runs: int = 30, seed: int = 7) -> pd.DataFrame:
    """模拟一段时间的爬虫运行历史。

    Args:
        n_runs: 运行次数。
        seed: 随机种子。

    Returns:
        每次运行一行的 DataFrame。

    模拟数据里**故意注入两个异常事件**，用于演示监控图表能否发现问题：
      · 第 18 次：被反爬盯上，403/429 激增
      · 第 24 次：目标站改版，解析成功率暴跌
    这正是真实监控要解决的核心问题：**在图上能否一眼看出异常**。
    """
    rng = np.random.default_rng(seed)
    random.seed(seed)
    rows: list[dict[str, Any]] = []

    base_time = datetime(2026, 9, 1, 2, 0, 0)
    for i in range(1, n_runs + 1):
        started = base_time + timedelta(days=(i - 1) // 2, hours=(i % 2) * 6)
        discovered = int(rng.normal(1200, 60))

        # 默认正常状态
        s_200, s_404, s_403, s_429, s_5xx = 0.94, 0.03, 0.02, 0.005, 0.005
        parse_rate = float(rng.normal(0.97, 0.01))
        base_latency = float(rng.normal(320, 40))
        retries = int(rng.poisson(8))

        # 异常事件 1：第 18 次被反爬上强度
        if i == 18:
            s_200, s_403, s_429 = 0.72, 0.20, 0.07
            base_latency = 1850.0
            retries = 380
        # 异常事件 2：第 24 次目标站改版，解析崩了
        if i == 24:
            parse_rate = 0.42

        fetched = int(discovered * rng.uniform(0.92, 0.99))
        n200 = int(fetched * s_200)
        n404 = int(fetched * s_404)
        n403 = int(fetched * s_403)
        n429 = int(fetched * s_429)
        n5xx = max(0, fetched - n200 - n404 - n403 - n429)

        latencies = [max(30.0, rng.normal(base_latency, base_latency * 0.25))
                     for _ in range(min(fetched, 200))]

        duration = fetched / max(1.0, rng.normal(8.0, 0.8))
        finished = started + timedelta(seconds=duration)

        m = CrawlMetrics(
            run_id=f"run-{i:03d}",
            started_at=started,
            finished_at=finished,
            urls_discovered=discovered,
            urls_fetched=fetched,
            urls_succeeded=n200,
            urls_new=int(n200 * rng.uniform(0.05, 0.15)),
            status_counts={200: n200, 404: n404, 403: n403, 429: n429, 500: n5xx},
            retry_count=retries,
            latencies_ms=latencies,
            parse_success=int(n200 * parse_rate),
            parse_total=n200,
        )
        rows.append(m.to_row())

    return pd.DataFrame(rows)


# ============================================================================
# 二、可视化
# ============================================================================
OUTPUT_DIR = Path(__file__).parent / "output"


def _ensure_out() -> Path:
    """确保输出目录存在。

    Returns:
        输出目录路径。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def _hex_to_rgba(hex_color: str, alpha: float = 0.65) -> str:
    """把 '#RRGGBB' 十六进制颜色转成 plotly 需要的 'rgba(r,g,b,a)' 字符串。

    Args:
        hex_color: 形如 '#059669' 的颜色字符串（# 可省略）。
        alpha: 透明度，0~1 之间的小数。

    Returns:
        形如 'rgba(5,150,105,0.65)' 的字符串。

    Raises:
        ValueError: 当输入不是 3 位或 6 位十六进制颜色时。

    为什么需要这个函数（踩坑记录）：
      最初我用链式 replace 拼接：
          color.replace("#", "rgba(").replace(color, f"rgba(...)")
      第一个 replace 已经把 '#059669' 变成 'rgba(059669'，
      第二个 replace 再去找原始的 '#059669' 自然找不到，于是原样返回。
      结果 plotly 收到 'rgba(059669' 这个残缺字符串直接报：
          ValueError: Invalid value of type 'builtins.str'
          received for the 'fillcolor' property of scatter

      教训：字符串的多步链式替换，只要前一步改变了后一步要找的目标，
            就会静默失效。这种 bug 不报 Python 层的错，
            而是把坏数据透传到下游库才炸 —— 排查成本极高。
            正确做法是写一个语义明确的转换函数，一步到位。
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)  # '#abc' -> 'aabbcc'
    if len(h) != 6:
        raise ValueError(f"无法解析的颜色值：{hex_color!r}（应为 #RGB 或 #RRGGBB）")
    try:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError as exc:
        raise ValueError(f"无法解析的颜色值：{hex_color!r}") from exc
    return f"rgba({r},{g},{b},{alpha})"


def chart_trend(df: pd.DataFrame) -> str:
    """图 1：核心指标趋势图（含异常标注）。

    Args:
        df: 运行历史 DataFrame。

    Returns:
        保存的 HTML 文件路径。

    设计思路（这张图回答「现在正常吗」）：
      · 三个关键指标放子图：成功率 / P95 延迟 / 解析成功率
      · 每个指标画一条「基线」（历史中位数）作参考
      · 异常点用红色标出 —— 让人一眼看到问题在哪一天
      · 不要把所有指标塞进一张图（不同量纲，共享 Y 轴会失真）
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = _ensure_out()
    # 用运行序号当 X 轴，因为"运行次数"比日期更能反映趋势
    x = list(range(1, len(df) + 1))

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        subplot_titles=("请求成功率（含反爬异常）",
                        "P95 延迟（毫秒）",
                        "字段解析成功率（含改版异常）"),
        vertical_spacing=0.12,
    )

    # ---- 子图 1：成功率 ----
    fig.add_trace(go.Scatter(
        x=x, y=df["success_rate"], mode="lines+markers",
        name="成功率", line=dict(color="#2563eb", width=2),
        marker=dict(size=5),
        hovertemplate="第 %{x} 次<br>成功率 %{y:.1%}<extra></extra>",
    ), row=1, col=1)

    # 基线：中位数。比平均数更抗异常值干扰
    baseline = df["success_rate"].median()
    fig.add_hline(y=baseline, line_dash="dash", line_color="#94a3b8",
                  row=1, col=1,
                  annotation_text=f"基线 {baseline:.1%}",
                  annotation_position="bottom right")

    # 阈值线：低于 85% 就告警
    fig.add_hline(y=0.85, line_dash="dot", line_color="#ef4444", row=1, col=1,
                  annotation_text="告警阈值 85%", annotation_position="top left")

    # 标出异常点
    anomalies = df[df["success_rate"] < 0.90]
    fig.add_trace(go.Scatter(
        x=[x[i] for i in anomalies.index], y=anomalies["success_rate"],
        mode="markers", name="异常",
        marker=dict(color="#ef4444", size=13, symbol="x"),
        hovertemplate="第 %{x} 次<br>成功率骤降至 %{y:.1%}<extra></extra>",
    ), row=1, col=1)

    # ---- 子图 2：P95 延迟 ----
    fig.add_trace(go.Scatter(
        x=x, y=df["p95_latency_ms"], mode="lines+markers",
        name="P95 延迟", line=dict(color="#7c3aed", width=2),
        marker=dict(size=5),
        hovertemplate="第 %{x} 次<br>P95 %{y:.0f} ms<extra></extra>",
    ), row=2, col=1)
    lat_baseline = df["p95_latency_ms"].median()
    fig.add_hline(y=lat_baseline, line_dash="dash", line_color="#94a3b8",
                  row=2, col=1,
                  annotation_text=f"基线 {lat_baseline:.0f} ms",
                  annotation_position="top right")

    # ---- 子图 3：解析成功率 ----
    fig.add_trace(go.Scatter(
        x=x, y=df["parse_rate"], mode="lines+markers",
        name="解析成功率", line=dict(color="#059669", width=2),
        marker=dict(size=5),
        hovertemplate="第 %{x} 次<br>解析率 %{y:.1%}<extra></extra>",
    ), row=3, col=1)
    p_baseline = df["parse_rate"].median()
    fig.add_hline(y=p_baseline, line_dash="dash", line_color="#94a3b8",
                  row=3, col=1,
                  annotation_text=f"基线 {p_baseline:.1%}",
                  annotation_position="bottom right")
    parse_bad = df[df["parse_rate"] < 0.90]
    fig.add_trace(go.Scatter(
        x=[x[i] for i in parse_bad.index], y=parse_bad["parse_rate"],
        mode="markers", name="解析异常",
        marker=dict(color="#ef4444", size=13, symbol="x"),
        hovertemplate="第 %{x} 次<br>解析率跌至 %{y:.1%}<extra></extra>",
    ), row=3, col=1)

    fig.update_layout(
        height=760, showlegend=True,
        title_text="爬虫运行趋势监控（30 次运行）",
        hovermode="x unified",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(tickformat=".0%", row=1, col=1)
    fig.update_yaxes(tickformat=".0%", row=3, col=1)
    fig.update_xaxes(title_text="运行序号", row=3, col=1)

    path = out / "01_trend.html"
    fig.write_html(str(path))
    return str(path)


def chart_status_dist(df: pd.DataFrame) -> str:
    """图 2：HTTP 状态码构成（堆叠面积图）。

    Args:
        df: 运行历史 DataFrame。

    Returns:
        保存的 HTML 文件路径。

    为什么用堆叠面积图而不是饼图？
      饼图只能看「某一时刻」的构成，无法看趋势。
      而状态码构成的关键洞察恰恰在「变化」上：
      403 占比从 2% 涨到 20% 的过程，只有在时间轴上才看得见。
      饼图会把它拍平成一个静态快照，丢掉最有价值的信息。
    """
    import plotly.graph_objects as go

    out = _ensure_out()
    x = list(range(1, len(df) + 1))

    fig = go.Figure()
    specs = [
        ("http_200", "200 成功", "#059669"),
        ("http_404", "404 不存在", "#94a3b8"),
        ("http_403", "403 被拒", "#f59e0b"),
        ("http_429", "429 限流", "#dc2626"),
        ("http_5xx", "5xx 服务端错误", "#7c3aed"),
    ]
    for col, label, color in specs:
        if col not in df.columns:
            continue
        fig.add_trace(go.Scatter(
            x=x, y=df[col], name=label, mode="lines",
            stackgroup="one", line=dict(width=0.5, color=color),
            fillcolor=_hex_to_rgba(color, 0.65),
            hovertemplate=f"{label}: %{{y}}<extra></extra>",
        ))

    fig.update_layout(
        height=460, template="plotly_white",
        title_text="HTTP 状态码构成随时间变化（第 18 次 403/429 明显抬升）",
        xaxis_title="运行序号", yaxis_title="请求数",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )

    path = out / "02_status_dist.html"
    fig.write_html(str(path))
    return str(path)


def chart_latency_dist(df: pd.DataFrame) -> str:
    """图 3：延迟分布箱线图（正常 vs 异常对比）。

    Args:
        df: 运行历史 DataFrame。

    Returns:
        保存的 HTML 文件路径。

    箱线图的价值：**它一眼就能看出「分布是否偏移」**。
      中位线、四分位、离群点全在一张图里。
      对比两组数据（正常运行 vs 异常运行）时尤其清晰 ——
      你能看到整条分布整体右移，而不是只看到两个平均值不同。
    """
    import plotly.graph_objects as go

    out = _ensure_out()

    normal = df[df["success_rate"] >= 0.90]["p95_latency_ms"]
    abnormal = df[df["success_rate"] < 0.90]["p95_latency_ms"]

    fig = go.Figure()
    if len(normal):
        fig.add_trace(go.Box(
            y=normal, name=f"正常运行 (n={len(normal)})",
            boxmean="sd", marker_color="#059669",
            hovertemplate="P95延迟 %{y:.0f} ms<extra></extra>",
        ))
    if len(abnormal):
        fig.add_trace(go.Box(
            y=abnormal, name=f"异常运行 (n={len(abnormal)})",
            boxmean="sd", marker_color="#dc2626",
            hovertemplate="P95延迟 %{y:.0f} ms<extra></extra>",
        ))

    fig.update_layout(
        height=460, template="plotly_white",
        title_text="P95 延迟分布：正常 vs 异常运行",
        yaxis_title="P95 延迟（毫秒）",
        showlegend=True,
    )

    path = out / "03_latency_box.html"
    fig.write_html(str(path))
    return str(path)


def chart_coverage(df: pd.DataFrame) -> str:
    """图 4：覆盖率漏斗 + 双轴对比。

    Args:
        df: 运行历史 DataFrame。

    Returns:
        保存的 HTML 文件路径。

    这张图回答「发现的数据都抓到了吗、有多少是新的」。
    用双 Y 轴：左边是绝对数量，右边是比率。
      ⚠ 双轴图有被滥用的风险（可以操纵视觉印象），
        但在「数量 + 比率」这种明确的量纲区分下是合适的。
        判断标准：两个轴的物理意义是否清晰可分。
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    out = _ensure_out()
    x = list(range(1, len(df) + 1))

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=x, y=df["urls_discovered"], name="发现 URL",
        marker_color="#cbd5e1",
        hovertemplate="第 %{x} 次<br>发现 %{y}<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Bar(
        x=x, y=df["urls_fetched"], name="实际请求",
        marker_color="#93c5fd",
        hovertemplate="第 %{x} 次<br>请求 %{y}<extra></extra>",
    ), secondary_y=False)
    fig.add_trace(go.Bar(
        x=x, y=df["urls_succeeded"], name="成功抓取",
        marker_color="#2563eb",
        hovertemplate="第 %{x} 次<br>成功 %{y}<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=x, y=df["coverage"], name="覆盖率",
        mode="lines+markers", line=dict(color="#dc2626", width=2),
        marker=dict(size=5),
        hovertemplate="第 %{x} 次<br>覆盖率 %{y:.1%}<extra></extra>",
    ), secondary_y=True)

    fig.update_layout(
        height=460, template="plotly_white", barmode="group",
        title_text="漏斗视角：发现 → 请求 → 成功（附覆盖率曲线）",
        xaxis_title="运行序号",
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.02),
    )
    fig.update_yaxes(title_text="URL 数量", secondary_y=False)
    fig.update_yaxes(title_text="覆盖率", tickformat=".0%", secondary_y=True)

    path = out / "04_coverage.html"
    fig.write_html(str(path))
    return str(path)


def chart_heatmap(df: pd.DataFrame) -> str:
    """图 5：运行时刻 × 状态的健康度热力图。

    Args:
        df: 运行历史 DataFrame。

    Returns:
        保存的 HTML 文件路径。

    热力图适合回答「什么时间/什么维度上问题最集中」。
      这里按「运行序号的十位数」分组做行，展示多个指标的热度。
      实际项目中更常见的是「小时 × 星期」的热力图，
      能一眼看出「是不是每天这个点都被限流」。
    """
    import plotly.graph_objects as go

    out = _ensure_out()

    # 取几个关键指标构建热力图矩阵
    metrics = ["success_rate", "coverage", "parse_rate"]
    labels = ["成功率", "覆盖率", "解析率"]
    # 把运行序号切成 6 个时间窗
    n_windows = 6
    chunk = max(1, len(df) // n_windows)
    z: list[list[float]] = []
    y_labels: list[str] = []
    for i in range(0, len(df), chunk):
        part = df.iloc[i:i + chunk]
        if part.empty:
            continue
        z.append([float(part[c].mean()) for c in metrics])
        y_labels.append(f"第 {i + 1}-{i + len(part)} 次")

    fig = go.Figure(data=go.Heatmap(
        z=z, x=labels, y=y_labels,
        colorscale="RdYlGn", zmin=0.4, zmax=1.0,
        text=[[f"{v:.1%}" for v in row] for row in z],
        texttemplate="%{text}", textfont={"size": 13},
        hovertemplate="%{y}<br>%{x}: %{z:.1%}<extra></extra>",
        colorbar=dict(title="比率", tickformat=".0%"),
    ))

    fig.update_layout(
        height=420, template="plotly_white",
        title_text="健康度热力图（红=异常，绿=正常）",
        xaxis_title="指标", yaxis_title="运行区间",
    )

    path = out / "05_heatmap.html"
    fig.write_html(str(path))
    return str(path)


def exp1_charts() -> None:
    """实验 1：生成五张监控图表。"""
    print("=" * 74)
    print("实验 1 · 监控可视化：五张图回答五个问题")
    print("=" * 74)

    df = simulate_runs(30)
    print(f"\n  模拟了 {len(df)} 次爬虫运行，注入 2 个异常：")
    print("    · 第 18 次：被反爬盯上（403/429 激增）")
    print("    · 第 24 次：目标站改版（解析成功率暴跌）")

    print("\n  --- 五张图 ---")
    charts = [
        (chart_trend, "01_trend.html", "现在正常吗？（三指标趋势 + 基线 + 异常点）"),
        (chart_status_dist, "02_status_dist.html", "被什么方式拦了？（状态码构成随时间）"),
        (chart_latency_dist, "03_latency_box.html", "慢到什么程度？（延迟分布对比）"),
        (chart_coverage, "04_coverage.html", "该抓的都抓到了吗？（漏斗 + 覆盖率）"),
        (chart_heatmap, "05_heatmap.html", "哪个时间段问题集中？（健康度热力图）"),
    ]
    for fn, name, question in charts:
        p = fn(df)
        size = Path(p).stat().st_size / 1024
        print(f"    {name:<22}{size:>8.1f} KB   ← {question}")

    print("\n  --- 图里能看到什么（用数据验证）---")
    bad = df[df["success_rate"] < 0.90]
    print(f"\n    成功率低于 90% 的运行：{list(bad['run_id'])}")
    if len(bad):
        for _, r in bad.iterrows():
            print(f"      {r['run_id']}: 成功率 {r['success_rate']:.1%}，"
                  f"403={int(r['http_403'])}，429={int(r['http_429'])}，"
                  f"P95={r['p95_latency_ms']:.0f}ms")

    pbad = df[df["parse_rate"] < 0.90]
    print(f"\n    解析率低于 90% 的运行：{list(pbad['run_id'])}")
    for _, r in pbad.iterrows():
        print(f"      {r['run_id']}: 解析率 {r['parse_rate']:.1%} "
              f"（但成功率 {r['success_rate']:.1%} 正常）")

    print("\n  ▸ 关键洞察：**两个异常需要不同的应对**")
    print("      · 第 18 次成功率跌 → 反爬问题 → 降速、换代理、加 header")
    print("      · 第 24 次解析率跌 → 页面改版 → 改选择器，与反爬无关")
    print("    如果只看「成功率」这一个指标，第 24 次的问题**完全看不出来** ——")
    print("    请求全都 200 成功，但解析出来的全是 None。")
    print("    这就是为什么健康度指标必须和成功率分开监控。")

    print("\n  ▸ 图表的三个设计要点（复用自真实项目）：")
    print("      ① 画**基线**（中位数），让人知道「偏离多远算异常」")
    print("      ② 画**阈值线**，把「需要行动的边界」可视化")
    print("      ③ 标出**异常点**，不要让看图的人自己找")
    print("    运维看图的耐心只有 3 秒，信息必须一眼可得。")


# ============================================================================
# 三、调度：让爬虫自己跑
# ============================================================================
# APScheduler 的三种触发器，覆盖 95% 的调度需求：
#
#   ┌──────────────┬────────────────────────────────────────────┐
#   │ interval     │ 每隔 N 秒/分/时跑一次                        │
#   │              │ 用途：高频增量采集、监控类爬虫                │
#   │              │ 例：IntervalTrigger(minutes=10)             │
#   ├──────────────┼────────────────────────────────────────────┤
#   │ cron         │ 按日历规则跑（像 Linux crontab）             │
#   │              │ 用途：每日/每周定时任务                       │
#   │              │ 例：CronTrigger(hour=2, minute=30)          │
#   ├──────────────┼────────────────────────────────────────────┤
#   │ date         │ 在某个具体时刻跑一次                          │
#   │              │ 用途：延迟任务、一次性任务                    │
#   │              │ 例：DateTrigger(run_date='2026-10-01 09:00')│
#   └──────────────┴────────────────────────────────────────────┘
#
# 三个必须配置的参数（不配就会踩坑）：
#
#   ① **misfire_grace_time**：任务错过触发时间后的宽限期。
#      场景：调度器重启，错过了 2:30 的任务，3:00 才起来。
#      不设宽限期 → 任务被跳过，你以为跑了其实没跑。
#      设 3600 秒 → 60 分钟内补跑。
#
#   ② **max_instances**：同一任务最多同时跑几个实例。
#      场景：上次爬虫跑了 2 小时，10 分钟后又触发了一次。
#      不设限制 → 两个实例同时跑，重复抓取还可能互相冲突。
#      设 max_instances=1 → 上次没跑完就跳过这次触发（并记录警告）。
#
#   ③ **coalesce**：积压的多次触发是否合并成一次。
#      场景：调度器停了 5 小时，interval=10min 的任务积压了 30 次触发。
#      coalesce=True → 只补跑 1 次（合并）。
#      coalesce=False → 补跑 30 次！这通常不是你想要的。


@dataclass
class TaskResult:
    """一次任务执行的结果。

    Attributes:
        task_name: 任务名。
        started_at: 开始时间。
        finished_at: 结束时间。
        success: 是否成功。
        message: 结果说明。
        metrics: 本次的指标快照。
    """

    task_name: str
    started_at: datetime
    finished_at: datetime
    success: bool
    message: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def duration(self) -> float:
        """执行时长（秒）。

        Returns:
            秒数。
        """
        return (self.finished_at - self.started_at).total_seconds()


class TaskHistory:
    """任务执行历史（内存版，生产应落库）。

    为什么需要它？
      调度器只告诉你「任务在跑」，不告诉你「跑得好不好」。
      连续失败 5 次和成功 5 次，在调度器眼里是一样的。
      必须有执行历史，才能回答「这个任务健康吗」。
    """

    def __init__(self, maxlen: int = 200) -> None:
        """初始化。

        Args:
            maxlen: 最多保留多少条历史。
        """
        self.records: list[TaskResult] = []
        self.maxlen = maxlen

    def add(self, r: TaskResult) -> None:
        """添加一条记录。

        Args:
            r: 任务结果。

        Returns:
            None
        """
        self.records.append(r)
        if len(self.records) > self.maxlen:
            self.records.pop(0)

    def consecutive_failures(self, task_name: str) -> int:
        """统计某任务最近连续失败次数。

        Args:
            task_name: 任务名。

        Returns:
            连续失败次数。

        这是**告警最常用的判据**：
        单次失败可能只是网络抖动，连续 3 次失败就必须通知人了。
        用「连续失败」而不是「失败率」，
        是因为爬虫的失败往往是突发的（被封、站点挂了），
        连续失败能更快地反映「当前正在出问题」。
        """
        n = 0
        for r in reversed(self.records):
            if r.task_name != task_name:
                continue
            if r.success:
                break
            n += 1
        return n

    def summary(self) -> pd.DataFrame:
        """生成汇总表。

        Returns:
            每个任务一行的 DataFrame。
        """
        if not self.records:
            return pd.DataFrame()
        rows = []
        names = sorted({r.task_name for r in self.records})
        for name in names:
            rs = [r for r in self.records if r.task_name == name]
            ok = sum(1 for r in rs if r.success)
            rows.append({
                "task": name,
                "runs": len(rs),
                "success": ok,
                "failed": len(rs) - ok,
                "success_rate": round(ok / len(rs), 3),
                "avg_duration_s": round(float(np.mean([r.duration for r in rs])), 2),
                "consecutive_failures": self.consecutive_failures(name),
            })
        return pd.DataFrame(rows)


class AlertManager:
    """告警管理器。

    这里只做「判断是否需要告警」的逻辑，实际发送可以接邮件/webhook/
    企业微信机器人 —— 但**判断逻辑**才是重点，发送只是最后一公里。
    """

    def __init__(self, fail_threshold: int = 3,
                 duration_threshold: float = 300.0) -> None:
        """初始化。

        Args:
            fail_threshold: 连续失败多少次触发告警。
            duration_threshold: 执行超过多少秒触发告警。
        """
        self.fail_threshold = fail_threshold
        self.duration_threshold = duration_threshold
        self.triggered: list[dict[str, Any]] = []

    def check(self, r: TaskResult, history: TaskHistory) -> str | None:
        """检查是否需要告警。

        Args:
            r: 刚完成的任务结果。
            history: 任务历史（用于判断连续失败）。

        Returns:
            告警文案；不需要告警时返回 None。

        告警设计的原则：**降噪**。
        如果每次失败都告警，人很快就会忽略所有告警（告警疲劳）。
        所以这里用了三道闸：
          ① 连续失败 >= 3 才告警（忽略单次抖动）
          ② 同样的告警 30 分钟内不重复发（去重）
          ③ 区分级别（失败=ERROR，超时=WARN）
        """
        # 闸 1：连续失败
        consec = history.consecutive_failures(r.task_name)
        if consec >= self.fail_threshold:
            msg = (f"[ERROR] 任务 {r.task_name} 连续失败 {consec} 次"
                   f"（最近一次：{r.message}）")
            if self._dedup(r.task_name, "fail"):
                self.triggered.append(
                    {"level": "ERROR", "task": r.task_name, "message": msg})
                return msg

        # 闸 2：执行超时
        if r.duration > self.duration_threshold:
            msg = (f"[WARN] 任务 {r.task_name} 执行耗时 {r.duration:.0f}s，"
                   f"超过阈值 {self.duration_threshold:.0f}s")
            if self._dedup(r.task_name, "slow"):
                self.triggered.append(
                    {"level": "WARN", "task": r.task_name, "message": msg})
                return msg

        return None

    def _dedup(self, task: str, kind: str, window_s: float = 1800.0) -> bool:
        """告警去重（同一任务同一类型 30 分钟内只报一次）。

        Args:
            task: 任务名。
            kind: 告警类型。
            window_s: 去重时间窗（秒）。

        Returns:
            True 表示应该发送（未被去重）。
        """
        now = time.time()
        key = (task, kind)
        last = getattr(self, "_last_sent", {}).get(key, 0.0)
        if now - last < window_s:
            return False
        if not hasattr(self, "_last_sent"):
            self._last_sent: dict[tuple[str, str], float] = {}
        self._last_sent[key] = now
        return True


def make_crawl_task(name: str, history: TaskHistory, alerts: AlertManager,
                    failure_rate: float = 0.0,
                    duration: tuple[float, float] = (0.05, 0.15),
                    ) -> Callable[[], TaskResult]:
    """构造一个模拟的爬虫任务函数。

    Args:
        name: 任务名。
        history: 任务历史（任务内部写入）。
        alerts: 告警管理器。
        failure_rate: 模拟失败概率。
        duration: 模拟耗时区间（秒）。

    Returns:
        可被调度器调用的零参数函数。

    任务函数必须做到三点（这是生产爬虫任务的标配）：
      ① **捕获所有异常**，绝不让异常冒泡到调度器（否则任务会被禁用）
      ② **记录执行结果**到历史，供监控使用
      ③ **返回结构化结果**而不是 None，便于上层聚合
    """
    rng = random.Random(hash(name) & 0xFFFF)

    def _task() -> TaskResult:
        started = datetime.now()
        time.sleep(rng.uniform(*duration))
        success = rng.random() >= failure_rate
        finished = datetime.now()
        msg = "OK" if success else "模拟失败：HTTP 503"
        r = TaskResult(
            task_name=name, started_at=started, finished_at=finished,
            success=success, message=msg,
            metrics={"items": rng.randint(50, 200)} if success else {},
        )
        history.add(r)
        alert = alerts.check(r, history)
        if alert:
            print(f"      🔔 {alert}")
        return r

    return _task


def exp2_scheduler() -> None:
    """实验 2：APScheduler 调度 —— 三种触发器 + 错过触发的处理。"""
    print("\n" + "=" * 74)
    print("实验 2 · 任务调度：三种触发器与关键参数")
    print("=" * 74)

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    print("\n  --- 三种触发器（只展示配置，不实际等待）---")
    triggers = [
        ("IntervalTrigger", IntervalTrigger(minutes=10),
         "每 10 分钟一次 —— 高频增量采集"),
        ("CronTrigger", CronTrigger(hour=2, minute=30),
         "每天 02:30 一次 —— 全量更新（低峰期）"),
        ("CronTrigger(周)", CronTrigger(day_of_week="mon", hour=9),
         "每周一 09:00 —— 周报数据采集"),
        ("DateTrigger", DateTrigger(
            run_date=datetime(2026, 10, 1, 9, 0)),
         "2026-10-01 09:00 执行一次 —— 一次性任务"),
    ]
    print(f"\n    {'触发器类型':<20}{'下次触发时间':<26}{'用途'}")
    print("    " + "-" * 74)
    from datetime import timezone
    now = datetime.now(timezone.utc)
    for label, trg, use in triggers:
        try:
            nxt = trg.get_next_fire_time(None, now)
            nxt_s = nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else "-"
        except Exception as e:
            nxt_s = f"({type(e).__name__})"
        print(f"    {label:<20}{nxt_s:<26}{use}")

    print("\n  --- 实际跑一个短周期调度器（8 秒）---")
    history = TaskHistory()
    alerts = AlertManager(fail_threshold=2, duration_threshold=0.3)

    sched = BackgroundScheduler()
    ok_task = make_crawl_task("incremental_crawl", history, alerts,
                              failure_rate=0.0, duration=(0.02, 0.05))
    flaky_task = make_crawl_task("flaky_crawl", history, alerts,
                                 failure_rate=1.0, duration=(0.02, 0.05))

    sched.add_job(
        ok_task, "interval", seconds=2, id="incremental_crawl",
        max_instances=1, coalesce=True, misfire_grace_time=10,
    )
    sched.add_job(
        flaky_task, "interval", seconds=2, id="flaky_crawl",
        max_instances=1, coalesce=True, misfire_grace_time=10,
    )
    sched.start()
    print("    调度器已启动，等待 8 秒...\n")
    time.sleep(8)
    sched.shutdown(wait=False)

    print("\n  --- 执行历史汇总 ---")
    print(history.summary().to_string(index=False))

    print(f"\n  --- 触发的告警（{len(alerts.triggered)} 条）---")
    for a in alerts.triggered:
        print(f"    {a['level']:<7} {a['message']}")

    print("\n  ▸ 观察要点：")
    # 注意：这里刻意从真实执行结果推导文案，而不是写死「4 次」。
    # 因为 interval=2s + sleep(8s) 的实际触发次数受调度器启动开销、
    # 首次触发是否立即执行等因素影响，实测是 3 次而非 4 次。
    # 写死数字会在换机器/换版本时静默变成谎言 —— 这是演示脚本的常见坏味道。
    snap = history.summary().set_index("task") if not history.summary().empty else None
    if snap is not None and {"incremental_crawl", "flaky_crawl"} <= set(snap.index):
        ok_runs = int(snap.loc["incremental_crawl", "runs"])
        ok_rate = float(snap.loc["incremental_crawl", "success_rate"])
        bad_runs = int(snap.loc["flaky_crawl", "runs"])
        bad_rate = float(snap.loc["flaky_crawl", "success_rate"])
        print(f"      ① 稳定任务跑了 {ok_runs} 次、成功率 {ok_rate:.0%}；"
              f"失败任务跑了 {bad_runs} 次、成功率 {bad_rate:.0%}")
        print("         注意：触发次数不是 sleep 时长 ÷ 间隔 ——")
        print("         调度器启动 + 首次触发都在消耗这 8 秒，实际只跑到 3 次。")
    print("      ② 失败任务在连续失败达到阈值（2 次）时触发告警")
    print("      ③ 之后**不再重复告警**（去重窗口 30 分钟）——")
    print("         这就是「降噪」：告警一次就够，反复喊会让人麻痺")

    print("\n  ▸ 三个必须配的参数（回顾）：")
    print("      · misfire_grace_time：错过触发后的补跑宽限期")
    print("        不设 → 调度器重启后错过的任务永久丢失")
    print("      · max_instances=1：防止同一任务并发跑")
    print("        不设 → 上次没跑完又触发一次，重复抓取")
    print("      · coalesce=True：积压的多次触发合并成一次")
    print("        不设 → 停机 5 小时后重启，积压 30 次会全部补跑")


def exp3_cron_patterns() -> None:
    """实验 3：cron 表达式速查与常见误解。"""
    print("\n" + "=" * 74)
    print("实验 3 · cron 表达式：爬虫常用调度模式")
    print("=" * 74)

    from apscheduler.triggers.cron import CronTrigger
    from datetime import timezone

    patterns = [
        ("每天凌晨 2:30 全量更新", dict(hour=2, minute=30)),
        ("每 6 小时一次", dict(hour="*/6", minute=0)),
        ("工作日 9 点到 18 点每小时", dict(day_of_week="mon-fri",
                                          hour="9-18", minute=0)),
        ("每周一早上 8 点", dict(day_of_week="mon", hour=8, minute=0)),
        ("每月 1 号凌晨", dict(day=1, hour=0, minute=0)),
        ("每 15 分钟", dict(minute="*/15")),
        ("每天 8:00 和 20:00", dict(hour="8,20", minute=0)),
    ]

    now = datetime.now(timezone.utc)
    print(f"\n    {'调度需求':<26}{'参数':<44}{'下次触发'}")
    print("    " + "-" * 92)
    for label, kwargs in patterns:
        trg = CronTrigger(**kwargs)
        nxt = trg.get_next_fire_time(None, now)
        nxt_s = nxt.strftime("%m-%d %H:%M") if nxt else "-"
        param_s = ",".join(f"{k}={v}" for k, v in kwargs.items())
        print(f"    {label:<26}{param_s:<44}{nxt_s}")

    print("\n  ▸ 三个常见误解：")
    print("\n      ① **时区**。CronTrigger 默认用本地时区。")
    print("         服务器在 UTC、你在 UTC+8，写 hour=2 会在你早上 10 点跑。")
    print("         解决：显式传 timezone='Asia/Shanghai'。")
    print("         这是跨国项目最经典的坑 —— 一定要显式指定时区。")

    print("\n      ② **day_of_week 的取值**。APScheduler 用 0-6 表示 mon-sun")
    print("         （0=周一），但 Unix crontab 里 0=周日。")
    print("         两者**完全不同**！建议直接用 'mon'/'tue' 字符串，")
    print("         可读性也更好。")

    print("\n      ③ **'*' 与 '?'**。APScheduler 里 '?' 不是必需的区别符")
    print("         （Quartz 才需要）。直接省略即可，写成 '*' 也等价。")

    # 实测时区差异
    print("\n  --- 实测：时区对触发时刻的影响 ---")
    local_trg = CronTrigger(hour=2, minute=0)
    sh_trg = CronTrigger(hour=2, minute=0, timezone="Asia/Shanghai")
    utc_trg = CronTrigger(hour=2, minute=0, timezone="UTC")
    print(f"    默认时区   下次触发："
          f"{local_trg.get_next_fire_time(None, now).strftime('%m-%d %H:%M %Z')}")
    print(f"    Asia/上海  下次触发："
          f"{sh_trg.get_next_fire_time(None, now).strftime('%m-%d %H:%M %Z')}")
    print(f"    UTC        下次触发："
          f"{utc_trg.get_next_fire_time(None, now).strftime('%m-%d %H:%M %Z')}")
    print("\n    ▸ 同一个 hour=2，因为时区不同，实际的 UTC 时刻可能差 8 小时。")
    print("      爬虫要挑目标站的低峰期，所以**必须知道目标站在哪个时区**。")


def exp4_alert_design() -> None:
    """实验 4：告警设计 —— 让告警真的有用。"""
    print("\n" + "=" * 74)
    print("实验 4 · 告警设计：从「告警疲劳」到「告警有效」")
    print("=" * 74)

    history = TaskHistory()
    alerts = AlertManager(fail_threshold=3, duration_threshold=0.15)

    print("\n  --- 模拟一次「逐步恶化」的故障过程 ---")
    rng = random.Random(42)

    scenarios = [
        ("第 1 次：网络抖动，单次失败", False, 0.05),
        ("第 2 次：恢复正常", True, 0.05),
        ("第 3 次：又失败一次（仍未达阈值）", False, 0.05),
        ("第 4 次：连续失败第 1 次", False, 0.05),
        ("第 5 次：连续失败第 2 次", False, 0.05),
        ("第 6 次：连续失败第 3 次 → 触发告警", False, 0.05),
        ("第 7 次：连续失败第 4 次 → 去重，不再告警", False, 0.05),
        ("第 8 次：恢复正常", True, 0.4),
    ]

    print(f"\n    {'场景':<44}{'结果':<8}{'耗时':<10}{'告警'}")
    print("    " + "-" * 78)
    for label, success, dur in scenarios:
        started = datetime.now()
        time.sleep(dur)
        finished = datetime.now()
        r = TaskResult("daily_crawl", started, finished, success,
                       "OK" if success else "HTTP 503")
        history.add(r)
        alert = alerts.check(r, history)
        result_s = "成功" if success else "失败"
        alert_s = "🔔 已发送" if alert else ("—（去重）" if not success else "—")
        if success and dur > 0.15:
            alert_s = "⚠ 慢任务"
        print(f"    {label:<44}{result_s:<8}{r.duration:<10.2f}{alert_s}")

    print(f"\n  --- 汇总 ---")
    print(history.summary().to_string(index=False))
    print(f"\n  实际发送告警 {len(alerts.triggered)} 条：")
    for a in alerts.triggered:
        print(f"    {a['level']:<7} {a['message']}")

    print("\n  ▸ 告警设计的四条原则（这是运维的核心经验）：")
    print("\n      ① **可行动（Actionable）**")
    print("         每条告警都要能回答「我该做什么」。")
    print("         反例：「任务失败」→ 然后呢？")
    print("         正例：「任务连续失败 3 次，最近错误 HTTP 503，")
    print("                建议检查目标站点状态」")
    print("\n      ② **去重（Dedup）**")
    print("         同一个问题连续发生只报一次。")
    print("         否则你的收件箱会被同一个告警淹没，然后你就开始忽略了。")
    print("\n      ③ **分级（Severity）**")
    print("         ERROR（立即处理）/ WARN（今天内看）/ INFO（知悉即可）")
    print("         全都设成 ERROR，等于没有分级。")
    print("\n      ④ **有恢复通知**")
    print("         故障恢复时也要发一条 —— 否则你不知道问题是否解决了。")
    print("         （本实验没演示，但生产必备）")

    print("\n  ▸ 最重要的反直觉经验：")
    print("      **告警太少比太多更危险。**")
    print("      告警太多 → 人会忽略 → 等于没有告警（慢性的）")
    print("      告警太少 → 出问题没人知道 → 数据静默缺失（急性的）")
    print("      平衡点：只对「需要人介入」的情况告警，")
    print("              能用自动化处理的（重试、换代理）就不要惊动人。")


def exp5_pipeline_orchestration() -> None:
    """实验 5：任务编排 —— 依赖关系与失败传播。"""
    print("\n" + "=" * 74)
    print("实验 5 · 任务编排：串行依赖与失败传播")
    print("=" * 74)

    @dataclass
    class Step:
        """流水线中的一个步骤。

        Attributes:
            name: 步骤名。
            fn: 执行函数，返回 bool 表示成功。
            critical: 失败时是否阻断后续步骤。
        """

        name: str
        fn: Callable[[], bool]
        critical: bool = True

    print("\n  --- 典型爬虫数据流水线 ---")
    pipeline_desc = [
        ("① 发现 URL", "抓列表页，产出待抓队列"),
        ("② 抓取详情", "并发抓取，落原始数据"),
        ("③ 数据清洗", "规范化 + 校验（第 52 课）"),
        ("④ 增量入库", "UPSERT + 变化日志（第 54 课）"),
        ("⑤ 生成报表", "聚合统计 + 出图"),
        ("⑥ 发送通知", "把结果推给人"),
    ]
    print(f"\n    {'步骤':<16}{'说明'}")
    print("    " + "-" * 60)
    for name, desc in pipeline_desc:
        print(f"    {name:<16}{desc}")
    print("\n    ▸ 前 4 步 critical=True（任一失败则整条流水线中止）")
    print("      第 5、6 步 critical=False（报表失败不该阻断数据入库）")

    def run_pipeline(fail_at: str | None, slow_at: str | None = None
                     ) -> tuple[bool, list[str]]:
        """运行模拟流水线。

        Args:
            fail_at: 在哪一步失败（None 表示不失败）。
            slow_at: 在哪一步变慢。

        Returns:
            (整体是否成功, 日志列表)。
        """
        log: list[str] = []
        steps = [
            Step("① 发现 URL", lambda: fail_at != "①"),
            Step("② 抓取详情", lambda: fail_at != "②"),
            Step("③ 数据清洗", lambda: fail_at != "③"),
            Step("④ 增量入库", lambda: fail_at != "④"),
            Step("⑤ 生成报表", lambda: fail_at != "⑤", critical=False),
            Step("⑥ 发送通知", lambda: fail_at != "⑥", critical=False),
        ]
        overall_ok = True
        for st in steps:
            t0 = time.perf_counter()
            time.sleep(0.02 if slow_at != st.name else 0.08)
            ok = st.fn()
            dt = time.perf_counter() - t0
            if ok:
                log.append(f"    ✓ {st.name:<16}{dt * 1000:>7.1f} ms")
            else:
                mark = "✗ 关键" if st.critical else "⚠ 非关键"
                log.append(f"    {mark} {st.name:<16}{dt * 1000:>7.1f} ms  ← 失败")
                if st.critical:
                    log.append(f"      → 中止流水线（后续步骤不再执行）")
                    overall_ok = False
                    break
                log.append(f"      → 继续执行（这一步骤不阻断）")
                overall_ok = overall_ok and False if st.critical else overall_ok
        return overall_ok, log

    print("\n  --- 场景 A：全部成功 ---")
    ok, log = run_pipeline(fail_at=None)
    print("\n".join(log))
    print(f"    结果：{'成功' if ok else '失败'}")

    print("\n  --- 场景 B：第 ③ 步（清洗）失败 —— 关键步骤 ---")
    ok, log = run_pipeline(fail_at="③")
    print("\n".join(log))
    print(f"    结果：{'成功' if ok else '失败'}")

    print("\n  --- 场景 C：第 ⑤ 步（报表）失败 —— 非关键步骤 ---")
    ok, log = run_pipeline(fail_at="⑤")
    print("\n".join(log))
    print(f"    结果：{'成功' if ok else '失败'}  ← 数据已入库，报表失败不影响主流程")

    print("\n  ▸ 编排设计的核心问题：**哪些步骤失败应该阻断，哪些不该？**")
    print("\n      应该阻断（critical）：")
    print("        · 数据写入失败 → 后面基于数据的所有步骤都无意义")
    print("        · 依赖的上游数据缺失 → 硬跑出来的是错的")
    print("\n      不该阻断（non-critical）：")
    print("        · 报表/可视化失败 → 数据是对的，重跑报表即可")
    print("        · 通知失败 → 更不该阻断（通知本身是附带功能）")
    print("\n  ▸ 判据：**这一步的产出是不是下游的必需品？**")
    print("    是 → critical；不是 → 记日志继续。")
    print("    把所有步骤都设成 critical 的结果是：")
    print("    一个 QQ 机器人挂了，导致整个爬虫数据都没入库 —— 荒谬但很常见。")


def main() -> None:
    """运行全部实验。"""
    exp1_charts()
    exp2_scheduler()
    exp3_cron_patterns()
    exp4_alert_design()
    exp5_pipeline_orchestration()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 监控四维度：覆盖率 / 成功率 / 速度 / 健康度，各有独立异常信号",
        "2. 健康度（解析率）必须和成功率分开监控 —— 200 不代表数据是对的",
        "3. 看趋势和基线，不看绝对值：95% 是好是坏取决于历史",
        "4. 延迟看 P95 而不是平均值，平均值会被大量快请求掩盖长尾",
        "5. 给运维的图必须有基线 + 阈值线 + 异常点标注，3 秒内看懂",
        "6. 堆叠面积图 > 饼图：状态码的故事在「变化」上，饼图会拍平它",
        "7. misfire_grace_time 防止调度器重启后任务永久丢失",
        "8. max_instances=1 防止同一任务并发跑（重复抓取）",
        "9. coalesce=True 防止停机后积压的触发全部补跑",
        "10. CronTrigger 必须显式指定 timezone，否则服务器时区会坑你",
        "11. 告警四原则：可行动 / 去重 / 分级 / 有恢复通知",
        "12. 告警太少比太多更危险 —— 一次失败就报会让人麻痺",
        "13. 编排要区分 critical / non-critical，判据是「下游是否必需」",
        "14. 通知类步骤永远不该阻断主流程 —— 别让机器人挂了导致数据不入库",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
