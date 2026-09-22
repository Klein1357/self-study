"""
spiderkit 状态管理模块（断点续爬）
==================================
对应网页章节：#s3-6

用 SQLite 表记录每个任务的状态，实现崩溃后可恢复、重复运行幂等。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("spiderkit.state")


class Status(str, Enum):
    """任务状态机。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass
class StateStats:
    """状态统计快照。"""

    pending: int = 0
    running: int = 0
    done: int = 0
    failed: int = 0

    @property
    def total(self) -> int:
        """总任务数。"""
        return self.pending + self.running + self.done + self.failed

    @property
    def progress(self) -> float:
        """完成百分比。"""
        return self.done / self.total * 100 if self.total else 0.0

    def __str__(self) -> str:
        """一行摘要。"""
        return (f"总计 {self.total} | 完成 {self.done} ({self.progress:.1f}%) | "
                f"待处理 {self.pending} | 进行中 {self.running} | 失败 {self.failed}")


class StateStore:
    """
    SQLite 任务状态存储。

    设计要点：
      · WAL 日志模式 —— 崩溃后数据不丢，读写并发更好
      · INSERT OR IGNORE —— 重复启动不覆盖已有进度
      · 主键 url —— 天然幂等，同一 URL 不会重复入库
      · reset_stale() —— 处理进程被 kill 时悬空的 running 任务

    Attributes:
        path: 数据库文件路径。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        url         TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT 'pending',
        attempts    INTEGER NOT NULL DEFAULT 0,
        payload     TEXT,
        error       TEXT,
        updated_at  REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
    CREATE INDEX IF NOT EXISTS idx_attempts ON tasks(attempts);
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    # ---------------- 写入 ----------------
    def seed(self, urls: list[str]) -> int:
        """
        初始化任务列表。

        Args:
            urls: 全部待采集 URL。

        Returns:
            新增条目数（已存在的不会重复计入）。
        """
        before = self.stats().total
        self.conn.executemany(
            "INSERT OR IGNORE INTO tasks(url, status, updated_at) VALUES(?, 'pending', ?)",
            [(u, time.time()) for u in urls],
        )
        self.conn.commit()
        added = self.stats().total - before
        log.debug("初始化任务：新增 %d 条（总 %d 条）", added, self.stats().total)
        return added

    def claim_batch(self, limit: int, include_failed: bool = True) -> list[str]:
        """
        原子地领取一批任务并标记为 running。

        用 BEGIN IMMEDIATE 事务保证多 worker 场景下不会重复领取。

        Args:
            limit: 批量大小。
            include_failed: 是否把失败任务也重新拾起。

        Returns:
            领取到的 URL 列表。
        """
        statuses = ["pending"] + (["failed"] if include_failed else [])
        ph = ",".join("?" * len(statuses))
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            rows = self.conn.execute(
                f"SELECT url FROM tasks WHERE status IN ({ph}) "
                f"ORDER BY attempts ASC, rowid ASC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
            urls = [r["url"] for r in rows]
            if urls:
                now = time.time()
                self.conn.executemany(
                    "UPDATE tasks SET status='running', attempts=attempts+1, "
                    "updated_at=? WHERE url=?",
                    [(now, u) for u in urls],
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return urls

    def mark_done(self, url: str, payload: dict[str, Any] | None = None) -> None:
        """
        标记完成并保存数据。

        Args:
            url: 已完成的 URL。
            payload: 采集到的数据结构。
        """
        self.conn.execute(
            "UPDATE tasks SET status='done', payload=?, error=NULL, updated_at=? "
            "WHERE url=?",
            (json.dumps(payload, ensure_ascii=False) if payload else None,
             time.time(), url),
        )
        self.conn.commit()

    def mark_failed(self, url: str, error: str = "") -> None:
        """
        标记失败。

        Args:
            url: 失败的 URL。
            error: 错误摘要。
        """
        self.conn.execute(
            "UPDATE tasks SET status='failed', error=?, updated_at=? WHERE url=?",
            (error[:500], time.time(), url),
        )
        self.conn.commit()

    def release(self, urls: list[str]) -> int:
        """
        把任务退回 pending（用于优雅退出时释放手上的任务）。

        Args:
            urls: 要释放的 URL 列表。

        Returns:
            实际释放的数量。
        """
        if not urls:
            return 0
        now = time.time()
        cur = self.conn.executemany(
            "UPDATE tasks SET status='pending', updated_at=? "
            "WHERE url=? AND status='running'",
            [(now, u) for u in urls],
        )
        self.conn.commit()
        return cur.rowcount

    def reset_stale(self) -> int:
        """
        把悬空的 running 任务打回 pending。

        必须在启动时调用 —— 否则上次被 kill 时正在处理的任务会永远卡住。

        Returns:
            重置数量。
        """
        cur = self.conn.execute(
            "UPDATE tasks SET status='pending', updated_at=? WHERE status='running'",
            (time.time(),),
        )
        self.conn.commit()
        n = cur.rowcount
        if n:
            log.warning("发现 %d 条悬空任务（上次未正常退出），已重置为待处理", n)
        return n

    # ---------------- 查询 ----------------
    def stats(self) -> StateStats:
        """
        统计各状态数量。

        Returns:
            StateStats 快照。
        """
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        s = StateStats()
        for r in rows:
            if hasattr(s, r["status"]):
                setattr(s, r["status"], r["n"])
        return s

    def results(self) -> list[dict[str, Any]]:
        """
        取出全部已完成数据。

        Returns:
            数据列表。
        """
        rows = self.conn.execute(
            "SELECT url, payload FROM tasks WHERE status='done' ORDER BY url"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            rec: dict[str, Any] = {"url": r["url"]}
            if r["payload"]:
                try:
                    rec.update(json.loads(r["payload"]))
                except json.JSONDecodeError:
                    log.debug("payload 解析失败：%s", r["url"])
            out.append(rec)
        return out

    def failures(self) -> list[tuple[str, str, int]]:
        """
        取出失败任务清单。

        Returns:
            [(url, error, attempts), ...]
        """
        rows = self.conn.execute(
            "SELECT url, error, attempts FROM tasks WHERE status='failed' "
            "ORDER BY attempts DESC"
        ).fetchall()
        return [(r["url"], r["error"] or "", r["attempts"]) for r in rows]

    def has(self, url: str) -> bool:
        """
        检查 URL 是否已完成。

        Args:
            url: 待检查的 URL。

        Returns:
            True 表示已完成。
        """
        row = self.conn.execute(
            "SELECT 1 FROM tasks WHERE url=? AND status='done'", (url,)
        ).fetchone()
        return row is not None

    def close(self) -> None:
        """关闭数据库连接。"""
        self.conn.close()

    def __enter__(self) -> StateStore:
        """支持 with 语法。"""
        return self

    def __exit__(self, *exc: object) -> None:
        """退出时关闭。"""
        self.close()
