"""spiderkit —— 一个工程化的异步爬虫工具包（阶段 3 毕业项目）。"""

__version__ = "1.0.0"

from .config import Env, Settings, setup_logging
from .fetcher import CircuitBreaker, Fetcher, TokenBucket, backoff_delay, is_retryable
from .models import (
    Book,
    ListItem,
    extract_book_links,
    fix_encoding,
    parse_detail_page,
    parse_list_page,
    parse_price,
    parse_stock,
)
from .spider import BooksSpider
from .state import StateStats, StateStore, Status
from .writer import BatchWriter, write_failures, write_json

__all__ = [
    "BatchWriter",
    "Book",
    "BooksSpider",
    "CircuitBreaker",
    "Env",
    "Fetcher",
    "ListItem",
    "Settings",
    "StateStats",
    "StateStore",
    "Status",
    "TokenBucket",
    "backoff_delay",
    "extract_book_links",
    "fix_encoding",
    "is_retryable",
    "parse_detail_page",
    "parse_list_page",
    "parse_price",
    "parse_stock",
    "setup_logging",
    "write_failures",
    "write_json",
]
