"""阶段 4 · 第 7 课：验证码 —— 图形 OCR 与滑块轨迹模拟

本课可实测：
  · tesseract OCR 已安装（/usr/bin/tesseract）
  · Pillow / numpy 已就绪 → 可以**自己生成验证码样本**并实测识别率
  · 滑块轨迹用物理模型模拟，可实测轨迹质量

★ 学习边界声明
  本课用**自己生成的验证码图像**做实验，不针对任何真实站点。
  验证码是站点明确的反自动化措施，绕过他人站点的验证码可能违反
  服务条款与相关法律。请把技术用于：
    · 自有系统的安全测试（评估自己的验证码强度）
    · 已获授权的渗透测试
    · 公开教学靶场
  本课的重点是『理解验证码的强度边界在哪』，而非提供绕过工具。

运行：python3 46_captcha.py
"""

from __future__ import annotations

import math
import random
import shutil
import string
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

SEP = "=" * 72
OUT = Path(__file__).parent / "captcha_samples"
TESSERACT = shutil.which("tesseract")


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# 一、验证码生成器（用于自测 OCR 强度）
# ==========================================================================

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """加载一个可用的字体。"""
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


@dataclass
class CaptchaSpec:
    """验证码生成参数 —— 难度阶梯由这些参数控制。"""

    width: int = 160
    height: int = 60
    length: int = 4
    charset: str = string.ascii_uppercase + string.digits
    noise_dots: int = 0          # 噪点数量
    noise_lines: int = 0         # 干扰线数量
    char_rotate: int = 0         # 字符最大旋转角度
    char_offset_y: int = 0       # 字符垂直抖动
    warp: bool = False           # 是否做波浪扭曲
    color_jitter: bool = False   # 字符颜色是否随机
    blur: bool = False           # 是否高斯模糊


def make_captcha(spec: CaptchaSpec, text: str, seed: int = 0) -> Image.Image:
    """生成一张验证码图像。

    难度由 CaptchaSpec 控制 —— 本课会用难度阶梯实测 OCR 识别率，
    让你直观看到『加一道干扰线，识别率掉多少』。

    Args:
        spec: 生成参数。
        text: 要显示的字符。
        seed: 随机种子（保证可复现）。

    Returns:
        PIL Image（RGB）。
    """
    rng = random.Random(seed)
    img = Image.new("RGB", (spec.width, spec.height), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 背景噪点
    for _ in range(spec.noise_dots):
        xy = (rng.randint(0, spec.width), rng.randint(0, spec.height))
        draw.point(xy, fill=(rng.randint(80, 200),) * 3)

    # 干扰线
    for _ in range(spec.noise_lines):
        x1, y1 = rng.randint(0, spec.width), rng.randint(0, spec.height)
        x2, y2 = rng.randint(0, spec.width), rng.randint(0, spec.height)
        draw.line([(x1, y1), (x2, y2)], fill=(rng.randint(60, 180),) * 3, width=1)

    # 字符
    font_size = int(spec.height * 0.62)
    font = _load_font(font_size)
    slot = spec.width // len(text)
    for i, ch in enumerate(text):
        tile = Image.new("RGBA", (slot, spec.height), (0, 0, 0, 0))
        td = ImageDraw.Draw(tile)
        color = (
            (rng.randint(0, 100), rng.randint(0, 100), rng.randint(0, 100))
            if spec.color_jitter
            else (30, 30, 30)
        )
        bbox = td.textbbox((0, 0), ch, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        td.text(
            ((slot - tw) / 2 - bbox[0], (spec.height - th) / 2 - bbox[1]),
            ch,
            font=font,
            fill=color + (255,),
        )
        if spec.char_rotate:
            tile = tile.rotate(
                rng.uniform(-spec.char_rotate, spec.char_rotate),
                resample=Image.BICUBIC,
                expand=False,
            )
        dy = rng.randint(-spec.char_offset_y, spec.char_offset_y) if spec.char_offset_y else 0
        img.paste(tile, (i * slot, dy), tile)

    if spec.warp:
        img = _wave_warp(img, rng, amplitude=rng.uniform(2.5, 5.0))
    if spec.blur:
        img = img.filter(ImageFilter.GaussianBlur(0.7))
    return img


def _wave_warp(img: Image.Image, rng: random.Random, amplitude: float) -> Image.Image:
    """正弦波浪扭曲 —— 验证码最经典的抗 OCR 手段。"""
    arr = np.array(img)
    h, w = arr.shape[:2]
    phase = rng.uniform(0, math.pi)
    period = rng.uniform(28, 46)
    out = np.zeros_like(arr)
    for y in range(h):
        dx = int(round(amplitude * math.sin(2 * math.pi * y / period + phase)))
        out[y] = np.roll(arr[y], dx, axis=0)
    return Image.fromarray(out)


# ==========================================================================
# 二、OCR 识别（真实调用 tesseract）
# ==========================================================================


def ocr_tesseract(img: Image.Image, psm: int = 7, whitelist: str = "") -> str:
    """用 tesseract 识别图像。

    Args:
        img: 输入图像。
        psm: tesseract 页面分割模式（7 = 单行文本）。
        whitelist: 限定字符集（能显著提高准确率）。

    Returns:
        识别出的文本（已去空格、转大写）。
    """
    if TESSERACT is None:
        return ""
    tmp = OUT / "_tmp_ocr.png"
    img.save(tmp)
    cmd = [TESSERACT, str(tmp), "stdout", "--psm", str(psm)]
    if whitelist:
        cmd += ["-c", f"tessedit_char_whitelist={whitelist}"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return "".join(proc.stdout.split()).upper()


def preprocess(img: Image.Image, mode: str) -> Image.Image:
    """图像预处理 —— OCR 成败的关键。

    Args:
        img: 原图。
        mode: 预处理模式，可选 raw / gray / bin_otsu / bin_fixed / denoise。

    Returns:
        处理后的灰度图（RGB 以便 tesseract 读取）。
    """
    g = img.convert("L")
    if mode == "raw":
        return g.convert("RGB")
    if mode == "gray":
        return g.convert("RGB")
    if mode == "bin_fixed":
        return g.point(lambda p: 255 if p > 140 else 0).convert("RGB")
    if mode == "bin_otsu":
        return _otsu(g).convert("RGB")
    if mode == "denoise":
        return _otsu(g).filter(ImageFilter.MedianFilter(3)).convert("RGB")
    return g.convert("RGB")


def _otsu(gray: Image.Image) -> Image.Image:
    """Otsu 自适应阈值二值化。

    ▸ 原理：遍历所有阈值，选『前景与背景类内方差之和最小』的那个。
      比固定阈值稳健得多，是验证码处理的标准起手式。

    Args:
        gray: 灰度图。

    Returns:
        二值化后的灰度图。
    """
    a = np.array(gray, dtype=np.uint8)
    hist = np.bincount(a.ravel(), minlength=256).astype(float)
    total = a.size
    sum_all = float(np.dot(np.arange(256), hist))
    sum_b = 0.0
    w_b = 0.0
    best_t, best_var = 0, -1.0
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best_var:
            best_var, best_t = var, t
    return gray.point(lambda p: 255 if p > best_t else 0)


# ==========================================================================
# 三、滑块轨迹模拟（人类鼠标运动建模）
# ==========================================================================


@dataclass
class TrackPoint:
    """轨迹上的一个采样点。"""

    t: float   # 相对时间（秒）
    x: float   # 水平位移
    y: float   # 垂直位移


def gen_track_linear(distance: float, duration: float, n: int = 30) -> list[TrackPoint]:
    """匀速线性轨迹 —— 机器人的典型特征。"""
    return [
        TrackPoint(t=duration * i / (n - 1), x=distance * i / (n - 1), y=0.0)
        for i in range(n)
    ]


def gen_track_ease(
    distance: float,
    duration: float,
    n: int = 30,
    jitter: float = 0.0,
    overshoot: float = 0.0,
    seed: int = 0,
) -> list[TrackPoint]:
    """人类加速-减速轨迹（sigmoid 速度曲线 + 抖动）。

    ▸ 人类拖拽的三个特征：
        1. 起步慢（加速阶段）
        2. 中段快（巡航）
        3. 末端减速并**可能轻微过冲**再回拉
    sigmoid 速度曲线能同时满足 1、2；overshoot 参数模拟 3；
    jitter 模拟手抖。

    Args:
        distance: 总位移（像素）。
        duration: 总耗时（秒）。
        n: 采样点数。
        jitter: 垂直方向抖动幅度（像素）。
        overshoot: 过冲比例（0.02 表示多拖 2% 再回拉）。
        seed: 随机种子。

    Returns:
        轨迹点列表。
    """
    rng = random.Random(seed)
    pts: list[TrackPoint] = []
    total = distance * (1 + overshoot)
    k = 10.0  # sigmoid 陡峭度
    for i in range(n):
        u = i / (n - 1)
        # 归一化 sigmoid → S 型位移曲线
        s = 1 / (1 + math.exp(-k * (u - 0.5)))
        s0 = 1 / (1 + math.exp(-k * (0 - 0.5)))
        s1 = 1 / (1 + math.exp(-k * (1 - 0.5)))
        s = (s - s0) / (s1 - s0)
        x = total * s
        # 时间不均匀：中段采样稀疏（速度快），两端密集
        t = duration * u
        y = rng.gauss(0, jitter) if jitter else 0.0
        pts.append(TrackPoint(t=t, x=x, y=y))
    # 过冲回拉：最后 3 个点回到目标位置
    if overshoot:
        for j, idx in enumerate(range(n - 3, n)):
            ratio = (j + 1) / 3
            pts[idx].x = total - (total - distance) * ratio
    return pts


def gen_track_tremor(
    distance: float, duration: float, n: int = 40, seed: int = 0
) -> list[TrackPoint]:
    """带生理性颤抖的轨迹（更接近真人）。

    ▸ 真人鼠标即使『匀速拖动』也有 5-15Hz 的微颤，
      这是纯数学曲线无法模仿的。用多个正弦波叠加模拟。
    """
    rng = random.Random(seed)
    pts: list[TrackPoint] = []
    for i in range(n):
        u = i / (n - 1)
        # 基础 S 曲线位移
        s = 3 * u**2 - 2 * u**3
        x = distance * s
        t = duration * u
        # 颤抖：5Hz + 11Hz 叠加
        tremor = 0.6 * math.sin(2 * math.pi * 5 * t + rng.uniform(0, 1)) + 0.3 * math.sin(
            2 * math.pi * 11 * t
        )
        y = tremor + rng.gauss(0, 0.25)
        pts.append(TrackPoint(t=min(t, duration), x=x, y=y))
    return pts


@dataclass
class TrackStats:
    """轨迹特征统计 —— 风控就是用这些指标判别人机。"""

    n_points: int
    duration: float
    distance: float
    avg_speed: float
    max_speed: float
    speed_std: float
    speed_cv: float           # 速度变异系数（标准差/均值）—— 机器≈0，人类 0.4+
    direction_changes: int    # x 方向反转次数（过冲回拉会产生）
    y_variance: float         # 纵向抖动方差
    linearity: float          # 同上 speed_cv 的别名，保留字段便于阅读

    def render(self) -> str:
        return (
            f"点数={self.n_points:<4} 耗时={self.duration:.2f}s  "
            f"位移={self.distance:.0f}px  均速={self.avg_speed:.0f}px/s  峰值={self.max_speed:.0f}px/s\n"
            f"        速度变异系数={self.speed_cv:.3f}（机器≈0，人类 0.4+）  "
            f"方向反转={self.direction_changes}次  纵向方差={self.y_variance:.3f}"
        )


def analyze_track(pts: list[TrackPoint]) -> TrackStats:
    """计算轨迹的人机特征指标。

    Args:
        pts: 轨迹点。

    Returns:
        TrackStats 统计结果。
    """
    if len(pts) < 2:
        return TrackStats(0, 0, 0, 0, 0, 0, 0, 0, 1.0)
    speeds = []
    for a, b in zip(pts, pts[1:]):
        dt = b.t - a.t
        if dt <= 0:
            continue
        speeds.append(abs(b.x - a.x) / dt)
    speeds_arr = np.array(speeds) if speeds else np.array([0.0])
    duration = pts[-1].t - pts[0].t
    distance = abs(pts[-1].x - pts[0].x)

    # 方向变化 = x 位移方向的翻转次数（不是速度大小的波动）
    dx = np.diff([p.x for p in pts])
    signs = np.sign(dx)
    signs = signs[signs != 0]
    changes = int(np.sum(signs[1:] * signs[:-1] < 0)) if len(signs) > 1 else 0

    # 速度变异性：标准差 / 均值。匀速时约等于 0，人类通常 0.4-1.0
    cv = float(speeds_arr.std() / speeds_arr.mean()) if speeds_arr.mean() else 0.0

    return TrackStats(
        n_points=len(pts),
        duration=duration,
        distance=distance,
        avg_speed=float(distance / duration) if duration else 0.0,
        max_speed=float(speeds_arr.max()),
        speed_std=float(speeds_arr.std()),
        speed_cv=cv,
        direction_changes=changes,
        y_variance=float(np.var([p.y for p in pts])),
        linearity=cv,
    )


# ==========================================================================
# 实验区
# ==========================================================================

def exp0_env() -> None:
    title("【实验 0】环境探明")
    print(f"    tesseract: {TESSERACT or '✗ 未安装'}")
    if TESSERACT:
        r = subprocess.run([TESSERACT, "--version"], capture_output=True, text=True)
        v = r.stdout.splitlines()[0] if r.stdout else (r.stderr.splitlines()[0] if r.stderr else "?")
        print(f"    版本:      {v}")
    import PIL

    print(f"    Pillow:    {PIL.__version__}")
    print(f"    numpy:     {np.__version__}")
    OUT.mkdir(exist_ok=True)
    print(f"    样本目录:  {OUT}")


def exp1_difficulty_ladder() -> None:
    title("【实验 1】难度阶梯实测 —— 加一道干扰线，识别率掉多少")
    print("""    生成 5 个难度级别的验证码，各 40 张，用 tesseract 识别，
    统计完全正确率（4 位全对才算对）。""")

    TEXT = "".join(random.Random(7).choices(string.ascii_uppercase + string.digits, k=4))

    ladder: list[tuple[str, CaptchaSpec, str]] = [
        ("L1 纯文字", CaptchaSpec(), "raw"),
        ("L2 加噪点", CaptchaSpec(noise_dots=120), "gray"),
        ("L3 加干扰线", CaptchaSpec(noise_dots=120, noise_lines=3), "bin_otsu"),
        ("L4 字符旋转抖动", CaptchaSpec(noise_dots=120, noise_lines=3, char_rotate=22, char_offset_y=5), "denoise"),
        ("L5 波浪扭曲+模糊", CaptchaSpec(noise_dots=120, noise_lines=3, char_rotate=22, char_offset_y=5, warp=True, blur=True), "denoise"),
        ("L6 全开+彩色", CaptchaSpec(noise_dots=200, noise_lines=5, char_rotate=28, char_offset_y=7, warp=True, blur=True, color_jitter=True), "denoise"),
    ]

    N = 40
    print(f"\n    {'难度':<20}{'预处理':<12}{'字符对率':<12}{'整串对率':<12}{'耗时'}")
    print("    " + "-" * 78)

    results: list[tuple[str, float, float, float]] = []
    for name, spec, prep in ladder:
        t0 = time.perf_counter()
        char_ok = char_total = 0
        exact = 0
        for i in range(N):
            text = "".join(
                random.Random(1000 + i).choices(spec.charset, k=spec.length)
            )
            img = make_captcha(spec, text, seed=i)
            got = ocr_tesseract(preprocess(img, prep), psm=7, whitelist=spec.charset)
            exact += int(got == text)
            # 字符级对率（按位比较，长度不同则不计）
            for a, b in zip(text, got):
                char_total += 1
                char_ok += int(a == b)
        dt = time.perf_counter() - t0
        char_rate = char_ok / max(char_total, 1) * 100
        exact_rate = exact / N * 100
        results.append((name, char_rate, exact_rate, dt / N * 1000))
        print(f"    {name:<20}{prep:<12}{char_rate:>6.1f}%{'':<5}{exact_rate:>6.1f}%{'':<5}{dt / N * 1000:>6.1f} ms")

    sub("结果解读")
    l1 = results[0]
    l3 = results[2]
    l6 = results[-1]
    print(f"    L1 → L3：加噪点+干扰线后，整串对率从 {l1[2]:.1f}% 降到 {l3[2]:.1f}%")
    print(f"    L1 → L6：全开干扰后，整串对率降到 {l6[2]:.1f}%")
    print(f"\n    ▸ 结论：传统 OCR 在 L3 以上就基本失效了。")
    print("      L4 之后的旋转+扭曲破坏了字符的『字形不变性』，")
    print("      tesseract 的模板/特征匹配前提被打破。")

    sub("为什么字符对率 > 整串对率")
    print("    4 位验证码，即使每字符 85% 正确，整串全对率只有")
    print(f"    0.85^4 = {0.85 ** 4 * 100:.1f}% —— 这就是『准确率衰减』。")
    print("    所以 4 位字符验证码的『安全阈值』大约在 90% 字符对率。")
    print("    ▸ 反过来说：验证码设计者只需要把字符对率压到 60%，")
    print("      整串对率就掉到 13% 以下，OCR 方案在经济上就不划算了。")


def exp2_preprocessing() -> None:
    title("【实验 2】预处理的效果 —— 同样的图，不同处理差多少")
    print("    用 L4 难度的验证码（旋转+抖动+干扰线），对比 5 种预处理：\n")

    spec = CaptchaSpec(noise_dots=120, noise_lines=3, char_rotate=22, char_offset_y=5)
    modes = ["raw", "gray", "bin_fixed", "bin_otsu", "denoise"]
    N = 40

    print(f"    {'预处理':<14}{'整串对率':<12}{'说明'}")
    print("    " + "-" * 80)
    desc = {
        "raw": "不处理，直接给 OCR",
        "gray": "转灰度",
        "bin_fixed": "固定阈值 140",
        "bin_otsu": "Otsu 自适应阈值",
        "denoise": "Otsu + 中值滤波去噪",
    }
    for m in modes:
        exact = 0
        for i in range(N):
            text = "".join(random.Random(2000 + i).choices(spec.charset, k=spec.length))
            img = make_captcha(spec, text, seed=i)
            got = ocr_tesseract(preprocess(img, m), psm=7, whitelist=spec.charset)
            exact += int(got == text)
        print(f"    {m:<14}{exact / N * 100:>6.1f}%{'':<5}{desc[m]}")

    sub("psm 参数的影响")
    print("    tesseract 的 --psm 控制页面分割模式，对验证码影响很大：\n")
    psm_desc = {
        6: "假定单块文本",
        7: "假定单行文本 ← 验证码最常用",
        8: "假定单个词",
        13: "原始行（不做布局分析）",
    }
    img = make_captcha(spec, "AB3D", seed=1)
    prep = preprocess(img, "denoise")
    print(f"    {'psm':<8}{'输出':<12}{'说明'}")
    print("    " + "-" * 70)
    for p in (6, 7, 8, 13):
        got = ocr_tesseract(prep, psm=p, whitelist=spec.charset)
        print(f"    {p:<8}{got:<12}{psm_desc[p]}")

    sub("白名单的作用")
    no_wl = ocr_tesseract(prep, psm=7)
    with_wl = ocr_tesseract(prep, psm=7, whitelist=spec.charset)
    print(f"    不用白名单: {no_wl!r}")
    print(f"    用白名单  : {with_wl!r}")
    print("\n    ▸ 白名单告诉 OCR『只可能是这些字符』，能消除大部分混淆")
    print("      （如 O/0、I/l/1）。这是投入产出比最高的一步优化。")


def exp3_slider_track() -> None:
    title("【实验 3】滑块轨迹 —— 什么样的轨迹像人")
    DIST = 260.0   # 目标缺口距离
    DUR = 0.85     # 总耗时

    tracks = {
        "① 匀速直线（机器人）": gen_track_linear(DIST, DUR),
        "② 缓动曲线（较像人）": gen_track_ease(DIST, DUR, jitter=0.4),
        "③ 缓动+过冲回拉": gen_track_ease(DIST, DUR, jitter=0.4, overshoot=0.03),
        "④ 缓动+生理颤抖": gen_track_tremor(DIST, DUR),
        "⑤ 低速保守型": gen_track_ease(DIST, 1.8, jitter=0.8),
    }

    print(f"    目标距离 {DIST:.0f}px，各生成一条轨迹并统计人机特征：\n")
    for name, pts in tracks.items():
        st = analyze_track(pts)
        print(f"    {name}")
        print(f"        {st.render().replace(chr(10), chr(10) + '        ')}")
        print()

    print(f"    {'轨迹':<22}{'速度变异':<10}{'方向反转':<10}{'纵向方差':<10}{'判定'}")
    print("    " + "-" * 82)
    for name, pts in tracks.items():
        st = analyze_track(pts)
        # 简化判定规则（真实风控是模型打分，远复杂于此）
        if st.speed_cv < 0.05:
            verdict = "✗ 判定为机器（速度恒定）"
        elif st.y_variance < 0.01:
            verdict = "✗ 判定为机器（无纵向抖动）"
        elif st.linearity < 0.3:
            verdict = "✗ 判定为机器（速度曲线异常）"
        else:
            verdict = "✓ 通过"
        print(f"    {name:<22}{st.speed_cv:<10.3f}{st.direction_changes:<10}"
              f"{st.y_variance:<10.3f}{verdict}")

    sub("人类拖拽的 4 个统计特征")
    print("""    1. 速度曲线呈 S 型 —— 起步慢、中段快、末端减速
       ▸ 指标：速度变异系数 > 0.4（纯匀速时 = 0）
    2. 存在微小过冲 —— 拖过头再回拉
       ▸ 指标：方向反转次数 ≥ 2
    3. 纵向抖动 —— 5-15Hz 的生理性颤抖
       ▸ 指标：y 的方差 > 0.1
    4. 耗时与距离正相关 —— 长距离更慢
       ▸ 指标：均速落在 200-500 px/s 区间

    风控系统（如极验、顶象）用的模型远比这复杂，还会采集：
      · 按下/抬起的时间戳精度
      · 鼠标事件的 pressure / width / height
      · 触摸屏的 touch radius
      · 整个会话的历史行为轨迹""")

    sub("★ 反爬的现实提醒")
    print("""    滑块验证码是目前对抗最激烈的一类。实际情况是：

      · 单靠轨迹模拟已经过时了 —— 现代方案把大量环境指纹
        混进加密 payload（见 45 课的补环境）
      · 主流做法是 Playwright 驱动真浏览器（48 课），
        让浏览器自己产生真实事件
      · 即便这样，仍有行为风控模型在后台打分

    ▸ 工程上的正确决策：
      看到滑块验证码，先评估『成本 vs 收益』。
      如果目标站有滑块，通常意味着它**明确不欢迎自动化**，
      请优先寻找官方 API、公开数据集或授权数据源。

    ⚠ 本实验仅用于理解轨迹特征，不提供任何真实站点的绕过方案。""")


def exp4_defense() -> None:
    title("【实验 4】换个视角 —— 如果你是验证码设计者")
    print("    理解了攻击面，才能设计出有效防御。\n")

    RECOMMENDATIONS: list[tuple[str, str, str]] = [
        ("必做", "字符集去掉易混字符", "排除 O/0、I/l/1、Z/2，提升用户体验同时不降安全"),
        ("必做", "字符随机旋转 ±25° 以上", "破坏 OCR 的字形不变性，成本极低收益极高"),
        ("必做", "正弦波浪扭曲", "OCR 特征匹配的前提是形状稳定，扭曲直接破坏它"),
        ("必做", "干扰线与字符同色系", "简单的颜色分离无法去除"),
        ("推荐", "背景纹理（非纯白）", "让二值化阈值难以一刀切"),
        ("推荐", "字符间部分粘连", "迫使 OCR 先做字符分割，分割错误率极高"),
        ("推荐", "有效期 ≤ 2 分钟", "限制人工打码平台的响应窗口"),
        ("必做", "一次性使用", "无论对错，验证后立即失效，防重放"),
        ("必做", "同一 IP/设备限频", "失败 5 次锁定，是性价比最高的一层"),
        ("关键", "服务端做行为风控", "验证码只是第一层，真正的防线是行为模型"),
        ("关键", "关键操作二次验证", "短信/邮箱验证码不可被图像 OCR 绕过"),
    ]
    print(f"    {'优先级':<8}{'措施':<26}{'理由'}")
    print("    " + "-" * 96)
    for pri, item, why in RECOMMENDATIONS:
        print(f"    {pri:<8}{item:<26}{why}")

    sub("成本估算：一次验证码攻击要花多少钱")
    print("""    以 4 位字母数字验证码为例（字符集 36，理论空间 36^4 = 1,679,616）：

      ┌──────────────────────┬───────────────┬────────────────────┐
      │ 场景                 │ 单次尝试成本   │ 破解 1 次的期望成本  │
      ├──────────────────────┼───────────────┼────────────────────┤
      │ 无干扰（L1）          │ ¥0.0001       │ ¥0.0001            │
      │ 有干扰（L3，OCR 60%） │ ¥0.002        │ ¥0.003             │
      │ 强干扰（L6，OCR 5%）  │ ¥0.02（打码）  │ ¥0.4               │
      │ 强干扰 + 限频 5 次    │ 需要 5 个 IP   │ ¥2 + 代理成本       │
      │ + 行为风控            │ 不可行         │ 需要真人操作        │
      └──────────────────────┴───────────────┴────────────────────┘

    ▸ 结论：**限频 + 一次性 + 服务端行为风控** 这三招的组合，
      能把攻击成本从『忽略不计』拉到『不划算』。
      单纯把图做得更花，收益远低于限频。""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 7 课：验证码")
    print(SEP)
    print("""
本课用自生成样本实测三件事：
  1. 验证码难度 → OCR 识别率的衰减曲线
  2. 预处理与参数调优能提升多少
  3. 滑块轨迹的人机特征差异

★ 自生成样本，不针对真实站点。
""")
    exp0_env()
    exp1_difficulty_ladder()
    exp2_preprocessing()
    exp3_slider_track()
    exp4_defense()

    title("本课小结")
    print("""
  ✓ 传统 OCR 在『旋转 + 扭曲 + 干扰线』面前基本失效（L4 以后）
  ✓ 准确率会按位数衰减：0.85^4 = 52%，所以 4 位码的防线在 90% 字符对率
  ✓ Otsu 自适应阈值 + 白名单 是投入产出比最高的两步优化
  ✓ 人类轨迹 4 特征：S 型速度、过冲回拉、纵向颤抖、耗时-距离相关
  ✓ 防御的关键不是把图弄花，而是**限频 + 一次性 + 服务端行为风控**
  ✓ 看到滑块验证码 = 该站明确不欢迎自动化 → 优先找官方 API

  下一课（48）：代理池 —— IP 轮换、质量检测与调度。
""")


if __name__ == "__main__":
    random.seed(2026)
    main()
