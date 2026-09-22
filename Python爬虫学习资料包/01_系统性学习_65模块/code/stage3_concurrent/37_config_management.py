"""
阶段 3 · 3.8 配置与密钥管理
======================================================
对应网页章节：#s3-8

本脚本演示生产项目的配置体系：
  1. 硬编码配置的四个坑
  2. 分层配置：默认值 → .env 文件 → 环境变量（优先级递增）
  3. pydantic-settings 做类型校验 + 启动即失败（fail fast）
  4. 密钥绝不进代码：.env / .gitignore / 环境变量
  5. 多环境切换：dev / staging / prod

运行：python3 37_config_management.py
（脚本会自己造一个临时 .env 做演示，不会碰你真实的配置）
"""

from __future__ import annotations

import os
import textwrap
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

WORK = Path("/root/.codebuddy/artifact/spider_systematic/config_demo")
WORK.mkdir(parents=True, exist_ok=True)


# ============================================================
# 一、配置模型
# ============================================================
class Env(str, Enum):
    """运行环境。"""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class SpiderSettings(BaseSettings):
    """
    爬虫项目配置。

    pydantic-settings 的核心价值：
      · 类型校验：把 SPIDER_CONCURRENCY=abc 这种错误在启动时就拦住
      · 自动读取：环境变量 + .env 文件，优先级明确
      · 敏感字段用 SecretStr，打印/日志时自动变 **********
      · IDE 有补全，不用猜配置项叫什么

    字段名 → 环境变量名的映射规则：
      忽略大小写，前缀可选。spider_concurrency / SPIDER_CONCURRENCY 都能识别。
    """

    model_config = SettingsConfigDict(
        env_file=str(WORK / ".env"),
        env_file_encoding="utf-8",
        env_prefix="SPIDER_",
        extra="ignore",              # 忽略无关的环境变量，避免误报
        case_sensitive=False,
    )

    # ---- 基础 ----
    env: Env = Env.DEV
    debug: bool = False

    # ---- 请求参数 ----
    concurrency: int = Field(default=8, ge=1, le=100)       # ge/le 直接做范围校验
    rate_limit: float = Field(default=5.0, gt=0)
    timeout_connect: float = Field(default=5.0, gt=0)
    timeout_read: float = Field(default=15.0, gt=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str = "SpiderBot/1.0 (+https://example.com/bot)"

    # ---- 目标 ----
    base_url: str = "https://books.toscrape.com/"
    start_pages: int = Field(default=5, ge=1)

    # ---- 存储 ----
    output_dir: Path = WORK / "output"
    db_path: Path = WORK / "data.db"

    # ---- 密钥（关键：用 SecretStr）----
    proxy_url: SecretStr | None = None
    api_key: SecretStr | None = None

    # ---- 校验器 ----
    @field_validator("user_agent")
    @classmethod
    def ua_must_be_sane(cls, v: str) -> str:
        """
        校验 UA：生产环境不该用默认的 python-requests。

        Args:
            v: 传入的 UA 字符串。

        Returns:
            校验后的 UA。

        Raises:
            ValueError: UA 为空。
        """
        if not v.strip():
            raise ValueError("user_agent 不能为空")
        return v.strip()

    @field_validator("base_url")
    @classmethod
    def base_url_must_end_with_slash(cls, v: str) -> str:
        """
        自动补齐结尾斜杠，避免 urljoin 拼接出错误路径。

        Args:
            v: 基础 URL。

        Returns:
            以 / 结尾的 URL。
        """
        return v if v.endswith("/") else v + "/"

    @model_validator(mode="after")
    def prod_needs_proxy(self) -> "SpiderSettings":
        """
        跨字段校验：生产环境必须配代理和密钥。

        Returns:
            校验后的自身。

        Raises:
            ValueError: 生产环境缺少必要配置。
        """
        if self.env is Env.PROD:
            missing = []
            if not self.proxy_url:
                missing.append("proxy_url")
            if not self.api_key:
                missing.append("api_key")
            if missing:
                raise ValueError(f"生产环境必须配置：{', '.join(missing)}")
        return self

    # ---- 派生属性 ----
    @property
    def timeout(self) -> tuple[float, float]:
        """requests 风格的 (连接超时, 读取超时) 元组。"""
        return (self.timeout_connect, self.timeout_read)

    @property
    def is_prod(self) -> bool:
        """是否生产环境。"""
        return self.env is Env.PROD

    def describe(self) -> list[tuple[str, str]]:
        """
        生成可安全打印的配置摘要（密钥自动脱敏）。

        Returns:
            (配置项, 值) 列表。
        """

        def safe(v: Any) -> str:
            """SecretStr 打印为 ********，其余原样。"""
            if isinstance(v, SecretStr):
                return "********" if v else "(未设置)"
            return str(v)

        return [
            ("运行环境", self.env.value),
            ("调试模式", str(self.debug)),
            ("并发数", str(self.concurrency)),
            ("限速(次/秒)", str(self.rate_limit)),
            ("超时(连接/读取)", f"{self.timeout_connect}s / {self.timeout_read}s"),
            ("最大重试", str(self.max_retries)),
            ("目标站点", self.base_url),
            ("起始页数", str(self.start_pages)),
            ("输出目录", str(self.output_dir)),
            ("代理", safe(self.proxy_url)),
            ("API Key", safe(self.api_key)),
        ]


# ============================================================
# 二、反面教材：硬编码配置
# ============================================================
@dataclass
class BadHardcodedConfig:
    """
    硬编码配置 —— 每个新项目都会写，然后每个都后悔。

    四个坑：
      1. 改配置要改代码，改代码要重新测试、重新发布
      2. 密钥写死在代码里，一推到 Git 就泄露（爬虫圈最常见的翻车方式）
      3. dev 和 prod 用同一份配置，测试时的并发数会打到生产站上
      4. 没有校验：并发数写成 0 或 "abc"，跑到一半才炸
    """

    concurrency: int = 8
    timeout: float = 15.0
    proxy: str = "http://user:pass@1.2.3.4:8080"     # ← 密钥直接暴露
    api_key: str = "sk-live-9f8a7b6c5d4e3f2a1b0c"   # ← 这个千万别学


# ============================================================
# 三、演示
# ============================================================
def write_demo_env() -> Path:
    """
    生成一份演示用 .env 文件。

    Returns:
        .env 文件路径。
    """
    p = WORK / ".env"
    p.write_text(
        textwrap.dedent("""
        # ===== 爬虫项目配置示例 =====
        # 这个文件绝对不要提交到 Git！
        # 用 .env.example 提交"有哪些配置项"，用 .env 装"真实值"

        SPIDER_ENV=staging
        SPIDER_DEBUG=false

        # 并发与限速
        SPIDER_CONCURRENCY=12
        SPIDER_RATE_LIMIT=4.5

        # 超时（秒）
        SPIDER_TIMEOUT_CONNECT=3.0
        SPIDER_TIMEOUT_READ=20.0
        SPIDER_MAX_RETRIES=5

        # 目标站点
        SPIDER_BASE_URL=https://books.toscrape.com
        SPIDER_START_PAGES=3

        # 密钥（示例值，真实项目里填真的）
        SPIDER_PROXY_URL=http://user:secretpass@127.0.0.1:7890
        SPIDER_API_KEY=sk-test-abcdef1234567890
        """).strip() + "\n",
        encoding="utf-8",
    )
    return p


def demo_hardcode_problems() -> None:
    """展示硬编码配置的问题。"""
    print("=" * 78)
    print("【实验 1】硬编码配置的四个坑")
    print("=" * 78)
    cfg = BadHardcodedConfig()
    print(f"\n  如果代码里直接写死：")
    print(f"    concurrency = {cfg.concurrency}")
    print(f"    proxy       = {cfg.proxy}")
    print(f"    api_key     = {cfg.api_key}")
    print("\n  问题：")
    print("    1. 想改并发要改代码 → 重新测试 → 重新发布")
    print("    2. 上面这行 proxy/api_key 一旦 git push，密钥就公开了")
    print("       （爬虫项目泄露代理账号是最常见的翻车方式）")
    print("    3. 测试环境和生产环境没法区分")
    print("    4. 没有校验，并发写成 0 也不报错，跑到线上才发现")
    print("\n  → 正确做法：全部外置到环境变量 + .env，代码只读配置。")


def demo_env_file() -> None:
    """演示 .env 文件读取。"""
    print("\n" + "=" * 78)
    print("【实验 2】分层配置：默认值 → .env → 环境变量")
    print("=" * 78)

    env_path = write_demo_env()
    print(f"\n  已生成演示配置：{env_path}")
    print("  内容节选：")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SPIDER_") and "#" not in line:
            print(f"    {line}")

    # 第一层：只有默认值（临时挪走 .env）
    backup = env_path.read_text(encoding="utf-8")
    env_path.unlink()
    base = SpiderSettings()
    print(f"\n  ── 第 1 层：只有代码默认值 ──")
    print(f"    env={base.env.value}  concurrency={base.concurrency}  "
          f"start_pages={base.start_pages}  proxy={base.proxy_url}")

    # 第二层：有 .env
    env_path.write_text(backup, encoding="utf-8")
    with_env = SpiderSettings()
    print(f"\n  ── 第 2 层：加载 .env 后 ──")
    print(f"    env={with_env.env.value}  concurrency={with_env.concurrency}  "
          f"start_pages={with_env.start_pages}  proxy={'已设置' if with_env.proxy_url else '无'}")
    print(f"    → 12 覆盖了默认值 8，3 覆盖了默认值 5")

    # 第三层：环境变量优先级最高
    os.environ["SPIDER_CONCURRENCY"] = "30"
    os.environ["SPIDER_ENV"] = "dev"
    os.environ.pop("SPIDER_PROXY_URL", None)
    top = SpiderSettings()
    print(f"\n  ── 第 3 层：再加环境变量 ──")
    print(f"    env={top.env.value}  concurrency={top.concurrency}")
    print(f"    → 环境变量 concurrency=30 覆盖了 .env 里的 12")
    print(f"    → 环境变量 env=dev 覆盖了 .env 里的 staging")
    print("\n  优先级：环境变量 > .env 文件 > 代码默认值")
    print("  为什么这样设计：容器化部署时用环境变量注入最方便，")
    print("  且不用把 .env 打进镜像（避免密钥留在镜像层里）。")

    # 清理环境变量
    for k in ("SPIDER_CONCURRENCY", "SPIDER_ENV"):
        os.environ.pop(k, None)


def demo_type_validation() -> None:
    """演示类型校验。"""
    print("\n" + "=" * 78)
    print("【实验 3】类型校验：启动就失败，而不是跑到一半才炸")
    print("=" * 78)

    cases: list[tuple[str, str, str]] = [
        ("SPIDER_CONCURRENCY", "abc", "不是数字"),
        ("SPIDER_CONCURRENCY", "0", "小于最小值 1"),
        ("SPIDER_CONCURRENCY", "9999", "超过最大值 100"),
        ("SPIDER_MAX_RETRIES", "-1", "负数重试次数"),
        ("SPIDER_RATE_LIMIT", "0", "必须 > 0"),
        ("SPIDER_TIMEOUT_CONNECT", "0", "必须 > 0"),
        ("SPIDER_ENV", "production", "枚举值不存在（应为 prod）"),
    ]

    print(f"\n  {'配置项':<26}{'测试值':<12}{'场景':<26}结果")
    print("  " + "-" * 74)
    for key, val, desc in cases:
        os.environ[key] = val
        try:
            SpiderSettings()
            print(f"  {key:<26}{val:<12}{desc:<26}✗ 竟然通过了？")
        except Exception as e:                      # noqa: BLE001
            # 从冗长的校验错误里提取关键一行
            msg = str(e)
            short = "字段校验失败"
            for line in msg.splitlines():
                line = line.strip()
                if line.startswith("Value error") or "Input should be" in line:
                    short = line[:40]
                    break
            print(f"  {key:<26}{val:<12}{desc:<26}✓ 已拦截：{short}")
        finally:
            os.environ.pop(key, None)

    print("\n  关键：这些错误全部发生在【程序启动时】，一个请求都还没发出去。")
    print("  对比硬编码 —— 你得跑到第 800 条才发现并发数是 0，白等 10 分钟。")


def demo_secret_protection() -> None:
    """演示密钥保护。"""
    print("\n" + "=" * 78)
    print("【实验 4】密钥保护：日志里永远不会打印明文")
    print("=" * 78)

    s = SpiderSettings()
    print(f"\n  proxy_url 字段类型：{type(s.proxy_url).__name__}")
    print(f"  直接 print(s.proxy_url)          → {s.proxy_url}")
    print(f"  打印整个 settings 对象           → {str(s.proxy_url)!r}")
    print(f"  想真的用它的值？要显式取          → {s.proxy_url.get_secret_value()[:18]}…（截断显示）")

    print("\n  SecretStr 的保护效果：")
    print("    · logger.info('配置: %s', settings)  → 输出里是 ********")
    print("    · 异常堆栈里也不会泄露")
    print("    · 想用真值必须写 .get_secret_value()，一个显眼的动作")
    print("    · 而普通 str 字段会在任何 repr/print/log 里裸奔")

    # 演示：写入配置文件时也要小心
    dump_path = WORK / "settings_dump.txt"
    lines = [f"{k:<20}{v}" for k, v in s.describe()]
    dump_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n  安全摘要已写入：{dump_path.name}")
    for line in lines:
        print(f"    {line}")
    print("\n  → 这份摘要可以随便发给同事/贴到工单里，不会泄露密钥。")


def demo_prod_validation() -> None:
    """演示生产环境的额外校验。"""
    print("\n" + "=" * 78)
    print("【实验 5】跨字段校验：生产环境必须配齐代理和密钥")
    print("=" * 78)

    for k in list(os.environ):
        if k.startswith("SPIDER_"):
            os.environ.pop(k)
    os.environ["SPIDER_ENV"] = "prod"
    os.environ["SPIDER_PROXY_URL"] = ""
    os.environ["SPIDER_API_KEY"] = ""

    print("\n  场景：SPIDER_ENV=prod 但没配代理和 API Key")
    try:
        SpiderSettings()
        print("  ✗ 没拦住")
    except Exception as e:                          # noqa: BLE001
        first = str(e).splitlines()[1].strip() if len(str(e).splitlines()) > 1 else str(e)
        print(f"  ✓ 启动失败：{first}")

    os.environ["SPIDER_PROXY_URL"] = "http://user:pass@10.0.0.1:8080"
    os.environ["SPIDER_API_KEY"] = "sk-live-real-key-here"
    s = SpiderSettings()
    print(f"\n  配齐后：环境={s.env.value}，代理={'已设置' if s.proxy_url else '无'}，"
          f"API Key={'已设置' if s.api_key else '无'}")
    print("  ✓ 启动成功")

    for k in list(os.environ):
        if k.startswith("SPIDER_"):
            os.environ.pop(k)

    print("\n  这种校验的价值：把『配置错误』从『线上故障』降级为『启动失败』。")
    print("  启动失败你 10 秒就发现了；线上故障可能烧掉一整晚。")


def demo_gitignore() -> None:
    """打印 .gitignore 与 .env.example 规范。"""
    print("\n" + "=" * 78)
    print("【实验 6】项目文件规范")
    print("=" * 78)

    gitignore = """
  # ===== .gitignore =====
  .env
  .env.*
  !.env.example          # ← 例外：模板文件要提交
  *.db
  *.log
  output/
  __pycache__/
  .venv/
  .pytest_cache/
  .ruff_cache/
"""
    env_example = """
  # ===== .env.example（提交到 Git，只有键名 + 占位值）=====
  SPIDER_ENV=dev
  SPIDER_CONCURRENCY=8
  SPIDER_RATE_LIMIT=5.0
  SPIDER_TIMEOUT_CONNECT=5.0
  SPIDER_TIMEOUT_READ=15.0
  SPIDER_MAX_RETRIES=3
  SPIDER_BASE_URL=https://example.com/
  # 下面两个填真实值，不要提交
  SPIDER_PROXY_URL=
  SPIDER_API_KEY=
"""
    print(gitignore)
    print(env_example)

    # 安全检查：模拟扫描代码里的密钥
    print("  自检：提交前跑一次密钥扫描")
    suspicious: list[tuple[str, int, str]] = []
    for f in Path(__file__).parent.glob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            low = line.lower()
            if ("sk-live-" in low or "password@" in low) and not line.strip().startswith("#"):
                suspicious.append((f.name, i, line.strip()[:60]))

    if suspicious:
        print(f"    发现 {len(suspicious)} 处可疑硬编码：")
        for name, ln, snippet in suspicious:
            print(f"      {name}:{ln}  {snippet}")
        print("    （这就是为什么本文件里的示例密钥全部写成明显的假值）")
    else:
        print("    未发现硬编码密钥 ✓")


def main() -> None:
    """运行全部配置管理实验。"""
    demo_hardcode_problems()
    demo_env_file()
    demo_type_validation()
    demo_secret_protection()
    demo_prod_validation()
    demo_gitignore()

    print("\n" + "=" * 78)
    print("配置管理检查清单")
    print("=" * 78)
    print("  □ 所有可变参数是否都在配置文件里（代码里零硬编码）")
    print("  □ 密钥是否用 SecretStr / 环境变量（不在代码、不在日志）")
    print("  □ .env 是否在 .gitignore 里")
    print("  □ 是否提供了 .env.example 供新人上手")
    print("  □ 配置是否有类型和范围校验（启动即失败）")
    print("  □ 生产环境是否有额外的强制校验（如必须配代理）")
    print("  □ 是否有可安全外发的配置摘要（脱敏后）")
    print("\n  一句话：配置是『部署时才能确定的东西』，它就绝不该出现在代码里。")


if __name__ == "__main__":
    main()
