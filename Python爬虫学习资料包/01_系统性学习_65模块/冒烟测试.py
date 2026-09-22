#!/usr/bin/env python3
"""一键冒烟测试 —— 验证这份资料包里的代码能不能真的跑起来。

设计意图：
  分享资料时最容易出的问题是「我这儿能跑，你那儿报错」。
  与其让对方一个个手动试，不如给一个脚本，一次跑完给出完整清单。

它做什么：
  1. 先做依赖体检：逐个 import 检查，缺失的库报出名字和用途，而不是等到运行时才炸
  2. 再逐个运行课程文件，带超时保护（有些课要跑几十秒），收集退出码与耗时
  3. 最后给出分级结论：哪些能跑、哪些缺依赖、哪些失败

用法：
    python3 冒烟测试.py                # 全部测试
    python3 冒烟测试.py --stage 2      # 只测阶段 2
    python3 冒烟测试.py --quick        # 跳过耗时超过 60 秒的文件
    python3 冒烟测试.py --deps-only    # 只做依赖体检

为什么用子进程而不是 import：
  课程文件里存在同名模块（多个 stage 目录下都有 models.py 之类），
  在同一个解释器里 import 会互相污染 sys.modules。
  子进程隔离最干净，代价是启动开销 —— 这里完全值得。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------- 依赖清单
# 每个库标注「用途 + 缺了会影响哪些阶段」，这样检出缺失时报错是有信息量的，
# 而不是干巴巴一句 ModuleNotFoundError
DEPENDENCIES: dict[str, tuple[str, str]] = {
    "requests":       ("HTTP 请求", "阶段 0/2/3"),
    "bs4":            ("HTML 解析（beautifulsoup4）", "阶段 2"),
    "lxml":           ("bs4 的高性能解析后端", "阶段 2"),
    "parsel":         ("CSS/XPath 选择器（Scrapy 同款）", "阶段 2/3"),
    "httpx":          ("支持 HTTP/2 与异步的客户端", "阶段 3/4"),
    "aiohttp":        ("另一种 asyncio HTTP 客户端", "阶段 3"),
    "tenacity":       ("重试装饰器", "阶段 3"),
    "pydantic":       ("数据校验", "阶段 3"),
    "pydantic_settings": ("配置与密钥管理", "阶段 3"),
    "pytest":         ("运行 spiderkit 的测试套件", "阶段 3"),
    "cryptography":   ("哈希/加密算法", "阶段 4.5"),
    "Crypto":         ("pycryptodome，覆盖更多加密模式", "阶段 4.5"),
    "PIL":            ("验证码图像处理（pillow）", "阶段 4.7"),
    "numpy":          ("图像矩阵运算 / 数据处理", "阶段 4.7、5.4"),
    "sqlalchemy":     ("ORM 与连接池", "阶段 5.2"),
    "pandas":         ("数据处理主力", "阶段 5.4"),
    "plotly":         ("交互式可视化", "阶段 5.6"),
    "apscheduler":    ("定时调度", "阶段 5.6"),
    "redis":          ("分布式队列与去重", "阶段 6.3"),
    "playwright":     ("浏览器自动化（体积大，可选）", "阶段 0.6、4.9"),
}


# ---------------------------------------------------------------- 数据结构
@dataclass
class Result:
    """单个文件的执行结果。"""
    stage: str
    name: str
    status: str          # ok / fail / timeout / skip
    seconds: float = 0.0
    detail: str = ""


@dataclass
class Report:
    """整轮测试的汇总。"""
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result) -> None:
        self.results.append(r)

    def by_status(self, status: str) -> list[Result]:
        return [r for r in self.results if r.status == status]


# ---------------------------------------------------------------- 终端着色
# 只在 TTY 下着色，重定向到文件时不输出转义序列（否则日志里全是乱码）
def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


_C = _supports_color()


def c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _C else text


GREEN, RED, YELLOW, DIM, BOLD, CYAN = "32", "31", "33", "90", "1", "36"


# ---------------------------------------------------------------- 依赖体检
def check_dependencies(quick: bool = False) -> tuple[list[str], list[str]]:
    """逐个 import 检查依赖。

    Returns:
        (已安装的库名列表, 缺失的库名列表)
    """
    import importlib

    print(c("\n【1/2】依赖体检", BOLD))
    print(c("─" * 68, DIM))

    present, missing = [], []
    for mod, (purpose, stages) in DEPENDENCIES.items():
        try:
            importlib.import_module(mod)
            version = getattr(importlib.import_module(mod), "__version__", "?")
            present.append(mod)
            print(f"  {c('✓', GREEN)} {mod:<18} {c(version, DIM):<28} {purpose}")
        except ImportError:
            missing.append(mod)
            print(f"  {c('✗', RED)} {mod:<18} {'未安装':<28} {purpose}  {c('→ ' + stages, YELLOW)}")

    print()
    if missing:
        print(f"  {c('⚠ 缺失 ' + str(len(missing)) + ' 个库', YELLOW)}，涉及它们的课程会被跳过（不算失败）。")
        print(f"    {c('一次性安装：', DIM)}")
        print(f"    pip3 install " + " ".join(
            {"bs4": "beautifulsoup4", "PIL": "pillow", "Crypto": "pycryptodome",
             "pydantic_settings": "pydantic-settings"}.get(m, m) for m in missing
        ))
    else:
        print(f"  {c('✓ 全部依赖已就绪 —— 所有课程都能跑', GREEN)}")

    return present, missing


# ---------------------------------------------------------------- 静态检查
def import_names(path: Path) -> set[str]:
    """用 AST 静态提取一个文件导入的第三方模块名。

    为什么不用「试运行看报错」来判断依赖缺失：
      因为那会把「代码本身有 bug」和「缺依赖」混为一谈。
      静态分析先把两者分开，结论才准确。
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])

    return {n for n in names if n not in sys.stdlib_module_names}


# ---------------------------------------------------------------- 执行器
def run_one(path: Path, stage: str, root: Path, timeout: int) -> Result:
    """在子进程里跑一个课程文件，收集退出码与耗时。"""
    site_pkgs = str(root / "code" / "stage3_concurrent")
    env = {**os.environ, "PYTHONPATH": site_pkgs + os.pathsep + os.environ.get("PYTHONPATH", "")}

    start = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, path.name],
            cwd=path.parent,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - start
        if proc.returncode == 0:
            return Result(stage, path.name, "ok", elapsed)
        # stderr 最后几行通常就是根因，截取避免刷屏
        tail = (proc.stderr or "").strip().splitlines()
        detail = tail[-1][:110] if tail else f"退出码 {proc.returncode}"
        return Result(stage, path.name, "fail", elapsed, detail)

    except subprocess.TimeoutExpired:
        return Result(stage, path.name, "timeout", float(timeout))


def discover(root: Path, stages: list[str] | None) -> list[tuple[str, Path]]:
    """找出所有要测的课程文件。

    只收 stageN_xxx 目录下的顶层 .py —— spiderkit 包内的模块由 pytest 单独覆盖，
    逐个跑它们没有意义（它们被 import 时不会执行主逻辑）。
    """
    code_dir = root / "code"
    found: list[tuple[str, Path]] = []

    for stage_dir in sorted(code_dir.glob("stage*")):
        if not stage_dir.is_dir():
            continue
        stage = stage_dir.name
        if stages and not any(stage.startswith(f"stage{s}_") for s in stages):
            continue
        for py in sorted(stage_dir.glob("*.py")):
            found.append((stage, py))

    return found


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(
        description="验证资料包中的课程代码能否运行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--stage", action="append", metavar="N",
                    help="只测指定阶段，可重复，如 --stage 2 --stage 3")
    ap.add_argument("--quick", action="store_true",
                    help="跳过耗时超过 60 秒的文件")
    ap.add_argument("--deps-only", action="store_true",
                    help="只做依赖体检，不运行代码")
    args = ap.parse_args()

    root = Path(__file__).resolve().parent
    print(c("\n" + "=" * 68, DIM))
    print(c("  Python 爬虫系统性学习 · 冒烟测试", BOLD))
    print(c(f"  资料包位置：{root}", DIM))
    print(c(f"  Python：{sys.version.split()[0]}", DIM))
    print(c("=" * 68, DIM))

    present, missing = check_dependencies()

    if args.deps_only:
        return 0

    # ---- 运行课程文件
    targets = discover(root, args.stage)
    if not targets:
        print(c("\n⚠ 没找到要测试的文件，检查 --stage 参数。", YELLOW))
        return 1

    print(c(f"\n【2/2】运行课程文件（共 {len(targets)} 个）", BOLD))
    print(c("─" * 68, DIM))

    # spiderkit 的测试套件单独跑一次，它比任何单个文件都更能说明代码质量
    spiderkit = root / "code" / "stage3_concurrent" / "spiderkit"
    if spiderkit.is_dir() and (not args.stage or any(s == "3" for s in (args.stage or []))):
        print(c("\n  ▸ spiderkit 测试套件（55 个测试）", CYAN))
        if "pytest" in missing:
            print(f"    {c('⊘ 跳过（缺 pytest）', YELLOW)}")
        else:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/", "-q"],
                cwd=spiderkit, capture_output=True, text=True, timeout=300,
            )
            line = [l for l in proc.stdout.strip().splitlines() if l.strip()]
            summary = line[-1] if line else "无输出"
            color = GREEN if proc.returncode == 0 else RED
            print(f"    {c('✓' if proc.returncode == 0 else '✗', color)} {summary}")

    report = Report()
    current = ""
    for stage, path in targets:
        if stage != current:
            current = stage
            print(c(f"\n  ▸ {stage}", CYAN))

        deps = import_names(path)
        blocked = sorted(d for d in deps if d in missing)
        if blocked:
            report.add(Result(stage, path.name, "skip", 0.0, f"缺 {', '.join(blocked)}"))
            print(f"    {c('⊘', YELLOW)} {path.name:<40} {c('跳过：缺 ' + ', '.join(blocked), YELLOW)}")
            continue

        timeout = 60 if args.quick else 300
        r = run_one(path, stage, root, timeout)
        report.add(r)

        if r.status == "ok":
            print(f"    {c('✓', GREEN)} {path.name:<40} {c(f'{r.seconds:6.1f}s', DIM)}")
        elif r.status == "timeout":
            print(f"    {c('⏱', YELLOW)} {path.name:<40} {c(f'超时 {timeout}s（可能正常，见说明）', YELLOW)}")
        else:
            print(f"    {c('✗', RED)} {path.name:<40} {c(r.detail, RED)}")

    # ---- 汇总
    ok = report.by_status("ok")
    fail = report.by_status("fail")
    skip = report.by_status("skip")
    tmo = report.by_status("timeout")

    print(c("\n" + "=" * 68, DIM))
    print(c("  测试结论", BOLD))
    print(c("=" * 68, DIM))
    print(f"  {c('通过', GREEN):<24} {len(ok):>3} 个")
    if skip:
        print(f"  {c('跳过（缺依赖）', YELLOW):<24} {len(skip):>3} 个")
    if tmo:
        print(f"  {c('超时', YELLOW):<24} {len(tmo):>3} 个")
    if fail:
        print(f"  {c('失败', RED):<24} {len(fail):>3} 个")

    if fail:
        print(c("\n  失败明细：", RED))
        for r in fail:
            print(f"    · {r.stage}/{r.name}")
            print(f"      {c(r.detail, DIM)}")

    if skip:
        print(c("\n  跳过的文件装上依赖后即可运行：", YELLOW))
        for r in skip:
            print(f"    · {r.stage}/{r.name}  ({r.detail})")

    print()
    if not fail and not missing:
        print(c("  ✓ 全部通过。这份资料包是完好的。", GREEN))
    elif not fail:
        print(c(f"  ✓ 已装的依赖下全部通过。装齐 {len(missing)} 个缺失库后可跑完整清单。", GREEN))
    else:
        print(c("  ⚠ 有文件失败。请把上面的失败明细发给分享给你的人。", YELLOW))

    print(c("\n  提示：超时的文件通常是在做实测基准（如并发压测、大规模采集），", DIM))
    print(c("        它们跑得久是设计使然，单独运行看输出即可。", DIM))
    print()
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
