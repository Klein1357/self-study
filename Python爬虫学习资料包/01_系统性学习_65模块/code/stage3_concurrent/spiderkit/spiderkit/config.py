"""
spiderkit 配置模块
==================
对应网页章节：#s3-8 #s3-9

分层配置：代码默认值 < .env 文件 < 环境变量。
所有字段都有类型校验，配置错误在启动时即报错。
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(str, Enum):
    """运行环境。"""

    DEV = "dev"
    PROD = "prod"


class Settings(BaseSettings):
    """
    spiderkit 全局配置。

    环境变量前缀统一为 SPIDERKIT_，例如：
        SPIDERKIT_CONCURRENCY=16
        SPIDERKIT_RATE_LIMIT=3
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SPIDERKIT_",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 运行 ----
    env: Env = Env.DEV
    log_level: str = "INFO"

    # ---- 并发与限速 ----
    concurrency: int = Field(default=8, ge=1, le=64)
    rate_limit: float = Field(default=5.0, gt=0, le=10000)
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_base: float = Field(default=0.5, gt=0)

    # ---- 超时 ----
    timeout_connect: float = Field(default=5.0, gt=0)
    timeout_read: float = Field(default=20.0, gt=0)

    # ---- 目标 ----
    base_url: str = "https://books.toscrape.com/"
    start_page: int = Field(default=1, ge=1)
    max_pages: int = Field(default=3, ge=1)
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )

    # ---- 存储 ----
    output_dir: Path = Path("output")
    state_db: Path = Path("state.db")

    # ---- 密钥 ----
    proxy_url: SecretStr | None = None

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, v: str) -> str:
        """
        统一补全结尾斜杠，避免 urljoin 拼出错误路径。

        Args:
            v: 原始 base_url。

        Returns:
            以 / 结尾的 URL。
        """
        return v if v.endswith("/") else v + "/"

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, v: str) -> str:
        """
        校验日志级别合法性。

        Args:
            v: 级别名。

        Raises:
            ValueError: 级别名非法。

        Returns:
            大写级别名。
        """
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法日志级别：{v}")
        return v

    @property
    def timeout(self) -> tuple[float, float]:
        """requests/httpx 风格的 (连接超时, 读取超时)。"""
        return (self.timeout_connect, self.timeout_read)

    @property
    def proxy(self) -> str | None:
        """取出明文代理地址（仅在真的要发请求时调用）。"""
        return self.proxy_url.get_secret_value() if self.proxy_url else None

    def safe_dump(self) -> dict[str, Any]:
        """
        生成可安全打印/外发的配置摘要（密钥脱敏）。

        Returns:
            配置字典。
        """
        return {
            "env": self.env.value,
            "log_level": self.log_level,
            "concurrency": self.concurrency,
            "rate_limit": self.rate_limit,
            "max_retries": self.max_retries,
            "timeout": f"{self.timeout_connect}/{self.timeout_read}",
            "base_url": self.base_url,
            "pages": f"{self.start_page}-{self.max_pages}",
            "output_dir": str(self.output_dir),
            "proxy": "********" if self.proxy_url else None,
        }


def setup_logging(settings: Settings, log_dir: Path | None = None) -> logging.Logger:
    """
    配置并返回项目 logger。

    控制台输出 INFO 以上（彩色），文件输出 DEBUG 以上（含轮转）。

    Args:
        settings: 配置对象。
        log_dir: 日志目录，None 表示不写文件。

    Returns:
        配置好的 logger。
    """
    import logging.handlers as handlers
    import sys

    logger = logging.getLogger("spiderkit")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    class ColorFormatter(logging.Formatter):
        """按级别着色的控制台格式化器。"""

        COLORS = {
            logging.DEBUG: "\033[37m",
            logging.INFO: "\033[36m",
            logging.WARNING: "\033[33m",
            logging.ERROR: "\033[31m",
            logging.CRITICAL: "\033[1;41m",
        }
        RESET = "\033[0m"

        def format(self, record: logging.LogRecord) -> str:
            """着色后输出。"""
            c = self.COLORS.get(record.levelno, "")
            record.levelname_c = f"{c}{record.levelname:<7}{self.RESET}"
            return super().format(record)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, settings.log_level))
    console.setFormatter(ColorFormatter("%(asctime)s %(levelname_c)s %(message)s",
                                        datefmt="%H:%M:%S"))
    logger.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = handlers.RotatingFileHandler(
            log_dir / "spiderkit.log", maxBytes=2 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        ))
        logger.addHandler(fh)

    logger.debug("日志系统就绪，级别=%s", settings.log_level)
    return logger
