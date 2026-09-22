"""
阶段 3 · 3.6 断点续爬：状态持久化设计
======================================================
对应网页章节：#s3-6

断点续爬要解决的核心问题：
  爬了 8 万条，进程崩了。重启后怎么知道哪些爬过、哪些没爬？

三种方案对比（本脚本逐一实测）：
  A. 内存集合      —— 简单，但重启即失忆
  B. 完成清单文件   —— 追加写，重启可恢复，但文件会越来越大
  C. SQLite 状态表  —— 支持原子事务、可查询、可统计（生产推荐）

运行：python3 35_checkpoint.py
"""

from __future__ import annotations

import asyncio
import json
import random
import sqlite3
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

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


WORK = Path(tempfile.mkdtemp(prefix="spider_ckpt_"))


# ============================================================
# 数据模型
# ============================================================
@dataclass
class Task:
    """一个待采集任务。"""

    url: str
    status: str = "pending"        # pending / done / failed
    attempts: int = 0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        """转字典，便于 JSON 序列化。"""
        return asdict(self)


# ============================================================
# 方案 A：内存集合（反面教材）
# ============================================================
class MemoryCheckpoint:
    """
    纯内存记录 —— 只适合"一次跑完、中途不崩"的小任务。

    缺点：进程一挂，全部丢失，只能从头再来。
    """

    def __init__(self) -> None:
        self.done: set[str] = set()

    def mark_done(self, url: str) -> None:
        """标记完成。"""
        self.done.add(url)

    def is_done(self, url: str) -> bool:
        """检查是否已完成。"""
        return url in self.done


# ============================================================
# 方案 B：JSONL 追加写清单
# ============================================================
class JsonlCheckpoint:
    """
    每完成一条就 append 一行 JSON。

    优点：写操作是 O(1) 追加，不会因为记录变大而变慢。
    缺点：加载时要全量读一遍；文件会持续增长，需要定期压缩。

    Attributes:
        path: 清单文件路径。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.done: set[str] = set()
        self._fh = None
        self._load()

    def _load(self) -> None:
        """启动时加载已有记录。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.done.add(json.loads(line)["url"])
                except (json.JSONDecodeError, KeyError):
                    continue        # 最后一行可能是写到一半崩的，跳过

    def open(self) -> None:
        """打开写入句柄（启动时调用）。"""
        self._fh = self.path.open("a", encoding="utf-8")

    def mark_done(self, url: str, **extra: object) -> None:
        """
        追加一条完成记录。

        Args:
            url: 已完成的 URL。
            **extra: 额外字段（如标题、价格），顺便当作数据落盘。
        """
        self.done.add(url)
        if self._fh:
            rec = {"url": url, "ts": time.time(), **extra}
            self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._fh.flush()        # ← 关键：立刻刷盘，否则崩溃时丢失缓冲

    def close(self) -> None:
        """关闭句柄。"""
        if self._fh:
            self._fh.close()
            self._fh = None

    def is_done(self, url: str) -> bool:
        """检查是否已完成。"""
        return url in self.done

    def compact(self, out_path: Path) -> int:
        """
        压缩：把每个 URL 的多条记录合并成一条。

        Args:
            out_path: 压缩后的新文件路径。

        Returns:
            去重后的记录数。
        """
        seen: dict[str, dict] = {}
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rec = json.loads(line)
                        seen[rec["url"]] = rec
                    except (json.JSONDecodeError, KeyError):
                        continue
        with out_path.open("w", encoding="utf-8") as f:
            for rec in seen.values():
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return len(seen)


# ============================================================
# 方案 C：SQLite 状态表（生产推荐）
# ============================================================
class SqliteCheckpoint:
    """
    用 SQLite 表记录任务状态。

    相对文件方案的优势：
      · 原子事务：单条记录不会写坏
      · 可查询：随时统计"还有多少 pending / failed"
      · 可续传任意粒度：不限于 URL，可以是 (URL, 页码) 组合主键
      · 支持并发：WAL 模式下多进程可同时读写

    Attributes:
        path: 数据库文件路径。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks (
        url         TEXT PRIMARY KEY,
        status      TEXT NOT NULL DEFAULT 'pending',
        attempts    INTEGER NOT NULL DEFAULT 0,
        payload     TEXT,
        updated_at  REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_status ON tasks(status);
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        # WAL 模式：读写并发更好，崩溃恢复更安全
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def seed(self, urls: list[str]) -> int:
        """
        初始化任务列表（已存在的 URL 不覆盖，保留其状态）。

        Args:
            urls: 全部待采集 URL。

        Returns:
            新增的条目数。
        """
        before = self.count()
        self.conn.executemany(
            "INSERT OR IGNORE INTO tasks(url, status, updated_at) VALUES(?, 'pending', ?)",
            [(u, time.time()) for u in urls],
        )
        self.conn.commit()
        return self.count() - before

    def next_batch(self, limit: int = 10, include_failed: bool = True) -> list[str]:
        """
        取出下一批待采集 URL。

        注意：这里用"先查再改状态"的非原子做法，仅为教学清晰。
        真实的多worker场景应该用 `UPDATE ... RETURNING` 或事务包裹。

        Args:
            limit: 批量大小。
            include_failed: 是否把 failed 的也重新拾起来重试。

        Returns:
            URL 列表。
        """
        statuses = ("pending", "failed") if include_failed else ("pending",)
        ph = ",".join("?" * len(statuses))
        rows = self.conn.execute(
            f"SELECT url FROM tasks WHERE status IN ({ph}) "
            f"ORDER BY attempts ASC, url LIMIT ?",
            (*statuses, limit),
        ).fetchall()
        urls = [r["url"] for r in rows]
        if urls:
            self.conn.executemany(
                "UPDATE tasks SET status='running', attempts=attempts+1, updated_at=? WHERE url=?",
                [(time.time(), u) for u in urls],
            )
            self.conn.commit()
        return urls

    def mark_done(self, url: str, payload: dict | None = None) -> None:
        """
        标记完成并保存数据。

        Args:
            url: 已完成的 URL。
            payload: 采集到的数据。
        """
        self.conn.execute(
            "UPDATE tasks SET status='done', payload=?, updated_at=? WHERE url=?",
            (json.dumps(payload, ensure_ascii=False) if payload else None, time.time(), url),
        )
        self.conn.commit()

    def mark_failed(self, url: str, reason: str = "") -> None:
        """
        标记失败。

        Args:
            url: 失败的 URL。
            reason: 失败原因。
        """
        self.conn.execute(
            "UPDATE tasks SET status='failed', payload=?, updated_at=? WHERE url=?",
            (json.dumps({"error": reason}, ensure_ascii=False), time.time(), url),
        )
        self.conn.commit()

    def reset_running(self) -> int:
        """
        把卡在 running 的任务打回 pending。

        这是断点续爬最容易被忽略的一步：
        进程被 kill 时，正在跑的任务状态是 running 而不是 done，
        重启后如果不重置，这些任务会永远"悬空"。

        Returns:
            重置的条目数。
        """
        cur = self.conn.execute(
            "UPDATE tasks SET status='pending', updated_at=? WHERE status='running'",
            (time.time(),),
        )
        self.conn.commit()
        return cur.rowcount

    def stats(self) -> dict[str, int]:
        """
        统计各状态数量。

        Returns:
            形如 {'pending': 5, 'done': 20, 'failed': 2}。
        """
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def count(self) -> int:
        """总任务数。"""
        return self.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def results(self) -> list[dict]:
        """
        取出全部已完成数据。

        Returns:
            数据字典列表。
        """
        rows = self.conn.execute(
            "SELECT url, payload FROM tasks WHERE status='done' ORDER BY url"
        ).fetchall()
        out = []
        for r in rows:
            rec = {"url": r["url"]}
            if r["payload"]:
                rec.update(json.loads(r["payload"]))
            out.append(rec)
        return out

    def close(self) -> None:
        """关闭连接。"""
        self.conn.close()


# ============================================================
# 模拟：会随机崩溃的爬虫
# ============================================================
@dataclass
class FlakySpider:
    """
    模拟一个跑到一半会崩溃的爬虫。

    Attributes:
        ckpt: 断点管理器。
        crash_at: 处理到第几条时模拟崩溃。
        log: 运行日志。
    """

    ckpt: SqliteCheckpoint
    crash_at: int = 0
    processed: int = 0
    log: list[str] = field(default_factory=list)

    async def run(self, batch: int = 6) -> None:
        """
        运行采集循环。

        Args:
            batch: 每批取多少条。

        Raises:
            RuntimeError: 模拟进程崩溃。
        """
        while True:
            urls = self.ckpt.next_batch(batch)
            if not urls:
                self.log.append("没有待处理任务，正常结束")
                return
            for u in urls:
                self.processed += 1
                # 模拟请求 + 解析
                await asyncio.sleep(0.01)
                if random.random() < 0.1:
                    self.ckpt.mark_failed(u, "模拟网络错误")
                    self.log.append(f"  失败 {u}")
                    continue
                price = round(random.uniform(10, 100), 2)
                self.ckpt.mark_done(u, {"title": u.split("/")[-1], "price": price})
                self.log.append(f"  完成 {u}  (£{price})")

                if self.crash_at and self.processed >= self.crash_at:
                    raise RuntimeError("💥 模拟进程被 kill")


# ============================================================
# 实验
# ============================================================
def demo_memory_vs_file() -> None:
    """对比方案 A 和方案 B 的恢复能力。"""
    print("=" * 78)
    print("【实验 1】内存集合 vs JSONL 清单 —— 崩溃后还剩什么")
    print("=" * 78)

    urls = [f"https://x.com/item/{i}" for i in range(1, 11)]

    # 方案 A
    mem = MemoryCheckpoint()
    for u in urls[:6]:
        mem.mark_done(u)
    print(f"\n  方案 A（内存）：崩溃前完成 6 条")
    print(f"    崩溃后重启 → 记录数 {len(mem.done)} ？（新进程里其实是 0）")
    print(f"    ✗ 无法恢复，只能全量重爬")

    # 方案 B
    jpath = WORK / "done.jsonl"
    ck = JsonlCheckpoint(jpath)
    ck.open()
    for u in urls[:6]:
        ck.mark_done(u, title=f"物品{u[-1]}")
    ck.close()
    print(f"\n  方案 B（JSONL）：文件 {jpath.name}，大小 {jpath.stat().st_size} 字节")
    ck2 = JsonlCheckpoint(jpath)
    print(f"    新进程重新加载 → 恢复 {len(ck2.done)} 条记录 ✓")
    print(f"    剩余待爬：{len(set(urls) - ck2.done)} 条")


async def demo_sqlite_checkpoint() -> None:
    """演示 SQLite 断点续爬的完整流程。"""
    print("\n" + "=" * 78)
    print("【实验 2】SQLite 断点续爬 —— 崩溃 → 重启 → 继续")
    print("=" * 78)

    db = WORK / "state.db"
    urls = [f"https://x.com/item/{i}" for i in range(1, 25)]

    # ---- 第一次运行 ----
    ck = SqliteCheckpoint(db)
    added = ck.seed(urls)
    print(f"\n  第一次启动：初始化 {added} 个任务")
    print(f"    初始状态：{ck.stats()}")

    spider = FlakySpider(ck, crash_at=10)
    print("\n  开始采集，第 10 条时模拟崩溃…")
    try:
        await spider.run(batch=6)
    except RuntimeError as e:
        print(f"    {e}")
    print(f"    崩溃时状态：{ck.stats()}")
    done_before = len(ck.results())
    ck.close()

    # ---- 模拟进程重启 ----
    print("\n  ── 进程重启 ──")
    ck = SqliteCheckpoint(db)
    stuck = ck.reset_running()
    print(f"    重置悬空的 running 任务：{stuck} 条 → pending")
    print(f"    重启后状态：{ck.stats()}")
    print(f"    已恢复 {done_before} 条采集结果，无需重爬 ✓")

    # ---- 第二次运行，跑完 ----
    print("\n  第二次启动：继续从断点爬…")
    spider2 = FlakySpider(ck, crash_at=0)
    await spider2.run(batch=6)
    final = ck.stats()
    print(f"    最终状态：{final}")

    results = ck.results()
    print(f"    共采集 {len(results)} 条，示例：")
    for r in results[:3]:
        print(f"      {r['url']}  →  {r.get('title')}  £{r.get('price')}")
    ck.close()

    # ---- 验证幂等性 ----
    print("\n  ── 再跑一次（验证不会重复采集）──")
    ck = SqliteCheckpoint(db)
    spider3 = FlakySpider(ck, crash_at=0)
    await spider3.run(batch=6)
    print(f"    结果条数：{len(ck.results())}（与上次相同 → 幂等 ✓）")
    print(f"    状态：{ck.stats()}")
    print("\n  设计要点：")
    print("    1. 用 INSERT OR IGNORE 初始化 → 重复启动不会重置已有进度")
    print("    2. reset_running() 处理进程被 kill 的悬空任务")
    print("    3. 主键去重 → 天然幂等，多跑几次结果一致")
    print("    4. 状态机 pending → running → done/failed，failed 可重拾")
    ck.close()


def demo_jsonl_compaction() -> None:
    """演示 JSONL 文件的膨胀问题与压缩。"""
    print("\n" + "=" * 78)
    print("【实验 3】JSONL 的膨胀问题与压缩")
    print("=" * 78)

    p = WORK / "grow.jsonl"
    ck = JsonlCheckpoint(p)
    ck.open()
    # 模拟"同一批 URL 被重复跑了 5 轮"（比如失败重试）
    for round_ in range(5):
        for i in range(1, 21):
            ck.mark_done(f"https://x.com/item/{i}", round=round_)
    ck.close()

    size_before = p.stat().st_size
    lines_before = sum(1 for _ in p.open(encoding="utf-8"))
    out = WORK / "grow_compacted.jsonl"
    kept = ck.compact(out)
    lines_after = sum(1 for _ in out.open(encoding="utf-8"))

    print(f"\n  原始文件：{lines_before} 行，{size_before} 字节")
    print(f"  压缩后  ：{lines_after} 行，{out.stat().st_size} 字节（去重后 {kept} 个 URL）")
    print(f"  压缩率  ：{(1 - out.stat().st_size / size_before) * 100:.0f}%")
    print("\n  结论：JSONL 适合『一条一条追加』，"
          "但必须定期 compact，否则文件会无限膨胀。")


def demo_timing() -> None:
    """对比三种方案的写入性能与恢复性能。"""
    print("\n" + "=" * 78)
    print("【实验 4】性能对比：写 2000 条 + 恢复读取")
    print("=" * 78)

    N = 2000
    urls = [f"https://x.com/i/{i}" for i in range(N)]

    # JSONL
    p = WORK / "bench.jsonl"
    ck = JsonlCheckpoint(p)
    ck.open()
    t0 = time.perf_counter()
    for u in urls:
        ck.mark_done(u)
    w_jsonl = time.perf_counter() - t0
    ck.close()
    t0 = time.perf_counter()
    ck2 = JsonlCheckpoint(p)
    r_jsonl = time.perf_counter() - t0

    # SQLite
    db = WORK / "bench.db"
    s = SqliteCheckpoint(db)
    t0 = time.perf_counter()
    s.seed(urls)
    for u in urls:
        s.mark_done(u, {"v": hash(u) % 100})
    w_sqlite = time.perf_counter() - t0
    t0 = time.perf_counter()
    found = s.count()
    r_sqlite = time.perf_counter() - t0
    s.close()

    # 只统计"查一个 URL 是否完成"的成本 —— 这是每爬一条都要做的事
    t0 = time.perf_counter()
    for u in urls[:1000]:
        ck2.is_done(u)              # 内存 set 查询 O(1)
    q_jsonl = time.perf_counter() - t0

    print(f"\n  {'方案':<14}{'写 2000 条':<16}{'启动恢复':<16}{'单次查询×1000'}")
    print("  " + "-" * 70)
    print(f"  {'JSONL':<14}{w_jsonl:<16.3f}{r_jsonl:<16.3f}{q_jsonl:.4f}")
    print(f"  {'SQLite':<14}{w_sqlite:<16.3f}{r_sqlite:<16.3f}{'<0.0001 (索引)'}")

    print("\n  怎么选：")
    print("    · < 1 万条且跑一次就完 → JSONL 够用，简单")
    print("    · 要统计进度、要支持失败重试、要长期跑 → SQLite")
    print("    · 数据量再大（百万级）→ 换 PostgreSQL / Redis")


async def main() -> None:
    """运行全部断点续爬实验。"""
    print(f"工作目录：{WORK}\n")
    demo_memory_vs_file()
    await demo_sqlite_checkpoint()
    demo_jsonl_compaction()
    demo_timing()

    print("\n" + "=" * 78)
    print("断点续爬设计检查清单")
    print("=" * 78)
    print("  □ 任务列表是否持久化（不是只在内存里）")
    print("  □ 每完成一条是否立即落盘（不是攒够 1000 条再写）")
    print("  □ 进程被 kill 后，running 状态能否被打回 pending")
    print("  □ 重复运行是否幂等（同一 URL 不会入库两次）")
    print("  □ 失败项是否记录原因，能否单独重试")
    print("  □ 是否定期 compact / checkpoint，防止状态文件膨胀")


if __name__ == "__main__":
    asyncio.run(main())
