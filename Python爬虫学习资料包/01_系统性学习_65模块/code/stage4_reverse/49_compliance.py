"""阶段 4 · 第 10 课：合规红线（阶段 4 最重要的一课）

技术可以教你，但边界必须你自己守。
本课不讲任何攻击手法，只讲**什么不能做、为什么、以及怎么合规地做**。

本课包含：
  1. 法律框架速查（中国法域为主）
  2. robots.txt 的真实含义与正确用法（含实测解析器）
  3. 数据性质的四个等级 —— 决定你的风险高低
  4. 合规采集检查清单（可执行的自查工具）
  5. 三个典型场景的合规路径对比：能爬 / 高危 / 绝对不行
  6. 出问题时的责任边界（技术方 vs 需求方）

⚠ 免责声明
  本课内容是工程实践中的风险提示，**不构成法律意见**。
  真实项目请咨询执业律师。法律会随判例和修法变化，
  本课基于中国大陆现行法律框架的一般理解。

运行：python3 49_compliance.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SEP = "=" * 72


def title(text: str) -> None:
    print(f"\n{SEP}\n{text}\n{SEP}")


def sub(text: str) -> None:
    print(f"\n▸ {text}")


# ==========================================================================
# 一、法律框架
# ==========================================================================


@dataclass
class LawRef:
    """一条法律依据。"""

    name: str
    article: str
    gist: str
    risk: str
    level: int  # 1=提示 2=较高 3=刑事


LAWS: list[LawRef] = [
    LawRef(
        "《网络安全法》",
        "第 27 条",
        "禁止非法侵入他人网络、干扰正常功能、窃取网络数据",
        "使用技术手段绕过访问控制、造成目标系统异常，均落入此条",
        2,
    ),
    LawRef(
        "《数据安全法》",
        "第 32 条",
        "收集数据应合法正当，不得以窃取或其他非法方式获取",
        "批量抓取未授权数据可能构成『以其他非法方式获取』",
        2,
    ),
    LawRef(
        "《个人信息保护法》",
        "第 13、27 条",
        "处理个人信息需有合法性基础；已公开信息也须在合理范围内处理",
        "抓取含姓名/手机号/头像等内容，即使公开也受此约束",
        3,
    ),
    LawRef(
        "《刑法》",
        "第 285 条第 2 款",
        "非法获取计算机信息系统数据罪",
        "突破技术保护措施获取数据，情节严重可判刑（最高 7 年）",
        3,
    ),
    LawRef(
        "《刑法》",
        "第 286 条",
        "破坏计算机信息系统罪",
        "爬虫导致目标系统无法正常运行，可能落入此条",
        3,
    ),
    LawRef(
        "《刑法》",
        "第 253 条之一",
        "侵犯公民个人信息罪",
        "获取、出售公民个人信息，达数量标准即入罪",
        3,
    ),
    LawRef(
        "《反不正当竞争法》",
        "第 12 条",
        "不得利用技术手段妨碍、破坏其他经营者合法提供的网络服务",
        "爬取竞品数据用于商业竞争，可能构成不正当竞争",
        2,
    ),
    LawRef(
        "《著作权法》",
        "第 24 条",
        "合理使用需指明作者、不影响作品正常使用、不损害权利人合法权益",
        "抓取内容二次发布需注意著作权，即使是『转载注明出处』",
        2,
    ),
    LawRef(
        "《民法典》",
        "第 1035 条",
        "处理个人信息应遵循合法、正当、必要、诚信原则",
        "爬取用于用户画像、精准营销时的民事风险",
        2,
    ),
    LawRef(
        "《robots.txt》",
        "行业惯例",
        "非法律文件，但违反可能作为『明知故犯』的证据",
        "不遵守 robots 本身不违法，但会在诉讼中对你不利",
        1,
    ),
]

LEVEL_LABEL = {1: "提示", 2: "较高风险", 3: "刑事风险"}


# ==========================================================================
# 二、robots.txt 解析器
# ==========================================================================


@dataclass
class RobotsRule:
    """一条 robots 规则。"""

    allow: bool
    path: str

    def matches(self, path: str) -> bool:
        """判断某路径是否命中此规则（前缀匹配，支持 * 与 $）。"""
        pattern = self.path
        if pattern.endswith("$"):
            core = pattern[:-1]
            return path == core or (self._wildcard_match(core, path) and path.endswith(core.split("*")[-1]))
        if "*" in pattern:
            return self._wildcard_match(pattern, path)
        return path.startswith(pattern)

    @staticmethod
    def _wildcard_match(pattern: str, path: str) -> bool:
        parts = pattern.split("*")
        pos = 0
        for i, part in enumerate(parts):
            if not part:
                continue
            idx = path.find(part, pos if i else 0)
            if idx < 0:
                return False
            if i == 0 and idx != 0:
                return False
            pos = idx + len(part)
        return True


@dataclass
class RobotsFile:
    """解析后的 robots.txt。"""

    user_agent: str
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    raw_groups: dict[str, list[str]] = field(default_factory=dict)

    def can_fetch(self, path: str, ua: str = "*") -> tuple[bool, str]:
        """判断当前 UA 能否抓取某路径。

        ▸ 规则匹配原则（RFC 9309）：
          1. 取最长匹配的那条规则（而不是第一条匹配的）
          2. Allow 与 Disallow 同长时 Allow 优先
          3. 没有匹配规则 → 默认允许

        Args:
            path: 要检查的路径。
            ua: User-Agent 标识。

        Returns:
            (是否允许, 命中的规则说明)。
        """
        best: tuple[int, bool, str] | None = None
        for r in self.rules:
            if r.matches(path):
                length = len(r.path)
                # 同长度时 Allow 优先
                if best is None or length > best[0] or (length == best[0] and r.allow and not best[1]):
                    best = (length, r.allow, r.path)
        if best is None:
            return True, "无匹配规则 → 默认允许"
        return best[1], f"命中规则 {'Allow' if best[1] else 'Disallow'}: {best[2]}"


def parse_robots(text: str, user_agent: str = "*") -> RobotsFile:
    """解析 robots.txt 内容。

    Args:
        text: robots.txt 全文。
        user_agent: 要提取的 UA 段（支持 '*' 与被明确列出的 UA）。

    Returns:
        RobotsFile 对象。
    """
    lines = [ln.split("#")[0].strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    groups: dict[str, list[tuple[str, str]]] = {}
    current: list[str] = []
    expecting_ua = True

    for raw in lines:
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key == "user-agent":
            if not expecting_ua:
                current = []
                expecting_ua = True
            current.append(val)
            groups.setdefault(val, [])
        elif key in ("allow", "disallow", "crawl-delay", "sitemap"):
            expecting_ua = False
            if key == "sitemap":
                groups.setdefault("__sitemap__", []).append((key, val))
            elif key == "crawl-delay":
                for ua in current or ["*"]:
                    groups.setdefault(ua, []).append((key, val))
            else:
                for ua in current or ["*"]:
                    groups.setdefault(ua, []).append((key, val))

    # 合并：精确 UA 优先，其次 '*'，最后取并集（保守）
    rf = RobotsFile(user_agent=user_agent)
    chosen: list[tuple[str, str]] = []
    if user_agent in groups:
        chosen = groups[user_agent]
        rf.raw_groups[user_agent] = [f"{k}: {v}" for k, v in chosen]
    elif "*" in groups:
        chosen = groups["*"]
        rf.raw_groups["*"] = [f"{k}: {v}" for k, v in chosen]

    for k, v in chosen:
        if k == "allow":
            rf.rules.append(RobotsRule(allow=True, path=v))
        elif k == "disallow":
            # 空 Disallow 表示全部允许
            if v:
                rf.rules.append(RobotsRule(allow=False, path=v))
        elif k == "crawl-delay":
            try:
                rf.crawl_delay = float(v)
            except ValueError:
                pass
    rf.sitemaps = [v for k, v in groups.get("__sitemap__", [])]
    return rf


def robots_audit(text: str) -> dict[str, Any]:
    """对 robots.txt 做合规相关审计。

    Args:
        text: robots.txt 全文。

    Returns:
        审计结果字典。
    """
    low = text.lower()
    return {
        "has_disallow": "disallow" in low,
        "has_crawl_delay": "crawl-delay" in low,
        "has_sitemap": "sitemap" in low,
        "blocks_all": "disallow: /" in low.replace(" ", ""),
        "mentions_api": "/api" in low,
        "mentions_search": "search" in low,
        "length": len(text),
    }


# ==========================================================================
# 三、数据性质分级
# ==========================================================================


class DataLevel(Enum):
    """数据风险等级。"""

    PUBLIC = "公开数据"
    SEMI = "半公开数据"
    PERSONAL = "个人信息"
    SENSITIVE = "敏感/受限数据"


@dataclass
class DataType:
    """一类数据的合规画像。"""

    level: DataLevel
    examples: str
    examples_ok: str
    examples_bad: str
    verdict: str


DATA_MATRIX: list[DataType] = [
    DataType(
        DataLevel.PUBLIC,
        "商品价格、公开榜单、新闻标题、法律法规、天气、股价、学术论文元数据",
        "公开商品页的价格与库存；政府开放数据平台；维基百科；公开财报",
        "把抓到的新闻全文二次发布（著作权）；号称『实时』却缓存造假",
        "✓ 合规区间 —— 注意频率与 robots，不要影响对方服务",
    ),
    DataType(
        DataLevel.SEMI,
        "需要登录才能看的内容、付费墙后内容、论坛帖子（含用户名）",
        "使用自己账号访问自己的数据；获得书面授权的接口调用",
        "用技术手段绕过登录墙；共享账号批量采集；绕过付费墙",
        "⚠ 高危 —— 绕过技术措施可能触及《刑法》285 条",
    ),
    DataType(
        DataLevel.PERSONAL,
        "姓名+手机号、邮箱、头像、收货地址、评论者昵称与主页、简历",
        "公开信息的合理范围内使用（如学术研究、去标识化统计）",
        "批量收集联系方式用于营销；构建个人画像；出售/交换数据",
        "⛔ 强烈不建议 —— 《个人信息保护法》《刑法》253 条之一",
    ),
    DataType(
        DataLevel.SENSITIVE,
        "身份证号、银行卡、医疗记录、行踪轨迹、生物识别、14 岁以下儿童信息",
        "无合法授权的任何采集行为都不可取",
        "任何未经明确同意的采集、存储、使用",
        "⛔ 绝对红线 —— 刑事风险极高，不存在『技术中立』抗辩空间",
    ),
]


# ==========================================================================
# 四、合规自查清单
# ==========================================================================


@dataclass
class ChecklistItem:
    """一条自查项。"""

    question: str
    why: str
    weight: int  # 1-3，越高越关键
    blocking: bool = False  # 是否一票否决


CHECKLIST: list[ChecklistItem] = [
    ChecklistItem(
        "你采集的数据属于哪一类？（公开 / 半公开 / 个人信息 / 敏感）",
        "决定整体风险等级，这是第一道也是最重要的判断",
        3,
        blocking=True,
    ),
    ChecklistItem(
        "目标站点是否有官方 API？",
        "有 API 就用 API —— 这是最合规、最稳定、最省事的路径",
        3,
    ),
    ChecklistItem(
        "你遵守 robots.txt 了吗？",
        "非法定要求，但在诉讼中会被作为『主观故意』的证据",
        2,
    ),
    ChecklistItem(
        "你的请求频率会影响对方服务吗？",
        "造成服务异常可能触及《刑法》286 条；至少也要加延时与限速",
        3,
        blocking=True,
    ),
    ChecklistItem(
        "你使用了代理池 / 多 IP 轮换来规避限频吗？",
        "规避技术措施是量刑中的重要情节",
        3,
    ),
    ChecklistItem(
        "你绕过了登录、验证码或其他访问控制吗？",
        "绕过技术保护措施可能构成『非法获取计算机信息系统数据』",
        3,
        blocking=True,
    ),
    ChecklistItem(
        "你采集的数据会用于商业竞争吗？",
        "爬取竞品数据可能构成不正当竞争（《反不正当竞争法》12 条）",
        2,
    ),
    ChecklistItem(
        "你会二次发布抓到的内容吗？",
        "需注意著作权（《著作权法》24 条），转载不等于免费",
        2,
    ),
    ChecklistItem(
        "数据里含个人信息吗？如果是，有合法性基础吗？",
        "《个人信息保护法》13 条列明了合法性基础的几种情形",
        3,
        blocking=True,
    ),
    ChecklistItem(
        "你能接受被对方封禁 / 发律师函的后果吗？",
        "现实评估：小项目被封号 vs 商业项目被起诉，后果完全不同",
        1,
    ),
    ChecklistItem(
        "你的代码里有硬编码的账号密码 / Cookie 吗？",
        "泄露他人凭证可能涉及更多罪名；也是你自己的安全风险",
        2,
    ),
    ChecklistItem(
        "你有留存采集日志与授权凭证吗？",
        "万一被质疑，能证明数据来源与授权范围是最好的保护",
        2,
    ),
]


def run_checklist(answers: dict[int, bool]) -> tuple[int, list[str]]:
    """执行合规自查。

    Args:
        answers: {清单索引: 是否满足}。True = 已满足合规要求。

    Returns:
        (得分 0-100, 风险提示列表)。
    """
    total_weight = sum(i.weight for i in CHECKLIST)
    got = 0.0
    warnings: list[str] = []
    for idx, item in enumerate(CHECKLIST):
        ok = answers.get(idx, False)
        if ok:
            got += item.weight
        elif item.blocking:
            warnings.append(f"⛔ 一票否决项未满足：{item.question}")
        elif item.weight >= 3:
            warnings.append(f"⚠ 高风险项未满足：{item.question}")
    return int(got / total_weight * 100), warnings


# ==========================================================================
# 实验区
# ==========================================================================

def exp1_laws() -> None:
    title("【实验 1】法律框架速查 —— 爬虫可能触及的 10 条依据")
    print("    ▸ 这不是恐吓，而是让你知道风险边界在哪、以及哪一类最危险。\n")
    print(f"    {'法律':<22}{'条款':<14}{'风险等级':<12}{'要点'}")
    print("    " + "-" * 106)
    for lw in LAWS:
        label = LEVEL_LABEL[lw.level]
        mark = "⛔" if lw.level == 3 else ("⚠" if lw.level == 2 else "·")
        print(f"    {mark} {lw.name:<20}{lw.article:<14}{label:<12}{lw.gist}")

    sub("按风险从低到高排列的三个区间")
    print("""    ┌──────────────┬────────────────────────────────────────────────────┐
    │ 区间         │ 典型行为                                            │
    ├──────────────┼────────────────────────────────────────────────────┤
    │ 基本安全     │ 遵守 robots + 低频率 + 公开数据 + 自己研究用          │
    │ 需要谨慎     │ 无 robots 声明但高频率；数据含用户名；用于商业分析     │
    │ 高危/违法    │ 绕过登录/验证码；突破付费墙；批量抓个人信息；          │
    │              │ 抓取导致目标服务不可用                                │
    └──────────────┴────────────────────────────────────────────────────┘

    ▸ 最危险的三个动作（记住，永远别做）：
      1. 绕过访问控制（登录墙、验证码、付费墙、加密签名）
         → 直接对标《刑法》285 条，这是爬虫案件中最常见的入罪路径
      2. 批量抓取个人信息
         → 《刑法》253 条之一，数量达标即入罪（司法解释：50 条敏感
           信息 / 500 条重要信息 / 5000 条一般信息）
      3. 高并发导致目标站点瘫痪
         → 《刑法》286 条，可能构成破坏计算机信息系统罪

    ▸ 一个真实存在的认知误区：
      『数据是公开的，所以我抓了不违法』—— 错。
      公开数据也可能受《个人信息保护法》《反不正当竞争法》《著作权法》约束。
      『公开』只影响风险的**高低**，不等于**免责**。""")


def exp2_robots() -> None:
    title("【实验 2】robots.txt 实测解析 —— 别把它当摆设")
    print("    用一份贴近真实的 robots.txt 实测解析（非真实站点内容，教学用）：\n")

    demo = """\
# 通用规则
User-agent: *
Disallow: /admin/
Disallow: /private/
Disallow: /search?
Disallow: /*.pdf$
Allow: /api/public/
Crawl-delay: 1

# 针对特定爬虫
User-agent: BadBot
Disallow: /

User-agent: GoodBot
Disallow: /admin/
Allow: /
Crawl-delay: 0.5

Sitemap: https://example.com/sitemap.xml
"""
    for i, ln in enumerate(demo.splitlines(), 1):
        print(f"    {i:>3} │ {ln}")

    sub("解析结果（以 UA='*' 为例）")
    rf = parse_robots(demo, "*")
    print(f"    crawl-delay: {rf.crawl_delay} 秒")
    print(f"    sitemap:     {rf.sitemaps}")
    print(f"    规则数:      {len(rf.rules)}")

    sub("逐路径测试")
    paths = [
        "/",
        "/books/",
        "/admin/users",
        "/private/data.json",
        "/search?q=python",
        "/manual.pdf",
        "/api/public/books",
        "/api/secret/keys",
    ]
    print(f"    {'路径':<26}{'可抓取':<10}{'命中规则'}")
    print("    " + "-" * 86)
    for p in paths:
        ok, reason = rf.can_fetch(p)
        flag = "✓ 可以" if ok else "✗ 禁止"
        print(f"    {p:<26}{flag:<10}{reason}")

    sub("换成 GoodBot 的 UA")
    rf2 = parse_robots(demo, "GoodBot")
    print(f"    crawl-delay: {rf2.crawl_delay} 秒（比通用规则的 1 秒更宽松）")
    for p in ["/books/", "/admin/users"]:
        ok, reason = rf2.can_fetch(p)
        print(f"    {p:<26}{'✓ 可以' if ok else '✗ 禁止':<10}{reason}")

    sub("★ robots.txt 的三个关键认知")
    print("""    1. **它不是法律文件**
       遵守 robots 不是法律义务，但违反会成为『明知故犯』的有力证据。
       在多个判例中，无视 robots 都被法院作为主观恶意的考量因素。

    2. **它不是安全边界，而是意愿声明**
       robots 里写 Disallow 不代表技术上访问不了 —— 恰恰相反，
       遇到 Disallow 你就该停下，因为对方已经明确表达意愿。

    3. **Crawl-delay 是请求间隔，不是 QPS**
       `Crawl-delay: 1` 意思是『两次请求间隔至少 1 秒』，
       对应约 0.5 QPS（不是 1 QPS）。

    ▸ 正确做法：先请求 /robots.txt，解析后据此设置限速，
      并把 robots 规则纳入你的配置（阶段 3 的 pydantic-settings 直接用）。

    ▸ 如果 robots 里禁了你需要的路径怎么办？
      写邮件问对方要授权。很多站点的运营者会同意 —— 尤其是
      你说明用途、承诺频率、并提供联系方式时。这比硬爬靠谱得多。""")


def exp3_data_matrix() -> None:
    title("【实验 3】数据性质四级 —— 决定风险的不是技术，是数据")
    print("    ▸ 同一个技术手段，抓不同数据，风险可能差三个数量级。\n")

    for dt in DATA_MATRIX:
        print(f"    {'━' * 100}")
        print(f"    【{dt.level.value}】")
        print(f"      典型内容: {dt.examples}")
        print(f"      ✓ 可以做: {dt.examples_ok}")
        print(f"      ✗ 不能做: {dt.examples_bad}")
        print(f"      结论:     {dt.verdict}")

    sub("判决参考（公开判例的一般规律）")
    print("""    ┌──────────────────────────────┬────────────────────────────────┐
    │ 情形                         │ 一般后果                        │
    ├──────────────────────────────┼────────────────────────────────┤
    │ 公开数据 + 合理频率 + 未造成  │ 通常被认定为不违法或仅民事责任   │
    │ 影响 + 目的正当               │                                │
    │ 违反 robots + 高频 + 商业使用 │ 民事赔偿 / 不正当竞争           │
    │ 绕过登录/验证码获取数据        │ 可能构成刑事犯罪                │
    │ 抓取个人信息达数量标准         │ 侵犯公民个人信息罪              │
    │ 导致目标系统瘫痪               │ 破坏计算机信息系统罪            │
    └──────────────────────────────┴────────────────────────────────┘

    ▸ 有意思的一点：法院在判定时非常看重
      『是否突破技术保护措施』和『是否影响系统正常运行』。
      这两个因素比『抓了多少数据』更能决定案件性质。

    ▸ 也就是说：低频率 + 不绕技术措施，即使抓了较多公开数据，
      通常也只是民事层面的争议；而一旦绕过技术措施，
      性质就变了 —— 哪怕只抓了很少的数据。""")


def exp4_checklist() -> None:
    title("【实验 4】合规自查清单 —— 跑一遍你的项目")
    print(f"    共 {len(CHECKLIST)} 项，其中一票否决项 "
          f"{sum(1 for i in CHECKLIST if i.blocking)} 项。\n")

    for i, item in enumerate(CHECKLIST, 1):
        mark = "⛔" if item.blocking else ("⚠" if item.weight >= 3 else "  ")
        print(f"    {mark} {i:>2}. {item.question}")
        print(f"           └ 为何重要: {item.why}")

    sub("实测：三种典型项目的评分")
    print("    ▸ 场景 A：学术研究，抓公开论文元数据\n"
          "       遵守 robots、1 req/s、不加代理、不绕任何措施、数据不含个人信息")
    a_answers = {
        0: True,   # 公开数据
        1: True,   # 有官方 API（arXiv API）
        2: True,   # 遵守 robots
        3: True,   # 频率低
        4: True,   # 没用代理规避
        5: True,   # 没绕过访问控制
        6: True,   # 非商业竞争
        7: False,  # 会二次发布（未明确著作权）
        8: True,   # 不含个人信息
        9: True,
        10: True,
        11: True,
    }
    score_a, warn_a = run_checklist(a_answers)

    print("\n    ▸ 场景 B：商业项目，抓竞品商品价格用于定价参考\n"
          "       遵守 robots、2 req/s、用代理池轮换 IP、非商业敏感数据")
    b_answers = {
        0: True,   # 公开数据
        1: False,  # 目标站没有公开 API
        2: True,   # 遵守 robots
        3: True,
        4: False,  # ★ 用了代理池规避限频
        5: True,
        6: False,  # ★ 用于商业竞争
        7: True,
        8: True,
        9: False,  # ★ 不能接受被起诉的后果
        10: True,
        11: True,
    }
    score_b, warn_b = run_checklist(b_answers)

    print("\n    ▸ 场景 C：抓取用户评论中的昵称、主页与头像\n"
          "       未检查 robots、高并发、绕过登录墙获取更多数据")
    c_answers = {
        0: False,  # ★ 含个人信息
        1: False,
        2: False,  # ★ 未检查 robots
        3: False,  # ★ 高并发
        4: False,  # ★ 用代理
        5: False,  # ★ 绕过登录墙
        6: False,
        7: False,
        8: False,  # ★ 无合法性基础
        9: False,
        10: False,
        11: False,
    }
    score_c, warn_c = run_checklist(c_answers)

    def grade(s: int) -> str:
        if s >= 85:
            return "✓ 合规区间"
        if s >= 60:
            return "⚠ 需要整改"
        return "⛔ 高风险，建议停止"

    for name, score, warns in (
        ("A 学术研究（公开元数据）", score_a, warn_a),
        ("B 商业竞品比价", score_b, warn_b),
        ("C 抓取用户信息", score_c, warn_c),
    ):
        print(f"\n    {'━' * 96}")
        print(f"    {name}")
        print(f"      得分: {score}/100   评级: {grade(score)}")
        if warns:
            for w in warns:
                print(f"      {w}")
        else:
            print("      ✓ 无阻断项")

    sub("★ 清单的使用方式")
    print("""    1. 项目**开始前**跑一遍 —— 不要在出事之后再跑
    2. 任何一票否决项未满足 → 停下来，重新设计采集方案
    3. 得分 < 60 → 大概率会在某个环节出问题
    4. 把答案存档（附上时间戳与授权凭证）—— 万一被质疑，
       这是你『已尽合理注意义务』的最好证据

    ▸ 最重要的一条：
      如果某一项你回答得很勉强，那通常就是问题所在。
      别自我说服，那正是风险点。""")


def exp5_scenarios() -> None:
    title("【实验 5】三个真实场景的合规路径对比")
    print("    同一类需求，走不同路径，风险与成本差异巨大。\n")

    SCENARIOS: list[tuple[str, list[tuple[str, str, str]]]] = [
        (
            "场景一：我需要某电商的实时商品价格",
            [
                ("❌ 硬爬", "直接抓商品页，用代理池轮换 IP，5000 QPS",
                 "违反 robots、高频影响对方服务、可能构成不正当竞争。风险最高。"),
                ("⚠ 低配", "抓商品页，遵守 robots，1 req/s，不加代理",
                 "技术可行，但法律位置模糊。用于个人研究尚可，商业用途需谨慎。"),
                ("✓ 最优", "申请官方开放平台 API / 采购数据服务 / 对接比价平台",
                 "完全合规、数据质量稳定、有 SLA 保障。成本可预期，是唯一可持续的路径。"),
            ],
        ),
        (
            "场景二：我做研究，需要大量学术论文元数据",
            [
                ("❌ 硬爬", "抓 Google Scholar / 知网并绕开反爬",
                 "违反其服务条款，且知网等平台有明确法律维权记录。风险高。"),
                ("⚠ 低配", "抓出版社公开页面，遵守 robots，1 req/s",
                 "基本可行。注意著作权，论文元数据（标题/作者/DOI）风险低于全文。"),
                ("✓ 最优", "用 arXiv / Crossref / PubMed / OpenAlex 官方 API",
                 "完全免费、合规、数据规范、有速率说明。学术界标准做法。"),
            ],
        ),
        (
            "场景三：我想做一个小红书/微博数据分析工具",
            [
                ("❌ 硬爬", "抓 App 接口，逆向签名，用设备指纹池",
                 "很可能涉及《刑法》285 条（突破技术措施）；大量内容含个人信息。"),
                ("⚠ 低配", "抓公开网页内容，仅统计宏观指标，不保留个人信息",
                 "风险降低但仍需注意：内容著作权、平台服务条款、robots。"),
                ("✓ 最优", "找平台是否有开放数据/商业化 API；"
                           "或改用公开数据集（如公开的舆情数据集）",
                 "合规、可交付、可长期运营。很多『数据产品』其实是买 API 做二次加工。"),
            ],
        ),
    ]

    for title_text, options in SCENARIOS:
        print(f"    {'━' * 100}")
        print(f"    {title_text}")
        print(f"    {'━' * 100}")
        for tag, path, note in options:
            print(f"      {tag}")
            print(f"          路径: {path}")
            print(f"          评估: {note}")
        print()

    sub("★ 一条贯穿所有场景的原则")
    print("""    在动手写爬虫之前，先问自己一个问题：

        『这个数据，有没有一条**对方希望我走**的路？』

      答案几乎总是『有』：
        · 官方 API（最理想）
        · 开放数据平台 / 公开数据集
        · 数据服务商（付费）
        · 直接发邮件要授权（很多站点会同意）
        · 自己生产数据（问卷、众包、自建）

    ▸ 硬爬是**最后的选择**，不是第一选择。
      这不仅是合规要求，也是工程上的理性：
      硬爬的维护成本、被封风险、法律风险，通常远高于买数据。

    ▸ 教程到此为止 —— 后面（阶段 5、6）你学的是『怎么把
      合法的采集做得更快更稳』，而不是『怎么绕过更多防线』。""")


def exp6_responsibility() -> None:
    title("【实验 6】责任边界 —— 技术方与需求方")
    print("""    很多人以为『我只是接单写代码，出了事是需求方的问题』。
    这个认知在法律上站不住脚。

    ▸ 三种常见角色的风险：

    ┌────────────────┬──────────────────────────────────────────────┐
    │ 角色           │ 风险                                          │
    ├────────────────┼──────────────────────────────────────────────┤
    │ 需求方（甲方）  │ 组织、指使、获利 —— 通常是主责                 │
    │ 开发方（你）    │ 明知用途违法仍提供技术支持 → 可能构成共同犯罪   │
    │ 平台/工具提供方 │ 提供专门用于侵入的工具 → 可能构成帮助犯          │
    └────────────────┴──────────────────────────────────────────────┘

    ▸ 接单时的自保清单：

      1. 白纸黑字写清『数据用途、数据性质、采集范围』
         如果对方拒绝明确，这本身就是危险信号

      2. 保留沟通记录（聊天记录、邮件）
         证明你是按对方描述的需求实现，而非主动设计违法方案

      3. 拒绝这三类需求，直接拒绝：
         · 『帮我绕过登录 / 验证码 / 付费墙』
         · 『帮我抓某个 App 的接口，我提供逆向结果』
         · 『抓取用户手机号 / 身份证等个人信息』

      4. 报价时把『合规成本』算进去
         （申请 API 的时间、采购数据的费用、法律咨询），
         反而能给对方更专业的印象

      5. 如果你已经交付了有问题的项目：
         立即停止维护、书面提示风险、保留证据、必要时咨询律师

    ▸ 一句实用判断标准：
      『这个需求能不能坦然地跟对方老板、或者跟监管当面讲清楚？』
      如果讲的时候你自己都觉得别扭，那它就是有问题的。""")


def main() -> None:
    print(SEP)
    print("阶段 4 · 第 10 课：合规红线")
    print(SEP)
    print("""
本课不讲攻击手法，只讲边界、风险与合规路径。
技术能让你做到，但只有你自己能决定该不该做。

⚠ 本课为工程风险提示，不构成法律意见。
""")
    exp1_laws()
    exp2_robots()
    exp3_data_matrix()
    exp4_checklist()
    exp5_scenarios()
    exp6_responsibility()

    title("阶段 4 总结")
    print("""
【技术地图回顾】
  40  反爬全景图      6 层 17 种手段 + 排查决策树
  41  请求头与指纹    Sec-Ch-Ua 自洽性 / JA3 原理 / HeaderFactory
  42  Cookie 与会话   属性详解 / 三种会话管理 / JWT / SessionManager
  43  JS 逆向基础     调用栈 / 参数溯源 / Hook / 混淆对抗
  44  加密参数        MD5 家族 / AES 四要素矩阵 / RSA / 对齐验证器
  45  JS 补环境       Node 沙箱 / 渐进式修补 / 指纹陷阱 / Python 桥接
  46  验证码          难度阶梯实测 / Otsu / 滑块轨迹特征
  47  代理池          检测三要素 / 评分算法 / 调度策略 / 会话粘性
  48  Playwright      stealth / 等待策略 / 资源拦截 / 网络层取数
  49  合规红线        法律框架 / robots / 数据分级 / 自查清单

【三个最重要的结论】
  1. **排查顺序永远从易到难** —— 先试完所有低难度手段再考虑逆向
  2. **『能算出签名』≠『会被接受』** —— 指纹自洽性才是关键
  3. **合规成本低于对抗成本** —— 官方 API 通常是更理性的选择

  下一阶段（5）：数据工程 —— 采集只是起点，
                 把原始 HTML 变成可分析的结构化数据才是价值所在。
""")


if __name__ == "__main__":
    main()
