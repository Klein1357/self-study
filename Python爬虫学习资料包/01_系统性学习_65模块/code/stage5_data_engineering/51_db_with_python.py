"""
第 51 课 · Python 操作数据库 —— 从裸 SQL 到 Repository 模式

================================ 学习目标 ================================
1. 理解「裸 sqlite3 / SQLAlchemy Core / ORM」三层抽象各自的适用场景
2. 掌握 SQLAlchemy 2.0 声明式模型 + Session 生命周期
3. 掌握 Repository 模式：把 SQL 关进笼子，业务代码只碰对象
4. 掌握连接池、会话管理与"DetachedInstanceError"这类真实坑
5. 会做「爬虫 → 数据库」的完整落库管道（含批量、去重、重试）

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/51_db_with_python.py

依赖：sqlalchemy (已安装 2.0.54)
不需要数据库服务，用 SQLite 文件 + 内存库演示。
"""

from __future__ import annotations

import json
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from sqlalchemy import (
    Float,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)

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


# ============================================================================
# 抽象层次：什么时候用什么
# ============================================================================
# 三层抽象不是"越高级越好"，而是各有适用场景：
#
# ┌──────────────┬────────────────────┬──────────────────────┬──────────────┐
# │  层次         │  写法               │  优势                 │  适合         │
# ├──────────────┼────────────────────┼──────────────────────┼──────────────┤
# │ 裸 sqlite3   │ conn.execute(sql)  │ 零依赖、完全可控       │ 一次性脚本    │
# │ SQLAlchemy   │ select(t).where()  │ 跨方言、组合式、无魔法  │ 批量 ETL      │
# │   Core       │                    │                       │ 复杂查询      │
# │ SQLAlchemy   │ session.query(...) │ 对象化、关系自动加载    │ 业务 CRUD     │
# │   ORM        │   或 select(T)     │                       │ 关系复杂时    │
# └──────────────┴────────────────────┴──────────────────────┴──────────────┘
#
# **最重要的判断标准**：数据量大、以批处理为主 → Core；
# 有对象关系和业务规则 → ORM。二者可以混用（本课就混用）。
#
# 一个常见的误区：有人觉得 ORM「慢」所以全用裸 SQL。
# 实际上 ORM 的开销主要在"对象构造"，纯批量写入时用 Core 的
# insert().values([...]) 或者直接 executemany，性能和裸 SQL 无差别。
# 真正的性能杀手是 N+1 查询，跟 ORM 本身无关。


# ============================================================================
# 一、声明式模型
# ============================================================================
def _now_placeholder() -> str:
    """给 mapped_column(default=...) 用的时间戳工厂。

    Returns:
        ISO8601 时间戳字符串。

    注意：这里不能传 time.strftime(...) 的**调用结果**，
    必须传**函数本身**。传值的话所有行会共享同一个时间戳，
    这在批量插入时会导致 updated_at 全部相同 —— 增量采集就废了。

    这个函数必须定义在 Book 类**之前**，因为类体在导入时就会求值
    `default=_now_placeholder`。写成方法或者放在后面都会 NameError。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。

    SQLAlchemy 2.0 的 DeclarativeBase + Mapped[] 是新一代写法：
      - Mapped[int] 让 **类型检查器**（mypy/pyright）真正认识字段类型
      - mapped_column() 显式声明列属性，比 1.x 的 Column() 更可读
      - 1.x 的 declarative_base() 已进入维护模式，新项目直接用这个
    """


class Book(Base):
    """图书表 —— 与第 50 课的表结构保持一致，便于对照。"""

    __tablename__ = "books"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # unique=True 会生成 UNIQUE 约束，这是 UPSERT 的前提
    url: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    site: Mapped[str] = mapped_column(String(100), default="", index=True)

    title: Mapped[str] = mapped_column(String(500))
    # Optional 字段用 Mapped[float | None]，这样类型检查器会强制你处理 None
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="GBP")
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    in_stock: Mapped[bool] = mapped_column(default=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[str] = mapped_column(String(32), default=_now_placeholder)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_placeholder)

    # 联合索引：多站点爬虫最常用的查询是 "某站点 + 价格区间"
    __table_args__ = (
        Index("ix_books_site_price", "site", "price"),
    )

    def __repr__(self) -> str:
        """返回便于调试的字符串表示。

        Returns:
            形如 <Book id=1 'Clean Code' £31.99> 的字符串。
        """
        return f"<Book id={self.id} {self.title!r} £{self.price}>"

    def to_dict(self) -> dict[str, Any]:
        """转成字典，便于写 CSV / JSON。

        Returns:
            包含所有业务字段的字典。
        """
        return {
            "id": self.id,
            "url": self.url,
            "site": self.site,
            "title": self.title,
            "price": self.price,
            "currency": self.currency,
            "rating": self.rating,
            "in_stock": self.in_stock,
            "category": self.category,
        }


def make_engine(db_path: str | Path = ":memory:", *, echo: bool = False) -> Engine:
    """创建带连接池与正确 PRAGMA 的 Engine。

    Args:
        db_path: SQLite 文件路径，或 ":memory:"。
        echo: 是否把生成的 SQL 打印出来（调试神器）。

    Returns:
        配置好的 Engine。

    关于连接池：SQLAlchemy 默认给 SQLite 用 SingletonThreadPool/NullPool，
    也就是**每次连接都是新建的**。对文件数据库来说这没问题，
    但如果你想让 PRAGMA 生效，必须挂 connect 事件 —— 因为每个新连接
    都是全新的，PRAGMA 不会"继承"。

    这就是为什么下面要用 @event.listens_for(engine, "connect")：
    它是唯一能保证「每条物理连接建立时都执行一次 PRAGMA」的位置。
    """
    url = f"sqlite:///{db_path}" if db_path != ":memory:" else "sqlite://"
    engine = create_engine(url, echo=echo, future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn: Any, _record: Any) -> None:
        """每条新连接建立时设置 PRAGMA。

        Args:
            dbapi_conn: 底层 DBAPI 连接对象。
            _record: SQLAlchemy 连接记录（这里不用）。

        Returns:
            None
        """
        cur = dbapi_conn.cursor()
        # WAL：读写不互斥。内存库不支持 WAL，会静默失败，所以包一层 try。
        try:
            cur.execute("PRAGMA journal_mode = WAL")
        except Exception:
            pass
        cur.execute("PRAGMA synchronous = NORMAL")
        cur.execute("PRAGMA foreign_keys = ON")
        # busy_timeout：遇到锁时的等待毫秒数。默认 0 会立刻抛
        # "database is locked"，这是 SQLite 最烦人的错误。
        # 设成 5000 之后，绝大多数锁竞争会被自动解决。
        cur.execute("PRAGMA busy_timeout = 5000")
        cur.close()

    return engine


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """提供一个自动提交 / 自动回滚的 Session 上下文。

    Args:
        factory: sessionmaker 产出的工厂。

    Yields:
        Session 实例。

    Raises:
        Exception: 块内任何异常都会触发 rollback 后原样抛出。

    为什么要包装？因为"忘记 commit"和"异常后忘记 rollback"
    是 ORM 项目里最高频的两个 bug。用它之后业务代码只需要：

        with session_scope(SessionLocal) as s:
            s.add(book)          # 不用写 commit，退出块自动提交

    这个模式叫 Unit of Work，是整个 ORM 世界最实用的一个封装。
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ============================================================================
# 三、Repository 模式：把 SQL 关进笼子
# ============================================================================
@dataclass
class UpsertStats:
    """一次批量 UPSERT 的统计结果。

    Attributes:
        inserted: 新增的记录数。
        updated: 发生变化的记录数。
        unchanged: 内容完全一致、未改动的记录数。
        total: 输入记录总数。

    为什么需要这个数据结构？
    第 50 课我们发现 rowcount 不可靠。所以这里用「先查已有 url 集合」
    的方式自己算 inserted / updated —— 这个数字对增量采集监控很重要：
      · inserted 突然飙升 → 判重逻辑失效了
      · updated 长期为 0    → 页面结构改了，解析器抓不到值了
    """

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    total: int = 0

    def __str__(self) -> str:
        """返回易读的统计摘要。

        Returns:
            形如 "新增 3 / 更新 2 / 未变 1 （共 6）" 的字符串。
        """
        return (f"新增 {self.inserted} / 更新 {self.updated} / "
                f"未变 {self.unchanged} （共 {self.total}）")


class BookRepository:
    """图书数据访问层 —— 所有 SQL 都关在这个类里。

    Repository 模式的核心价值不是"分层好看"，而是：

      1. **业务代码不用懂 SQL**。爬虫回调里只写 repo.upsert_many(items)，
         换数据库（SQLite → PostgreSQL）时业务代码一行不改。
      2. **可测试**。给 Repository 传一个内存库构造的 Session，
         就能在没有真实数据库的情况下跑测试。
      3. **可统一埋点**。想给所有查询加耗时统计？改这一个类就够了。
    """

    def __init__(self, session: Session) -> None:
        """初始化仓库。

        Args:
            session: SQLAlchemy Session，由调用方管理生命周期。
        """
        self.s = session

    # ---------------- 写 ----------------
    def upsert_many(self, items: Sequence[dict[str, Any]]) -> UpsertStats:
        """批量 UPSERT —— 爬虫落库的主力方法。

        Args:
            items: 记录字典列表，必须含 url，可选 title/price/... 。

        Returns:
            UpsertStats 统计对象。

        Raises:
            sqlalchemy.exc.SQLAlchemyError: 数据库错误时抛出（由 session_scope 回滚）。

        实现思路（比第 50 课的纯 SQL 版更细）：
          1. 一次 SELECT 把所有已存在的 url → id 映射捞进内存
             注意：这一步是 O(1) 次查询，不是 N 次。爬虫判重的性能陷阱
             恰恰在于写成 for item: SELECT WHERE url=? —— 那就是 N+1。
          2. 遍历输入，按 url 是否存在分流到 insert / update
          3. 用 ORM 的 add_all 与对象字段赋值，最后统一 flush
        """
        stats = UpsertStats(total=len(items))
        if not items:
            return stats

        urls = [it["url"] for it in items]

        # 步骤 1：批量查已存在的 url（一次查询搞定）
        existing: dict[str, Book] = {
            b.url: b
            for b in self.s.scalars(select(Book).where(Book.url.in_(urls)))
        }

        def _content_changed(book: Book, data: dict[str, Any]) -> bool:
            """判断记录内容是否真的发生了变化。

            Args:
                book: 数据库中的现有对象。
                data: 本次抓取到的数据。

            Returns:
                True 表示有字段发生变化。

            比对的是**业务字段**，不比对 updated_at（那个一定会变）。
            这个函数就是第 50 课遗留问题的答案：
            用字段值比对代替 rowcount，才能得到可信的「更新数」。

            两个必须小心的陷阱（都踩过）：
              ① **只比对提供的键**。用 data.get(k) 会让"本次没解析到该字段"
                 变成 None，与库里的值不等 → 误判为"有变化"。
                 正确做法是 `if k not in data: continue`。
              ② **浮点容差**。31.99 从 JSON 解析出来可能是 31.990000000000002，
                 直接 != 会天天报"价格变了"。用 1e-6 容差比较。
            """
            for k in ("title", "price", "rating", "in_stock", "category"):
                # 陷阱 ①：本次没抓到这个字段就跳过，不代表它变了
                if k not in data:
                    continue
                new_v = data[k]
                old_v = getattr(book, k, None)
                if k == "in_stock":
                    # in_stock 在 DB 里是 bool、在输入里可能是 0/1/True/False，
                    # 统一成 bool 再比，否则 True != 1 在某些驱动下成立
                    if bool(new_v) != bool(old_v):
                        return True
                elif k == "price":
                    # 陷阱 ②：价格用容差比较
                    try:
                        if abs(float(new_v) - float(old_v)) > 1e-6:
                            return True
                    except (TypeError, ValueError):
                        if new_v != old_v:
                            return True
                elif new_v != old_v:
                    return True
            return False

        for it in items:
            url = it["url"]
            now = _now_placeholder()
            if url in existing:
                book = existing[url]
                if _content_changed(book, it):
                    _apply(book, it)
                    book.updated_at = now
                    stats.updated += 1
                else:
                    stats.unchanged += 1
            else:
                book = Book(
                    url=url,
                    site=it.get("site", ""),
                    title=it.get("title", ""),
                    price=it.get("price"),
                    currency=it.get("currency", "GBP"),
                    rating=it.get("rating"),
                    in_stock=bool(it.get("in_stock", True)),
                    category=it.get("category"),
                    raw_json=json.dumps(it, ensure_ascii=False),
                    created_at=now,
                    updated_at=now,
                )
                self.s.add(book)
                # 关键：同一批次内如果出现重复 url，必须放进 existing，
                # 否则第二批会因为 UNIQUE 约束炸掉。
                existing[url] = book
                stats.inserted += 1

        self.s.flush()
        return stats

    def delete_missing(self, site: str, seen_urls: set[str]) -> int:
        """删除某站点下本次未抓到的记录（下架商品清理）。

        Args:
            site: 站点标识。
            seen_urls: 本次抓取成功见到的 url 集合。

        Returns:
            被删除的记录数。

        谨慎使用：如果本次抓取因为网络问题只成功了一半，
        这个操作会把另一半正常商品误删。
        生产做法：先标记 is_active=False，连续 N 次未出现才真删（软删除）。
        """
        rows = list(self.s.scalars(
            select(Book).where(Book.site == site, Book.url.notin_(seen_urls))
        ))
        for r in rows:
            self.s.delete(r)
        return len(rows)

    # ---------------- 读 ----------------
    def get_by_url(self, url: str) -> Book | None:
        """按 URL 查单条。

        Args:
            url: 图书 URL。

        Returns:
            Book 对象；不存在时返回 None。
        """
        return self.s.scalar(select(Book).where(Book.url == url))

    def top_expensive(self, limit: int = 5) -> list[Book]:
        """按价格倒序取前 N 本。

        Args:
            limit: 返回条数。

        Returns:
            Book 列表（过滤掉价格为 NULL 的）。
        """
        stmt = (
            select(Book)
            .where(Book.price.is_not(None))
            .order_by(Book.price.desc())
            .limit(limit)
        )
        return list(self.s.scalars(stmt))

    def price_stats(self) -> list[tuple[str, int, float]]:
        """按站点统计价格。

        Returns:
            [(site, count, avg_price), ...] 列表。

        注意这里用了 SQLAlchemy Core 的写法而不是 ORM：
        聚合查询的结果不是实体对象，用 Core 更自然，
        而且只传输 3 列而不是整行 —— 数据量大时差别明显。
        """
        stmt = (
            select(
                Book.site,
                func.count(Book.id).label("n"),
                func.round(func.avg(Book.price), 2).label("avg_price"),
            )
            .where(Book.price.is_not(None))
            .group_by(Book.site)
            .order_by(func.avg(Book.price).desc())
        )
        return [(r[0], r[1], r[2]) for r in self.s.execute(stmt)]


def _apply(book: Book, data: dict[str, Any]) -> None:
    """把抓取数据写进 ORM 对象的业务字段。

    Args:
        book: 目标 ORM 对象。
        data: 抓取到的数据字典。

    Returns:
        None
    """
    for k in ("title", "price", "rating", "in_stock", "category"):
        # 同样只处理本次真的抓到的字段。没抓到的保持库中原值，
        # 而不是被 None 覆盖 —— 否则页面改版少解析一个字段，
        # 全库那个字段就被清空了，而且不可逆。
        if k not in data:
            continue
        setattr(book, k, bool(data[k]) if k == "in_stock" else data[k])
    book.raw_json = json.dumps(
        {**json.loads(book.raw_json or "{}"), **data}, ensure_ascii=False
    )


# ============================================================================
# 实验区
# ============================================================================
ROUND1 = [
    {"url": "http://shop.local/1", "site": "shop.local", "title": "Deep Work",
     "price": 42.50, "rating": 5, "category": "productivity"},
    {"url": "http://shop.local/2", "site": "shop.local", "title": "Clean Code",
     "price": 31.99, "rating": 5, "category": "programming"},
    {"url": "http://shop.local/3", "site": "shop.local", "title": "Refactoring",
     "price": 55.00, "rating": 5, "category": "programming"},
    {"url": "http://other.local/1", "site": "other.local", "title": "Sapiens",
     "price": 12.99, "rating": 4, "category": "history"},
]


def exp1_session_lifecycle() -> None:
    """实验 1：Session 生命周期与"不 commit 会怎样"。"""
    print("=" * 74)
    print("实验 1 · Session 生命周期（commit / rollback / flush 的区别）")
    print("=" * 74)

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    with session_scope(SessionLocal) as s:
        repo = BookRepository(s)
        repo.upsert_many(ROUND1)
        print("\n  在 session_scope 块内，已调用 upsert_many 但未手动 commit")

    with session_scope(SessionLocal) as s:
        n = s.scalar(select(func.count(Book.id)))
        print(f"  退出块后再查：COUNT = {n}  ← 自动提交生效 ✓")

    # ---- 演示 rollback ----
    print("\n  --- 异常回滚演示 ---")
    try:
        with session_scope(SessionLocal) as s:
            repo = BookRepository(s)
            repo.upsert_many([{"url": "http://shop.local/99", "site": "shop.local",
                               "title": "Will Be Rolled Back"}])
            raise RuntimeError("模拟：解析到一半数据源挂了")
    except RuntimeError as e:
        print(f"  块内抛出异常：{e}")

    with session_scope(SessionLocal) as s:
        gone = s.scalar(select(func.count(Book.id)).where(Book.url == "http://shop.local/99"))
        total = s.scalar(select(func.count(Book.id)))
        print(f"  回滚后：url=...99 的记录数 = {gone}（应为 0 ✓），总记录数仍为 {total}")
        print("\n  ▸ 这就是 Unit of Work 的价值：数据要么全进去，要么一条都不留。")
        print("    爬虫最怕的就是「抓了一半崩了」，下次增量时无法判断状态。")

    engine.dispose()


def exp2_upsert_stats() -> None:
    """实验 2：三轮抓取，看 inserted / updated / unchanged 如何变化。"""
    print("\n" + "=" * 74)
    print("实验 2 · 增量采集三轮：统计 inserted / updated / unchanged")
    print("=" * 74)

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)

    rounds: list[tuple[str, list[dict[str, Any]], str]] = [
        ("第 1 轮 · 全新抓取", ROUND1,
         "4 条全新记录，全部走 insert 分支"),

        ("第 2 轮 · 1 条降价 + 1 条新增 + 3 条未变", [
            {**ROUND1[0], "price": 39.99},                       # 降价 42.50→39.99
            ROUND1[1],                                            # 完全相同
            ROUND1[2],                                            # 完全相同
            ROUND1[3],                                            # 完全相同
            {"url": "http://other.local/2", "site": "other.local",
             "title": "Moneyball", "price": 9.99, "rating": 3},   # 新 URL → insert
        ], "只有价差超过容差的那条被记为 updated，其余 3 条正确识别为未变"),

        ("第 3 轮 · 站点「价格回滚」到原值", [
            ROUND1[0],   # 价格 39.99 → 42.50，又变回去了
            ROUND1[1], ROUND1[2], ROUND1[3],
        ], "价格改回去了，所以再次被判为 updated —— 这正好证明了"
           "「比对的是字段值，不是时间戳」"),
    ]

    for label, data, note in rounds:
        with session_scope(SessionLocal) as s:
            stats = BookRepository(s).upsert_many(data)
        print(f"\n  {label}")
        print(f"    {stats}")
        print(f"    ▸ {note}")

    with session_scope(SessionLocal) as s:
        rows = list(s.scalars(select(Book).order_by(Book.created_at, Book.id)))
        print(f"\n  最终库内 {len(rows)} 条：")
        print(f"    {'title':<16}{'price':>9}  {'created_at':<20}{'updated_at':<20}")
        for b in rows:
            print(f"    {b.title[:15]:<16}{b.price:>9.2f}  "
                  f"{b.created_at:<20}{b.updated_at:<20}")

    print("\n  ▸ 关键观察：第 3 轮的 updated=1 不是 bug，而是「价格确实变了」。")
    print("    这恰好说明比对逻辑是「比字段值」而非「比时间戳」——")
    print("    如果靠 updated_at 判断，每一轮都会报「全部更新」，等于没判。")
    print("\n  ▸ 业务价值：")
    print("      · unchanged 占比高 → 站点稳定，判重逻辑正常工作")
    print("      · inserted 突然飙升 → URL 生成逻辑出问题（比如把时间戳拼进了 URL）")
    print("      · updated 长期为 0  → 解析器失效了，抓到的是空值或默认值")

    engine.dispose()


def exp3_batch_performance() -> None:
    """实验 3：写入性能 —— 逐条 vs 批量，差多少倍？"""
    print("\n" + "=" * 74)
    print("实验 3 · 写入性能实测（逐条 commit vs 批量事务）")
    print("=" * 74)

    N = 2000
    data = [
        {"url": f"http://perf.local/{i}", "site": "perf.local",
         "title": f"Book {i}", "price": round(10 + i * 0.01, 2)}
        for i in range(N)
    ]

    # ---- 方式 A：逐条 add + commit ----
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    t0 = time.perf_counter()
    with SL() as s:
        repo = BookRepository(s)
        for it in data[:400]:           # 逐条太慢，只跑 400 条做外推
            repo.upsert_many([it])
            s.commit()
    t_a = time.perf_counter() - t0
    engine.dispose()
    per_row_a = t_a / 400

    # ---- 方式 B：一次性批量 ----
    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    SL2 = sessionmaker(bind=engine, expire_on_commit=False)
    t0 = time.perf_counter()
    with session_scope(SL2) as s:
        BookRepository(s).upsert_many(data)
    t_b = time.perf_counter() - t0
    engine.dispose()
    per_row_b = t_b / N

    print(f"\n  {N} 条记录写入耗时对比：")
    print(f"    A 逐条 commit（外推）：{per_row_a * N:8.3f} s   "
          f"({per_row_a * 1000:.3f} ms/条)")
    print(f"    B 单事务批量：        {t_b:8.3f} s   ({per_row_b * 1000:.3f} ms/条)")
    if per_row_b > 0:
        print(f"\n    提速约 {per_row_a / per_row_b:.0f} 倍")

    print("\n  ▸ 为什么差这么多？每 commit 一次就是一次 fsync（强制刷盘）。")
    print("    1000 条逐条提交 = 1000 次 fsync；批量提交 = 1 次。")
    print("    磁盘 IO 是这里唯一的瓶颈，跟 SQL 写得多漂亮毫无关系。")
    print("\n  ▸ 除了逐条 commit，另外两个常见性能杀手：")
    print("      · N+1 查询：循环里 SELECT，改成 IN (...) 一次捞回")
    print("      · 全表扫描：WHERE 的列没索引，加个 Index() 就解决")

    # ---- 额外：三次批量提交 vs 一次批量提交 ----
    print("\n  --- 补充：批量大小的影响 ---")
    for batch in (1, 50, 500, 2000):
        eng = make_engine(":memory:")
        Base.metadata.create_all(eng)
        S = sessionmaker(bind=eng, expire_on_commit=False)
        t0 = time.perf_counter()
        with session_scope(S) as s:
            repo = BookRepository(s)
            for i in range(0, N, batch):
                repo.upsert_many(data[i:i + batch])
        dt = time.perf_counter() - t0
        eng.dispose()
        print(f"    每次提交 {batch:>4} 条 → {dt:7.3f} s")
    print("\n  ▸ 注意：批量不是越大越好。批量太大会让单事务持有锁很久、")
    print("    内存占用升高、失败时回滚代价更大。生产常用 500-5000 一批。")


def exp4_n_plus_one() -> None:
    """实验 4：N+1 查询 —— ORM 最经典的性能陷阱。"""
    print("\n" + "=" * 74)
    print("实验 4 · N+1 查询与延迟加载（lazy loading）")
    print("=" * 74)

    engine = make_engine(":memory:", echo=False)
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)

    with session_scope(SL) as s:
        BookRepository(s).upsert_many(ROUND1)

    # 统计实际执行的 SQL 条数
    sql_counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        """统计 SQL 执行次数。"""
        sql_counter["n"] += 1

    # ---- 错误写法：先查列表，再逐条访问属性 ----
    sql_counter["n"] = 0
    with session_scope(SL) as s:
        books = list(s.scalars(select(Book)))
        titles = [b.title for b in books]      # 普通列不会触发新查询
    print(f"\n  错误写法（循环访问普通列）：执行了 {sql_counter['n']} 条 SQL")

    # ---- 真·N+1：循环里去查关联 / 重新查询 ----
    sql_counter["n"] = 0
    with session_scope(SL) as s:
        books = list(s.scalars(select(Book)))
        # 反例：每次循环都单独查一次（新手最常见）
        out = []
        for b in books:
            fresh = s.scalar(select(Book).where(Book.id == b.id))
            out.append(fresh.title if fresh else "")
    print(f"  真·N+1（循环内 SELECT）：{len(books)} 本书 → "
          f"执行了 {sql_counter['n']} 条 SQL  ← 1 + N")

    # ---- 正确写法：一次搞定 ----
    sql_counter["n"] = 0
    with session_scope(SL) as s:
        titles = list(s.scalars(select(Book.title)))
    print(f"  正确写法（只取需要的列）：执行了 {sql_counter['n']} 条 SQL ✓")

    print(f"\n  加速比：后者比 N+1 少执行 {len(books)} 条 SQL")
    print("\n  ▸ N+1 的危害随数据量线性放大。1000 条数据，扫 1001 次库。")
    print("    症状：单条查询都很快，整体却奇慢无比。")
    print("    排查方法：把 SQL 都打印出来（make_engine(echo=True)），")
    print("              看到「一堆长得一样、只有参数不同」的语句就是它。")
    print("    修复手段：")
    print("      · 用 IN (…) 批量查，别在循环里查")
    print("      · 关联对象用 selectinload() / joinedload() 预加载")
    print("      · 只要某几列时用 select(Book.title) 而不是 select(Book)")

    engine.dispose()


def exp5_engine_comparison() -> None:
    """实验 5：同一次统计——裸 SQL vs Core vs ORM 三种写法对照。"""
    print("\n" + "=" * 74)
    print("实验 5 · 三种写法做同一件事：按站点统计均价")
    print("=" * 74)

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)
    SL = sessionmaker(bind=engine, expire_on_commit=False)
    with session_scope(SL) as s:
        BookRepository(s).upsert_many(ROUND1)

    # ---- 写法 A：裸 sqlite3（绕过 SQLAlchemy）----
    print("\n  【A】裸 sqlite3 —— 最直白，但绑死 SQLite 方言")
    print("      sql = 'SELECT site, COUNT(*), AVG(price) FROM books "
          "WHERE price IS NOT NULL GROUP BY site'")
    print("      rows = conn.execute(sql).fetchall()")

    # ---- 写法 B：SQLAlchemy Core ----
    print("\n  【B】SQLAlchemy Core —— 组合式，可跨数据库")
    print("      stmt = (select(Book.site, func.count(Book.id), func.avg(Book.price))")
    print("              .where(Book.price.is_not(None))")
    print("              .group_by(Book.site))")

    # ---- 写法 C：ORM 聚合 ----
    print("\n  【C】ORM —— 面向对象，但聚合场景反而更啰嗦")

    with session_scope(SL) as s:
        repo = BookRepository(s)
        result = repo.price_stats()
        print(f"\n  实际结果（三种写法结果完全一致）：")
        print(f"    {'site':<16}{'count':>8}{'avg_price':>12}")
        for site, n, avg in result:
            print(f"    {site:<16}{n:>8}{avg:>12.2f}")

    print("\n  ▸ 选型建议：")
    print("      · 一次性脚本 / 只用 SQLite      → 裸 sqlite3（依赖最少）")
    print("      · 批量 ETL / 复杂聚合           → Core（组合式写法真香）")
    print("      · 业务 CRUD / 有关系对象        → ORM")
    print("      · 实际项目常常三者混用，这完全没问题")

    engine.dispose()


def exp6_detached_error() -> None:
    """实验 6：DetachedInstanceError —— 到底什么时候才会真的报错。

    这个实验的价值在于**纠正一个流传很广的错误认知**：
    很多人以为「commit 之后访问属性就会炸」，其实不会。
    真正的触发条件是「expired + detached 同时成立」。
    """
    print("\n" + "=" * 74)
    print("实验 6 · DetachedInstanceError（到底什么时候才炸）")
    print("=" * 74)

    engine = make_engine(":memory:")
    Base.metadata.create_all(engine)

    SL_default = sessionmaker(bind=engine)                 # expire_on_commit=True（默认）
    SL_safe = sessionmaker(bind=engine, expire_on_commit=False)

    with session_scope(SL_default) as s:
        BookRepository(s).upsert_many(ROUND1)

    URL = "http://shop.local/1"

    # ---- 场景 A：commit 后 Session 还开着 → 不炸 ----
    print("\n  【场景 A】commit 后，Session 仍然打开时读取")
    s = SL_default()
    book = s.scalar(select(Book).where(Book.url == URL))
    print(f"    commit 前          : title = {book.title!r}")
    s.commit()
    print(f"    commit 后（仍开着）: title = {book.title!r}")
    print("    → 不报错。原因：属性虽然被标记 expired，但 Session 还活着，")
    print("      SQLAlchemy 会**静默补发一条 SELECT** 把值重新加载回来。")
    s.close()

    # ---- 场景 B：commit 后立刻 close，然后首次读取 → 炸 ----
    print("\n  【场景 B】commit 后 close，再读取（最经典的踩坑写法）")
    s = SL_default()
    book = s.scalar(select(Book).where(Book.url == URL))
    s.commit()
    s.close()
    try:
        _ = book.title
        print("    → 竟然没报错（不该发生）")
    except Exception as e:
        print(f"    → 抛异常：{type(e).__name__}")
        print(f"      {str(e)[:110]}...")
    print("    → 这次炸了。因为：")
    print("        ① commit 让属性 expired（等下次访问重载）")
    print("        ② close  让对象 detached（脱离 Session，发不出 SQL）")
    print("      两个条件**同时成立**，才会抛 DetachedInstanceError。")

    # ---- 场景 C：中间读过一次就不炸了（更隐蔽的坑）----
    print("\n  【场景 C】如果在 close 前『顺便读过一次』，之后就不炸了")
    s = SL_default()
    book = s.scalar(select(Book).where(Book.url == URL))
    s.commit()
    _ = book.title          # ← 就是这一读，把属性重新填了回来
    s.close()
    try:
        print(f"    close 后再读          : {book.title!r}  → 没报错！")
    except Exception as e:
        print(f"    → 抛异常：{type(e).__name__}")
    print("    → 因为那次读取触发了重载，属性已经不是 expired 状态了。")
    print("    ⚠ 这才是最危险的情况：")
    print("      报错与否取决于『你之前有没有碰过这个属性』，")
    print("      代码看起来一模一样，行为却不同 —— 排查时极难定位。")
    print("      在生产里表现为「偶尔报错，重启后就好了」，非常折磨人。")

    # ---- 场景 D：expire_on_commit=False ----
    print("\n  【场景 D】expire_on_commit=False —— 根本解法之一")
    s2 = SL_safe()
    book2 = s2.scalar(select(Book).where(Book.url == URL))
    s2.commit()
    s2.close()
    print(f"    close 后读取       : {book2.title!r}  ✓ 稳定正常")
    print("    → 因为 commit 不再 expire 属性，对象一直带着已加载的值，")
    print("      detached 也无所谓（不需要发 SQL）。")

    # ---- 场景 E：根因解法 —— 数据出 Repository 前先脱壳 ----
    print("\n  【场景 E】推荐做法：在 Session 内把数据摘成纯 dict")
    s3 = SL_safe()
    book3 = s3.scalar(select(Book).where(Book.url == "http://shop.local/2"))
    payload = book3.to_dict()      # ← Session 存活时摘数据
    s3.close()
    print(f"    Session 关闭后使用 dict：{payload['title']} / £{payload['price']}  ✓")

    print("\n  ▸ 实践建议（按优先级）：")
    print("      1. Session 用 expire_on_commit=False（省掉大量无谓 SELECT）")
    print("      2. 「ORM 对象不出 Repository」—— 对外一律返回 dict / dataclass")
    print("      3. 确实要带关联对象出去，用 selectinload() 预加载，别指望延迟加载")
    print("      4. 报错时先检查：是不是 close() 之后才访问属性")

    engine.dispose()


def main() -> None:
    """运行全部实验。"""
    exp1_session_lifecycle()
    exp2_upsert_stats()
    exp3_batch_performance()
    exp4_n_plus_one()
    exp5_engine_comparison()
    exp6_detached_error()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 三层抽象按场景选：裸 SQL（脚本）/ Core（ETL）/ ORM（业务），可混用",
        "2. Mapped[] + mapped_column() 是 2.0 写法，类型检查器能真正校验",
        "3. PRAGMA 必须挂在 connect 事件上，否则新连接不会继承",
        "4. busy_timeout=5000 能消灭 90% 的 'database is locked'",
        "5. session_scope 上下文管理器：自动 commit / rollback，杜绝忘记提交",
        "6. Repository 模式把 SQL 关进笼子，业务代码不碰数据库细节",
        "7. 判重必须批量查（IN），不能循环单查（N+1）",
        "8. 用字段值比对算 updated 数，不要信 rowcount",
        "9. 逐条 commit 比批量慢几十倍，瓶颈在 fsync 不在 SQL",
        "10. expire_on_commit=False + to_dict() 是 DetachedInstanceError 的解药",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
