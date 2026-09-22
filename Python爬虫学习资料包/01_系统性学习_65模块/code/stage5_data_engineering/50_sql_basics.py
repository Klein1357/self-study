"""
第 50 课 · SQL 基础 —— 从「列表套字典」到「关系表」

================================ 学习目标 ================================
1. 理解为什么爬虫数据最终要落进数据库，以及"表结构"该怎么设计
2. 掌握 SQL 四大动作：CREATE / INSERT / SELECT / UPDATE + DELETE
3. 掌握 WHERE / ORDER BY / LIMIT / GROUP BY / JOIN / 索引
4. 掌握 UPSERT —— 增量采集的基石
5. 知道 SQLite / MySQL / PostgreSQL 的选型边界

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/50_sql_basics.py

不需要任何数据库服务，全程用 Python 内置的 sqlite3（零依赖、文件即数据库）。
学完这节课你会发现：SQLite 完全够用，直到你有了真实并发写入需求。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

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
# 为什么是 SQLite 而不是 MySQL？
# ============================================================================
# 教学场景下 SQLite 有三个压倒性优势：
#   1. 零部署 —— Python 内置，一个文件就是一个数据库
#   2. SQL 方言几乎通用 —— 学完 SQLite 的 SQL，MySQL/PG 只需要补 5% 差异
#   3. 便于"看"—— 你可以直接把 .db 文件拷走给别人
#
# SQLite 的真实边界（什么时候必须换 MySQL/PG）：
#   - 并发写入：SQLite 同一时刻只允许一个写事务，写入密集会频繁 database is locked
#   - 网络访问：SQLite 是本地文件，多台机器没法共享
#   - 数据类型：SQLite 是"动态类型"，INTEGER 列里塞字符串不会报错（双刃剑）
#   - 全文检索 / JSON / 地理查询：PG 的扩展生态强太多
#
# 判断标准很简单：**如果你的爬虫单机跑、写入不密集，SQLite 就是最优解。**


# ============================================================================
# 一、表设计：从爬虫数据的特点出发
# ============================================================================
# 爬虫数据有三个绕不开的特点，直接决定了表该怎么设计：
#
#   1. **同一个 URL 会被抓到很多次** → 需要唯一约束 + UPSERT
#   2. **字段会中途新增**（今天有价格，明天想要库存） → 需要预留 raw_json 列
#   3. **需要知道"什么时候抓的"** → 需要 created_at / updated_at 时间戳
#
# 下面这份 DDL 是「商品类」爬虫的推荐骨架，注意每一行的注释：

BOOKS_DDL = """
CREATE TABLE IF NOT EXISTS books (
    -- ① 主键：自增整数。为什么不用 URL 当主键？
    --    因为 URL 可能很长，而且改版时会变；用自增 ID 让主键永远稳定。
    id           INTEGER PRIMARY KEY AUTOINCREMENT,

    -- ② 业务唯一键：用来判重。这里是 URL，实际项目常见 (site, sku) 组合。
    url          TEXT    NOT NULL,
    site         TEXT    NOT NULL DEFAULT '',

    -- ③ 业务字段：给"会查询/排序/统计"的字段单独建列
    title        TEXT    NOT NULL,
    price        REAL,               -- 可能为空 → 不写 NOT NULL
    currency     TEXT    DEFAULT 'GBP',
    rating       INTEGER,
    in_stock     INTEGER DEFAULT 1,  -- SQLite 没有 bool，用 0/1
    category     TEXT,

    -- ④ 原始数据：字段会变，但千万别丢原始 JSON。
    --    有了它，未来加字段可以"回溯重跑"，不用重新抓一遍。
    raw_json     TEXT,

    -- ⑤ 时间戳：排查问题、做增量采集都靠它
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,

    -- ⑥ 唯一约束：UPSERT 的前提
    UNIQUE(url)
)
"""

# 索引：数据库性能 90% 的问题出在这里。
# 没有索引时，SELECT ... WHERE price > 20 会全表扫描（O(n)）；
# 建了索引之后变成 B+ 树查找（O(log n)）。
#
# 但索引不是免费的：每个索引都会让 INSERT 变慢、占用额外空间。
# 经验法则：**只给 WHERE / ORDER BY / JOIN ON 里出现的列建索引**。
BOOKS_INDEXES = [
    # 按价格筛选是最常见的查询
    "CREATE INDEX IF NOT EXISTS idx_books_price ON books(price)",
    # 按站点筛选（多站点爬虫必备）
    "CREATE INDEX IF NOT EXISTS idx_books_site  ON books(site)",
    # 联合索引：同时按站点 + 价格查，比两个单列索引更高效
    "CREATE INDEX IF NOT EXISTS idx_books_site_price ON books(site, price)",
]


# ============================================================================
# 二、连接管理：三个必须知道的坑
# ============================================================================
def connect(db_path: str | Path) -> sqlite3.Connection:
    """创建一个配置正确的 SQLite 连接。

    这里做了三件很多教程不会提、但生产上必须做的事。

    Args:
        db_path: 数据库文件路径，传 ":memory:" 则用内存数据库。

    Returns:
        配置好的连接对象。

    Raises:
        sqlite3.Error: 数据库文件无法打开或创建时抛出。

    坑 1：row_factory
        默认返回 tuple，你得用 row[0]/row[3] 取值——三个月后自己都看不懂。
        设置 sqlite3.Row 之后可以用 row["title"]，代码自解释。

    坑 2：WAL 模式
        默认 journal 模式下，写事务会阻塞所有读。
        开启 WAL（Write-Ahead Logging）后读写可以并发，单机吞吐提升巨大。
        这是**一行 PRAGMA 换来的最大收益**，但 90% 的教程都不提。

    坑 3：foreign_keys
        SQLite 默认**不**开启外键约束（为了兼容旧行为）。
        如果你建了外键却不 PRAGMA，约束形同虚设——这会导致脏数据悄悄堆积。
    """
    conn = sqlite3.connect(str(db_path), timeout=30.0)

    # 坑 1：用列名取值而不是下标
    conn.row_factory = sqlite3.Row

    # 坑 2：WAL 模式，读写不互斥
    conn.execute("PRAGMA journal_mode = WAL")
    # 平衡耐久性与性能：NORMAL 下大部分场景不会丢数据，但快很多
    conn.execute("PRAGMA synchronous = NORMAL")

    # 坑 3：真正开启外键约束
    conn.execute("PRAGMA foreign_keys = ON")

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表 + 建索引。用 IF NOT EXISTS 保证可重复执行（幂等）。

    Args:
        conn: 数据库连接。

    Returns:
        None
    """
    conn.execute(BOOKS_DDL)
    for stmt in BOOKS_INDEXES:
        conn.execute(stmt)
    conn.commit()


# ============================================================================
# 三、INSERT：批量写入的正确姿势
# ============================================================================
# 新手写法（慢 100 倍）：
#     for book in books:
#         conn.execute("INSERT INTO books(...) VALUES(?,?,?)", (...))
#         conn.commit()
#
# 问题在于：每一条 INSERT 都是一次独立事务，每次都要 fsync 到磁盘。
# 正确姿势是 **executemany + 单次 commit**，把 N 次磁盘同步压缩成 1 次。

def insert_many(conn: sqlite3.Connection, books: list[dict[str, Any]]) -> int:
    """批量插入图书记录。

    Args:
        conn: 数据库连接。
        books: 记录列表，每条需含 title/url/price 等键。

    Returns:
        实际插入的行数。

    Raises:
        sqlite3.IntegrityError: URL 重复且未使用 UPSERT 时由调用方决定如何处理。
    """
    now = _now()
    rows = [
        (
            b.get("url", ""),
            b.get("site", ""),
            b.get("title", ""),
            b.get("price"),
            b.get("currency", "GBP"),
            b.get("rating"),
            1 if b.get("in_stock", True) else 0,
            b.get("category"),
            json.dumps(b, ensure_ascii=False),
            now,
            now,
        )
        for b in books
    ]

    # 用 ? 占位符，**绝不要**用 f-string 拼 SQL！
    # 字符串拼接 = SQL 注入漏洞。哪怕数据来源"很干净"也不要破例，
    # 因为爬虫抓来的内容恰恰是最不可信的输入。
    sql = """
        INSERT INTO books
            (url, site, title, price, currency, rating, in_stock,
             category, raw_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    cur = conn.executemany(sql, rows)
    conn.commit()
    return cur.rowcount


# ============================================================================
# 四、UPSERT：增量采集的基石
# ============================================================================
# 需求："这本书我上周抓过，这周价格变了，该更新；新书则插入。"
#
# 三种实现方式的对比：
#
#   方式 1：先 SELECT 判断存在，再决定 INSERT / UPDATE
#          → 两次网络往返 + 有竞态。**不要用**。
#
#   方式 2：INSERT OR REPLACE
#          → 会先 DELETE 再 INSERT。副作用：id 变了、created_at 被重置。
#            **如果你有其他表外键引用它，会级联删除！**
#
#   方式 3：INSERT ... ON CONFLICT DO UPDATE（推荐）
#          → 真正意义上的"更新"，主键不变，只改指定列。
#            这是 SQLite 3.24+ / PostgreSQL 9.5+ / MySQL 8.0.19+ 的标准写法。

UPSERT_SQL = """
    INSERT INTO books
        (url, site, title, price, currency, rating, in_stock,
         category, raw_json, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(url) DO UPDATE SET
        title      = excluded.title,
        price      = excluded.price,
        currency   = excluded.currency,
        rating     = excluded.rating,
        in_stock   = excluded.in_stock,
        category   = excluded.category,
        raw_json   = excluded.raw_json,
        updated_at = excluded.updated_at
        -- 注意：这里**故意不更新** created_at。
        -- created_at 表示"首次见到这条记录的时间"，是业务语义，不该被覆盖。
    WHERE excluded.updated_at <> books.updated_at
        -- 这个 WHERE 是性能优化 + 语义优化：
        -- 内容没变化时不写磁盘，同时让 rowcount 只统计"真正改变的"记录。
"""


def upsert_many(conn: sqlite3.Connection, books: list[dict[str, Any]]) -> int:
    """UPSERT 批量写入：存在则更新，不存在则插入。

    Args:
        conn: 数据库连接。
        books: 记录列表。

    Returns:
        真正发生变化的行数（内容未变的记录不计入）。

    Raises:
        sqlite3.DatabaseError: SQL 执行失败时抛出。
    """
    now = _now()
    rows = [
        (
            b.get("url", ""),
            b.get("site", ""),
            b.get("title", ""),
            b.get("price"),
            b.get("currency", "GBP"),
            b.get("rating"),
            1 if b.get("in_stock", True) else 0,
            b.get("category"),
            json.dumps(b, ensure_ascii=False),
            now,
            now,
        )
        for b in books
    ]
    cur = conn.executemany(UPSERT_SQL, rows)
    conn.commit()
    return cur.rowcount


# ============================================================================
# 五、SELECT：从"取数据"到"问问题"
# ============================================================================
# 新手写 SELECT 是"把数据取出来，在 Python 里处理"；
# 老手写 SELECT 是"让数据库把答案算好，我只取结果"。
#
# 为什么重要？因为跨网络传 100 万行再在 Python 里 groupby，
# 和让数据库直接返回 20 行聚合结果，差距是**数量级**的。

QUERIES: dict[str, tuple[str, str]] = {
    "① 基础筛选 + 排序 + 分页": (
        """
        SELECT title, price, rating
        FROM   books
        WHERE  price IS NOT NULL AND price < ?
        ORDER  BY price ASC, rating DESC
        LIMIT  ? OFFSET ?
        """,
        "WHERE 过滤 → ORDER BY 排序 → LIMIT 截断。OFFSET 分页在大数据量下会越来越慢"
        "（要扫描并丢弃前 N 行），深度分页应改用「游标分页」：WHERE id > 上次最大 id。",
    ),
    "② 聚合：GROUP BY + 聚合函数": (
        """
        SELECT site,
               COUNT(*)              AS n,
               ROUND(AVG(price), 2)  AS avg_price,
               MIN(price)            AS min_price,
               MAX(price)            AS max_price
        FROM   books
        WHERE  price IS NOT NULL
        GROUP  BY site
        HAVING n >= 1
        ORDER  BY avg_price DESC
        """,
        "GROUP BY 分组 → 聚合函数算值 → HAVING 筛分组（WHERE 筛行，HAVING 筛组，顺序不能混）。",
    ),
    "③ CASE WHEN：把行变成列（透视）": (
        """
        SELECT site,
               SUM(CASE WHEN price < 20              THEN 1 ELSE 0 END) AS cheap,
               SUM(CASE WHEN price BETWEEN 20 AND 40 THEN 1 ELSE 0 END) AS mid,
               SUM(CASE WHEN price > 40              THEN 1 ELSE 0 END) AS expensive
        FROM   books
        WHERE  price IS NOT NULL
        GROUP  BY site
        """,
        "这就是「透视表」的 SQL 本质：用 CASE WHEN 造出一列条件值，再用 SUM 把它压成一个数。",
    ),
    "④ JOIN：关联两张表": (
        """
        SELECT b.title, b.price, c.name AS category_name
        FROM   books b
        LEFT   JOIN categories c ON b.category = c.slug
        ORDER  BY b.price DESC
        LIMIT  3
        """,
        "LEFT JOIN 保证左表（books）全部保留，右表（categories）缺失时补 NULL。"
        "若用 INNER JOIN，没有分类的书会被整行丢掉——这往往是 bug 而非意图。",
    ),
    "⑤ 窗口函数：排名与环比": (
        """
        SELECT title, price,
               RANK()       OVER (ORDER BY price DESC) AS price_rank,
               ROUND(AVG(price) OVER (), 2)            AS overall_avg
        FROM   books
        WHERE  price IS NOT NULL
        ORDER  BY price DESC
        LIMIT  3
        """,
        "窗口函数 = 「在结果集上开一个滑窗做计算，但不折叠行」。"
        "对比 GROUP BY 会把 10 行压成 1 行，OVER() 让 10 行各自都拿到统计值。",
    ),
}


def _now() -> str:
    """返回 ISO8601 时间戳字符串。

    Returns:
        形如 '2026-09-19T21:07:23' 的字符串。

    为什么存字符串而不是时间戳整数？
    - SQLite 没有原生日期类型，存 TEXT 的 ISO8601 可以直接字符串比较排序
    - 人类可读，出问题时直接 SELECT 就能看懂
    - 代价：占空间稍大（约 19 字节 vs 8 字节）。爬虫场景数据量下无所谓。
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ============================================================================
# 六、事务：要么全成，要么全败
# ============================================================================
def with_transaction(conn: sqlite3.Connection, ops: Iterable[tuple[str, tuple]]) -> int:
    """把一批 SQL 放进一个事务里执行，失败则整体回滚。

    Args:
        conn: 数据库连接。
        ops: (sql, params) 二元组序列。

    Returns:
        成功执行的语句数量。

    Raises:
        sqlite3.Error: 任意一条语句失败，抛出异常且整个事务回滚。

    为什么爬虫需要事务？
      典型场景：抓到 10 条数据，其中第 7 条触发了数据库约束错误。
      没有事务 → 前 6 条已写入、后 3 条丢失，数据处于"半截"状态，
                 下次跑增量时无法判断到底抓没抓过。
      有事务   → 全部回滚，下次重跑即可，状态永远一致。
    """
    executed = 0
    try:
        conn.execute("BEGIN")
        for sql, params in ops:
            conn.execute(sql, params)
            executed += 1
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    return executed


# ============================================================================
# 实验区
# ============================================================================
SAMPLE_BOOKS = [
    {"url": "http://books.local/a1", "site": "books.local", "title": "Deep Work",
     "price": 42.50, "rating": 5, "category": "productivity"},
    {"url": "http://books.local/a2", "site": "books.local", "title": "Clean Code",
     "price": 31.99, "rating": 5, "category": "programming"},
    {"url": "http://books.local/a3", "site": "books.local", "title": "The Pragmatic Programmer",
     "price": 38.00, "rating": 5, "category": "programming"},
    {"url": "http://books.local/a4", "site": "books.local", "title": "Sharp Objects",
     "price": 47.82, "rating": 4, "category": "fiction"},
    {"url": "http://other.local/b1", "site": "other.local", "title": "Sapiens",
     "price": 12.99, "rating": 4, "category": "history"},
    {"url": "http://other.local/b2", "site": "other.local", "title": "Moneyball",
     "price": 9.99, "rating": 3, "category": "sports"},
]


def _make_db() -> sqlite3.Connection:
    """建一个临时数据库并灌入样例数据。

    Returns:
        初始化好的连接。
    """
    tmp = Path(tempfile.mkdtemp()) / "stage5_50.db"
    conn = connect(tmp)
    init_db(conn)
    # 额外建一张分类表用于演示 JOIN
    conn.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL
        )
    """)
    conn.executemany(
        "INSERT OR IGNORE INTO categories(slug, name) VALUES (?, ?)",
        [("programming", "编程"), ("productivity", "效率"), ("fiction", "小说"),
         ("history", "历史")],
    )
    conn.commit()
    insert_many(conn, SAMPLE_BOOKS)
    return conn


def exp1_basic_crud() -> None:
    """实验 1：CRUD 全流程 + 参数化查询防注入演示。"""
    print("=" * 74)
    print("实验 1 · CRUD 基础 + SQL 注入演示")
    print("=" * 74)

    conn = _make_db()
    n = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    print(f"\n[Create] 批量插入 {n} 条记录（executemany 单次 commit）")

    row = conn.execute(
        "SELECT title, price, rating FROM books ORDER BY price DESC LIMIT 1"
    ).fetchone()
    print(f"[Read]   最贵的一本：{row['title']} — £{row['price']} ({row['rating']}星)")

    conn.execute("UPDATE books SET price = ? WHERE url = ?", (35.00, "http://books.local/a2"))
    conn.commit()
    newp = conn.execute(
        "SELECT price FROM books WHERE url = ?", ("http://books.local/a2",)
    ).fetchone()["price"]
    print(f"[Update] Clean Code 价格改为 £{newp}")

    # ---- SQL 注入演示：这是安全课，不是教你攻击 ----
    evil = "x' OR '1'='1"
    print("\n--- SQL 注入对比 ---")

    # ✗ 错误写法：字符串拼接
    bad_sql = f"SELECT COUNT(*) FROM books WHERE title = '{evil}'"
    try:
        bad = conn.execute(bad_sql).fetchone()[0]
        # 'x' OR '1'='1' 恒为真 → 返回全部 6 行
        print(f"✗ 拼接写法：COUNT = {bad}  ← 本应 0，却匹配到全表！")
    except sqlite3.Error as e:
        print(f"✗ 拼接写法：抛异常 {e}")

    # ✓ 正确写法：参数化
    good = conn.execute("SELECT COUNT(*) FROM books WHERE title = ?", (evil,)).fetchone()[0]
    print(f"✓ 参数化写法：COUNT = {good}  ← 正确，被当作普通字符串")

    print("\n  结论：占位符 ? 让驱动层做「值」的转义，而不是让 SQL 解析器去理解它。")
    print("       这是唯一正确的防注入方式——转义字符黑名单永远能被绕过。")

    conn.close()


def exp2_upsert_incremental() -> None:
    """实验 2：UPSERT 增量更新 —— 核心中的核心。"""
    print("\n" + "=" * 74)
    print("实验 2 · UPSERT 增量更新（爬虫吃这口饭）")
    print("=" * 74)

    conn = _make_db()
    before = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    created = conn.execute(
        "SELECT created_at FROM books WHERE url = ?", ("http://books.local/a1",)
    ).fetchone()["created_at"]
    print(f"\n初始状态：{before} 条记录，a1 的 created_at = {created}")

    # 模拟"第二天再抓一次"：a1 降价、a4 下架、新增 a5
    time.sleep(1.1)  # 让时间戳真的不一样
    round2 = [
        {"url": "http://books.local/a1", "site": "books.local", "title": "Deep Work",
         "price": 29.90, "rating": 5, "category": "productivity"},      # 价格变了
        {"url": "http://books.local/a4", "site": "books.local", "title": "Sharp Objects",
         "price": 47.82, "rating": 4, "in_stock": False, "category": "fiction"},  # 下架
        {"url": "http://books.local/a5", "site": "books.local", "title": "Refactoring",
         "price": 55.00, "rating": 5, "category": "programming"},        # 新书
        {"url": "http://books.local/a2", "site": "books.local", "title": "Clean Code",
         "price": 35.00, "rating": 5, "category": "programming"},        # 内容与库中相同
    ]
    changed = upsert_many(conn, round2)
    after = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]

    print(f"\n第二轮抓取 {len(round2)} 条，UPSERT 后：")
    print(f"  表中总行数 {before} → {after}   （新增 1 本 a5）")
    print(f"  rowcount 报告 {changed} 条          ← 这个数字有个坑，下面细说")

    r = conn.execute(
        "SELECT id, price, in_stock, created_at, updated_at FROM books WHERE url = ?",
        ("http://books.local/a1",),
    ).fetchone()
    print(f"\n  a1 记录：id={r['id']} 价格={r['price']} 有货={r['in_stock']}")
    print(f"          created_at={r['created_at']}  ← 未被覆盖 ✓")
    print(f"          updated_at={r['updated_at']}  ← 已刷新 ✓")

    r4 = conn.execute(
        "SELECT title, in_stock, updated_at FROM books WHERE url = ?",
        ("http://books.local/a4",),
    ).fetchone()
    print(f"\n  a4 记录：{r4['title']} 有货={r4['in_stock']}  ← 状态被正确更新为「已下架」")

    print("\n  为什么 created_at 不能覆盖？")
    print("  它是「首次发现」的语义。若被刷新，你就再也答不出")
    print("  「这本书我是什么时候第一次抓到的」——做数据溯源时这是致命的。")

    # ④ 移动端/网页版 SQLite 驱动常常**不返回真正的受影响行数**，
    #    而是返回"尝试执行的行数"。这在增量采集里会骗到你。
    noop = conn.execute(
        "UPDATE books SET title = title WHERE url = ?",
        ("http://books.local/a2",),
    )
    print(f"\n  验证：把 a2 的 title 更新成它自己（内容零变化）")
    print(f"    UPDATE rowcount = {noop.rowcount}  ← 内容没变却报 1")
    print("    说明 rowcount 是「语句命中的行数」而非「值真正改变的行数」。")

    print("\n  ▸ 影响：如果你用 rowcount 统计「本次更新了几条」，数字会虚高。")
    print("    要在应用层区分「新增 / 更新 / 未变」，正确做法是：")
    print("      ① UPSERT 前先 SELECT url 集合，与本次抓取集合求差 → 新增数")
    print("      ② 或者依赖上面 UPSERT_SQL 里那个 WHERE excluded.updated_at <> books.updated_at")
    print("         ——但注意：时间戳每次都在变，所以它只保证「同一次抓取内的去重」，")
    print("         真正要判「内容变没变」得比对业务字段的哈希值（第 54 课会讲）。")

    conn.close()


def exp3_aggregate_queries() -> None:
    """实验 3：跑一遍 QUERIES 里的五类查询，看真实执行计划。"""
    print("\n" + "=" * 74)
    print("实验 3 · 五类核心 SELECT 查询")
    print("=" * 74)

    conn = _make_db()
    params_map = {
        "① 基础筛选 + 排序 + 分页": (40.0, 3, 0),
        "② 聚合：GROUP BY + 聚合函数": (),
        "③ CASE WHEN：把行变成列（透视）": (),
        "④ JOIN：关联两张表": (),
        "⑤ 窗口函数：排名与环比": (),
    }

    for name, (sql, note) in QUERIES.items():
        print(f"\n{'─' * 70}")
        print(f"{name}")
        print(f"{'─' * 70}")
        rows = conn.execute(sql, params_map[name]).fetchall()
        if not rows:
            print("  (无结果)")
            continue
        cols = rows[0].keys()
        print("  " + " | ".join(f"{c:<10}" for c in cols))
        print("  " + "-" * (13 * len(cols)))
        for r in rows:
            print("  " + " | ".join(
                f"{(str(r[c]) if r[c] is not None else 'NULL')[:10]:<10}" for c in cols
            ))
        print(f"\n  ▸ {note}")

    # ---- 执行计划：索引到底有没有生效？ ----
    print(f"\n{'─' * 70}")
    print("执行计划对比（EXPLAIN QUERY PLAN）")
    print(f"{'─' * 70}")
    for label, sql in [
        ("用到索引 idx_books_price", "SELECT * FROM books WHERE price < 40"),
        ("未建索引的列 category", "SELECT * FROM books WHERE category = 'programming'"),
    ]:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()
        detail = plan[0]["detail"]
        print(f"\n  {label}")
        print(f"    → {detail}")
    print("\n  ▸ 看到 'USING INDEX' 说明走索引；看到 'SCAN' 就是全表扫描。")
    print("    category 列没建索引，所以是全表扫描 —— 数据量一大就是性能瓶颈。")

    conn.close()


def exp4_join_and_missing() -> None:
    """实验 4：JOIN 的陷阱 —— 为什么你的数据「凭空少了」。"""
    print("\n" + "=" * 74)
    print("实验 4 · INNER JOIN vs LEFT JOIN（数据凭空消失之谜）")
    print("=" * 74)

    conn = _make_db()
    inner = conn.execute("""
        SELECT b.title, c.name AS cat FROM books b
        INNER JOIN categories c ON b.category = c.slug
    """).fetchall()
    left = conn.execute("""
        SELECT b.title, COALESCE(c.name, '未分类') AS cat FROM books b
        LEFT JOIN categories c ON b.category = c.slug
    """).fetchall()

    print(f"\n  INNER JOIN 返回 {len(inner)} 行；LEFT JOIN 返回 {len(left)} 行")
    if len(left) > len(inner):
        lost = set(r["title"] for r in left) - set(r["title"] for r in inner)
        print(f"  INNER JOIN 丢掉了：{lost}")
        print("  原因：category='sports' 在 categories 表里没有对应行，")
        print("        INNER JOIN 会把这整行剔除。")
    print("\n  ▸ 规则：**默认用 LEFT JOIN**，除非你明确就是想「取交集」。")
    print("    爬虫数据里外键对不上是常态（分类被删了、站点改版了），")
    print("    用 INNER JOIN 会让你在某个深夜对着缺失的数据怀疑人生。")
    print("    另外用 COALESCE(x, '默认值') 处理 NULL，比在 Python 里写 if 更干净。")

    conn.close()


def _db_file_size(path: Path) -> int:
    """统计一个 SQLite 数据库的磁盘占用（主库 + WAL + SHM 三个文件之和）。

    Args:
        path: .db 文件路径。

    Returns:
        总字节数（不存在的文件按 0 计）。

    为什么要算三个文件？
      WAL 模式下 SQLite 由三个文件组成：a.db / a.db-wal / a.db-shm。
      只看 a.db 会漏掉刚刚写入但还没 checkpoint 的数据 ——
      你会以为自己"插入了 8MB 数据但文件只有 8KB"，从而得出错误结论。
      这是排查磁盘占用问题时的经典盲区。
    """
    total = 0
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(path) + suffix)
        if f.exists():
            total += f.stat().st_size
    return total


def exp5_delete_and_vacuum() -> None:
    """实验 5：DELETE 与 VACUUM —— 删了数据文件为什么不变小？"""
    print("\n" + "=" * 74)
    print("实验 5 · DELETE 之后，文件为什么没变小？")
    print("=" * 74)

    tmp = Path(tempfile.mkdtemp()) / "vacuum_demo.db"

    # ---------- 步骤 1：灌入数据 ----------
    conn = connect(tmp)
    conn.execute("CREATE TABLE demo(id INTEGER PRIMARY KEY, blob TEXT)")
    # 必须用 executemany + 单次 commit。逐条 commit 会让 SQLite 频繁回收日志页，
    # 反而看不出"删除后不释放空间"的效果。
    conn.executemany(
        "INSERT INTO demo(blob) VALUES (?)",
        [("x" * 4000,) for _ in range(2000)],
    )
    conn.commit()
    # 关键：checkpoint(TRUNCATE) 把 WAL 里的数据落进主库并把 wal 文件截断为 0，
    # 否则你测到的是"主库很小 + wal 很大"的中间态，数字会让人困惑。
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()          # ← 必须关连接再测！见下面「踩坑记录」
    size_before = _db_file_size(tmp)

    # ---------- 步骤 2：删除全部数据 ----------
    conn = connect(tmp)
    conn.execute("DELETE FROM demo")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    size_after_delete = _db_file_size(tmp)

    # ---------- 步骤 3：VACUUM 重建 ----------
    conn = connect(tmp)
    conn.execute("VACUUM")
    conn.commit()
    conn.close()
    size_after_vacuum = _db_file_size(tmp)

    freed = size_after_delete - size_after_vacuum
    print(f"\n  插入 2000 行 × 4KB：")
    print(f"    初始文件大小     {size_before / 1024 / 1024:8.2f} MB")
    print(f"    DELETE 全部行后  {size_after_delete / 1024 / 1024:8.2f} MB  "
          f"（文件完全没变小！）")
    print(f"    VACUUM 之后      {size_after_vacuum / 1024 / 1024:8.2f} MB  （真正释放）")
    if size_after_delete > 0:
        print(f"\n    释放 {freed / 1024 / 1024:.2f} MB，"
              f"占删除前的 {100 * freed / size_after_delete:.1f}%")

    print("\n  ▸ DELETE 只是把数据页标记为「可复用」，并不归还给操作系统。")
    print("    这是数据库的通用设计（避免频繁 mmap 变动），不是 SQLite 的 bug。")
    print("    VACUUM 会重建整个文件，代价是需要 2 倍磁盘空间 + 期间独占锁。")
    print("    爬虫场景建议：定期（如每月）VACUUM 一次，而不是每次删完都跑。")

    # ---------- 踩坑记录：为什么中途必须 close() ----------
    print("\n  ── 踩坑记录 ──")
    print("  如果你在 conn 还开着的时候去测文件大小，会看到")
    print("  「DELETE 前后字节数一模一样」「VACUUM 也没变小」。")
    print("  原因：Python 的 sqlite3 连接在 close() 或事务提交后才会真正合并 WAL，")
    print("        连接存活期间主库文件可能还是旧的。")
    print("  正确做法：改完数据 → checkpoint → close() → 再 stat()。")
    print("  同样的坑在测「数据库膨胀了多少」时也会踩到：**先关连接再测**。")


def exp6_type_affinity() -> None:
    """实验 6：SQLite 的动态类型 —— 特性还是坑？"""
    print("\n" + "=" * 74)
    print("实验 6 · SQLite 类型亲和性（TYPE AFFINITY）")
    print("=" * 74)

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t(i INTEGER, r REAL, t TEXT, b BLOB, n NUMERIC)")
    tests = [
        ("INTEGER 列塞字符串", "INSERT INTO t(i) VALUES ('hello')"),
        ("INTEGER 列塞 '123'", "INSERT INTO t(i) VALUES ('123')"),
        ("TEXT   列塞数字 456", "INSERT INTO t(t) VALUES (456)"),
        ("REAL   列塞 '3.14'", "INSERT INTO t(r) VALUES ('3.14')"),
    ]
    print("\n  尝试写入不符合声明类型的值：\n")
    for label, sql in tests:
        try:
            conn.execute(sql)
            v = conn.execute(f"SELECT {sql.split('(')[1].split(')')[0]} FROM t "
                             "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            print(f"  {label:<24} → 成功，实际存为 {v!r} ({type(v).__name__})")
        except sqlite3.Error as e:
            print(f"  {label:<24} → 失败：{e}")

    print("\n  ▸ SQLite 会根据写入值「就近转换」到列的亲和类型，'hello' 塞不进 INTEGER，")
    print("    但会原样存成字符串——**不报错**。这就是动态类型的双刃剑。")
    print("    MySQL/PG 会直接拒绝。所以：")
    print("      · 用 SQLite 时，类型校验要在 Python 层做好（Pydantic 是好帮手）")
    print("      · 别指望数据库帮你兜底，脏数据进来时一声不吭")
    conn.close()


def main() -> None:
    """运行全部实验。"""
    exp1_basic_crud()
    exp2_upsert_incremental()
    exp3_aggregate_queries()
    exp4_join_and_missing()
    exp5_delete_and_vacuum()
    exp6_type_affinity()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 表设计三件套：业务唯一键（UNIQUE）+ raw_json 原样保留 + 双时间戳",
        "2. 连接三件套：row_factory=Row、PRAGMA journal_mode=WAL、foreign_keys=ON",
        "3. 批量写入用 executemany + 单次 commit，速度差 100 倍",
        "4. UPSERT 用 ON CONFLICT DO UPDATE，不要用 INSERT OR REPLACE（会重置 id）",
        "5. created_at 永不覆盖，updated_at 每次刷新 —— 这是数据溯源的根基",
        "6. 永远用 ? 占位符，字符串拼 SQL 是注入漏洞",
        "7. 默认 LEFT JOIN，除非你明确要交集",
        "8. EXPLAIN QUERY PLAN 是排查慢查询的第一工具",
        "9. DELETE 不释放磁盘，VACUUM 才释放（但要 2 倍空间 + 独占锁）",
        "10. SQLite 动态类型不报错，类型校验得在 Python 层做",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
