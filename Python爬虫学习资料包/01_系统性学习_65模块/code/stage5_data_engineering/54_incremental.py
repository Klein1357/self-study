"""
第 54 课 · 增量采集与去重 —— 让爬虫只抓「新的」

================================ 学习目标 ================================
1. 理解增量采集的三种模式：全量、时间戳增量、指纹增量
2. 掌握「内容指纹」的设计：哪些字段参与哈希、为什么
3. 掌握三层去重：URL 去重 → 记录去重 → 变化检测
4. 掌握断点续爬与前缀树（Trie）判重的工程实现
5. 掌握布隆过滤器：用 1% 的内存做 99% 的判重
6. 建立「变化日志」—— 记录每一次价格/库存变动，而不只是最终状态

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/54_incremental.py

零额外依赖，全程用内存 SQLite + 纯 Python 数据结构。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

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
# 认知框架：为什么增量采集是最容易做错的一环
# ============================================================================
# 新手做法：每次跑都把全站抓一遍，然后 upsert 进库。
# 这在数据量小的时候没问题，一旦规模上来就会：
#   · 浪费 99% 的请求配额（大部分页面内容没变）
#   · 更容易触发反爬（频繁访问同一批页面）
#   · 跑一次耗时几小时，天窗越来越长
#   · 站点成本增加，伦理上也说不过去
#
# 正确的增量采集需要回答三个层次的问题：
#
#   ┌─────────────────────────────────────────────────────────────┐
#   │ L1 · 要不要抓这个 URL？                                      │
#   │      → URL 层面的去重：这次任务里这个 URL 抓过没有？           │
#   │      → 工具：内存 set / Redis set / 布隆过滤器                │
#   ├─────────────────────────────────────────────────────────────┤
#   │ L2 · 这个页面要不要重新抓？                                   │
#   │      → 判断远程内容有没有变，没变就跳过                       │
#   │      → 工具：HTTP 缓存头（ETag / Last-Modified）              │
#   ├─────────────────────────────────────────────────────────────┤
#   │ L3 · 抓回来了，这条数据算新还是旧？                            │
#   │      → 内容指纹比对的去重                                     │
#   │      → 工具：业务字段哈希                                     │
#   └─────────────────────────────────────────────────────────────┘
#
# 这三层是**递进**的：L1 最省（不发请求），L2 次之（发请求但不读 body），
# L3 最贵（完整抓取）。优化顺序永远是 L1 → L2 → L3。


# ============================================================================
# 一、L2：HTTP 缓存协商 —— 最容易被忽略的省流量手段
# ============================================================================
@dataclass
class ConditionalGet:
    """条件请求的缓存状态。

    Attributes:
        etag: 服务端返回的 ETag（内容版本标识）。
        last_modified: 服务端返回的 Last-Modified 时间字符串。
        times_checked: 检查次数。
        times_not_modified: 命中 304 的次数（省下的完整下载次数）。

    很多人不知道 HTTP 有内建的「内容没变就别传了」机制：
      第一次请求，服务端返回 200 + 内容 + ETag: "abc123"
      第二次请求，带上 If-None-Match: "abc123"
      服务端比对后如果没变，返回 304 Not Modified（**没有 body**）

    省下的是**整个响应体**的传输。对一个 500KB 的页面，
    1000 次检查能省 500MB 流量 —— 而且 304 通常也不计入反爬的"内容请求"统计。

    为什么教程里很少讲？因为它需要服务端支持。
    但主流 CMS、电商、新闻站**大多支持**，值得先试一下。

    Last-Modified 的精度问题：它只到秒。如果内容在同一秒内多次变化，
    用 Last-Modified 会漏掉更新。所以**优先用 ETag**，
    ETag 没返回时再退回 Last-Modified。
    """

    etag: str = ""
    last_modified: str = ""
    times_checked: int = 0
    times_not_modified: int = 0

    def headers(self) -> dict[str, str]:
        """构造条件请求头。

        Returns:
            应附加到请求上的头字典；没有缓存信息时返回空字典。
        """
        h: dict[str, str] = {}
        if self.etag:
            h["If-None-Match"] = self.etag
        elif self.last_modified:
            # etag 优先 —— 它更精确
            h["If-Modified-Since"] = self.last_modified
        return h

    def update_from_response(self, status: int, headers: dict[str, str]) -> bool:
        """根据响应更新缓存状态。

        Args:
            status: HTTP 状态码。
            headers: 响应头（键大小写不敏感）。

        Returns:
            True 表示内容有更新（需要处理 body）；False 表示 304 未修改。
        """
        self.times_checked += 1
        low = {k.lower(): v for k, v in headers.items()}

        if status == 304:
            self.times_not_modified += 1
            return False

        self.etag = low.get("etag", "")
        self.last_modified = low.get("last-modified", "")
        return True

    @property
    def saved_rate(self) -> float:
        """304 命中率。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return (self.times_not_modified / self.times_checked
                if self.times_checked else 0.0)


class MockServer:
    """模拟一个支持 ETag 的服务端，用来演示条件请求的效果。

    这个模拟器的存在意义：**让我们能真实验证 304 的省流量效果**，
    而不是只背诵概念。
    """

    def __init__(self) -> None:
        """初始化模拟服务端。"""
        self._pages: dict[str, tuple[str, str]] = {}    # url → (content, etag)
        self.total_bytes_sent = 0
        self.request_count = 0

    def publish(self, url: str, content: str) -> None:
        """发布或更新一个页面（内容变化时 ETag 会变）。

        Args:
            url: 页面 URL。
            content: 页面内容。

        Returns:
            None
        """
        etag = f'"{hashlib.md5(content.encode()).hexdigest()[:16]}"'
        self._pages[url] = (content, etag)

    def get(self, url: str, req_headers: dict[str, str]) -> tuple[int, dict[str, str], str]:
        """处理一次请求（含条件请求逻辑）。

        Args:
            url: 请求的 URL。
            req_headers: 请求头。

        Returns:
            (状态码, 响应头, 响应体)。304 时响应体为空字符串。
        """
        self.request_count += 1
        if url not in self._pages:
            return 404, {}, ""

        content, etag = self._pages[url]
        low = {k.lower(): v for k, v in req_headers.items()}

        # 比对 If-None-Match
        if low.get("if-none-match") == etag:
            # 304：只发响应头（约 100 字节），不发 body
            self.total_bytes_sent += 100
            return 304, {"ETag": etag, "Content-Length": "0"}, ""

        body = content
        self.total_bytes_sent += len(body.encode()) + 100
        return 200, {"ETag": etag, "Content-Length": str(len(body))}, body


# ============================================================================
# 二、L3：内容指纹 —— 增量采集的核心
# ============================================================================
# 判断「这条数据变了没有」，本质是判断「两个状态是否相同」。
# 最直接的做法是逐字段比较，但字段一多就很啰嗦，而且不好存。
#
# 更好的做法：**把业务字段序列化后算哈希，得到一个定长指纹**。
#   指纹相同  → 内容没变
#   指纹不同  → 内容变了
#
# 关键在于：**哪些字段参与哈希？** 这决定了增量采集的准确度。
#
#   ✓ 应该参与：price / stock / title / rating / sales   （业务关心的）
#   ✗ 不该参与：crawled_at / updated_at / 页面访问量      （每次都变）
#   ✗ 不该参与：HTML 里的时间戳、随机 ID、广告位内容       （噪声）
#
# 一个真实的教训：如果指纹里包含了「抓取时间」，
# 那么每一条记录的指纹都不同，增量采集就完全失效 ——
# 你会以为全站都在变，实际什么都没变。

# 参与指纹计算的字段白名单。显式列出（而不是黑名单排除）更安全：
# 新增字段时不会意外影响指纹。
FINGERPRINT_FIELDS: tuple[str, ...] = (
    "title", "price", "stock", "rating", "sales",
)


def content_fingerprint(record: dict[str, Any],
                        fields: Sequence[str] = FINGERPRINT_FIELDS) -> str:
    """计算记录的内容指纹。

    Args:
        record: 记录字典。
        fields: 参与指纹的字段名序列。

    Returns:
        16 位十六进制指纹字符串。

    三个实现细节：
      ① **字段顺序必须固定**。用 sorted 或固定元组，
         否则同样的内容会算出不同指纹。
      ② **数值要归一化**。42.5 和 42.50 应该算同一个值，
         所以先转 float 再格式化。否则 '42.50' != '42.5'。
      ③ **只取前 16 位**。完整 md5 是 32 位，但碰撞概率在
         16 位（64 bit）下对千万级数据仍然足够低（约 1e-8），
         而且省一半存储。真要极致安全就用完整的 32 位。
    """
    parts: list[str] = []
    for f in sorted(fields):
        v = record.get(f)
        if v is None:
            parts.append("")
        elif isinstance(v, bool):
            parts.append("1" if v else "0")
        elif isinstance(v, (int, float)):
            # 归一化数值：统一保留 6 位小数，去掉尾部零
            parts.append(f"{float(v):.6f}".rstrip("0").rstrip("."))
        else:
            # 文本统一小写去空白，避免 'Apple ' 与 'apple' 被判为不同
            parts.append(str(v).strip().lower())
    raw = "|".join(parts)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class ChangeEvent:
    """一次内容变化事件。

    Attributes:
        url: 记录 URL。
        field: 变化的字段名。
        old: 旧值。
        new: 新值。
        detected_at: 检测时间。

    为什么需要它？
      只存「最终状态」的话，你只能回答「现在价格多少」，
      永远回答不了「这个月价格涨了几次」。
      而后者才是真正有价值的商业信息 ——
      价格监控、竞品分析、库存预警，全靠变化日志。
    """

    url: str
    field: str
    old: Any
    new: Any
    detected_at: str


class IncrementalStore:
    """支持增量采集的存储层（SQLite + 变化日志）。

    相比第 50 课的简单 upsert，这里多了三样东西：
      ① 内容指纹列 —— 快速判断是否需要更新
      ② 变化日志表 —— 记录每次字段级变动
      ③ 采集统计表 —— 记录每个 URL 的最后采集时间与次数
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS items (
        url         TEXT PRIMARY KEY,
        title       TEXT,
        price       REAL,
        stock       INTEGER,
        rating      REAL,
        sales       INTEGER,
        fingerprint TEXT NOT NULL,
        first_seen  TEXT NOT NULL,
        last_seen   TEXT NOT NULL,
        check_count INTEGER NOT NULL DEFAULT 1,
        change_count INTEGER NOT NULL DEFAULT 0
    );

    CREATE INDEX IF NOT EXISTS idx_items_fp ON items(fingerprint);

    -- 变化日志：只增不改，这是数据资产的真正价值所在
    CREATE TABLE IF NOT EXISTS change_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        url         TEXT NOT NULL,
        field       TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT,
        detected_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_log_url ON change_log(url);
    CREATE INDEX IF NOT EXISTS idx_log_time ON change_log(detected_at);
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        """初始化存储。

        Args:
            path: 数据库路径，默认内存库。
        """
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()
        self.stats = {"new": 0, "updated": 0, "unchanged": 0, "changes": 0}

    def ingest(self, record: dict[str, Any], *, now: str | None = None) -> str:
        """写入或更新一条记录，返回本次的处理结果。

        Args:
            record: 记录字典，必须含 url。
            now: 时间戳字符串，默认当前时间。

        Returns:
            'new' / 'updated' / 'unchanged' 三者之一。

        Raises:
            KeyError: 记录缺少 url 字段。
        """
        url = record["url"]
        ts = now or time.strftime("%Y-%m-%dT%H:%M:%S")
        fp = content_fingerprint(record)

        row = self.conn.execute(
            "SELECT * FROM items WHERE url = ?", (url,)
        ).fetchone()

        if row is None:
            self.conn.execute(
                """INSERT INTO items
                   (url, title, price, stock, rating, sales,
                    fingerprint, first_seen, last_seen, check_count, change_count)
                   VALUES (?,?,?,?,?,?,?,?,?,1,0)""",
                (url, record.get("title"), record.get("price"),
                 record.get("stock"), record.get("rating"), record.get("sales"),
                 fp, ts, ts),
            )
            self.stats["new"] += 1
            self.conn.commit()
            return "new"

        # 指纹相同 → 内容未变，只更新「最后看到时间」和检查次数
        if row["fingerprint"] == fp:
            self.conn.execute(
                """UPDATE items SET last_seen = ?, check_count = check_count + 1
                   WHERE url = ?""", (ts, url),
            )
            self.stats["unchanged"] += 1
            self.conn.commit()
            return "unchanged"

        # 指纹不同 → 找出具体是哪些字段变了，逐条写日志
        changes: list[ChangeEvent] = []
        for f in FINGERPRINT_FIELDS:
            old_v = row[f] if f in row.keys() else None
            new_v = record.get(f)
            if not _values_equal(old_v, new_v):
                changes.append(ChangeEvent(url, f, old_v, new_v, ts))
                self.conn.execute(
                    """INSERT INTO change_log
                       (url, field, old_value, new_value, detected_at)
                       VALUES (?,?,?,?,?)""",
                    (url, f, _to_text(old_v), _to_text(new_v), ts),
                )

        self.conn.execute(
            """UPDATE items SET
                 title=?, price=?, stock=?, rating=?, sales=?,
                 fingerprint=?, last_seen=?, check_count=check_count+1,
                 change_count=change_count+1
               WHERE url=?""",
            (record.get("title"), record.get("price"), record.get("stock"),
             record.get("rating"), record.get("sales"), fp, ts, url),
        )
        self.conn.commit()
        self.stats["updated"] += 1
        self.stats["changes"] += len(changes)
        return "updated"

    def ingest_many(self, records: Iterable[dict[str, Any]], *,
                    now: str | None = None) -> dict[str, int]:
        """批量写入。

        Args:
            records: 记录可迭代对象。
            now: 统一时间戳。

        Returns:
            本次批次的统计字典。
        """
        before = dict(self.stats)
        for r in records:
            self.ingest(r, now=now)
        return {k: self.stats[k] - before[k] for k in self.stats}

    def change_history(self, url: str) -> list[sqlite3.Row]:
        """查询某条记录的全部变化历史。

        Args:
            url: 记录 URL。

        Returns:
            变化日志行列表（按时间升序）。
        """
        return list(self.conn.execute(
            "SELECT * FROM change_log WHERE url = ? ORDER BY id", (url,)
        ))

    def price_history(self) -> list[sqlite3.Row]:
        """查询所有价格变化记录。

        Returns:
            价格变化日志行列表。
        """
        return list(self.conn.execute(
            "SELECT * FROM change_log WHERE field = 'price' ORDER BY id"
        ))

    def summary(self) -> dict[str, Any]:
        """汇总统计。

        Returns:
            含记录数、变化次数、平均检查次数等的字典。
        """
        r = self.conn.execute("""
            SELECT COUNT(*) AS n,
                   SUM(check_count) AS total_checks,
                   SUM(change_count) AS total_changes,
                   AVG(check_count) AS avg_checks
            FROM items
        """).fetchone()
        n = r["n"] or 0
        return {
            "items": n,
            "total_checks": r["total_checks"] or 0,
            "total_changes": r["total_changes"] or 0,
            "avg_checks": r["avg_checks"] or 0.0,
            "change_rate": ((r["total_changes"] or 0) / n) if n else 0.0,
        }


def _values_equal(a: Any, b: Any) -> bool:
    """比较两个值是否等价（含数值容差、None/空等价）。

    Args:
        a: 值 A。
        b: 值 B。

    Returns:
        True 表示等价。

    这个函数存在的理由：数据库里 price 是 REAL，读出来是 42.5；
    新数据从 JSON 来，可能是 42.5 或 "42.50" 或 42.499999999。
    直接用 != 会天天误报「价格变了」。
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        # 一方 None 一方有值：只有当有值那方是空字符串/0 时才算等价
        other = b if a is None else a
        return other in ("", 0, "0")
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return abs(float(a) - float(b)) < 1e-6
        # 字符串数字也比一下（"42.50" vs 42.5）
        fa, fb = float(a), float(b)
        return abs(fa - fb) < 1e-6
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _to_text(v: Any) -> str:
    """把任意值转成可存进 TEXT 列的字符串。

    Args:
        v: 任意值。

    Returns:
        字符串表示；None 转成空字符串。

    用空字符串而不是 'None' —— 后者在回读时无法和真的字符串
    'None' 区分开，会引入难以察觉的数据污染。
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


# ============================================================================
# 三、布隆过滤器：用极小内存做大规模判重
# ============================================================================
# 场景：你要抓 5000 万个 URL，判重需要记住所有已抓的 URL。
#   Python set 存 5000 万个 URL（平均 80 字节）≈ 4 GB 内存 —— 单机扛不住。
#   布隆过滤器用 100 MB 就能做到，代价是有一点点误判率。
#
# 布隆过滤器的两个性质：
#   ① **假阳性**：说"可能在集合里"，实际不在（概率 = 误判率）
#   ② **不会有假阴性**：说"一定不在集合里"，那就真的不在
#
# 这两个性质对爬虫判重**完全可接受**：
#   假阳性 → 某个 URL 被误判为"抓过了"，被跳过 → 漏抓少量数据
#   假阴性 → 不存在，所以不会重复抓
#
# 参数计算（经典公式）：
#   m = -(n * ln(p)) / (ln2)^2     位数组大小
#   k = (m / n) * ln2              哈希函数个数
#   其中 n = 预期元素数，p = 目标误判率


class BloomFilter:
    """布隆过滤器：空间高效的集合成员判断。

    实现用 Python 的整数做大位图（bitarray 的轻量替代），
    避免引入额外依赖。
    """

    def __init__(self, expected: int, fp_rate: float = 0.01) -> None:
        """初始化布隆过滤器。

        Args:
            expected: 预期的元素数量。
            fp_rate: 目标误判率（0 < fp_rate < 1）。

        Raises:
            ValueError: 参数非法时抛出。
        """
        if expected <= 0:
            raise ValueError("expected 必须为正整数")
        if not 0 < fp_rate < 1:
            raise ValueError("fp_rate 必须在 (0, 1) 开区间内")

        import math
        ln2 = math.log(2)
        # 位数组大小与哈希个数：布隆过滤器的标准最优解
        self.m = max(1, int(-(expected * math.log(fp_rate)) / (ln2 ** 2)))
        self.k = max(1, int((self.m / expected) * ln2))
        self.expected = expected
        self.fp_rate = fp_rate

        self._bits = bytearray((self.m + 7) // 8)
        self._added = 0

    def _hashes(self, item: str) -> Iterator[int]:
        """生成 k 个哈希位置。

        Args:
            item: 待哈希的字符串。

        Yields:
            位数组下标。

        实现要点：**不要用 k 个不同的哈希函数**（比如 md5、sha1、sha256...），
        太慢。标准做法是用两个独立哈希，然后线性组合出 k 个：
            h_i = h1 + i * h2
        这是 Kirsch-Mitzenmacher 优化，正确性有论文保证。
        """
        data = item.encode("utf-8")
        h1 = int.from_bytes(hashlib.md5(data).digest()[:8], "little")
        h2 = int.from_bytes(hashlib.sha1(data).digest()[:8], "little")
        h2 |= 1          # 保证 h2 是奇数，避免它和 m 有公因子导致哈希退化
        for i in range(self.k):
            yield (h1 + i * h2) % self.m

    def add(self, item: str) -> None:
        """加入一个元素。

        Args:
            item: 元素字符串。

        Returns:
            None
        """
        for pos in self._hashes(item):
            self._bits[pos >> 3] |= 1 << (pos & 7)
        self._added += 1

    def __contains__(self, item: str) -> bool:
        """判断元素是否可能存在。

        Args:
            item: 元素字符串。

        Returns:
            True 表示「可能存在」（有小概率是假阳性）；
            False 表示「一定不存在」（这个结果 100% 可靠）。
        """
        return all(
            self._bits[pos >> 3] & (1 << (pos & 7))
            for pos in self._hashes(item)
        )

    @property
    def memory_bytes(self) -> int:
        """位数组占用的字节数。

        Returns:
            字节数。
        """
        return len(self._bits)

    @property
    def fill_ratio(self) -> float:
        """位数组的填充率。

        Returns:
            0.0 - 1.0 的浮点数。

        填充率超过 50% 后误判率会快速上升，
        所以布隆过滤器**不能无限往里加元素** ——
        超出预期容量后必须重建（用更大的 m）。
        """
        return sum(bin(b).count("1") for b in self._bits) / self.m


# ============================================================================
# 四、前缀树（Trie）：URL 级别的结构化判重
# ============================================================================
class UrlTrie:
    """URL 前缀树 —— 用于按站点/路径批量判断是否访问过。

    什么时候需要 Trie 而不是 set？
      当你需要「某站点下所有已抓 URL」或者
      「这个路径前缀是否已经抓过」这类**结构化查询**时。
      纯 set 只能回答「这个完整字符串在不在」。
    """

    class Node:
        """Trie 节点。"""

        __slots__ = ("children", "is_end")

        def __init__(self) -> None:
            """初始化节点。"""
            self.children: dict[str, "UrlTrie.Node"] = {}
            self.is_end: bool = False

    def __init__(self) -> None:
        """初始化根节点。"""
        self.root = UrlTrie.Node()
        self.size = 0

    def add(self, url: str) -> bool:
        """插入一个 URL。

        Args:
            url: URL 字符串。

        Returns:
            True 表示新插入；False 表示之前已存在。
        """
        node = self.root
        for ch in url:
            if ch not in node.children:
                node.children[ch] = UrlTrie.Node()
            node = node.children[ch]
        if node.is_end:
            return False
        node.is_end = True
        self.size += 1
        return True

    def __contains__(self, url: str) -> bool:
        """判断 URL 是否已插入。

        Args:
            url: URL 字符串。

        Returns:
            True 表示已存在。
        """
        node = self.root
        for ch in url:
            node = node.children.get(ch)      # type: ignore[assignment]
            if node is None:
                return False
        return node.is_end

    def count_prefix(self, prefix: str) -> int:
        """统计以某前缀开头的 URL 数量。

        Args:
            prefix: 前缀字符串。

        Returns:
            URL 数量。
        """
        node = self.root
        for ch in prefix:
            node = node.children.get(ch)      # type: ignore[assignment]
            if node is None:
                return 0

        # DFS 统计子树里所有 is_end 节点
        total = 0
        stack = [node]
        while stack:
            cur = stack.pop()
            if cur.is_end:
                total += 1
            stack.extend(cur.children.values())
        return total

    def memory_estimate(self) -> int:
        """估算内存占用（字节）。

        Returns:
            估算的字节数。

        Trie 的**缺点**就是内存：每个字符一个 dict 节点，
        远比 set 存整个字符串浪费。所以实践中：
          · 判重为主    → 用 set 或布隆过滤器
          · 需要前缀查询 → 才用 Trie
        不要因为 Trie「听起来高级」就无脑用。
        """
        return self.size * 8 + self._count_nodes() * 200

    def _count_nodes(self) -> int:
        """统计节点总数。

        Returns:
            节点数。
        """
        total = 0
        stack = [self.root]
        while stack:
            cur = stack.pop()
            total += 1
            stack.extend(cur.children.values())
        return total


# ============================================================================
# 实验区
# ============================================================================
def _make_page(pid: int, price: float, stock: int = 10) -> str:
    """生成一个模拟商品页面的内容。

    Args:
        pid: 商品 ID。
        price: 价格。
        stock: 库存。

    Returns:
        页面内容字符串。

    注意这里**故意包含一个时间戳**（模拟真实页面的"最后更新"文字），
    用来演示"指纹字段选择不当会怎样"。
    """
    return (f"<html><body><h1>Product {pid}</h1>"
            f"<span class='price'>£{price:.2f}</span>"
            f"<span class='stock'>{stock} in stock</span>"
            f"<div class='ts'>Updated: {time.strftime('%H:%M:%S')}</div>"
            f"</body></html>")


def exp1_conditional_get() -> None:
    """实验 1：HTTP 条件请求 —— 省下整个 body。"""
    print("=" * 74)
    print("实验 1 · L2 层：HTTP 条件请求（ETag / 304）")
    print("=" * 74)

    server = MockServer()
    for i in range(1, 11):
        server.publish(f"https://shop.local/p/{i}", _make_page(i, 100.0 + i))

    # 第一次：全量抓取，建立缓存
    cache: dict[str, ConditionalGet] = {}
    print("\n  --- 第 1 轮：首次全量抓取 ---")
    for i in range(1, 11):
        url = f"https://shop.local/p/{i}"
        cg = ConditionalGet()
        status, headers, body = server.get(url, cg.headers())
        cg.update_from_response(status, headers)
        cache[url] = cg
    bytes_round1 = server.total_bytes_sent
    print(f"    请求 {server.request_count} 次，传输 {bytes_round1:,} 字节")

    # 第二轮：什么都没变
    server.total_bytes_sent = 0
    server.request_count = 0
    for i in range(1, 11):
        url = f"https://shop.local/p/{i}"
        cg = cache[url]
        status, headers, body = server.get(url, cg.headers())
        cg.update_from_response(status, headers)
    bytes_round2 = server.total_bytes_sent
    print(f"\n  --- 第 2 轮：内容全未变 ---")
    print(f"    请求 {server.request_count} 次，传输 {bytes_round2:,} 字节")
    print(f"    ▸ 全部命中 304，省下 {100 * (1 - bytes_round2 / bytes_round1):.0f}% 流量")

    # 第三轮：改 3 个页面的价格
    for i in (2, 5, 8):
        server.publish(f"https://shop.local/p/{i}", _make_page(i, 999.0))
    server.total_bytes_sent = 0
    server.request_count = 0
    fresh_count = 0
    for i in range(1, 11):
        url = f"https://shop.local/p/{i}"
        cg = cache[url]
        status, headers, body = server.get(url, cg.headers())
        if cg.update_from_response(status, headers):
            fresh_count += 1
    bytes_round3 = server.total_bytes_sent
    print(f"\n  --- 第 3 轮：改了 3 个页面的价格 ---")
    print(f"    请求 {server.request_count} 次，传输 {bytes_round3:,} 字节")
    print(f"    只有 {fresh_count} 个返回 200（真正变了），其余 304")
    print(f"    ▸ 精确定位到变化：只下载了 {fresh_count}/10 个页面")

    # 汇总
    total_checked = sum(c.times_checked for c in cache.values())
    total_304 = sum(c.times_not_modified for c in cache.values())
    print(f"\n  --- 累计效果 ---")
    print(f"    总检查 {total_checked} 次，其中 {total_304} 次命中 304 "
          f"（{total_304 / total_checked:.1%}）")

    print("\n  ▸ 三个必须知道的点：")
    print("      ① ETag 优先于 Last-Modified（后者只精确到秒）")
    print("      ② 304 响应通常不计入「内容请求」的反爬统计，更安全")
    print("      ③ 不是所有站点都支持。要先探测：第一次请求看有没有 ETag 头")
    print("\n  ▸ 为什么这个手段被严重低估？")
    print("      大部分爬虫教程只讲「怎么发请求」，不讲「怎么不发请求」。")
    print("      而在真实项目里，**减少请求比优化请求重要得多** ——")
    print("      它同时降低了成本、被发现概率、以及对目标站点的压力。")


def exp2_fingerprint() -> None:
    """实验 2：内容指纹 —— 哪些字段该参与哈希。"""
    print("\n" + "=" * 74)
    print("实验 2 · L3 层：内容指纹的字段选择")
    print("=" * 74)

    base = {"url": "https://shop.local/p/1", "title": "Widget",
            "price": 42.5, "stock": 10, "rating": 4.5, "sales": 100}

    print("\n  --- 字段变化的敏感度测试 ---")
    cases: list[tuple[str, dict[str, Any], bool]] = [
        ("完全相同的记录", dict(base), False),
        ("price 42.5 → 42.50（数值等价）", {**base, "price": 42.50}, False),
        ("price 42.5 → 42.6（真的变了）", {**base, "price": 42.6}, True),
        ("title 大小写变化 'widget'", {**base, "title": "widget"}, False),
        ("title 尾随空格 'Widget '", {**base, "title": "Widget "}, False),
        ("title 真的改了", {**base, "title": "Widget Pro"}, True),
        ("stock 10 → 0（缺货）", {**base, "stock": 0}, True),
        ("多了一个无关字段 extra='x'", {**base, "extra": "x"}, False),
        ("抓取时间变了（不应参与）", {**base, "crawled_at": "2026-09-19"}, False),
    ]

    fp0 = content_fingerprint(base)
    print(f"    基准指纹：{fp0}")
    print(f"\n    {'场景':<34}{'新指纹':<18}{'判定':<10}{'是否符合预期'}")
    print("    " + "-" * 72)
    for label, rec, should_change in cases:
        fp = content_fingerprint(rec)
        changed = fp != fp0
        ok = "✓" if changed == should_change else "✗"
        verdict = "已变化" if changed else "未变化"
        print(f"    {label:<34}{fp:<18}{verdict:<10}{ok}")

    print("\n  ▸ 关键设计原则：")
    print("      ① **只包含业务字段**。crawled_at / updated_at 这类")
    print("         每次都变的字段放进去，会让增量采集完全失效。")
    print("      ② **数值要归一化**。42.5 和 42.50 必须等价，")
    print("         否则价格字段会天天误报「变了」。")
    print("      ③ **文本要规范化**。大小写、首尾空格不该算变化 ——")
    print("         这是第 52 课 L1 清洗的直接应用。")
    print("      ④ **用白名单而非黑名单**。新增字段时不会意外污染指纹。")

    # ---- 演示指纹字段选错的后果 ----
    print("\n  --- 反例：如果指纹里包含了「抓取时间」---")
    bad_fields = ("title", "price", "stock", "rating", "sales", "crawled_at")
    store = IncrementalStore()
    r1 = {**base}
    store.ingest(r1, now="2026-09-19T10:00:00")

    # 连续 5 次抓取，内容完全相同，但 crawled_at 每次都变
    print(f"    {'抓取时间':<22}{'含 crawled_at':<16}{'不含 crawled_at'}")
    print("    " + "-" * 56)
    for i in range(1, 6):
        ts = f"2026-09-19T1{i}:00:00"
        # 含 crawled_at 的指纹
        with_ts = content_fingerprint({**r1, "crawled_at": ts}, bad_fields)
        # 不含的
        without_ts = content_fingerprint(r1)
        tag1 = "判定为变化!" if with_ts != content_fingerprint(
            {**r1, "crawled_at": "2026-09-19T10:00:00"}, bad_fields) else "未变化"
        print(f"    {ts:<22}{tag1:<16}{'未变化 ✓'}")
    print("\n    ▸ 含 crawled_at 的指纹每次都不同 → 每次都触发「更新」→")
    print("      增量采集彻底失效，你依然在全量重写数据库。")
    print("      这就是「字段选错，功能全废」的典型案例。")


def exp3_incremental_rounds() -> None:
    """实验 3：多轮增量采集 —— 完整生命周期演示。"""
    print("\n" + "=" * 74)
    print("实验 3 · 多轮增量采集（含变化日志）")
    print("=" * 74)

    store = IncrementalStore()

    rounds: list[tuple[str, list[dict[str, Any]], str]] = [
        ("第 1 天 · 首次全量", [
            {"url": "https://shop.local/p/1", "title": "Widget", "price": 42.50,
             "stock": 10, "rating": 4.5, "sales": 100},
            {"url": "https://shop.local/p/2", "title": "Gadget", "price": 31.99,
             "stock": 5, "rating": 5.0, "sales": 250},
            {"url": "https://shop.local/p/3", "title": "Doohickey", "price": 55.00,
             "stock": 2, "rating": 4.8, "sales": 80},
        ], "2026-09-15T09:00:00"),

        ("第 2 天 · 什么都没变", [
            {"url": "https://shop.local/p/1", "title": "Widget", "price": 42.50,
             "stock": 10, "rating": 4.5, "sales": 100},
            {"url": "https://shop.local/p/2", "title": "Gadget", "price": 31.99,
             "stock": 5, "rating": 5.0, "sales": 250},
            {"url": "https://shop.local/p/3", "title": "Doohickey", "price": 55.00,
             "stock": 2, "rating": 4.8, "sales": 80},
        ], "2026-09-16T09:00:00"),

        ("第 3 天 · p/1 降价、p/2 缺货、新增 p/4", [
            {"url": "https://shop.local/p/1", "title": "Widget", "price": 39.99,
             "stock": 10, "rating": 4.5, "sales": 130},      # 降价 + 销量增
            {"url": "https://shop.local/p/2", "title": "Gadget", "price": 31.99,
             "stock": 0, "rating": 5.0, "sales": 250},       # 缺货
            {"url": "https://shop.local/p/3", "title": "Doohickey", "price": 55.00,
             "stock": 2, "rating": 4.8, "sales": 80},
            {"url": "https://shop.local/p/4", "title": "Thingamajig", "price": 88.00,
             "stock": 7, "rating": 4.0, "sales": 15},        # 全新
        ], "2026-09-17T09:00:00"),

        ("第 4 天 · p/3 涨价、其余不变", [
            {"url": "https://shop.local/p/1", "title": "Widget", "price": 39.99,
             "stock": 10, "rating": 4.5, "sales": 130},
            {"url": "https://shop.local/p/2", "title": "Gadget", "price": 31.99,
             "stock": 0, "rating": 5.0, "sales": 250},
            {"url": "https://shop.local/p/3", "title": "Doohickey", "price": 59.99,
             "stock": 2, "rating": 4.8, "sales": 85},        # 涨价
            {"url": "https://shop.local/p/4", "title": "Thingamajig", "price": 88.00,
             "stock": 7, "rating": 4.0, "sales": 15},
        ], "2026-09-18T09:00:00"),
    ]

    for label, records, ts in rounds:
        diff = store.ingest_many(records, now=ts)
        print(f"\n  {label}")
        print(f"    新增 {diff['new']}  更新 {diff['updated']}  "
              f"未变 {diff['unchanged']}  字段级变化 {diff['changes']} 处")

    # ---- 汇总 ----
    s = store.summary()
    print(f"\n  --- 累计统计 ---")
    print(f"    记录总数        : {s['items']}")
    print(f"    总检查次数      : {s['total_checks']}")
    print(f"    总变化次数      : {s['total_changes']}")
    print(f"    平均检查次数    : {s['avg_checks']:.2f}")
    print(f"    变化率          : {s['change_rate']:.2f} 次/记录")

    print(f"\n  ▸ 4 轮 × 4 条 = 16 次检查，只发生了 {s['total_changes']} 次真正的变化。")
    writes = s['items'] + s['total_changes']     # 首次插入 4 次 + 变化更新 3 次
    print(f"    不做增量：16 次检查 → 16 次写库（每次都全量重写）")
    print(f"    做增量  ：16 次检查 → {writes} 次写库"
          f"（首次插入 {s['items']} + 真正变化 {s['total_changes']}），"
          f"省 {100 * (1 - writes / 14):.0f}%")
    print(f"    数据量越大、变化越少，收益越明显 ——")
    print(f"    真实场景里周级更新率通常低于 5%，收益接近 20 倍。")

    # ---- 变化日志：增量采集的真正价值 ----
    print("\n  --- 价格变化历史（这是最有价值的产出）---")
    print(f"    {'URL':<28}{'旧价':>9}{'新价':>9}{'变化':>9}  时间")
    print("    " + "-" * 74)
    for row in store.price_history():
        try:
            old_f, new_f = float(row["old_value"]), float(row["new_value"])
            delta = f"{(new_f - old_f) / old_f * 100:+.1f}%"
        except (TypeError, ValueError):
            delta = "-"
        print(f"    {row['url']:<28}{row['old_value']:>9}{row['new_value']:>9}"
              f"{delta:>9}  {row['detected_at']}")

    print("\n  ▸ 关键认知：**只存最终状态，你永远做不了趋势分析。**")
    print("    上面的价格历史能回答「涨跌了几次、幅度多大」，")
    print("    而一张只有当前价格的表什么都回答不了。")
    print("    变化日志（change_log）应该只增不改 ——")
    print("    它是你的数据资产，删了就再也回不来了。")

    # ---- 单条记录的完整时间线 ----
    print("\n  --- p/1 的完整变化时间线 ---")
    for row in store.change_history("https://shop.local/p/1"):
        print(f"    {row['detected_at']}  {row['field']:<8} "
              f"{row['old_value']!r:>10} → {row['new_value']!r}")


def exp4_bloom_filter() -> None:
    """实验 4：布隆过滤器 —— 小内存做大判重。"""
    print("\n" + "=" * 74)
    print("实验 4 · 布隆过滤器：空间与误判的权衡")
    print("=" * 74)

    # ---- 参数计算 ----
    print("\n  --- 不同规模下的参数与内存 ---")
    print(f"    {'预期元素':>12}{'误判率':>10}{'位数组':>14}{'哈希数':>8}{'内存':>12}")
    print("    " + "-" * 58)
    for n, p in [(1_000, 0.01), (100_000, 0.01), (1_000_000, 0.01),
                 (10_000_000, 0.01), (10_000_000, 0.001),
                 (50_000_000, 0.01)]:
        bf = BloomFilter(n, p)
        m_bits = bf.m
        mem = bf.memory_bytes / 1024 / 1024
        print(f"    {n:>12,}{p:>10}{m_bits:>14,}{bf.k:>8}{mem:>10.1f} MB")

    print("\n  ▸ 对比：用 Python set 存 5000 万个 URL（平均 80 字节）")
    print(f"      ≈ {50_000_000 * 80 / 1024 / 1024 / 1024:.1f} GB 内存")
    bf50 = BloomFilter(50_000_000, 0.01)
    print(f"    布隆过滤器只需 {bf50.memory_bytes / 1024 / 1024:.0f} MB "
          f"（省 {50_000_000 * 80 / bf50.memory_bytes:.0f} 倍）")
    print(f"    代价：{bf50.fp_rate:.1%} 的误判率")

    # ---- 误判率实测 ----
    print("\n  --- 误判率实测（理论 vs 实际）---")
    print(f"    {'预期n':>9}{'目标p':>8}{'实测FP':>10}{'理论FP':>10}{'填充率':>9}")
    print("    " + "-" * 46)
    for n in (10_000, 50_000):
        bf = BloomFilter(n, 0.01)
        # 加入 n 个元素
        for i in range(n):
            bf.add(f"https://shop.local/item/{i}")
        # 用 n 个**确定不在**集合里的元素测试
        fp = sum(1 for i in range(n, 2 * n)
                 if f"https://shop.local/item/{i}" in bf)
        actual = fp / n
        # 理论误判率：p_actual = (1 - e^(-kn/m))^k
        import math
        p_theory = (1 - math.exp(-bf.k * n / bf.m)) ** bf.k
        print(f"    {n:>9,}{0.01:>8.3f}{actual:>10.4f}{p_theory:>10.4f}"
              f"{bf.fill_ratio:>9.1%}")

    print("\n  ▸ 实测误判率和理论值吻合，说明实现正确。")
    print("    ⚠ 但注意：**布隆过滤器没有删除操作**（标准版本）。")
    print("      填充率超过 50% 后误判率会急剧上升，")
    print("      所以实际使用要预留 2 倍余量，或者用可计数的变体。")

    # ---- 关键性质验证：没有假阴性 ----
    print("\n  --- 关键性质验证：没有假阴性 ---")
    bf = BloomFilter(1000, 0.01)
    added = [f"u{i}" for i in range(500)]
    for a in added:
        bf.add(a)
    missed = sum(1 for a in added if a not in bf)
    print(f"    加入 500 个元素，其中被判为「不在集合里」的有 {missed} 个")
    print(f"    → {missed} = 0 说明**不存在假阴性** ✓")
    print("\n  ▸ 这个性质对爬虫判重至关重要：")
    print("      假阳性 → 误跳过少量 URL（可接受，损失很小）")
    print("      假阴性 → 会重复抓取已抓过的 URL（不可接受，浪费配额）")
    print("    布隆过滤器刚好只出现前者 —— 这就是它适合判重的原因。")


def exp5_trie_and_dedup() -> None:
    """实验 5：三层去重的工程实现对比。"""
    print("\n" + "=" * 74)
    print("实验 5 · 三层去重的实现与开销对比")
    print("=" * 74)

    # ---- 生成测试 URL 集合（含大量重复）----
    sites = ["shop-a.local", "shop-b.local", "shop-c.local"]
    urls: list[str] = []
    for s in sites:
        for i in range(2000):
            urls.append(f"https://{s}/p/{i}")
    # 故意让 40% 是重复的
    urls = urls + urls[:int(len(urls) * 0.4)]
    print(f"\n  测试数据：{len(urls):,} 个 URL，其中约 40% 是重复")
    print(f"  唯一 URL 数：{len(set(urls)):,}")

    # ---- 方案 A：Python set ----
    t0 = time.perf_counter()
    seen: set[str] = set()
    new_a = 0
    for u in urls:
        if u not in seen:
            seen.add(u)
            new_a += 1
    t_a = time.perf_counter() - t0
    mem_a = sum(len(u) + 49 for u in seen)     # 粗略：字符串 + set 槽位开销

    # ---- 方案 B：布隆过滤器 ----
    t0 = time.perf_counter()
    bf = BloomFilter(len(set(urls)), 0.01)
    new_b = 0
    for u in urls:
        if u not in bf:
            bf.add(u)
            new_b += 1
    t_b = time.perf_counter() - t0

    # ---- 方案 C：Trie ----
    t0 = time.perf_counter()
    trie = UrlTrie()
    new_c = 0
    for u in urls:
        if trie.add(u):
            new_c += 1
    t_c = time.perf_counter() - t0

    print(f"\n  {'方案':<18}{'识别为新':>12}{'准确':>8}{'耗时':>14}{'内存(粗估)':>14}")
    print("  " + "-" * 68)
    print(f"  {'A · Python set':<18}{new_a:>12,}{'基准':>8}{t_a * 1000:>11.1f} ms"
          f"{mem_a / 1024 / 1024:>11.1f} MB")
    fp_flag = "有误判" if new_b != new_a else "准确"
    print(f"  {'B · 布隆过滤器':<18}{new_b:>12,}{fp_flag:>8}{t_b * 1000:>11.1f} ms"
          f"{bf.memory_bytes / 1024 / 1024:>11.1f} MB")
    print(f"  {'C · Trie 前缀树':<18}{new_c:>12,}{'准确':>8}{t_c * 1000:>11.1f} ms"
          f"{trie.memory_estimate() / 1024 / 1024:>11.1f} MB")

    print(f"\n  ▸ 数据量只有 {len(urls):,}，set 完全够用 —— 这正说明")
    print("    **不要过早优化**。小规模下 set 最快最准最简单。")
    print("    布隆过滤器的价值要到千万级、内存吃紧时才体现。")

    # ---- Trie 的独有能力 ----
    print("\n  --- Trie 的独有能力：前缀统计 ---")
    for prefix in ["https://shop-a.local/", "https://shop-b.local/p/1",
                   "https://shop-c.local/p/19", "https://nonexistent/"]:
        n = trie.count_prefix(prefix)
        print(f"    '{prefix}'")
        print(f"      → 以它为前缀的已抓 URL 有 {n:,} 个")

    print("\n  ▸ set 做不到这个查询（它只认完整字符串相等）。")
    print("    实用场景：「这个站点/这个栏目的抓取进度如何？」")
    print("    导航站、分页站做覆盖率统计时很好用。")

    print("\n  ▸ 最终选型建议：")
    print("      · < 100 万 URL        → Python set（最简单，最快）")
    print("      · 100万 ~ 1亿 URL      → 布隆过滤器（省内存）")
    print("      · 需要前缀/层级查询     → Trie（但内存开销大）")
    print("      · 多机共享            → Redis 的 SET / PFCOUNT(HLL)")


def exp6_resume() -> None:
    """实验 6：断点续爬 —— 让任务可以随时中断与恢复。"""
    print("\n" + "=" * 74)
    print("实验 6 · 断点续爬：进度持久化")
    print("=" * 74)

    tmp = Path(tempfile.mkdtemp()) / "resume.db"
    conn = sqlite3.connect(tmp)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS crawl_state (
            url        TEXT PRIMARY KEY,
            status     TEXT NOT NULL,   -- pending / done / failed
            attempts   INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON crawl_state(status)")
    conn.commit()

    all_urls = [f"https://big.local/page/{i}" for i in range(20)]

    def save_pending(urls: Sequence[str], ts: str) -> None:
        """把待抓 URL 写入状态表（幂等）。

        Args:
            urls: URL 列表。
            ts: 时间戳。

        Returns:
            None
        """
        conn.executemany(
            """INSERT INTO crawl_state(url, status, attempts, updated_at)
               VALUES (?, 'pending', 0, ?)
               ON CONFLICT(url) DO NOTHING""",
            [(u, ts) for u in urls],
        )
        conn.commit()

    def next_batch(limit: int = 5) -> list[str]:
        """取下一批待抓 URL。

        Args:
            limit: 批次大小。

        Returns:
            URL 列表。

        排序策略很重要：先取 attempts 少的（新任务），
        再按 url 排序（保证稳定顺序，便于复现）。
        如果任务失败多次，可以把它降级放到最后 —— 或者直接标记为死信。
        """
        rows = conn.execute(
            """SELECT url FROM crawl_state
               WHERE status = 'pending' AND attempts < 3
               ORDER BY attempts ASC, url ASC
               LIMIT ?""", (limit,)
        ).fetchall()
        return [r[0] for r in rows]

    def mark(url: str, status: str, err: str = "") -> None:
        """更新 URL 状态。

        Args:
            url: URL。
            status: 新状态。
            err: 错误信息。

        Returns:
            None
        """
        conn.execute(
            """UPDATE crawl_state
               SET status = ?, attempts = attempts + 1,
                   last_error = ?, updated_at = ?
               WHERE url = ?""",
            (status, err, time.strftime("%Y-%m-%dT%H:%M:%S"), url),
        )
        conn.commit()

    def progress() -> dict[str, int]:
        """统计各状态的数量。

        Returns:
            状态 → 数量 的字典。
        """
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM crawl_state GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ---- 第一次运行：处理到一半崩溃 ----
    print("\n  --- 第 1 次运行：处理 8 个后模拟崩溃 ---")
    save_pending(all_urls, "2026-09-19T10:00:00")
    print(f"    登记 {len(all_urls)} 个待抓 URL，状态：{progress()}")

    processed = 0
    while True:
        batch = next_batch()
        if not batch:
            break
        for u in batch:
            # 模拟：第 7 个开始遇到网络故障
            if processed >= 7:
                mark(u, "failed", "连接超时")
                break
            mark(u, "done")
            processed += 1
        if processed >= 7:
            print(f"    💥 处理到第 {processed} 个时崩溃（模拟网络故障）")
            break

    print(f"    崩溃时状态：{progress()}")

    # ---- 第二次运行：从断点恢复 ----
    print("\n  --- 第 2 次运行：从断点恢复 ---")
    resumed = 0
    while True:
        batch = next_batch()
        if not batch:
            break
        for u in batch:
            resumed += 1
            # 模拟：这次网络恢复了，但有个别 URL 永久失效
            # 注意：上面崩溃时卡住的那个 URL（attempts=1）也会被重新取出来处理
            if u.endswith("/7"):
                mark(u, "failed", "404 Not Found（页面已删除）")
                continue
            mark(u, "done")
    print(f"    恢复处理 {resumed} 个 URL")
    print(f"    最终状态：{progress()}")
    print("\n    ▸ 注意：第一次崩溃时那个失败的 URL 被**自动重试**了 ——")
    print("      因为它的 attempts=1 < 3，仍在 next_batch() 的候选集里。")

    # ---- 结果核对 ----
    print("\n  --- 核对 ---")
    done = conn.execute(
        "SELECT COUNT(*) FROM crawl_state WHERE status='done'"
    ).fetchone()[0]
    failed_rows = conn.execute(
        "SELECT url, attempts, last_error FROM crawl_state WHERE status='failed'"
    ).fetchall()
    print(f"    成功 {done}/{len(all_urls)}")
    print(f"    失败 {len(failed_rows)} 个：")
    for u, att, err in failed_rows:
        print(f"      {u}  （尝试 {att} 次）{err}")

    print("\n  ▸ 断点续爬的四个设计要点：")
    print("      ① **状态表独立于数据表**。数据表管内容，状态表管进度。")
    print("      ② **幂等登记**。用 ON CONFLICT DO NOTHING，重复登记不出错。")
    print("      ③ **attempts 计数 + 上限**。失败 3 次就停，避免死循环。")
    print("         但要**记录 last_error**，否则你永远不知道失败原因。")
    print("      ④ **状态机清晰**：pending → done / failed。")
    print("         复杂场景还需要 doing（正在处理）状态来防止重复处理。")

    print("\n  ▸ 为什么「doing」状态很重要？")
    print("      如果进程在 mark(u, 'doing') 之后、mark(u, 'done') 之前崩溃，")
    print("      重启时这个 URL 就卡在 doing 状态 ——")
    print("      所以要记录 updated_at，把超过 N 分钟还是 doing 的重新置为 pending。")
    print("      这叫「超时回收」，是分布式任务的必备机制（第 6 阶段会再讲）。")

    conn.close()


def main() -> None:
    """运行全部实验。"""
    exp1_conditional_get()
    exp2_fingerprint()
    exp3_incremental_rounds()
    exp4_bloom_filter()
    exp5_trie_and_dedup()
    exp6_resume()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 增量采集分三层：URL 去重(不发请求) → 条件请求(不发 body) → 指纹比对",
        "2. 优化顺序永远是 L1 → L2 → L3：减少请求比优化请求更重要",
        "3. ETag 优先于 Last-Modified；先探测服务端是否支持条件请求",
        "4. 内容指纹只包含业务字段 —— 混入时间戳会让增量采集彻底失效",
        "5. 指纹里的数值必须归一化（42.5 == 42.50），文本要去空白转小写",
        "6. 指纹字段用白名单而非黑名单，新增字段不会污染指纹",
        "7. change_log 只增不改，它才是数据资产的真正价值",
        "8. 只存最终状态 → 只能回答「现在多少」，答不了「涨了几次」",
        "9. 布隆过滤器：只能有假阳性，不会有假阴性 —— 恰好适配判重场景",
        "10. 布隆过滤器不支持删除，填充率 >50% 误判率急升，要预留余量",
        "11. 小规模用 set 就够了，不要过早优化成布隆过滤器",
        "12. Trie 的独有价值是前缀统计，但内存开销远大于 set",
        "13. 断点续爬要独立状态表 + attempts 上限 + last_error 记录",
        "14. 需要 doing 状态 + 超时回收，才能处理「处理到一半崩溃」的情况",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
