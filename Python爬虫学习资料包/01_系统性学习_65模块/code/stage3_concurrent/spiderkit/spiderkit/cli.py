"""
spiderkit CLI 入口
==================
对应网页章节：#s3-9

用 typer 把爬虫封装成命令行工具，支持子命令与参数覆盖。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer

from .config import Settings, setup_logging
from .spider import BooksSpider
from .state import StateStore

app = typer.Typer(
    name="spiderkit",
    help="工程化异步爬虫工具包（阶段 3 毕业项目）",
    add_completion=False,
    no_args_is_help=True,
)


def _build_settings(
    concurrency: int | None = None,
    rate: float | None = None,
    pages: int | None = None,
    env: str | None = None,
) -> Settings:
    """
    从 CLI 参数构造配置（CLI 参数优先级最高）。

    Args:
        concurrency: 并发数。
        rate: 限速。
        pages: 最大页数。
        env: 环境名。

    Returns:
        Settings 对象。
    """
    overrides: dict[str, object] = {}
    if concurrency is not None:
        overrides["concurrency"] = concurrency
    if rate is not None:
        overrides["rate_limit"] = rate
    if pages is not None:
        overrides["max_pages"] = pages
    if env is not None:
        overrides["env"] = env
    return Settings(**overrides)


@app.command()
def crawl(
    concurrency: Annotated[int | None, typer.Option("--concurrency", "-c",
                                                       help="最大并发数")] = None,
    rate: Annotated[float | None, typer.Option("--rate", "-r",
                                                  help="每秒请求上限")] = None,
    pages: Annotated[int | None, typer.Option("--pages", "-p",
                                                 help="抓取页数")] = None,
    resume: Annotated[bool, typer.Option("--resume", help="跳过列表页，直接从状态库续爬")] = False,
    env: Annotated[str | None, typer.Option("--env", "-e",
                                               help="运行环境 dev/prod")] = None,
) -> None:
    """
    执行采集任务。

    示例：
        spiderkit crawl -c 12 -r 5 -p 5
        spiderkit crawl --resume
    """
    settings = _build_settings(concurrency, rate, pages, env)
    logger = setup_logging(settings, settings.output_dir)

    logger.info("=" * 60)
    logger.info("spiderkit 启动")
    for k, v in settings.safe_dump().items():
        logger.info("  %-14s %s", k, v)
    logger.info("=" * 60)

    spider = BooksSpider(settings)
    try:
        result = asyncio.run(spider.run(skip_collect=resume))
    except KeyboardInterrupt:
        logger.warning("用户强制中断")
        raise typer.Exit(code=130) from None
    finally:
        spider.close()

    typer.echo("\n" + "=" * 60)
    typer.echo("采集结果")
    typer.echo("=" * 60)
    typer.echo(f"  {result['stats']}")
    typer.echo(f"  本轮写入：{result['written']} 条")
    typer.echo(f"  抓取统计：{result['fetch']}")
    typer.echo(f"  失败数量：{result['failures']}")
    typer.echo(f"\n  统计：{json.dumps(result['summary'], ensure_ascii=False)}")
    typer.echo(f"\n  输出目录：{settings.output_dir}")


@app.command()
def status(
    db: Annotated[Path, typer.Option("--db", help="状态数据库路径")] = Path("state.db"),
) -> None:
    """
    查看采集进度。

    示例：
        spiderkit status
    """
    if not db.exists():
        typer.echo(f"状态库不存在：{db}")
        raise typer.Exit(code=1)

    with StateStore(db) as st:
        stats = st.stats()
        typer.echo(f"状态库：{db}")
        typer.echo(f"  {stats}")

        failures = st.failures()
        if failures:
            typer.echo(f"\n失败清单（前 10 条，共 {len(failures)} 条）：")
            for url, err, att in failures[:10]:
                typer.echo(f"  [{att}次] {url[:60]}  {err[:40]}")

        results = st.results()
        if results:
            typer.echo(f"\n已采集 {len(results)} 条，示例：")
            for r in results[:5]:
                typer.echo(f"  {str(r.get('title'))[:44]:<46} "
                           f"£{r.get('price')}  ★{r.get('rating')}")


@app.command()
def reset(
    db: Annotated[Path, typer.Option("--db", help="状态数据库路径")] = Path("state.db"),
    only_failed: Annotated[bool, typer.Option("--only-failed",
                                              help="只重置失败任务")] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="跳过确认")] = False,
) -> None:
    """
    重置任务状态（默认只重置失败项）。

    示例：
        spiderkit reset               # 失败项打回待处理
        spiderkit reset --only-failed=false -y   # 全部重来
    """
    if not db.exists():
        typer.echo(f"状态库不存在：{db}")
        raise typer.Exit(code=1)

    if not yes:
        scope = "失败任务" if only_failed else "全部任务"
        typer.confirm(f"确认重置 {scope}？", abort=True)

    with StateStore(db) as st:
        before = st.stats()
        if only_failed:
            cur = st.conn.execute(
                "UPDATE tasks SET status='pending', attempts=0, error=NULL "
                "WHERE status='failed'"
            )
        else:
            cur = st.conn.execute(
                "UPDATE tasks SET status='pending', attempts=0, error=NULL, payload=NULL"
            )
        st.conn.commit()
        typer.echo(f"已重置 {cur.rowcount} 条")
        typer.echo(f"  重置前：{before}")
        typer.echo(f"  重置后：{st.stats()}")


@app.command()
def info() -> None:
    """
    显示当前配置（密钥自动脱敏）。

    示例：
        spiderkit info
    """
    settings = Settings()
    typer.echo("当前配置（环境变量 > .env > 默认值）")
    typer.echo("=" * 46)
    for k, v in settings.safe_dump().items():
        typer.echo(f"  {k:<14} {v}")
    typer.echo("\n环境变量前缀：SPIDERKIT_")
    typer.echo("例如：SPIDERKIT_CONCURRENCY=16 SPIDERKIT_RATE_LIMIT=3 spiderkit crawl")


if __name__ == "__main__":
    app()
