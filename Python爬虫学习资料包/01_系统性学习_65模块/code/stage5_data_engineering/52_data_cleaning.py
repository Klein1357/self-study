"""
第 52 课 · 数据清洗 —— 爬虫数据的「垃圾进、垃圾出」防线

================================ 学习目标 ================================
1. 建立「清洗不是一次性动作，而是四级流水线」的认知
2. 掌握字符串清洗：空白、全半角、HTML 实体、控制字符
3. 掌握数值解析：价格/销量/百分比/带单位数字的通用提取器
4. 掌握日期时间解析：多种格式、相对时间（"3天前"）、时区
5. 掌握电话/邮箱/URL/ID 的规范化与脱敏
6. 建立数据质量报告（Data Quality Report）—— 让问题可见

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/52_data_cleaning.py

零额外依赖，仅用标准库 + re + unicodedata。
所有"脏数据"都是内置的真实世界样本（来自各种站点的真实坑）。
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

# ============================================================================
# 认知框架：清洗是四级流水线
# ============================================================================
# 很多人把「清洗」理解成"写几个正则把脏字符去掉"。这是最低级的理解。
#
# 真实的清洗流水线有四级，每一级的职责完全不同：
#
#   L1 · 规范化（Normalize）
#        把"同一种东西的不同写法"统一成一种。
#        例：'  ＡＢＣ  ' → 'ABC'；'&amp;' → '&'；'２０２４年' → '2024年'
#        目标：让后续比较/判重能生效
#
#   L2 · 抽取（Extract）
#        从"人看的文本"里提取"机器要的值"。
#        例：'£12.99 - £18.50' → (12.99, 18.50)；'1,234 条评论' → 1234
#        目标：把非结构化变成结构化
#
#   L3 · 校验（Validate）
#        判断值是否合理，不合规则标记/丢弃。
#        例：价格 -5 元（不可能）；评分 8.7（满分 5）；日期 2026-13-45
#        目标：拦住脏数据，而不是让它进库
#
#   L4 · 报告（Report）
#        统计每一级的"修复率"和"丢弃率"，让数据质量问题可见。
#        目标：**这一级最容易被忽略，但它决定你能否持续维护清洗逻辑**
#
# 关键原则：**每一级都必须可独立测试**。
# 把四级揉成一个 500 行的 clean_all() 是维护灾难 ——
# 三个月后站点改版，你根本不知道该改哪一行。


# ============================================================================
# 一、L1 规范化：字符串清洗
# ============================================================================
# 全角字符映射表。中文站点尤其常见：
#   '１２３' （全角） ≠ '123'（半角）
#   判重时会认为是两个不同的值，导致重复数据。
_FULLWIDTH_OFFSET = 0xFEE0


def normalize_unicode(text: str) -> str:
    """Unicode 规范化 + 全角转半角 + 去除控制字符。

    Args:
        text: 原始文本。

    Returns:
        规范化后的文本。

    做三件事：
      1. NFKC 规范化
         Unicode 里有多种"看起来一样但码点不同"的字符。
         典型：'Ⅳ'（罗马数字，U+2163）和 'IV'（两个字母）。
         NFKC 把它们统一。这一步能解决大量"肉眼看不出区别"的判重失败。
      2. 全角 → 半角
         NFKC 已经处理了大部分，但**数字和字母**的全角转换
         NFKC 确实会做，而**中文标点**（，。！？）不会 —— 那些是独立字符，
         不是全角的 ASCII。所以还需要手动处理全角 ASCII 区间。
      3. 去除控制字符
         \x00-\x1f 里除了 \t \n \r 之外都是不可见垃圾。
         常见来源：从 PDF 复制的文本、编码错误的网页。
         它们会让字符串比较莫名其妙失败，且 print 出来看不见。
    """
    if not text:
        return ""

    # 步骤 1：NFKC 规范化（处理罗马数字、上下标、连字等）
    text = unicodedata.normalize("NFKC", text)

    # 步骤 2：全角 ASCII（U+FF01-U+FF5E）→ 半角（U+0021-U+007E）
    out_chars: list[str] = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - _FULLWIDTH_OFFSET))
        elif code == 0x3000:            # 全角空格 U+3000
            out_chars.append(" ")
        else:
            out_chars.append(ch)
    text = "".join(out_chars)

    # 步骤 3：去控制字符，但保留 \t \n \r（它们有排版语义）
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    return text


# 空白压缩：把连续的空白（含 \n \t \u00a0 不间断空格）压成单个空格
#
# 为什么要专门处理 \u00a0（NBSP，不间断空格）？
# 因为它**长得和普通空格一模一样**，但 '\xa0' != ' '。
# 从网页复制下来的文本里非常常见 —— 尤其是带千分位的价格
# '1 234.56'，中间那个可能是 NBSP。不处理的话：
#   int('1\xa0234') → ValueError
#   而 '1 234' == '1 234' 又是 False，判重失效
_WS_RE = re.compile(r"[\s\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+")


def normalize_whitespace(text: str) -> str:
    """压缩空白并去除首尾空格。

    Args:
        text: 输入文本。

    Returns:
        压缩后的文本。

    注意 re 的 \\s 在 Python 3 里默认匹配 Unicode 空白，
    但不包含 \\u00a0 等特殊空格（它们属于 Zs 类但不被 \\s 覆盖，
    取决于 re.UNICODE 的具体实现），所以上面显式列出来了。
    这是踩过坑之后的写法：不要相信 \\s 能覆盖所有空白。
    """
    return _WS_RE.sub(" ", text).strip()


def decode_html_entities(text: str) -> str:
    """解码 HTML 实体。

    Args:
        text: 可能含实体的文本。

    Returns:
        解码后的文本。

    用标准库 html.unescape 而不是自己写映射表 ——
    它处理了 2000+ 个实体以及数字实体（&#163; / &#x00A3;）。
    自己写一定会漏。

    为什么爬虫需要这一步？
      用 requests + BS4 时通常已经自动解码了，但两种情况例外：
        ① 用正则直接从原始 HTML 抓内容（没经过解析器）
        ② 页面把实体**双重转义**了（&amp;amp; → 实际想要 &amp;）
      第二种情况在 JSON 接口返回 HTML 片段时特别常见。
    """
    return html.unescape(text)


# 统一标点：中文标点 → 英文标点
#
# 什么时候该做？**只在做判重/匹配时做，展示时保留原文。**
# 因为把 '书名：爬虫' 变成 '书名:爬虫' 会损失中文排版习惯。
# 但做 URL/ID 匹配时，标点必须统一。
_PUNCT_MAP = str.maketrans({
    "，": ",", "。": ".", "；": ";", "：": ":", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]", "《": "<", "》": ">",
    "“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "－": "-",
    "～": "~", "、": ",", "％": "%", "＃": "#", "＆": "&", "＊": "*",
})


def normalize_punctuation(text: str) -> str:
    """把中文标点换成英文标点。

    Args:
        text: 输入文本。

    Returns:
        标点统一后的文本。
    """
    return text.translate(_PUNCT_MAP)


def clean_text(text: str | None, *, keep_case: bool = False) -> str:
    """L1 文本清洗总入口。

    Args:
        text: 原始文本，可能是 None（爬虫字段缺失很常见）。
        keep_case: 是否保留大小写。默认 False（转小写便于匹配）。

    Returns:
        清洗后的文本；输入为 None 时返回空字符串。

    注意返回空字符串而非 None ——
    这能让后续所有 len(x) / x.lower() / x.split() 都不用判 None，
    减少大量 if 分支。这是"把 None 挡在流水线入口"的实践。
    """
    if text is None:
        return ""
    s = normalize_unicode(text)
    s = decode_html_entities(s)
    s = normalize_whitespace(s)
    if not keep_case:
        s = s.lower()
    return s


# ============================================================================
# 二、L2 抽取：数值解析
# ============================================================================
# 这是爬虫清洗里最麻烦的一块。价格字段的真实形态五花八门：
#   '£12.99'          '￥1,234.56'      '12.99 元'
#   '$1,234'          '12,99'（欧式小数点） 'Free'      '价格面议'
#   '12.99 - 18.50'   'From £9.99'      '£12.99*'
#
# 一个健壮的解析器需要处理：货币符号、千分位、小数点变体、区间、
# 前后缀文字、以及"根本没有数字"的情况。

# 货币符号 → ISO 代码。中文站点常见 ￥/¥/元/人民币
#
# ⚠ 这里有一个非常隐蔽的坑，务必注意键的顺序：
#
#   '￥' 是 U+FFE5（全角日元/人民币符号）
#   '¥' 是 U+00A5（半角日元符号）
#
#   而 NFKC 规范化会把 U+FFE5 **折叠成 U+00A5**！
#   也就是说：'￥1,234.56' 经过 clean_text() 之后变成 '¥1,234.56'，
#   原本想识别成 CNY 的，会被 '¥' 抢先匹配成 JPY。
#
#   解决办法（本函数采用）：**货币识别必须在规范化之前做**，
#   在原始文本上匹配。见 parse_price() 里的步骤 3。
#   这里保留两个键是给"已经规范化过的文本"兜底用的。
CURRENCY_SYMBOLS: dict[str, str] = {
    "￥": "CNY",        # 全角，中文站最常见
    "¥": "CNY",        # 半角中文环境也常用人民币
    "£": "GBP", "€": "EUR", "$": "USD",
    "元": "CNY", "人民币": "CNY",
    "美金": "USD", "美元": "USD",
    "hkd": "HKD", "港币": "HKD", "新台币": "TWD", "twd": "TWD",
}

# 在**原始文本**（未规范化）上做货币识别的映射。
# 这里可以精确区分 U+FFE5 和 U+00A5 —— 规范化之后就区分不了了。
CURRENCY_SYMBOLS_RAW: dict[str, str] = {
    "\uffe5": "CNY",     # ￥ 全角
    "\u00a5": "JPY",     # ¥ 半角（国际标准里这是日元）
    "\u5143": "CNY",     # 元
    "\u4eba\u6c11\u5e01": "CNY",
    "\u7f8e\u5143": "USD",
    "\u6e2f\u5e01": "HKD",
    "\uffe1": "GBP",     # ￡ 全角英镑
    "\u00a3": "GBP",     # £
    "\u20ac": "EUR",     # €
    "\u0024": "USD",     # $
    "\uff04": "USD",     # ＄ 全角美元
}

# 表示"没有价格"的文案
NO_PRICE_MARKERS = {
    "free", "免费", "面议", "价格面议", "暂无报价", "n/a", "na",
    "-", "--", "", "coming soon", "敬请期待", "sold out", "已售罄",
}


@dataclass
class PriceResult:
    """价格解析结果。

    Attributes:
        value: 解析出的数值；无法解析时为 None。
        currency: ISO 货币代码；未识别时为空字符串。
        is_range: 是否为价格区间。
        high: 区间上限（is_range 为 True 时有意义）。
        raw: 原始文本。
        reason: 无法解析时的原因说明。

    为什么不直接返回 float | None？
      因为调用方需要区分"这个字段本来就没有价格"和"我解析失败了"。
      前者是正常业务，后者是 bug 需要报警 —— 用同一个 None 表示会混淆。
      `reason` 字段让"解析失败"这件事变得可观测。
    """

    value: float | None = None
    currency: str = ""
    is_range: bool = False
    high: float | None = None
    raw: str = ""
    reason: str = ""

    @property
    def ok(self) -> bool:
        """是否解析成功。

        Returns:
            True 表示拿到了有效数值。
        """
        return self.value is not None


def _to_float(num_str: str) -> float | None:
    """把数字字符串转成 float，自动处理千分位与小数分隔符变体。

    Args:
        num_str: 形如 '1,234.56' / '1.234,56' / '12 345,67' 的字符串。

    Returns:
        float 值；无法解析时返回 None。

    核心难点：**逗号到底是千分位还是小数点？**
      '1,234'   → 英文习惯：1234
      '1,23'    → 欧式习惯：1.23
      '1,234.56'→ 英文：1234.56
      '1.234,56'→ 欧式：1234.56

    判断规则（业界通用启发式）：
      ① 如果同时有 '.' 和 ',' → 后出现的是小数点
      ② 如果只有 ','：
         - 逗号后恰好 3 位数字**且**逗号前有数字 → 视为千分位（1234）
         - 否则视为小数点（1,23 → 1.23）
      ③ 如果只有 '.'：通常就是小数点；但 '1.234' 在欧式里是 1234
         —— 这个歧义无法消除，只能靠站点上下文，这里默认按小数点处理
    """
    s = num_str.strip()
    if not s:
        return None

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        # 规则 ①：后出现的那个是小数点
        if s.rfind(",") > s.rfind("."):
            # 欧式：1.234,56 → 去掉 '.'，把 ',' 换成 '.'
            s = s.replace(".", "").replace(",", ".")
        else:
            # 英文：1,234.56 → 去掉 ','
            s = s.replace(",", "")
    elif has_comma:
        # 规则 ②：看逗号后面的位数
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 3 and parts[0].isdigit():
            s = s.replace(",", "")          # 千分位
        else:
            s = s.replace(",", ".")         # 欧式小数点
    # 规则 ③：只有 '.' 的按小数点处理，无需改动

    # 去掉残留的空白（有些站点用空格做千分位：'12 345.67'）
    s = s.replace(" ", "").replace("\u00a0", "")

    try:
        return float(s)
    except ValueError:
        return None


# 数字 + 可选区间。用 finditer 而不是 search，才能拿到区间两端。
_NUM_RE = re.compile(r"\d[\d\s\u00a0.,]*\d|\d")


def parse_price(text: str | None) -> PriceResult:
    """从任意文本里解析价格。

    Args:
        text: 原始价格文本，如 'From £12.99'、'￥1,234 - ￥2,000'。

    Returns:
        PriceResult 对象，含数值、币种、区间信息。

    解析流程：
      1. 清洗 → 小写、去实体、压空白
      2. 检查"无价格"标记（Free / 面议 / 已售罄）
      3. **在原始文本上**识别货币符号（关键！见 CURRENCY_SYMBOLS_RAW 的注释）
      4. 提取所有数字
      5. 1 个数字 → 单价；2 个数字 → 区间；0 个 → 失败
    """
    raw = text or ""
    res = PriceResult(raw=raw)

    if not raw.strip():
        res.reason = "空值"
        return res

    # 步骤 3 必须用 raw（原始文本），不能用 s（清洗后）
    # 因为 NFKC 会把 ￥(U+FFE5) 折叠成 ¥(U+00A5)，丢失 CNY/JPY 的区分。
    for sym in sorted(CURRENCY_SYMBOLS_RAW, key=len, reverse=True):
        if sym in raw:
            res.currency = CURRENCY_SYMBOLS_RAW[sym]
            break

    s = clean_text(raw)

    # 步骤 2：无价格标记
    stripped = s.strip(" .。*·")
    if stripped in NO_PRICE_MARKERS:
        res.reason = f"无价格标记：{stripped!r}"
        return res

    # 步骤 3 兜底：如果原始文本里没识别到，再在清洗后的文本里试一次
    # （覆盖 'HKD 120' 这类用字母代码表示的情况）
    if not res.currency:
        for sym, code in CURRENCY_SYMBOLS.items():
            if sym in s:
                res.currency = code
                break

    # 步骤 4：提取数字（最多取 2 个，即区间）
    nums: list[float] = []
    for m in _NUM_RE.finditer(s):
        v = _to_float(m.group())
        if v is not None:
            nums.append(v)
        if len(nums) >= 2:
            break

    # 步骤 5：判定
    if not nums:
        res.reason = "未找到数字"
        return res
    if len(nums) == 1:
        res.value = nums[0]
        return res

    res.value = min(nums)
    res.high = max(nums)
    res.is_range = True
    return res


@dataclass
class CountResult:
    """数量类解析结果（销量、评论数、浏览量）。

    Attributes:
        value: 数值。
        unit: 识别到的单位（'k'/'w'/'万' 等）。
        multiplier: 使用的倍数。
        raw: 原始文本。
    """

    value: float | None = None
    unit: str = ""
    multiplier: float = 1.0
    raw: str = ""

    @property
    def ok(self) -> bool:
        """是否解析成功。

        Returns:
            True 表示拿到了有效数值。
        """
        return self.value is not None


# 中文/英文数量单位 → 倍数
COUNT_UNITS: dict[str, float] = {
    "k": 1e3, "K": 1e3, "千": 1e3,
    "w": 1e4, "W": 1e4, "万": 1e4, "萬": 1e4,
    "m": 1e6, "M": 1e6, "百万": 1e6,
    "亿": 1e8, "億": 1e8, "b": 1e9, "B": 1e9,
}


def parse_count(text: str | None) -> CountResult:
    """解析带单位的数量，如 '1.2k'、'3.5万'、'1,234 条评论'。

    Args:
        text: 原始文本。

    Returns:
        CountResult 对象。

    这是一个高频需求：电商的销量、社交的点赞数、
    评论数几乎都用缩写。'3.5万' 必须变成 35000，
    否则你做不了任何数值比较和排序。

    注意 'w' 在中文语境下是"万"，在英文语境下可能是别的意思
    （比如 weight）。所以生产环境应该结合站点判断，
    这里给出通用实现并标注这个局限。
    """
    raw = text or ""
    res = CountResult(raw=raw)
    if not raw.strip():
        return res

    s = clean_text(raw)

    # 先找 "数字 + 可选单位"
    m = re.search(r"(\d[\d\s.,]*)\s*([a-zA-Z千万萬亿億])?", s)
    if not m:
        return res

    num = _to_float(m.group(1))
    if num is None:
        return res

    unit = m.group(2) or ""
    mult = COUNT_UNITS.get(unit, 1.0)
    res.value = num * mult
    res.unit = unit
    res.multiplier = mult
    return res


# ============================================================================
# 三、L2 抽取：日期时间解析
# ============================================================================
# 日期是清洗里第二麻烦的字段。真实世界的日期文本：
#   '2024-01-15'              ISO 标准
#   '2024/01/15'              斜杠分隔
#   '15/01/2024'              欧式日月年（歧义！）
#   '01/15/2024'              美式月日年（歧义！）
#   '2024年1月15日'            中文
#   'Jan 15, 2024'           英文缩写
#   '15 Jan 2024'            英式
#   '3天前'                   相对时间
#   '2 hours ago'            英文相对
#   '昨天' / '刚刚' / '今天'
#   '2024-01-15T10:30:00Z'    带时区
#   '1712345678'              Unix 时间戳
#
# **最危险的是 '15/01/2024' vs '01/15/2024'。**
# 这两个格式无法从单条数据判断，必须：
#   ① 从同站点的大量数据里找 "> 12 的位置" 来推断
#   ② 或者看站点所属地区（.uk → 日月年，.com → 月日年不可靠）
# 本课实现会用"探测法"演示 ①。

MONTH_NAMES: dict[str, int] = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


@dataclass
class DateResult:
    """日期解析结果。

    Attributes:
        value: 解析出的 datetime（naive，无时区）；失败为 None。
        fmt: 匹配到的格式名称，便于统计与排查。
        raw: 原始文本。
        ambiguous: 是否命中过"日月/月日"歧义格式。
    """

    value: datetime | None = None
    fmt: str = ""
    raw: str = ""
    ambiguous: bool = False

    @property
    def ok(self) -> bool:
        """是否解析成功。

        Returns:
            True 表示拿到了有效日期。
        """
        return self.value is not None


def parse_relative(text: str, *, now: datetime | None = None) -> DateResult:
    """解析相对时间文本（'3天前' / '2 hours ago' / '昨天'）。

    Args:
        text: 相对时间文本。
        now: 基准时间，默认当前时间（显式传入便于测试）。

    Returns:
        DateResult 对象。

    相对时间的坑：**它依赖抓取时刻**。
      '3天前' 在 1 月 1 日抓 = 12 月 29 日；在 1 月 10 日抓 = 1 月 7 日。
    所以：
      ① 必须记录抓取时间（这就是第 50 课的 created_at 的用途之一）
      ② 重跑历史数据时，用当时的 created_at 当基准，而不是 datetime.now()
         —— 否则同一份页面间隔一个月抓两次，会算出两个不同的日期
      这正是 `now` 参数存在的意义：让调用方能传入"抓取时刻"而非"当前时刻"。
    """
    raw = text or ""
    res = DateResult(raw=raw)
    if not raw.strip():
        return res

    base = now or datetime.now()
    s = clean_text(raw)

    # 中文特殊词
    special = {
        "刚刚": timedelta(seconds=0), "刚才": timedelta(seconds=0),
        "今天": timedelta(0), "今日": timedelta(0),
        "昨天": timedelta(days=1), "昨日": timedelta(days=1),
        "前天": timedelta(days=2), "明天": timedelta(days=-1),
    }
    for word, delta in special.items():
        if word in s:
            res.value = base - delta
            res.fmt = f"中文特殊词:{word}"
            return res

    # 中文："N单位前"
    cn_units = {"秒": "seconds", "分钟": "minutes", "分": "minutes",
                "小时": "hours", "时": "hours", "天": "days", "日": "days",
                "周": "weeks", "星期": "weeks", "个月": "days", "月": "days",
                "年": "days"}
    m = re.search(r"(\d+)\s*(秒|分钟|分|小时|时|天|日|周|星期|个月|月|年)\s*前", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        key = cn_units[unit]
        # 月按 30 天、年按 365 天近似 —— 这是"精度换简单"的取舍
        if unit == "个月":
            delta = timedelta(days=30 * n)
        elif unit == "年":
            delta = timedelta(days=365 * n)
        else:
            delta = timedelta(**{key: n})
        res.value = base - delta
        res.fmt = f"中文相对:{n}{unit}前"
        return res

    # 英文："N units ago"
    en_units = {"second": "seconds", "sec": "seconds", "s": "seconds",
                "minute": "minutes", "min": "minutes", "m": "minutes",
                "hour": "hours", "hr": "hours", "h": "hours",
                "day": "days", "d": "days",
                "week": "weeks", "w": "weeks",
                "month": "days", "year": "days"}
    m = re.search(r"(\d+)\s*(second|sec|minute|min|hour|hr|day|week|month|year|s|m|h|d|w)s?\s*ago", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "month":
            delta = timedelta(days=30 * n)
        elif unit == "year":
            delta = timedelta(days=365 * n)
        else:
            delta = timedelta(**{en_units[unit]: n})
        res.value = base - delta
        res.fmt = f"英文相对:{n} {unit} ago"
        return res

    return res


def parse_datetime(text: str | None, *, dayfirst: bool | None = None,
                   now: datetime | None = None) -> DateResult:
    """解析绝对日期时间。

    Args:
        text: 原始日期文本。
        dayfirst: 对 '15/01/2024' 这类歧义格式，是否按「日/月/年」解析。
            None 表示按默认（美式，即月/日/年）解析，并标记 ambiguous。
        now: 相对时间的基准（透传给 parse_relative）。

    Returns:
        DateResult 对象。

    实现顺序很重要：**先试相对时间，再试绝对格式**。
    因为 '3天前' 也能被某些宽松的日期解析器"猜"成一个荒谬的日期，
    先做精确匹配能避免这类误判。
    """
    raw = text or ""
    res = DateResult(raw=raw)
    if not raw.strip():
        return res

    # ---- 第一步：相对时间 ----
    rel = parse_relative(raw, now=now)
    if rel.ok:
        return rel

    s = clean_text(raw)

    # ---- 第二步：Unix 时间戳（10 位秒 / 13 位毫秒）----
    if re.fullmatch(r"\d{10}", s):
        res.value = datetime.fromtimestamp(int(s))
        res.fmt = "unix_seconds"
        return res
    if re.fullmatch(r"\d{13}", s):
        res.value = datetime.fromtimestamp(int(s) / 1000)
        res.fmt = "unix_millis"
        return res

    # ---- 第三步：中文 '2024年1月15日 10:30' ----
    m = re.search(
        r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?"
        r"(?:\s*(\d{1,2})\s*[:时]\s*(\d{1,2})(?:\s*[:分]\s*(\d{1,2}))?)?", s
    )
    if m:
        try:
            res.value = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
            )
            res.fmt = "中文:YYYY年M月D日"
            return res
        except ValueError as e:
            res.fmt = f"中文格式但值非法:{e}"
            return res

    # ---- 第四步：ISO 8601（含时区）----
    # ⚠ 坑：s 经过 clean_text 已经**转成小写**了（'2024-01-15t10:30:00z'），
    #   所以正则里的 T/Z 必须写成小写，或者用 re.IGNORECASE。
    #   这里显式用 re.IGNORECASE 更保险，因为调用方可能传 keep_case 的文本进来。
    iso = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[t ](\d{2}):(\d{2})(?::(\d{2}))?"
        r"(z|[+-]\d{2}:?\d{2})?$", s, re.IGNORECASE
    )
    if iso:
        try:
            dt = datetime(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3)),
                int(iso.group(4)), int(iso.group(5)), int(iso.group(6) or 0),
            )
            tzpart = iso.group(7)
            if tzpart:
                tzlow = tzpart.lower()
                if tzlow == "z":
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    sign = 1 if tzlow[0] == "+" else -1
                    hh = int(tzlow[1:3])
                    mm = int(tzlow[-2:])
                    dt = dt.replace(tzinfo=timezone(
                        timedelta(hours=sign * hh, minutes=sign * mm)))
                # 统一转 UTC 再去掉时区，保证所有时间可比
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            res.value = dt
            res.fmt = "iso8601" + ("_tz" if tzpart else "")
            return res
        except ValueError as e:
            res.fmt = f"ISO 格式但值非法:{e}"
            return res

    # ---- 第五步：纯日期 '2024-01-15' / '2024/01/15' / '2024.01.15' ----
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            res.value = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            res.fmt = "ymd"
            return res
        except ValueError as e:
            res.fmt = f"ymd 但值非法:{e}"
            return res

    # ---- 第六步：歧义格式 '15/01/2024' ----
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        res.ambiguous = True
        # 如果 a > 12，那它一定是"日"；反之无法判断
        if a > 12:
            d, mo = a, b
            res.fmt = "dmy(推断)"
        elif b > 12:
            mo, d = a, b
            res.fmt = "mdy(推断)"
        else:
            # 两边都 <= 12，只能靠调用方给的 dayfirst
            if dayfirst:
                d, mo = a, b
                res.fmt = "dmy(默认)"
            else:
                mo, d = a, b
                res.fmt = "mdy(默认)"
        try:
            res.value = datetime(y, mo, d)
        except ValueError as e:
            res.fmt += f" 但值非法:{e}"
        return res

    # ---- 第七步：英文月份 'Jan 15, 2024' / '15 Jan 2024' ----
    m = re.search(r"([a-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})", s)
    if m and m.group(1) in MONTH_NAMES:
        try:
            res.value = datetime(int(m.group(3)), MONTH_NAMES[m.group(1)], int(m.group(2)))
            res.fmt = "mon_d_y"
            return res
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})\s+([a-z]{3,9})\.?,?\s+(\d{4})", s)
    if m and m.group(2) in MONTH_NAMES:
        try:
            res.value = datetime(int(m.group(3)), MONTH_NAMES[m.group(2)], int(m.group(1)))
            res.fmt = "d_mon_y"
            return res
        except ValueError:
            pass

    res.fmt = "未匹配任何格式"
    return res


def infer_dayfirst(samples: Sequence[str]) -> dict[str, Any]:
    """从一批日期样本中推断站点使用的是「日月年」还是「月日年」。

    Args:
        samples: 日期字符串列表。

    Returns:
        含 dayfirst / evidence / confidence 的字典。

    这是解决歧义格式的唯一正确做法：**用统计而非猜测**。
    原理：
      如果站点用「日/月/年」，那么第一个位置会出现 13-31 的值。
      如果站点用「月/日/年」，那么第一个位置永远不会超过 12，
      而第二个位置会出现 13-31。

    这个函数就是"数据本身会告诉你答案"的典型案例 ——
    不要靠站点域名去猜，拿数据说话。
    """
    first_gt12 = 0
    second_gt12 = 0
    total = 0
    for s in samples:
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", (s or "").strip())
        if not m:
            continue
        total += 1
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12:
            first_gt12 += 1
        if b > 12:
            second_gt12 += 1

    if total == 0:
        return {"dayfirst": False, "evidence": "无样本，使用默认(月日年)",
                "confidence": 0.0}

    if first_gt12 > 0 and second_gt12 == 0:
        return {"dayfirst": True,
                "evidence": f"第一个位置出现 >12 的值 {first_gt12} 次，"
                            f"第二个位置从未出现 → 必为「日/月/年」",
                "confidence": min(1.0, first_gt12 / 3)}
    if second_gt12 > 0 and first_gt12 == 0:
        return {"dayfirst": False,
                "evidence": f"第二个位置出现 >12 的值 {second_gt12} 次，"
                            f"第一个位置从未出现 → 必为「月/日/年」",
                "confidence": min(1.0, second_gt12 / 3)}
    if first_gt12 > 0 and second_gt12 > 0:
        return {"dayfirst": False,
                "evidence": f"两个位置都出现过 >12（{first_gt12}/{second_gt12}）→ "
                            f"样本混杂了两种格式，数据源不干净",
                "confidence": 0.0}
    return {"dayfirst": False,
            "evidence": f"{total} 条样本全在 1-12 范围内，无法区分 → 需要更多数据",
            "confidence": 0.0}


# ============================================================================
# 四、L2 抽取：联系方式
# ============================================================================
# 手机号在不同来源里的写法：
#   '13812345678'        '138 1234 5678'      '138-1234-5678'
#   '+86 138 1234 5678'  '0086 13812345678'   '(86)13812345678'
#
# 规范化目标：统一成 '13812345678'（去掉所有分隔与国码）
#
# ⚠ 合规提示：抓取手机号属于《个人信息保护法》定义的「个人信息」，
#    批量爬取可能触犯《刑法》253 条之一。本课仅演示**技术上的规范化**，
#    不代表这类抓取是合法的。参见第 49 课。

_CN_MOBILE_RE = re.compile(r"(?:\+?86|0086)?[\s\-()]*1[3-9]\d[\s\-]*\d{4}[\s\-]*\d{4}")


def normalize_phone_cn(text: str | None) -> str:
    """把中国大陆手机号规范成 11 位纯数字。

    Args:
        text: 可能含手机号的文本。

    Returns:
        11 位手机号；未匹配则返回空字符串。
    """
    if not text:
        return ""
    m = _CN_MOBILE_RE.search(text)
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group())
    # 去掉国码前缀
    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits if len(digits) == 11 else ""


def mask_phone(phone: str) -> str:
    """手机号脱敏（保留前 3 后 4）。

    Args:
        phone: 11 位手机号。

    Returns:
        形如 '138****5678' 的字符串。

    为什么爬虫要主动做脱敏？
      如果你的数据库里存了明文手机号，一旦泄露要承担法律责任。
      而绝大多数分析场景根本不需要完整号码 —— 前 3 后 4 足以做去重统计。
      **默认脱敏，确有需要再单独加密存储**，是风险最小的策略。
    """
    if len(phone) != 11:
        return phone
    return f"{phone[:3]}****{phone[-4:]}"


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def normalize_email(text: str | None) -> str:
    """从文本里提取并规范化邮箱。

    Args:
        text: 输入文本。

    Returns:
        小写邮箱地址；未匹配返回空字符串。

    规范化动作：
      ① 小写（邮箱域名部分不区分大小写，本地部分理论上区分，
         但现实中 99.9% 不区分，统一小写便于判重）
      ② Gmail 的 '+' 别名：a+tag@gmail.com 和 a@gmail.com 是同一个邮箱。
         做用户去重时应该剥掉 —— 但要注意只有部分服务商支持，
         无脑剥掉会破坏其他邮箱的地址。
    """
    if not text:
        return ""
    m = _EMAIL_RE.search(text)
    if not m:
        return ""
    addr = m.group().lower()
    # 只对明确支持 + 别名的服务商做处理
    local, _, domain = addr.partition("@")
    if domain in ("gmail.com", "googlemail.com") and "+" in local:
        local = local.split("+", 1)[0]
        addr = f"{local}@{domain}"
    return addr


def mask_email(email: str) -> str:
    """邮箱脱敏。

    Args:
        email: 邮箱地址。

    Returns:
        形如 'a***@gmail.com' 的字符串。
    """
    local, _, domain = email.partition("@")
    if not domain:
        return email
    if len(local) <= 1:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


# URL 规范化：爬虫判重的关键
#
# 同一个页面可能有十几种 URL 写法：
#   http://a.com/p/1        https://a.com/p/1
#   https://a.com/p/1/      https://a.com/p/1?utm_source=x
#   https://A.COM/p/1       https://a.com/p/1#comments
#
# 不规范化 → 判重失效 → 重复抓取 → 浪费配额还可能被封
#
# 规范化动作清单：
#   ① 统一协议（http → https，或反之）
#   ② 域名小写
#   ③ 去掉默认端口（:80 / :443）
#   ④ 去除 URL fragment（#...）—— 客户端锚点，不影响服务端返回
#   ⑤ 去除追踪参数（utm_* / fbclid / gclid / spm 等）
#   ⑥ 排序剩余查询参数（?b=2&a=1 与 ?a=1&b=2 是同一资源）
#   ⑦ 统一尾部斜杠策略（本项目统一去掉）
#
# 注意第 ⑥ 条有风险：有些站点参数顺序是有语义的。
# 生产环境应该先小样本验证再全量启用。

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "dclid", "msclkid",
    "spm", "scm", "from", "share_source", "share_medium", "ref", "referer",
    "_ga", "_gl", "yclid", "igshid", "mc_cid", "mc_eid",
}


def normalize_url(url: str | None, *, keep_params: bool = True) -> str:
    """规范化 URL，使同一资源只有一种表示。

    Args:
        url: 原始 URL。
        keep_params: 是否保留非追踪查询参数。设为 False 会激进地丢弃所有参数。

    Returns:
        规范化后的 URL；无法解析时返回去空格的原文。

    用 urllib.parse 而不是正则 —— URL 的规则比看起来复杂得多，
    正则一定会漏掉边角情况（比如 user:pass@host、IPv6 地址）。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    if not url:
        return ""
    u = url.strip()

    try:
        parts = urlsplit(u)
    except ValueError:
        return u

    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()

    # 去默认端口
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # 处理查询参数：去追踪 + 排序
    query = ""
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if not keep_params:
            pairs = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
        else:
            pairs = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMS]
        pairs.sort()
        query = urlencode(pairs)

    # fragment 一律丢弃：它只在浏览器端有意义
    path = parts.path or "/"
    path = re.sub(r"/{2,}", "/", path)          # 合并重复斜杠
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")                  # 统一去掉尾部斜杠

    return urlunsplit((scheme, netloc, path, query, ""))


# ============================================================================
# 五、L3 校验
# ============================================================================
@dataclass
class Rule:
    """单条校验规则。

    Attributes:
        name: 规则名。
        checker: 接收值、返回 (是否通过, 说明) 的函数。
        severity: 'error' 表示必须丢弃；'warn' 表示标记但保留。
    """

    name: str
    checker: Callable[[Any], tuple[bool, str]]
    severity: str = "error"


@dataclass
class ValidationIssue:
    """一条校验问题记录。

    Attributes:
        field: 字段名。
        rule: 触发的规则名。
        severity: 严重级别。
        detail: 说明。
        value: 触发问题的原始值。
    """

    field: str
    rule: str
    severity: str
    detail: str
    value: Any


@dataclass
class ValidationResult:
    """校验结果汇总。

    Attributes:
        issues: 所有问题列表。
        passed: 是否有 error 级问题。
    """

    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """是否通过校验（无 error 级问题）。

        Returns:
            True 表示通过。
        """
        return not any(i.severity == "error" for i in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        """仅 error 级问题。

        Returns:
            ValidationIssue 列表。
        """
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """仅 warn 级问题。

        Returns:
            ValidationIssue 列表。
        """
        return [i for i in self.issues if i.severity == "warn"]


def _rule_price_sane(v: Any) -> tuple[bool, str]:
    """校验价格在合理范围。

    Args:
        v: 价格值。

    Returns:
        (是否通过, 说明)。
    """
    if v is None:
        return True, ""
    if v < 0:
        return False, f"价格为负（{v}）—— 物理上不可能，通常是解析把 '-' 当成了负号"
    if v > 10_000_000:
        return False, f"价格异常大（{v}）—— 可能是把「数量」误当成了「价格」"
    if v == 0:
        return False, "价格为 0 —— 通常是解析失败被填成了默认值"
    return True, ""


def _rule_rating_range(v: Any) -> tuple[bool, str]:
    """校验评分在 0-5（或 0-10）范围内。

    Args:
        v: 评分值。

    Returns:
        (是否通过, 说明)。
    """
    if v is None:
        return True, ""
    if v < 0:
        return False, f"评分为负（{v}）"
    if v > 10:
        return False, f"评分超过 10（{v}）—— 星级系统最多 5，十分制最多 10"
    if v > 5:
        return True, ""      # 十分制，仅提示不拦截
    return True, ""


def _rule_title_not_empty(v: Any) -> tuple[bool, str]:
    """校验标题非空。

    Args:
        v: 标题值。

    Returns:
        (是否通过, 说明)。
    """
    s = (v or "").strip()
    if not s:
        return False, "标题为空 —— 解析器可能失效了"
    if len(s) < 2:
        return False, f"标题过短（{s!r}）—— 大概率是解析到了错误节点"
    if s.lower() in {"null", "none", "n/a", "undefined", "nan"}:
        return False, f"标题是占位值（{s!r}）—— 服务端返回了默认值"
    return True, ""


def _rule_url_valid(v: Any) -> tuple[bool, str]:
    """校验 URL 形似合法。

    Args:
        v: URL 值。

    Returns:
        (是否通过, 说明)。
    """
    s = (v or "").strip()
    if not s:
        return False, "URL 为空"
    if not s.startswith(("http://", "https://")):
        return False, f"URL 缺少协议头（{s[:50]!r}）—— 可能是相对路径没拼接"
    if re.search(r"\s", s):
        return False, f"URL 含空白字符（{s[:50]!r}）—— 说明拼接时没做编码"
    return True, ""


def _rule_date_not_future(v: Any) -> tuple[bool, str]:
    """校验日期不在未来。

    Args:
        v: datetime 值。

    Returns:
        (是否通过, 说明)。
    """
    if v is None:
        return True, ""
    if isinstance(v, datetime) and v > datetime.now() + timedelta(days=1):
        return False, f"日期在未来（{v:%Y-%m-%d}）—— 注意「相对时间」算错基准会产生这种值"
    return True, ""


RULES: dict[str, Rule] = {
    "title_not_empty": Rule("title_not_empty", _rule_title_not_empty),
    "url_valid": Rule("url_valid", _rule_url_valid),
    "price_sane": Rule("price_sane", _rule_price_sane, severity="warn"),
    "rating_range": Rule("rating_range", _rule_rating_range, severity="warn"),
    "date_not_future": Rule("date_not_future", _rule_date_not_future),
}

# 字段 → 适用规则
FIELD_RULES: dict[str, list[str]] = {
    "title": ["title_not_empty"],
    "url": ["url_valid"],
    "price": ["price_sane"],
    "rating": ["rating_range"],
    "published_at": ["date_not_future"],
}


def validate_record(record: dict[str, Any]) -> ValidationResult:
    """对一条记录跑全部适用规则。

    Args:
        record: 字段字典。

    Returns:
        ValidationResult 对象。
    """
    result = ValidationResult()
    for fname, rnames in FIELD_RULES.items():
        if fname not in record:
            continue
        value = record[fname]
        for rname in rnames:
            rule = RULES[rname]
            ok, detail = rule.checker(value)
            if not ok:
                result.issues.append(ValidationIssue(
                    field=fname, rule=rule.name,
                    severity=rule.severity, detail=detail, value=value,
                ))
    return result


# ============================================================================
# 六、L4 数据质量报告
# ============================================================================
@dataclass
class FieldStats:
    """单个字段的清洗统计。

    Attributes:
        name: 字段名。
        total: 总记录数。
        present: 非空记录数。
        changed: 清洗后值发生变化的记录数（说明脏数据有多少）。
        parse_failed: 解析失败数。
    """

    name: str
    total: int = 0
    present: int = 0
    changed: int = 0
    parse_failed: int = 0

    @property
    def coverage(self) -> float:
        """字段覆盖率（非空比例）。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.present / self.total if self.total else 0.0

    @property
    def dirty_rate(self) -> float:
        """脏数据率（需要清洗的比例）。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.changed / self.present if self.present else 0.0


@dataclass
class QualityReport:
    """数据质量报告。

    Attributes:
        fields: 字段名 → FieldStats。
        records_total: 总记录数。
        records_valid: 通过校验的记录数。
        issues: 校验问题计数（规则名 → 次数）。
    """

    fields: dict[str, FieldStats] = field(default_factory=dict)
    records_total: int = 0
    records_valid: int = 0
    issues: dict[str, int] = field(default_factory=dict)

    @property
    def pass_rate(self) -> float:
        """校验通过率。

        Returns:
            0.0 - 1.0 的浮点数。
        """
        return self.records_valid / self.records_total if self.records_total else 0.0

    def render(self) -> str:
        """渲染成可读的文本报告。

        Returns:
            多行字符串。
        """
        lines: list[str] = []
        lines.append("─" * 74)
        lines.append(f"{'字段':<16}{'覆盖':>8}{'脏数据率':>12}{'解析失败':>12}")
        lines.append("─" * 74)
        for name, st in self.fields.items():
            lines.append(
                f"{name:<16}{st.coverage:>7.1%}{st.dirty_rate:>11.1%}"
                f"{st.parse_failed:>12}"
            )
        lines.append("─" * 74)
        lines.append(f"记录总数 {self.records_total}，校验通过 {self.records_valid} "
                     f"（{self.pass_rate:.1%}）")
        if self.issues:
            lines.append("")
            lines.append("触发的校验规则：")
            for rname, cnt in sorted(self.issues.items(), key=lambda x: -x[1]):
                lines.append(f"  · {rname:<24} {cnt:>5} 次")
        return "\n".join(lines)


# ============================================================================
# 清洗流水线
# ============================================================================
def clean_record(raw: dict[str, Any], *,
                 dayfirst: bool | None = None,
                 fetched_at: datetime | None = None) -> dict[str, Any]:
    """对一条原始记录跑完整四级流水线。

    Args:
        raw: 原始记录。
        dayfirst: 歧义日期的解析方向。
        fetched_at: 抓取时刻，作为相对时间的基准。

    Returns:
        清洗后的记录，附带 `_quality` 字典记录本条的清洗元信息。
    """
    out: dict[str, Any] = {}
    meta: dict[str, Any] = {"changed": [], "failed": []}

    # ---- L1：文本字段 ----
    for f in ("title", "shop_name", "category"):
        if f in raw:
            before = raw[f]
            after = clean_text(before, keep_case=(f == "title"))
            out[f] = after
            if before != after:
                meta["changed"].append(f)

    # ---- L2：价格 ----
    if "price" in raw:
        pr = parse_price(raw["price"])
        out["price"] = pr.value
        out["currency"] = pr.currency
        if pr.ok:
            if not pr.is_range:
                pass
            else:
                out["price_high"] = pr.high
        else:
            meta["failed"].append(f"price:{pr.reason}")
        if str(raw["price"]) != str(pr.value):
            meta["changed"].append("price")

    # ---- L2：数量 ----
    if "sales" in raw:
        cr = parse_count(raw["sales"])
        out["sales"] = int(cr.value) if cr.ok else None
        if cr.ok and cr.multiplier != 1.0:
            meta["changed"].append("sales")
        if not cr.ok:
            meta["failed"].append("sales:未解析出数字")

    # ---- L2：评分 ----
    if "rating" in raw:
        rr = parse_count(raw["rating"])
        out["rating"] = rr.value
        if not rr.ok:
            meta["failed"].append("rating:未解析出数字")

    # ---- L2：日期 ----
    if "published_at" in raw:
        dr = parse_datetime(raw["published_at"], dayfirst=dayfirst, now=fetched_at)
        out["published_at"] = dr.value
        out["published_fmt"] = dr.fmt
        if dr.ok and dr.value:
            if dr.ambiguous:
                meta["changed"].append("published_at:歧义格式")
            out["published_at"] = dr.value
        else:
            meta["failed"].append(f"published_at:{dr.fmt}")

    # ---- L2：联系方式 ----
    if "contact" in raw:
        ph = normalize_phone_cn(raw["contact"])
        out["phone"] = mask_phone(ph) if ph else ""
        out["email"] = mask_email(normalize_email(raw["contact"])) if "@" in str(raw["contact"]) else ""
        if ph:
            meta["changed"].append("contact:手机号已脱敏")

    # ---- L2：URL ----
    if "url" in raw:
        nu = normalize_url(raw["url"])
        out["url"] = nu
        if nu != (raw["url"] or "").strip():
            meta["changed"].append("url")

    out["_quality"] = meta
    return out


def run_pipeline(records: Iterable[dict[str, Any]], *,
                 dayfirst: bool | None = None,
                 fetched_at: datetime | None = None) -> tuple[list[dict[str, Any]],
                                                               QualityReport]:
    """批量运行清洗流水线并产出质量报告。

    Args:
        records: 原始记录可迭代对象。
        dayfirst: 歧义日期解析方向。
        fetched_at: 抓取时刻。

    Returns:
        (清洗后保留的记录列表, QualityReport 对象)。

    注意这里**只保留通过校验的记录**，被丢弃的记入报告。
    生产环境更好的做法是把失败记录另存到 `_rejected` 表，
    方便人工复查和后续修复 —— 直接丢弃会让你永远不知道丢了多少。
    """
    report = QualityReport()
    kept: list[dict[str, Any]] = []
    report.fields = {
        "title": FieldStats("title"),
        "price": FieldStats("price"),
        "rating": FieldStats("rating"),
        "published_at": FieldStats("published_at"),
        "url": FieldStats("url"),
    }

    for raw in records:
        report.records_total += 1
        cleaned = clean_record(raw, dayfirst=dayfirst, fetched_at=fetched_at)
        meta = cleaned.pop("_quality", {})

        # 统计字段级指标
        for fname, st in report.fields.items():
            st.total += 1
            val = cleaned.get(fname)
            if val is not None and val != "":
                st.present += 1
            # 脏数据率的分母必须是"存在的记录"，且每个字段**最多计一次**。
            # 早先写成 `fname in meta["changed"] or any(...)` 会让
            # 同时命中两种模式的字段被计两次，产出 133% 这种荒谬的比率。
            changed_once = (
                fname in meta.get("changed", [])
                or any(c.startswith(fname + ":") for c in meta.get("changed", []))
            )
            if changed_once and val is not None and val != "":
                st.changed += 1
            if any(c.startswith(fname + ":") for c in meta.get("failed", [])):
                st.parse_failed += 1

        # L3 校验
        vr = validate_record(cleaned)
        for issue in vr.issues:
            report.issues[issue.rule] = report.issues.get(issue.rule, 0) + 1

        if vr.passed:
            report.records_valid += 1
            kept.append(cleaned)

    return kept, report


# ============================================================================
# 实验区
# ============================================================================
# 这批数据是「真实世界脏数据合集」，每一条都对应一个具体的坑
DIRTY_RECORDS: list[dict[str, Any]] = [
    {   # ① 全角 + 千分位 + 货币符号 + 多余空白
        "title": "  ＡＢＣ  手账本  ",
        "price": "￥1,234.56",
        "rating": "4.5",
        "published_at": "2024年1月15日",
        "url": "HTTP://Shop.Example.COM/p/1/?utm_source=x&b=2&a=1#top",
        "sales": "1.2万",
    },
    {   # ② HTML 实体 + NBSP 空格
        "title": "Tom&amp;Jerry&#39;s &nbsp;Mug",
        "price": "£12.99",
        "rating": "5",
        "published_at": "2024-01-15T10:30:00Z",
        "url": "https://shop.example.com/p/2",
        "sales": "3.5k",
    },
    {   # ③ 价格区间 + 前缀文字
        "title": "Vintage Lamp",
        "price": "From £45.00 - £89.99",
        "rating": "4",
        "published_at": "15/01/2024",       # 歧义：日/月 or 月/日
        "url": "https://shop.example.com/p/3/",
        "sales": "890",
    },
    {   # ④ 无价格 + 中文相对时间
        "title": "定制服务",
        "price": "价格面议",
        "rating": "暂无",
        "published_at": "3天前",
        "url": "https://shop.example.com/p/4",
        "sales": "12",
    },
    {   # ⑤ 欧式小数逗号
        "title": "French Perfume",
        "price": "1.234,56 €",
        "rating": "4.8",
        "published_at": "2024/01/20",
        "url": "https://shop.example.com/p/5",
        "sales": "2.5万",
    },
    {   # ⑥ 脏数据：负数价格 + 未来日期
        "title": "Broken Item",
        "price": "-9.99",
        "rating": "9.5",
        "published_at": "2030-01-01",
        "url": "https://shop.example.com/p/6",
        "sales": "0",
    },
    {   # ⑦ 解析失败：标题是占位值 + URL 相对路径
        "title": "N/A",
        "price": "N/A",
        "rating": "",
        "published_at": "unknown",
        "url": "/p/7",
        "sales": "",
    },
    {   # ⑧ 联系方式（含手机号）
        "title": "Contact Seller",
        "price": "$99.00",
        "rating": "4.2",
        "published_at": "2 hours ago",
        "url": "https://shop.example.com/p/8?ref=homepage",
        "sales": "156 条评论",
        "contact": "手机 138 1234 5678，邮箱 Seller.Name+promo@Gmail.com",
    },
]


def exp1_l1_normalize() -> None:
    """实验 1：L1 规范化 —— 把"看起来一样"变成"真的相等"。"""
    print("=" * 74)
    print("实验 1 · L1 规范化：为什么判重总失败")
    print("=" * 74)

    pairs = [
        ("全角数字", "１２３４５", "12345"),
        ("全角字母", "ＡＢＣ", "ABC"),
        ("全角空格", "a　b", "a b"),
        ("NBSP 空格", "1\u00a0234", "1 234"),
        ("HTML 实体", "Tom&amp;Jerry", "Tom&Jerry"),
        ("数字实体", "&#163;12.99", "£12.99"),
        ("罗马数字", "Ⅳ", "IV"),
        ("上下标", "x²y³", "x2y3"),
        ("连字", "ﬁle", "file"),
        ("中文标点", "价格：12.99，库存：3", "价格:12.99,库存:3"),
    ]

    print(f"\n  {'类型':<12}{'原始':<20}{'清洗后':<20}{'与目标相等'}")
    print("  " + "-" * 68)
    for label, src, target in pairs:
        cleaned = normalize_unicode(src)
        if label == "中文标点":
            cleaned = normalize_punctuation(cleaned)
        cleaned = normalize_whitespace(decode_html_entities(cleaned))
        eq = "✓" if cleaned == target or cleaned == normalize_unicode(target) else "✗"
        print(f"  {label:<12}{src!r:<20}{cleaned!r:<20}{eq}")

    print("\n  ▸ 关键结论：这些字符**肉眼看起来完全一样**，但码点不同。")
    print("    不规范化的话，'ＡＢＣ' 和 'ABC' 会被当成两条不同记录入库，")
    print("    你的判重逻辑就永远失效 —— 而且你看日志完全看不出问题在哪。")

    print("\n  ⚠ 一个反直觉的细节：罗马数字是「一个字符展开成多个字符」")
    for c in "ⅣⅤⅥⅫ":
        print(f"      {c!r} (U+{ord(c):04X}) → {unicodedata.normalize('NFKC', c)!r}  "
              f"长度 {len(c)} → {len(unicodedata.normalize('NFKC', c))}")
    print("    所以像 '第Ⅳ章' 规范化后是 '第IV章'（3 字符变 5 字符）。")
    print("    如果你按字符位置做截断，规范化前后长度会不一致 —— 要先规范化再截断。")

    # 展示码点差异
    print("\n  --- 码点对照 ---")
    for a, b in [("Ａ", "A"), ("　", " "), ("\u00a0", " "), ("：", ":")]:
        print(f"    {a!r} = U+{ord(a):04X}    {b!r} = U+{ord(b):04X}   "
              f"{'不同 ✓' if a != b else '相同'}")

    # 控制字符演示
    dirty = "标题\x00正常\x07文本\x1b[31m"
    print(f"\n  --- 控制字符 ---")
    print(f"    原始 repr：{dirty!r}")
    print(f"    清洗后   ：{normalize_unicode(dirty)!r}")
    print("    ▸ \\x1b[31m 是终端颜色控制码；\\x00 是空字节。")
    print("      它们能让字符串比较失败，而且 print 出来看不见 —— 排查噩梦。")


def exp2_l2_price() -> None:
    """实验 2：L2 价格解析 —— 几十种写法的统一处理。"""
    print("\n" + "=" * 74)
    print("实验 2 · L2 价格解析")
    print("=" * 74)

    samples = [
        "£12.99", "￥1,234.56", "12.99 元", "$1,234", "12,99", "1.234,56 €",
        "1 234.56", "£12.99 - £18.50", "From £9.99", "£12.99*",
        "Free", "免费", "价格面议", "已售罄", "N/A", "", None,
        "12.99元起", "约 ￥200", "£1,234,567.89",
    ]

    print(f"\n  {'原始':<24}{'数值':>12}{'币种':>6}{'区间':>6}{'上限':>12}")
    print("  " + "-" * 68)
    ok_count = 0
    for s in samples:
        r = parse_price(s)
        if r.ok:
            ok_count += 1
            val = f"{r.value:,.2f}"
            hi = f"{r.high:,.2f}" if r.high else "-"
            rng = "是" if r.is_range else "-"
        else:
            val = f"✗{r.reason}"[:12]
            hi, rng = "-", "-"
        print(f"  {str(s)!r:<24}{val:>12}{r.currency:>6}{rng:>6}{hi:>12}")

    print(f"\n  成功解析 {ok_count}/{len(samples)} 条")

    print("\n  ▸ 最容易出错的三个点：")
    print("      ① 逗号歧义：'1,234' 是 1234 还是 1.234？")
    print("         → 规则：逗号后恰好 3 位且前面是数字 → 千分位")
    print("      ② '12,99' 欧式小数 → 解析成 12.99，不能当千分位")
    print("      ③ 'Free'/'面议' 不是解析失败，是**业务上没有价格**")
    print("         → 用 reason 字段区分，别让它们变成 None 混进统计")

    print("\n  --- 千分位 vs 小数点的判断逻辑实测 ---")
    for s in ["1,234", "1,23", "1,234.56", "1.234,56", "12 345.67"]:
        print(f"    {s!r:<14} → {_to_float(s)}")


def exp3_l2_count() -> None:
    """实验 3：L2 数量解析 —— '3.5万' 到底是多少。"""
    print("\n" + "=" * 74)
    print("实验 3 · L2 数量解析（销量 / 评论数）")
    print("=" * 74)

    samples = [
        "1.2k", "3.5万", "1,234", "2.5M", "1.2亿", "890",
        "156 条评论", "23.4w", "1.5千", "500+", "10K+", "暂无",
    ]
    print(f"\n  {'原始':<16}{'数值':>14}{'单位':>6}{'倍数':>10}")
    print("  " + "-" * 48)
    for s in samples:
        r = parse_count(s)
        if r.ok:
            print(f"  {s!r:<16}{r.value:>14,.0f}{r.unit:>6}{r.multiplier:>10,.0f}")
        else:
            print(f"  {s!r:<16}{'✗ 失败':>14}{'-':>6}{'-':>10}")

    print("\n  ▸ '3.5万' → 35000，'1.2k' → 1200。")
    print("    不做这步转换的话，你无法对销量排序，也无法计算增长率。")
    print("    ⚠ 局限：'w' 在中文站点是「万」，在其他语境可能是别的意思。")
    print("      生产环境应结合站点语言判断，不能只靠字典。")


def exp4_l2_datetime() -> None:
    """实验 4：L2 日期解析 —— 12 种格式 + 歧义推断。"""
    print("\n" + "=" * 74)
    print("实验 4 · L2 日期时间解析")
    print("=" * 74)

    now = datetime(2026, 9, 19, 21, 7, 23)      # 固定基准，保证输出可复现

    samples = [
        "2024-01-15", "2024/01/15", "2024.01.15",
        "2024年1月15日", "2024年1月15日 10:30",
        "2024-01-15T10:30:00Z", "2024-01-15T10:30:00+08:00",
        "1712345678", "1712345678000",
        "Jan 15, 2024", "15 Jan 2024",
        "3天前", "2 hours ago", "昨天", "刚刚", "15/01/2024", "01/15/2024",
        "unknown", "",
    ]

    print(f"\n  （基准时间固定为 {now:%Y-%m-%d %H:%M:%S}，保证结果可复现）")
    print(f"\n  {'原始':<26}{'解析结果':<21}{'匹配格式':<24}{'歧义'}")
    print("  " + "-" * 76)
    for s in samples:
        r = parse_datetime(s, now=now)
        if r.ok and r.value:
            val = f"{r.value:%Y-%m-%d %H:%M}"
        else:
            val = "✗ 失败"
        print(f"  {s!r:<26}{val:<21}{r.fmt:<24}{'⚠' if r.ambiguous else ''}")

    print("\n  ▸ 三个必须注意的点：")
    print("      ① ISO 带时区的会被统一转成 UTC — 否则不同时区的数据没法比较")
    print("      ② 相对时间依赖基准时刻 — 所以必须传抓取时间，不能用 now()")
    print("      ③ '15/01/2024' 和 '01/15/2024' 单看无法区分 — 必须靠统计")

    # ---- 歧义推断实测 ----
    print("\n  --- 用统计法解决『日月/月日』歧义 ---")
    scenarios = {
        "站点 A（疑似英式 .uk）": [
            "15/01/2024", "23/02/2024", "03/03/2024", "28/04/2024", "07/05/2024",
        ],
        "站点 B（疑似美式 .com）": [
            "01/15/2024", "02/23/2024", "03/03/2024", "04/28/2024", "05/07/2024",
        ],
        "站点 C（样本全是 1-12）": [
            "03/04/2024", "05/06/2024", "07/08/2024",
        ],
        "站点 D（样本混杂）": [
            "15/01/2024", "01/15/2024", "20/03/2024",
        ],
    }
    for label, samples_ in scenarios.items():
        info = infer_dayfirst(samples_)
        print(f"\n    {label}")
        print(f"      推断 dayfirst = {info['dayfirst']}  "
              f"（置信度 {info['confidence']:.2f}）")
        print(f"      依据：{info['evidence']}")

    print("\n  ▸ 这就是「数据自己会说话」：不要靠猜域名后缀，")
    print("    从 13-31 出现在哪个位置来反推格式。样本越多越准。")


def exp5_l2_url_contact() -> None:
    """实验 5：L2 URL 规范化 + 联系方式脱敏。"""
    print("\n" + "=" * 74)
    print("实验 5 · L2 URL 规范化与联系方式脱敏")
    print("=" * 74)

    urls = [
        "HTTP://Shop.Example.COM/p/1/?utm_source=x&b=2&a=1#top",
        "https://shop.example.com/p/1?a=1&b=2",
        "https://shop.example.com:443/p/1",
        "https://shop.example.com/p/1/",
        "https://shop.example.com//p//1",
        "https://shop.example.com/p/1?fbclid=abc123",
        "https://shop.example.com/p/2?ref=nav&spm=a1b2c3",
    ]

    print("\n  --- URL 规范化（判重的关键）---")
    groups: dict[str, list[str]] = {}
    for u in urls:
        n = normalize_url(u)
        groups.setdefault(n, []).append(u)

    for n, members in groups.items():
        print(f"\n    规范化结果：{n}")
        for m in members:
            print(f"      ← {m}")

    dup = sum(len(v) - 1 for v in groups.values())
    print(f"\n  ▸ {len(urls)} 个原始 URL 归并成 {len(groups)} 个唯一资源")
    print(f"    消除了 {dup} 个重复 —— 不做这步，判重完全无效，配额白烧。")

    print("\n  ▸ 规范化动作清单：")
    for a in ["① 协议统一小写", "② 域名转小写", "③ 去默认端口 :80/:443",
              "④ 丢弃 fragment（#...）", "⑤ 去追踪参数（utm_/fbclid/spm/ref）",
              "⑥ 查询参数排序", "⑦ 合并重复斜杠", "⑧ 去尾部斜杠"]:
        print(f"      {a}")

    # ---- 联系方式 ----
    print("\n  --- 联系方式规范化 + 脱敏 ---")
    phones = [
        "13812345678", "138 1234 5678", "138-1234-5678",
        "+86 138 1234 5678", "0086 13812345678", "(86)13812345678",
        "12345678901", "1381234567",
    ]
    print(f"\n    {'原始':<24}{'规范化':<16}{'脱敏输出'}")
    print("    " + "-" * 56)
    for p in phones:
        n = normalize_phone_cn(p)
        print(f"    {p!r:<24}{n or '(不匹配)':<16}{mask_phone(n) if n else '-'}")

    emails = [
        "Seller.Name+promo@Gmail.com", "user@EXAMPLE.COM",
        "a.b.c@sub.domain.co.uk", "not-an-email",
    ]
    print(f"\n    {'原始':<30}{'规范化':<32}{'脱敏'}")
    print("    " + "-" * 76)
    for e in emails:
        n = normalize_email(e)
        print(f"    {e!r:<30}{n or '(不匹配)':<32}{mask_email(n) if n else '-'}")

    print("\n  ▸ 为什么 '+promo' 要剥掉？因为 Gmail 里 a+x@ 和 a@ 是同一个邮箱，")
    print("    不剥掉做用户去重会漏掉大量重复。但**只对明确的几家服务商做**，")
    print("    无脑剥掉会破坏其他邮箱地址。")
    print("\n  ⚠ 合规提醒：手机号/邮箱属于《个人信息保护法》定义的「个人信息」，")
    print("    批量抓取可能触犯《刑法》253 条之一。默认脱敏是最小风险策略。")


def exp6_pipeline_and_report() -> None:
    """实验 6：完整四级流水线 + 数据质量报告。"""
    print("\n" + "=" * 74)
    print("实验 6 · 完整流水线：8 条脏数据 → 清洗 → 校验 → 报告")
    print("=" * 74)

    fetched_at = datetime(2026, 9, 19, 21, 7, 23)

    # 先展示原始数据有多脏
    print(f"\n  --- 原始数据（{len(DIRTY_RECORDS)} 条）---")
    for i, r in enumerate(DIRTY_RECORDS, 1):
        print(f"\n    [{i}] title   = {r.get('title')!r}")
        print(f"        price   = {r.get('price')!r}")
        print(f"        date    = {r.get('published_at')!r}")
        print(f"        url     = {r.get('url')!r}")

    # 用推断出的 dayfirst 跑流水线
    # 注意：这里只能从"已经成功识别的样本"里推断，8 条里只有 1 条是歧义格式，
    # 所以置信度必然很低。真实项目要拿几百上千条来推断才靠谱。
    infer = infer_dayfirst([r.get("published_at", "") for r in DIRTY_RECORDS])
    print(f"\n  --- 日期方向推断 ---")
    print(f"    dayfirst = {infer['dayfirst']}（置信度 {infer['confidence']:.2f}）")
    print(f"    依据：{infer['evidence']}")
    print(f"    ⚠ 样本里只有 1 条歧义格式（'15/01/2024'），")
    print(f"      恰好它的第一段 >12，所以能「推断」出来 —— 但这是运气。")
    print(f"      生产环境必须积累足够样本，否则不如直接人工配置 dayfirst。")

    kept, report = run_pipeline(
        DIRTY_RECORDS, dayfirst=infer["dayfirst"], fetched_at=fetched_at
    )

    print(f"\n  --- 清洗后（保留 {len(kept)} 条）---")
    # 列宽用固定宽度 + 截断。中文占 2 个显示宽度但只算 1 个字符，
    # 所以这里所有文本列都**按显示宽度**来处理，否则表格会错位。
    def _pad(s: str, width: int) -> str:
        """按显示宽度补空格（中文按 2 宽度计）。

        Args:
            s: 待补齐的字符串。
            width: 目标显示宽度。

        Returns:
            补齐后的字符串。

        这就是终端表格对齐的正确做法：str.ljust 按字符数算，
        中文标题会让整列歪掉。需要自己算东亚宽字符。
        """
        w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)
        if w > width:
            # 超宽则按宽度截断
            out, cur = "", 0
            for ch in s:
                cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
                if cur + cw > width - 1:
                    break
                out += ch
                cur += cw
            return out + " " * max(0, width - cur)
        return s + " " * (width - w)

    header = (f"  {'#':<3}{_pad('title', 20)}{_pad('price', 11)}"
              f"{_pad('cur', 6)}{_pad('rating', 8)}{_pad('published_at', 14)}sales")
    print("\n" + header)
    print("  " + "-" * 70)
    for i, r in enumerate(kept, 1):
        pa = r.get("published_at")
        pa_s = f"{pa:%Y-%m-%d}" if isinstance(pa, datetime) else str(pa or "-")
        price = r.get("price")
        price_s = f"{price:.2f}" if price is not None else "-"
        rating = r.get("rating")
        rating_s = f"{rating:.1f}" if rating is not None else "-"
        print("  " + f"{i:<3}"
              + _pad(r.get("title") or "", 20)
              + _pad(price_s, 11)
              + _pad(r.get("currency") or "-", 6)
              + _pad(rating_s, 8)
              + _pad(pa_s, 14)
              + str(r.get("sales") if r.get("sales") is not None else "-"))

    print(f"\n  --- L4 数据质量报告 ---")
    print(report.render())

    print("\n  ▸ 报告怎么读（这是本课最重要的一页）：")
    print("      · **覆盖率**低 → 这个字段经常解析失败，优先修")
    print("      · **脏数据率**高 → 站点数据格式混乱，清洗规则正在发挥作用")
    print("                       （如果某个字段脏数据率是 0%，要么它很干净，")
    print("                         要么你的清洗函数根本没生效 —— 要警惕）")
    print("      · **通过率**低 → 大量记录被丢弃，检查是规则太严还是数据太差")
    print("\n    「解析失败」和「校验失败」是两回事，排查方向完全不同：")
    print("      · 解析失败 = 我读不懂这条数据（找解析器的问题）")
    print("      · 校验失败 = 我读懂了但值不合理（找数据源或规则的问题）")
    print("\n  ▸ 样本量只有 8 条，所以百分比颗粒度很粗（12.5% 一跳）。")
    print("    生产环境的报告应该跑在几千条以上才具备统计意义。")


def main() -> None:
    """运行全部实验。"""
    exp1_l1_normalize()
    exp2_l2_price()
    exp3_l2_count()
    exp4_l2_datetime()
    exp5_l2_url_contact()
    exp6_pipeline_and_report()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 清洗是四级流水线：规范化 → 抽取 → 校验 → 报告，每级可独立测试",
        "2. NFKC 规范化能解决「肉眼一样但码点不同」的判重失败（全角/罗马数字）",
        "3. \\u00a0（NBSP）长得像空格但不等价，必须显式处理",
        "4. 千分位 vs 小数点：逗号后恰好 3 位数字则视为千分位",
        "5. '面议'/'Free' 不是解析失败，是业务上无价格 —— 用 reason 区分",
        "6. '3.5万' → 35000，'1.2k' → 1200，不做转换就没法排序统计",
        "7. ISO 带时区的时间统一转 UTC，否则跨时区数据无法比较",
        "8. 相对时间必须用「抓取时刻」做基准，不能用 datetime.now()",
        "9. 日月/月日歧义用统计法推断：看 13-31 出现在哪个位置",
        "10. URL 规范化 8 个动作，不做的话判重完全失效",
        "11. 手机号/邮箱默认脱敏，这是合规要求不是可选项",
        "12. 数据质量报告让问题可见 —— 没有报告就没有持续维护",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
