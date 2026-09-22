"""
阶段 3 · 3.7 日志系统
======================================================
对应网页章节：#s3-7

本脚本演示生产级日志配置：
  1. 为什么 print 不够（无法分级、无法关闭、无法回溯）
  2. logging 四大组件：Logger / Handler / Formatter / Filter
  3. 双通道：控制台彩色简洁 + 文件详细可回溯
  4. 日志轮转：按大小切割，不会撑爆磁盘
  5. 上下文注入：每条日志自动带上"第几页/第几条"
  6. 结构化日志：JSON 格式，方便被 ELK / Loki 采集

运行：python3 36_logging_setup.py
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
import tempfile
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


WORK = Path(tempfile.mkdtemp(prefix="spider_log_"))

# 上下文变量：让日志自动带上"当前在处理哪一条"
# ContextVar 是协程安全的 —— 不像全局变量会在并发时串味
current_url: ContextVar[str] = ContextVar("current_url", default="-")
current_page: ContextVar[int] = ContextVar("current_page", default=0)


# ============================================================
# 一、自定义 Filter：注入上下文
# ============================================================
class ContextFilter(logging.Filter):
    """
    把 ContextVar 里的内容塞进日志记录。

    这样你就不用每条日志都手写 f"url={url}"，
    只要在任务开始时 set 一次，之后所有日志自动携带。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """
        注入上下文字段。永远返回 True（不过滤任何日志）。

        Args:
            record: 日志记录对象。

        Returns:
            True。
        """
        record.url = current_url.get()
        record.page = current_page.get()
        # 短 URL：长链接会把日志撑爆，截断显示
        if len(record.url) > 42:
            record.url = "…" + record.url[-41:]
        return True


# ============================================================
# 二、彩色控制台 Formatter
# ============================================================
class ColorFormatter(logging.Formatter):
    """带 ANSI 颜色的控制台格式化器。"""

    COLORS = {
        logging.DEBUG: "\033[37m",      # 灰
        logging.INFO: "\033[36m",       # 青
        logging.WARNING: "\033[33m",    # 黄
        logging.ERROR: "\033[31m",      # 红
        logging.CRITICAL: "\033[1;41m",  # 红底
    }
    RESET = "\033[0m"
    DIM = "\033[2m"

    def format(self, record: logging.LogRecord) -> str:
        """
        按级别着色。

        Args:
            record: 日志记录。

        Returns:
            着色后的字符串。
        """
        color = self.COLORS.get(record.levelno, "")
        record.levelname_c = f"{color}{record.levelname:<7}{self.RESET}"
        record.ts_c = f"{self.DIM}{self.formatTime(record, '%H:%M:%S')}{self.RESET}"
        return super().format(record)


# ============================================================
# 三、JSON 结构化 Formatter
# ============================================================
class JsonFormatter(logging.Formatter):
    """
    输出单行 JSON 日志。

    为什么要 JSON：文本日志要写正则才能解析，
    JSON 日志可以直接被 ELK / Loki / Datadog 结构化摄取。
    """

    def format(self, record: logging.LogRecord) -> str:
        """
        转成 JSON 行。

        Args:
            record: 日志记录。

        Returns:
            JSON 字符串。
        """
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "page": getattr(record, "page", 0),
            "url": getattr(record, "url", "-"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ============================================================
# 四、日志系统装配
# ============================================================
@dataclass
class LogPaths:
    """日志文件路径集合。"""

    main: Path
    jsonl: Path


def setup_logging(work: Path, level: int = logging.DEBUG) -> LogPaths:
    """
    装配完整的日志系统。

    设计要点：
      · 控制台只输出 INFO 以上（人看的，别太吵）
      · 文件输出 DEBUG 以上（机器/排查用，越全越好）
      · 文件按大小轮转（单文件 1MB × 3 个备份，永远不会撑爆磁盘）
      · 额外一份 JSONL 供日志系统采集

    Args:
        work: 工作目录。
        level: 根 logger 级别。

    Returns:
        日志文件路径。
    """
    root = logging.getLogger("spider")
    root.setLevel(level)
    root.handlers.clear()       # 避免重复配置导致日志打两遍
    root.propagate = False

    ctx_filter = ContextFilter()

    # ---- 通道 1：控制台（彩色，INFO+）----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(ColorFormatter(
        "%(ts_c)s %(levelname_c)s %(message)s \033[2m[p%(page)s]%(url)s\033[0m"
    ))
    console.addFilter(ctx_filter)
    root.addHandler(console)

    # ---- 通道 2：文本文件（详细，DEBUG+，按大小轮转）----
    main_path = work / "spider.log"
    file_h = logging.handlers.RotatingFileHandler(
        main_path, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | p%(page)s | %(url)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    file_h.addFilter(ctx_filter)
    root.addHandler(file_h)

    # ---- 通道 3：JSONL（供采集）----
    json_path = work / "spider.jsonl"
    json_h = logging.handlers.RotatingFileHandler(
        json_path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
    )
    json_h.setLevel(logging.INFO)
    json_h.setFormatter(JsonFormatter())
    json_h.addFilter(ctx_filter)
    root.addHandler(json_h)

    # ---- 通道 4：错误单独存（ERROR+）----
    err_h = logging.handlers.RotatingFileHandler(
        work / "error.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    err_h.setLevel(logging.ERROR)
    err_h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)s | %(url)s | %(message)s"
    ))
    err_h.addFilter(ctx_filter)
    root.addHandler(err_h)

    return LogPaths(main=main_path, jsonl=json_path)


# ============================================================
# 五、模拟爬虫：演示日志的实战用法
# ============================================================
def demo_print_vs_logging() -> None:
    """展示 print 的三个致命问题。"""
    print("=" * 78)
    print("【实验 1】print 的三个致命问题")
    print("=" * 78)
    print("""
  print 的问题：
    1. 无法开关 —— 上线后想去掉调试输出，得逐行删/注释
    2. 无级别   —— 紧急错误和调试信息混在一起，搜不到重点
    3. 无上下文 —— 没有时间戳、模块名、行号，出问题无法回溯
    4. 无落盘   —— 程序崩了，输出跟着终端一起没了

  对比：改成 logging 后，
    logger.debug("找到了 %d 个链接", n)   ← 生产环境一行配置就能全关
    logger.error("解析失败", exc_info=True)  ← 自动带上完整堆栈
""")


def demo_levels() -> None:
    """演示日志级别。"""
    log = logging.getLogger("spider.demo")
    print("=" * 78)
    print("【实验 2】五个级别长什么样")
    print("=" * 78)
    log.debug("这是 DEBUG：详细的中间过程，如『解析到 20 个 li 节点』")
    log.info("这是 INFO：正常进度，如『第 3/50 页完成，累计 60 条』")
    log.warning("这是 WARNING：不影响继续，但需要注意，如『价格字段缺失，用 0 填充』")
    log.error("这是 ERROR：单条失败，如『第 7 页解析失败: IndexError』")
    log.critical("这是 CRITICAL：整体不可用，如『连续 10 页全部失败，疑似被封』")
    print()


def demo_context_injection() -> None:
    """演示上下文自动注入。"""
    log = logging.getLogger("spider.context")
    print("=" * 78)
    print("【实验 3】上下文自动注入 —— 不用手写 f-string")
    print("=" * 78)

    for page in (1, 2):
        current_page.set(page)
        log.info("开始抓取第 %d 页", page)
        for i in range(1, 4):
            current_url.set(f"https://books.toscrape.com/catalogue/page-{page}/book-{i}.html")
            log.debug("解析书名：Book %d (Page %d)", i, page)
            if i == 2:
                log.warning("评分字段为空，填充默认值 0")
        log.info("第 %d 页完成", page)
    print()
    print("  注意：日志里自动出现了 [p1]/[p2] 和 URL，代码中一次都没写过这些变量。")
    print("  靠的是 ContextFilter 从 ContextVar 取值。并发时也不会串味。\n")


def demo_exception_logging() -> None:
    """演示异常日志。"""
    log = logging.getLogger("spider.exception")
    print("=" * 78)
    print("【实验 4】异常日志：exc_info=True 自动带堆栈")
    print("=" * 78)
    current_url.set("https://x.com/crash")
    current_page.set(3)

    try:
        data = {"a": 1}
        _ = data["missing_key"]       # 故意 KeyError
    except KeyError:
        log.error("解析详情页失败", exc_info=True)   # ← 完整堆栈进文件
    print()

    # 反面教材
    try:
        1 / 0
    except ZeroDivisionError as e:
        log.error(f"这样写只有一行信息，没有堆栈：{e}")
    print()
    print("  区别：exc_info=True 会写入完整 traceback，能直接定位到出错行。")
    print("  用 f-string 拼异常信息，丢掉了堆栈，排查时等于瞎猜。\n")


def demo_rotation() -> None:
    """演示日志轮转。"""
    log = logging.getLogger("spider.rotate")
    print("=" * 78)
    print("【实验 5】日志轮转：持续写，看文件怎么自动切")
    print("=" * 78)

    for i in range(8000):
        log.debug("模拟日志条目 %05d | 一些填充内容让文件变大 xxxxxxxxxxxxxxxxxxxxxx", i)

    files = sorted(WORK.glob("spider.log*"))
    total = sum(f.stat().st_size for f in files)
    print(f"\n  配置：单文件上限 1MB，保留 3 个备份")
    print(f"  当前文件列表（共 {total} 字节）：")
    for f in files:
        print(f"    {f.name:<24}{f.stat().st_size:>9} 字节")
    print("\n  RotatingFileHandler 的行为：")
    print("    spider.log 写满 1MB → 改名 spider.log.1 → 新建 spider.log")
    print("    spider.log.1 满了 → 变成 spider.log.2 …… 最多到 .3，更旧的被删")
    print("  → 磁盘占用恒定在 ~4MB，长期运行也不会撑爆。")
    print("  → 生产环境也可以用 TimedRotatingFileHandler 按天切。")


def demo_sampling() -> None:
    """演示高频日志的降噪技巧。"""
    log = logging.getLogger("spider.noise")
    print("\n" + "=" * 78)
    print("【实验 6】降噪：10 万条日志里只想看异常")
    print("=" * 78)

    # 技巧 1：用 logging.Filter 做采样（只记每 N 条）
    class SamplingFilter(logging.Filter):
        """每 N 条放行 1 条。"""

        def __init__(self, n: int = 10) -> None:
            super().__init__()
            self.n = n
            self.count = 0

        def filter(self, record: logging.LogRecord) -> bool:
            self.count += 1
            return self.count % self.n == 0

    sampler = logging.getLogger("spider.sampled")
    sampler.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(ColorFormatter("%(ts_c)s %(levelname_c)s %(message)s"))
    h.addFilter(SamplingFilter(10))
    sampler.addHandler(h)
    sampler.propagate = False

    for i in range(1, 31):
        sampler.debug("处理第 %d 条（只有每 10 条会打印）", i)
    print("\n  → 30 条里只打印了 3 条（第 10/20/30 条），信噪比提高 10 倍。")
    print("  → 生产爬虫的『每条进度』日志都应该采样，否则日志比数据还大。")

    # 技巧 2：运行时动态调级别
    print("\n  技巧：运行时动态调整级别（不用重启）")
    root_logger = logging.getLogger("spider")
    print(f"    当前文件通道级别：{root_logger.handlers[1].level}（DEBUG=10）")
    print("    想临时只看错误？root_logger.setLevel(logging.ERROR) 一行搞定。")


# ============================================================
# 六、日志统计：从日志里挖出有用信息
# ============================================================
def analyze_jsonl(path: Path) -> None:
    """解析 JSONL 日志，统计各级别数量。"""
    print("\n" + "=" * 78)
    print("【实验 7】结构化日志的威力：直接当数据查询")
    print("=" * 78)

    counts: dict[str, int] = {}
    pages: set[int] = set()
    urls: set[str] = set()

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue        # 进程被杀时最后一行可能不完整
            counts[rec["level"]] = counts.get(rec["level"], 0) + 1
            if rec.get("page"):
                pages.add(rec["page"])
            if rec.get("url", "-") != "-":
                urls.add(rec["url"])

    print(f"\n  日志文件：{path.name}（{path.stat().st_size} 字节）")
    print(f"  级别分布：{counts}")
    print(f"  涉及页码：{sorted(pages)}")
    print(f"  涉及 URL：{len(urls)} 个")
    print("\n  如果这是文本日志，你得写正则才能统计；")
    print("  JSON 日志一行 json.loads 就完事，还能直接喂给 Grafana 做看板。")


def main() -> None:
    """运行全部日志实验。"""
    paths = setup_logging(WORK)
    print(f"日志目录：{WORK}\n")

    demo_print_vs_logging()
    demo_levels()
    demo_context_injection()
    demo_exception_logging()

    # 写点东西给后面的统计用
    log = logging.getLogger("spider.crawl")
    for page in (1, 2, 3):
        current_page.set(page)
        log.info("开始抓取第 %d 页", page)
        for i in range(1, 6):
            current_url.set(f"https://books.toscrape.com/catalogue/page-{page}/book-{i}.html")
            log.debug("解析成功：Book %d", i)
            if i == 3 and page == 2:
                log.warning("价格字段为空")
        log.info("第 %d 页完成，5 本", page)

    time.sleep(0.05)
    demo_rotation()
    analyze_jsonl(paths.jsonl)
    demo_sampling()

    print("\n" + "=" * 78)
    print("日志配置模板（可以直接抄）")
    print("=" * 78)
    print("""
  logger = logging.getLogger("spider")
  logger.setLevel(logging.DEBUG)

  # 控制台：人看的，简洁+彩色，INFO 以上
  c = StreamHandler(sys.stdout); c.setLevel(INFO)
  c.setFormatter(ColorFormatter("%(ts)s %(levelname)s %(message)s"))

  # 文件：排查用的，详细，DEBUG 以上，按大小轮转
  f = RotatingFileHandler("spider.log", maxBytes=1MB, backupCount=3)
  f.setLevel(DEBUG)

  # 错误单独存：一眼定位到出问题的那几条
  e = RotatingFileHandler("error.log", maxBytes=512KB, backupCount=2)
  e.setLevel(ERROR)
""")
    print("  三条铁律：")
    print("    1. 别用 print —— 级别/开关/落盘一个都没有")
    print("    2. 异常一定用 exc_info=True —— 否则丢掉堆栈")
    print("    3. 高频进度日志要采样 —— 否则日志比数据大 100 倍")


if __name__ == "__main__":
    main()
