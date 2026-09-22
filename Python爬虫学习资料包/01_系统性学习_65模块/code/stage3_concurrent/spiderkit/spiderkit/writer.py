"""
spiderkit CSV/JSON 输出模块
===========================
对应网页章节：#s2-7

边爬边写：每完成一条立即落盘，避免内存爆炸和崩溃丢数据。
"""

from __future__ import annotations

import csv
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("spiderkit.writer")


@dataclass
class BatchWriter:
    """
    流式写入器：边爬边写，逐条落盘。

    为什么不用"先攒到内存再统一写"：
      爬 10 万条全放内存，要几百 MB 且崩溃全丢。
      逐条写虽然 IO 多，但任何时候中断都不丢已完成的数据。

    Attributes:
        path: 输出文件路径。
        fmt: 输出格式（csv / jsonl）。
    """

    path: Path
    fmt: str = "csv"
    _fh: Any = None
    _writer: Any = None
    _headers: list[str] | None = None
    count: int = 0

    def open(self, fieldnames: list[str] | None = None) -> BatchWriter:
        """
        打开文件准备写入。

        Args:
            fieldnames: CSV 表头（csv 格式需要）。

        Returns:
            自身，支持链式调用。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.fmt == "csv":
            # newline="" 是 csv 模块的硬性要求，否则 Windows 下会多空行
            # encoding="utf-8-sig" 让 Excel 正确识别中文
            self._fh = self.path.open("w", encoding="utf-8-sig", newline="")
            self._headers = fieldnames or []
            self._writer = csv.DictWriter(self._fh, fieldnames=self._headers,
                                          extrasaction="ignore")
            self._writer.writeheader()
        elif self.fmt == "jsonl":
            self._fh = self.path.open("w", encoding="utf-8")
        else:
            raise ValueError(f"不支持的格式：{self.fmt}")

        log.debug("输出文件已打开：%s（%s）", self.path.name, self.fmt)
        return self

    def write(self, record: dict[str, Any]) -> None:
        """
        写入一条记录。

        Args:
            record: 数据字典。
        """
        if self._fh is None:
            raise RuntimeError("写入前必须先调用 open()")

        if self.fmt == "csv":
            self._writer.writerow(record)
        else:
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        self.count += 1
        # 关键：立刻刷盘。不 flush 的话，进程被 kill 时缓冲区数据全丢
        if self.count % 20 == 0:
            self._fh.flush()

    def close(self) -> None:
        """关闭并刷盘。"""
        if self._fh:
            self._fh.flush()
            self._fh.close()
            self._fh = None
            log.info("输出已关闭：%s（共 %d 条）", self.path.name, self.count)

    def __enter__(self) -> BatchWriter:
        """支持 with 语法（需先设置 fieldnames 属性）。"""
        return self

    def __exit__(self, *exc: object) -> None:
        """退出时关闭。"""
        self.close()


def write_json(path: Path, records: Iterable[dict[str, Any]],
               extra: dict[str, Any] | None = None) -> int:
    """
    一次性写出 JSON（含可选统计信息）。

    适合最终汇总用；过程中请用 BatchWriter 流式写。

    Args:
        path: 输出路径。
        records: 记录列表。
        extra: 附加字段（如统计信息）。

    Returns:
        写入条数。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = list(records)
    payload: dict[str, Any] = {}
    if extra:
        payload.update(extra)
    payload["数据"] = data
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    log.info("JSON 已写出：%s（%d 条）", path.name, len(data))
    return len(data)


def write_failures(path: Path, failures: list[tuple[str, str, int]]) -> None:
    """
    写出失败清单，供下轮重爬。

    Args:
        path: 输出路径。
        failures: [(url, error, attempts), ...]
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "error", "attempts"])
        w.writerows(failures)
    log.info("失败清单已写出：%s（%d 条）", path.name, len(failures))
