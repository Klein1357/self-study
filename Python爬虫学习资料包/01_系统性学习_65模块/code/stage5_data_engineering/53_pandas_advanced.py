"""
第 53 课 · Pandas 进阶 —— 从「会读 CSV」到「会做分析」

================================ 学习目标 ================================
1. 掌握 DataFrame 的「心智模型」：索引 + 列 + 对齐
2. 掌握数据加载（read_csv 的真实参数）与落盘（多格式）
3. 掌握筛选、排序、分组、聚合、透视、窗口函数
4. 掌握 merge/join/concat 与「数据丢失」的经典陷阱
5. 掌握 groupby 的 apply/transform/filter 三兄弟的区别
6. 知道什么时候**不该**用 Pandas（大数据 → Polars/DuckDB）

================================ 运行方式 ================================
    python3 code/stage5_data_engineering/53_pandas_advanced.py

依赖：pandas 3.0.0（已安装）
本课所有数据由脚本内部生成，不依赖外部文件，可直接运行。
"""

from __future__ import annotations

import io
import random
import time
from typing import Any

import numpy as np
import pandas as pd

# ============================================================================
# 心智模型：Pandas 到底在做什么
# ============================================================================
# 新手把 DataFrame 当成"Excel 表格"，于是写出 `for i in range(len(df))` 这种代码。
# 老手把它当成**带标签的二维数组 + 索引对齐引擎**。差别在哪？
#
#   ┌──────────────┬───────────────────────┬──────────────────────────┐
#   │              │  Excel 思维            │  Pandas 思维              │
#   ├──────────────┼───────────────────────┼──────────────────────────┤
#   │ 遍历          │ 逐行循环               │ 向量化整列运算            │
#   │ 索引          │ 行号                   │ 有语义的标签（可为多级）   │
#   │ 对齐          │ 无                     │ 按标签自动对齐（核心！）   │
#   │ 缺失值        │ 空单元格               │ NaN，参与运算会传染       │
#   └──────────────┴───────────────────────┴──────────────────────────┘
#
# 「自动对齐」是最容易被低估的特性，也是最容易出 bug 的地方：
# 两个 Series 相加，Pandas 是**按索引标签**对齐的，不是按位置。
# 索引不匹配的位置会变成 NaN —— 这在合并不同来源的数据时天天发生。
#
# 记住一句话：**能用向量化就用向量化，能不用循环就不用循环。**
# Pandas 的慢，90% 来自不该用循环的地方用了循环（iterrows 是重灾区）。


# ============================================================================
# 一、数据加载：read_csv 的真实参数
# ============================================================================
def load_with_options(csv_text: str) -> pd.DataFrame:
    """演示 read_csv 的关键参数。

    Args:
        csv_text: CSV 文本内容。

    Returns:
        解析后的 DataFrame。

    生产环境读 CSV 的必设参数（漏一个就出问题）：
      · dtype        ：显式指定类型。不指定的话 '0123' 会被当成整数 123，
                       丢掉前导零 —— 商品 SKU、邮编、手机号全靠这个救命。
      · na_values    ：哪些字符串算缺失。默认只认空字符串和 'NA'/'NaN'，
                       但真实数据里 'N/A'、'-'、'null'、'NULL' 都很常见。
      · keep_default_na：是否保留默认识别。设为 False 时只认你给的 na_values。
      · parse_dates  ：直接解析为日期。比读完再 astype 高效得多。
      · encoding     ：中文 CSV 在 Windows 上是 gbk，Linux 上是 utf-8，
                       还有带 BOM 的 utf-8-sig —— 编码错了就全是乱码。
      · thousands    ：千分位分隔符。'1,234' 不设这个参数会变成字符串。
      · low_memory   ：大文件设为 False 避免类型推断警告（代价是更吃内存）。
    """
    return pd.read_csv(
        io.StringIO(csv_text),
        dtype={"sku": str, "postcode": str},      # 保住前导零
        na_values=["", "N/A", "-", "null", "NULL", "暂无"],
        keep_default_na=True,
        parse_dates=["listed_at"],
        thousands=",",
    )


RAW_CSV = """sku,title,price,sales,rating,listed_at,shop,postcode
0123,Deep Work,"£1,234.56",1.2k,4.5,2024-01-15,Alpha,012345
0456,Clean Code,£31.99,890,5.0,2024-02-20,Beta,023456
0789,Refactoring,£55.00,3.5万,4.8,2024-03-05,Alpha,034567
1011,Sharp Objects,£47.82,12,-,2024-01-30,Gamma,045678
1213,Sapiens,£12.99,156,4.2,2024-04-11,Beta,056789
1415,Moneyball,£9.99,N/A,3.8,2024-05-02,Alpha,
1617,The Pragmatic Programmer,£38.00,2.5k,5.0,2024-06-18,Gamma,067890
1819,Unknown Book,,45,4.0,2024-07-22,Beta,078901
"""


def exp1_loading() -> None:
    """实验 1：加载 CSV 的坑 —— 前导零、千分位、缺失值。"""
    print("=" * 74)
    print("实验 1 · 数据加载：read_csv 的三个致命默认值")
    print("=" * 74)

    print("\n  --- 原始 CSV 文本 ---")
    for line in RAW_CSV.strip().split("\n")[:4]:
        print(f"    {line}")
    print("    ...")

    # ---- 错误做法：什么都不设 ----
    print("\n  --- ✗ 默认参数的后果 ---")
    bad = pd.read_csv(io.StringIO(RAW_CSV))
    print(f"    sku 列 dtype = {bad['sku'].dtype}，"
          f"首值 = {bad['sku'].iloc[0]!r}  ← 前导零丢了！")
    print(f"    postcode 列 dtype = {bad['postcode'].dtype}，"
          f"首值 = {bad['postcode'].iloc[0]!r}  ← 前导零也丢了！")
    print(f"    price 列 dtype = {bad['price'].dtype}  ← 带 £ 和逗号，"
          f"只能是字符串，没法参与计算")
    print(f"    rating 列的 '-' 被当作 {bad['rating'].iloc[3]!r}  "
          f"← 字符串混进数值列，整列退化成 object")
    print(f"    listed_at 列 dtype = {bad['listed_at'].dtype}  ← 还是字符串")

    # ---- 正确做法 ----
    print("\n  --- ✓ 设置正确参数后 ---")
    good = load_with_options(RAW_CSV)
    print(f"    sku 列 dtype = {good['sku'].dtype}，"
          f"首值 = {good['sku'].iloc[0]!r}  ✓ 前导零保住了")
    print(f"    postcode 首值 = {good['postcode'].iloc[0]!r}  ✓")
    print(f"    listed_at dtype = {good['listed_at'].dtype}  ✓ 已解析为日期")
    print(f"    rating 的 '-' 变成 {good['rating'].iloc[3]!r}  ✓ NaN，"
          f"整列仍是 {good['rating'].dtype}")

    print("\n  --- 数据概览 ---")
    print(good.to_string(index=False, max_colwidth=24))

    print("\n  --- 类型与缺失值 ---")
    info = pd.DataFrame({
        "dtype": good.dtypes.astype(str),
        "非空数": good.notna().sum(),
        "缺失数": good.isna().sum(),
        "缺失率": (good.isna().mean() * 100).round(1).astype(str) + "%",
    })
    print(info.to_string())

    print("\n  ▸ 三条铁律：")
    print("      ① ID 类字段（SKU/邮编/手机号）**必须** dtype=str，否则前导零丢失")
    print("      ② 缺失值标记要显式列举，默认只认 ''/NA/NaN，漏了 N/A 和 - 就炸")
    print("      ③ 日期用 parse_dates 一次到位，别读完再转换")


# ============================================================================
# 二、构建分析用的数据集
# ============================================================================
def build_sales_df(n: int = 600, seed: int = 42) -> pd.DataFrame:
    """构造一个用于演示的销售明细表。

    Args:
        n: 记录数。
        seed: 随机种子，保证可复现。

    Returns:
        含 date/region/category/product/qty/unit_price 的 DataFrame。

    为什么用合成数据而不是真实数据？
      教学需要**可控的分布**来演示分组/透视的效果。
      真实数据的分布往往是歪的，反而看不清聚合逻辑。
    """
    rng = np.random.default_rng(seed)
    random.seed(seed)

    regions = ["华东", "华北", "华南", "西南"]
    # 有意让各区域的权重不同，这样分组统计才有对比价值
    region_weights = [0.40, 0.25, 0.22, 0.13]

    categories = ["电子", "家居", "服饰", "食品"]
    products = {
        "电子": ["耳机", "键盘", "充电宝"],
        "家居": ["台灯", "抱枕", "收纳盒"],
        "服饰": ["T恤", "外套", "围巾"],
        "食品": ["咖啡豆", "坚果", "茶叶"],
    }
    base_price = {
        "耳机": 399.0, "键盘": 259.0, "充电宝": 129.0,
        "台灯": 89.0, "抱枕": 59.0, "收纳盒": 39.0,
        "T恤": 99.0, "外套": 459.0, "围巾": 79.0,
        "咖啡豆": 118.0, "坚果": 68.0, "茶叶": 158.0,
    }

    # ⚠ random.choice 不能直接吃 DatetimeIndex（会触发 __bool__ 歧义错误），
    #   必须先转成 list。这是个很典型的「pandas 对象不是普通序列」的坑。
    dates = list(pd.date_range("2024-01-01", periods=180, freq="D"))
    rows: list[dict[str, Any]] = []
    for _ in range(n):
        region = random.choices(regions, weights=region_weights)[0]
        cat = random.choice(categories)
        prod = random.choice(products[cat])
        # 周末销量更高 —— 制造一个真实的业务规律供后续分析发现
        d = random.choice(dates)
        weekend_boost = 1.6 if d.dayofweek >= 5 else 1.0
        qty = max(1, int(rng.poisson(3 * weekend_boost)))
        # 价格围绕基准价波动 ±15%
        price = round(base_price[prod] * float(rng.normal(1.0, 0.15)), 2)
        price = max(9.9, price)
        rows.append({
            "date": d,
            "region": region,
            "category": cat,
            "product": prod,
            "qty": qty,
            "unit_price": price,
        })

    df = pd.DataFrame(rows)
    df["amount"] = (df["qty"] * df["unit_price"]).round(2)
    return df.sort_values("date").reset_index(drop=True)


# ============================================================================
# 三、筛选 / 排序 / 派生列
# ============================================================================
def exp2_select_filter() -> None:
    """实验 2：筛选、排序、派生列 —— loc/iloc/query 的正确用法。"""
    print("\n" + "=" * 74)
    print("实验 2 · 筛选 / 排序 / 派生列")
    print("=" * 74)

    df = build_sales_df()
    print(f"\n  数据集：{len(df)} 行 × {len(df.columns)} 列")
    print(f"    日期范围：{df['date'].min():%Y-%m-%d} ~ {df['date'].max():%Y-%m-%d}")
    print(f"    总销售额：¥{df['amount'].sum():,.2f}")

    # ---- 筛选的三种写法 ----
    print("\n  --- 筛选的三种写法 ---")

    # ① 布尔索引（最常用）
    mask1 = (df["category"] == "电子") & (df["amount"] > 1000)
    r1 = df[mask1]
    print(f"    ① 布尔索引 (& | ~)          : {len(r1)} 行")

    # ② query（可读性最好，适合复杂条件）
    r2 = df.query("category == '电子' and amount > 1000")
    print(f"    ② query('... and ...')      : {len(r2)} 行  ← 可读性最好")

    # ③ isin（枚举匹配）
    r3 = df[df["region"].isin(["华东", "华南"])]
    print(f"    ③ isin([...])               : {len(r3)} 行")

    print("\n    ▸ 布尔索引的坑：**位运算符不是 and/or！**")
    print("      写成 df[(df.a == 1) and (df.b == 2)] 会抛")
    print("      'The truth value of a Series is ambiguous' ——")
    print("      因为 Python 的 and 要求返回单个布尔值，而 Series 是一串布尔值。")
    print("      必须用 & / | / ~，并且**每个条件都要加括号**（优先级问题）。")

    # ---- 排序：多列 + 升降序混合 ----
    print("\n  --- 多列排序（含升降序混合）---")
    r4 = df.sort_values(["region", "amount"], ascending=[True, False])
    print("    按 region 升序、amount 降序，各区域 Top1：")
    top1 = r4.groupby("region", as_index=False).first()
    print(top1[["region", "product", "amount"]].to_string(index=False))

    print("\n    ▸ sort_values 的坑：默认 kind='quicksort' 是**不稳定排序**。")
    print("      如果只按一列排、又期望「相同值的行保持原顺序」，")
    print("      必须显式指定 kind='mergesort'（稳定）。否则结果每次可能不同。")

    # ---- 派生列：向量化的力量 ----
    print("\n  --- 派生列：向量化 vs 循环 ---")
    N = 200_000
    big = pd.DataFrame({
        "qty": np.random.randint(1, 10, N),
        "price": np.random.uniform(10, 500, N).round(2),
    })

    t0 = time.perf_counter()
    big["amount_vec"] = big["qty"] * big["price"]         # 向量化
    t_vec = time.perf_counter() - t0

    t0 = time.perf_counter()
    # 反面教材：iterrows 逐行循环
    vals = []
    for _, row in big.head(5000).iterrows():
        vals.append(row["qty"] * row["price"])
    t_loop_5k = time.perf_counter() - t0
    t_loop_extrapolated = t_loop_5k * (N / 5000)

    print(f"    {N:,} 行计算 amount 的耗时：")
    print(f"      向量化（df.qty * df.price）: {t_vec * 1000:9.2f} ms")
    print(f"      iterrows 循环（外推）      : {t_loop_extrapolated * 1000:9.2f} ms")
    if t_vec > 0:
        print(f"      向量化快约 {t_loop_extrapolated / t_vec:,.0f} 倍")

    print("\n    ▸ 为什么差这么多个数量级？")
    print("      iterrows 每次迭代都要**构造一个 Series 对象**（装箱），")
    print("      而向量化直接在底层 NumPy 数组上跑 C 循环。")
    print("      经验值：iterrows 比向量化慢 1000-10000 倍，")
    print("      它是 Pandas 里最该被禁用的 API ——")
    print("      真需要逐行处理时用 itertuples()（快 10 倍以上）或者干脆别用 Pandas。")


def exp3_groupby() -> None:
    """实验 3：groupby 三兄弟 —— agg / transform / filter。"""
    print("\n" + "=" * 74)
    print("实验 3 · groupby 三兄弟：agg / transform / filter")
    print("=" * 74)

    df = build_sales_df()

    # ---------- ① agg：多列多函数聚合 ----------
    print("\n  --- ① agg：把每个组压成一个值 ---")
    agg_result = df.groupby("region").agg(
        订单数=("amount", "count"),
        总销售额=("amount", "sum"),
        平均单价=("unit_price", "mean"),
        最大单笔=("amount", "max"),
    ).round(2)
    agg_result["销售额占比"] = (
        agg_result["总销售额"] / agg_result["总销售额"].sum() * 100
    ).round(1).astype(str) + "%"
    print(agg_result.to_string())

    print("\n    ▸ agg 的结果行数 = 组数（4 个区域 → 4 行）。")
    print("      这是「降维」操作：600 行 → 4 行。")

    # ---------- ② transform：保持行数不变，把组统计广播回每行 ----------
    print("\n  --- ② transform：行数不变，组统计广播到每行 ---")
    df2 = df.copy()
    df2["region_avg"] = df2.groupby("region")["amount"].transform("mean").round(2)
    df2["region_total"] = df2.groupby("region")["amount"].transform("sum").round(2)
    df2["占本区比重"] = (df2["amount"] / df2["region_total"] * 100).round(2)
    # 与区域均值的偏离度 —— 常用于找异常
    df2["偏离均值"] = ((df2["amount"] - df2["region_avg"]) / df2["region_avg"] * 100).round(1)

    print(f"    原始行数 {len(df)} → transform 后行数 {len(df2)}  （不变 ✓）")
    print("\n    每区域偏离均值最大的 1 笔：")
    idx = df2.groupby("region")["偏离均值"].idxmax()
    show = df2.loc[idx, ["region", "product", "amount", "region_avg", "偏离均值"]]
    print(show.to_string(index=False))

    print("\n    ▸ transform 的价值：**你能在同一行里同时看到「个体值」和「组统计」**。")
    print("      没有它，你需要 merge 回去 —— transform 就是那个 merge 的快捷方式。")
    print("      典型用途：算占比、算偏离度、组内标准化（z-score）。")

    # ---------- ③ filter：按组的整体特征筛掉整组 ----------
    print("\n  --- ③ filter：按组特征筛选，整组保留或整组丢弃 ---")
    big_regions = df.groupby("region").filter(lambda g: g["amount"].sum() > 100_000)
    small_regions = df.groupby("region").filter(lambda g: g["amount"].sum() <= 100_000)
    print(f"    总销售额 > 10万的区域：{sorted(big_regions['region'].unique())}，"
          f"共 {len(big_regions)} 行")
    print(f"    总销售额 ≤ 10万的区域：{sorted(small_regions['region'].unique())}，"
          f"共 {len(small_regions)} 行")

    print("\n    ▸ filter 与布尔索引的区别：")
    print("      filter 的判断函数接收的是**整个子 DataFrame**，返回一个 bool，")
    print("      决定这一组是整体留下还是整体丢弃 —— 不会拆散组。")
    print("      而布尔索引是逐行判断，可能把一组拆得七零八落。")

    # ---------- 对比总结表 ----------
    print("\n  --- 三兄弟对比 ---")
    compare = pd.DataFrame([
        ("agg", "组数", "把组压成一个值", "算区域总额、平均值"),
        ("transform", "原行数", "把组统计广播回每行", "算占本区比重、组内标准化"),
        ("filter", "≤原行数", "按组条件整体保留/丢弃", "筛掉样本量过小的组"),
    ], columns=["方法", "结果行数", "语义", "典型用途"])
    print(compare.to_string(index=False))


def exp4_pivot() -> None:
    """实验 4：透视表与交叉表 —— Excel 数据透视的代码版。"""
    print("\n" + "=" * 74)
    print("实验 4 · pivot_table / crosstab / melt")
    print("=" * 74)

    df = build_sales_df()
    df["month"] = df["date"].dt.to_period("M").astype(str)

    # ---------- pivot_table ----------
    print("\n  --- pivot_table：区域 × 品类 的销售额矩阵 ---")
    pt = pd.pivot_table(
        df, index="region", columns="category",
        values="amount", aggfunc="sum", fill_value=0, margins=True,
        margins_name="合计",
    ).round(0)
    print(pt.to_string())

    print("\n    ▸ 参数含义：")
    print("      index    = 行维度（相当于 GROUP BY 的列）")
    print("      columns  = 列维度（把行变成列，这是「透视」的核心）")
    print("      values   = 要聚合的数值列")
    print("      aggfunc  = 聚合函数，可以是 dict 给不同列不同函数")
    print("      margins  = 加「合计」行列（Excel 数据透视的『总计』）")
    print("      fill_value = 没有数据的格子填什么（默认 NaN，改 0 更好看）")

    # ---------- 多维度 + 多指标 ----------
    print("\n  --- 多维透视：区域 × 月份，同时看销售额和订单数 ---")
    pt2 = pd.pivot_table(
        df, index="region", columns="month",
        values=["amount", "qty"], aggfunc={"amount": "sum", "qty": "sum"},
        fill_value=0,
    ).round(0)
    print(pt2.to_string())

    print("\n    ▸ 多指标透视会产生**多级列索引**（MultiIndex columns）。")
    print(f"      当前列索引层级：{pt2.columns.nlevels} 级")
    print(f"      列名示例：{list(pt2.columns[:3])}")
    print("      取值时要两级都要给：pt2[('amount', '2024-01')]")

    # ---------- 用 stack 把交叉表还原成长表 ----------
    print("\n  --- 还原为长表（stack）---")
    long = pt.drop(index="合计", columns="合计").stack().reset_index()
    long.columns = ["region", "category", "amount"]
    print(long.head(6).to_string(index=False))
    print(f"    宽表 {pt.shape} → 长表 {long.shape}")
    print("    ▸ 这就是 pandas 的 melt/stack 方向：宽 → 长，便于画图。")
    print("      反过来 long → wide 用 pivot()（注意不是 pivot_table）。")

    # ---------- crosstab：频次统计的专用工具 ----------
    print("\n  --- crosstab：区域 × 品类 的订单**笔数** ---")
    ct = pd.crosstab(df["region"], df["category"], margins=True,
                     margins_name="合计")
    print(ct.to_string())
    print("\n    ▸ crosstab 专为「计数」设计，等价于")
    print("      pivot_table(index=..., columns=..., values=..., aggfunc='count')")
    print("      但写起来短得多。它还支持 normalize='index'/'columns'/'all'")
    print("      直接输出比例 —— 做占比分析时非常顺手。")

    print("\n  --- crosstab 归一化：行内占比 ---")
    ct_norm = pd.crosstab(df["region"], df["category"], normalize="index")
    print((ct_norm * 100).round(1).astype(str) + "%")


def exp5_merge_concat() -> None:
    """实验 5：merge / join / concat 与「数据凭空消失」陷阱。"""
    print("\n" + "=" * 74)
    print("实验 5 · 合并数据：merge / join / concat")
    print("=" * 74)

    df = build_sales_df(n=200)
    region_info = pd.DataFrame({
        "region": ["华东", "华北", "华南"],
        "manager": ["张三", "李四", "王五"],
        "target": [120_000, 90_000, 80_000],
    })
    print("\n  --- 待合并的两张表 ---")
    print(f"    销售表：{len(df)} 行，区域 {sorted(df['region'].unique())}")
    print(f"    区域表：{len(region_info)} 行，区域 {sorted(region_info['region'].unique())}")
    print("    ⚠ 注意：销售表里有「西南」，区域表里**没有**。")

    # ---------- INNER vs LEFT ----------
    inner = df.merge(region_info, on="region", how="inner")
    left = df.merge(region_info, on="region", how="left")
    print("\n  --- 四种 how 的对比 ---")
    for how in ("inner", "left", "right", "outer"):
        m = df.merge(region_info, on="region", how=how)
        print(f"    how={how:<6} → {len(m):>4} 行"
              + ("   ← 西南的数据全丢了！" if how == "inner" else "")
              + ("   ← 保持左表全部，西南的 manager 是 NaN"
                 if how == "left" else ""))

    print("\n    ▸ **默认 how='inner'，这是 merge 最大的坑。**")
    print("      你以为在「补充区域信息」，实际是「取交集」，")
    print("      把没有匹配的行整行删掉了 —— 而且不报错、不警告。")
    print("      爬虫场景里外键对不上是常态（分类改名、站点改版），")
    print("      默认应该用 how='left'，除非你明确就是要交集。")

    # ---------- 检测未匹配 ----------
    print("\n  --- 如何主动发现「没匹配上」的行 ---")
    unmatched = df.merge(region_info, on="region", how="left", indicator=True)
    lost = unmatched[unmatched["_merge"] == "left_only"]
    print(f"    indicator=True 会自动加一列 _merge，取值：")
    print(f"      both       = 两边都有  ({len(unmatched[unmatched['_merge'] == 'both'])} 行)")
    print(f"      left_only  = 只有左边  ({len(lost)} 行)  ← 这些就是会丢的数据")
    print(f"      right_only = 只有右边  ({len(unmatched[unmatched['_merge'] == 'right_only'])} 行)")
    print(f"    未匹配的区域：{sorted(lost['region'].unique())}")
    print("\n    ▸ 生产建议：**所有 merge 都加 indicator=True**，")
    print("      然后断言 left_only 的数量在预期内。")
    print("      这是一行代码换来的数据完整性保险，性价比极高。")

    # ---------- 重复键导致的「行数爆炸」----------
    print("\n  --- 更隐蔽的坑：右表有重复键 → 行数爆炸 ---")
    dup_info = pd.DataFrame({
        "region": ["华东", "华东", "华北"],       # 华东重复了！
        "channel": ["线上", "线下", "线上"],
    })
    exploded = df.merge(dup_info, on="region", how="left")
    print(f"    左表 {len(df)} 行 × 右表华东有 2 条 → 合并后 {len(exploded)} 行")
    print(f"    华东的行数：{len(df[df['region'] == '华东'])} → "
          f"{len(exploded[exploded['region'] == '华东'])}（翻倍）")
    print("\n    ▸ merge 是**笛卡尔积**：左表 1 行 × 右表 2 行 = 2 行。")
    print("      如果你的汇总数字突然翻倍，第一个要查的就是右表有没有重复键。")
    print("      防御手段：merge 前先 df.drop_duplicates(subset=['key']) 或断言")
    print("                right['key'].is_unique == True")

    # ---------- concat ----------
    print("\n  --- concat：纵向堆叠（加行）与横向拼接（加列）---")
    df_a = build_sales_df(n=50, seed=1)
    df_b = build_sales_df(n=50, seed=2)
    stacked = pd.concat([df_a, df_b], ignore_index=True)
    print(f"    纵向 concat：[{len(df_a)}] + [{len(df_b)}] = {len(stacked)} 行")
    print("      ▸ ignore_index=True 很重要，否则索引会重复（0..49, 0..49）")

    side = pd.concat([df_a.reset_index(drop=True),
                      df_b.reset_index(drop=True)], axis=1)
    print(f"    横向 concat：{df_a.shape} + {df_b.shape} = {side.shape}  "
          f"（列数相加）")
    print("      ▸ axis=1 是**按位置**对齐的，不看索引标签！")
    print("        如果两边索引不同（比如一个是 0..49 一个是 0..99），")
    print("        会产生大量 NaN —— 要横向合并请优先用 merge(how='outer')。")


def exp6_window() -> None:
    """实验 6：时间序列与窗口计算 —— 环比、移动平均、累计。"""
    print("\n" + "=" * 74)
    print("实验 6 · 时间序列：重采样、移动平均、环比")
    print("=" * 74)

    df = build_sales_df()

    # ---------- 重采样 ----------
    daily = df.set_index("date")["amount"].resample("D").sum()
    weekly = df.set_index("date")["amount"].resample("W").sum()
    monthly = df.set_index("date")["amount"].resample("ME").sum()
    print(f"\n  重采样：")
    print(f"    日频 {len(daily)} 个点  （原始就是日粒度，没减少）")
    print(f"    周频 {len(weekly)} 个点")
    print(f"    月频 {len(monthly)} 个点")

    print("\n  ▸ resample 的三个要点：")
    print("      ① 必须先设日期列为 index（set_index）")
    print("      ② 'M' 已被废弃，月末要用 'ME'（Month End）；")
    print("         同理 'A'→'YE'、'H'→'h'、'T'→'min'  —— pandas 2.2+ 的改动")
    print("      ③ resample 默认对每个桶求和/平均，桶内没数据的会变 NaN")
    print("         或 0（取决于是否 .fillna(0)）")

    # ---------- 移动平均与环比 ----------
    print("\n  --- 移动平均 + 环比（monthly）---")
    m = monthly.to_frame("销售额")
    m["3月移动平均"] = m["销售额"].rolling(window=3, min_periods=1).mean().round(0)
    m["环比"] = m["销售额"].pct_change().round(4)
    m["环比%"] = (m["环比"] * 100).round(1).astype(str) + "%"
    m["累计"] = m["销售额"].cumsum().round(0)
    print(m[["销售额", "3月移动平均", "环比%", "累计"]].to_string())

    print("\n    ▸ rolling(window=3) 用的是**前 3 行（含当前行）**，不是居中窗口。")
    print("      要居中用 center=True。")
    print("      min_periods=1 表示「不足 3 个也行」，否则前 2 行会是 NaN。")
    print("\n    ▸ pct_change() 默认与**前一行**比（periods=1）。")
    print("      注意它算的是 (now - prev) / prev，prev=0 时会得到 inf，要处理。")

    # ---------- 分组内的窗口计算 ----------
    print("\n  --- 分组内环比（每个区域各自的月度环比）---")
    df["month"] = df["date"].dt.to_period("M").astype(str)
    by_region = (df.groupby(["region", "month"])["amount"].sum()
                 .reset_index().sort_values(["region", "month"]))
    by_region["环比%"] = (by_region.groupby("region")["amount"]
                          .pct_change() * 100).round(1)
    print(by_region.head(10).to_string(index=False))

    print("\n    ▸ 关键点：pct_change 前必须先 groupby(region)。")
    print("      否则区域边界处会算出「华东最后一个月 → 华北第一个月」的环比，")
    print("      那个数字毫无意义 —— 这是分组时序分析的经典 bug。")

    # ---------- 周末效应验证 ----------
    print("\n  --- 验证「周末销量更高」这个假设 ---")
    df["is_weekend"] = df["date"].dt.dayofweek >= 5
    weekend_stat = df.groupby("is_weekend").agg(
        天数=("date", "nunique"),
        总销售额=("amount", "sum"),
        平均单笔=("amount", "mean"),
        总件数=("qty", "sum"),
    ).round(2)
    weekend_stat.index = ["工作日", "周末"]
    weekend_stat["日均销售额"] = (
        weekend_stat["总销售额"] / weekend_stat["天数"]
    ).round(0)
    print(weekend_stat.to_string())

    if len(weekend_stat) == 2:
        ratio = (weekend_stat.loc["周末", "日均销售额"]
                 / weekend_stat.loc["工作日", "日均销售额"])
        print(f"\n    周末/工作日 日均销售额比 = {ratio:.2f} 倍")
        print("    ▸ 数据确实呈现周末效应（构造数据时故意注入的）。")
        print("      这就是数据分析的价值：**用数据验证假设，而不是凭感觉。**")


def exp7_when_not_pandas() -> None:
    """实验 7：什么时候不该用 Pandas —— 性能边界实测。"""
    print("\n" + "=" * 74)
    print("实验 7 · Pandas 的性能边界（什么时候该换工具）")
    print("=" * 74)

    print("\n  --- 实测：不同数据量下的 groupby 耗时 ---")
    results = []
    for n in (10_000, 100_000, 500_000, 1_000_000):
        d = pd.DataFrame({
            "k": np.random.randint(0, 100, n),
            "v": np.random.uniform(0, 1000, n),
        })
        t0 = time.perf_counter()
        _ = d.groupby("k")["v"].agg(["sum", "mean", "count"])
        dt = time.perf_counter() - t0
        mem = d.memory_usage(deep=True).sum() / 1024 / 1024
        results.append((n, dt, mem))
        print(f"    {n:>9,} 行 → {dt * 1000:>9.2f} ms，内存 {mem:>7.1f} MB")

    print("\n  ▸ 经验边界（单机、常规硬件）：")
    print("      · < 100 万行       → Pandas 完全够用，别折腾")
    print("      · 100 万 ~ 5000万  → Pandas 能跑但吃力，考虑 Polars / DuckDB")
    print("      · > 5000 万行      → 必须换：Polars / DuckDB / Spark")
    print("\n  ▸ 三个换工具的明确信号：")
    print("      ① 内存占用超过可用内存的 1/3（Pandas 的中间结果会翻倍占用）")
    print("      ② 单次 groupby 超过 10 秒且需要反复跑")
    print("      ③ 数据在数据库里却要先全捞出来再分析")
    print("         → 这种情况直接用 SQL 聚合（第 50 课），别捞出来")
    print("\n  ▸ 但请注意：**Pandas 慢的是内存和单线程，不是逻辑表达力。**")
    print("    本课讲的 groupby / pivot / merge / rolling 心智模型，")
    print("    在 Polars 里是一一对应的 —— 学会 Pandas 不等于白学。")
    print("    Polars 的主要差异是「惰性求值 + 多线程 + 表达式 API」，")
    print("    但那属于另一个话题，本课程不展开。")

    # 演示 memory_usage 的具体含义
    print("\n  --- 内存诊断：object 列是个内存黑洞 ---")
    n = 200_000
    words = ["apple", "banana", "cherry", "date", "elderberry"]
    s_obj = pd.Series(np.random.choice(words, n))
    s_cat = s_obj.astype("category")
    mem_obj = s_obj.memory_usage(deep=True) / 1024 / 1024
    mem_cat = s_cat.memory_usage(deep=True) / 1024 / 1024
    print(f"    字符串列（object）  ：{mem_obj:7.2f} MB，"
          f"唯一值 {s_obj.nunique()} 个")
    print(f"    转为 category       ：{mem_cat:7.2f} MB")
    print(f"    节省 {100 * (1 - mem_cat / mem_obj):.1f}%")
    print("\n    ▸ 低基数（唯一值少）的字符串列转 category 是免费的午餐：")
    print("      内存降一个数量级，groupby 也更快（比整数还快）。")
    print("      典型的适用列：地区、品类、站点名、状态码。")
    print("      注意：深拷贝 deep=True 才能看到真实占用，")
    print("            默认的 memory_usage() 只算指针大小，会严重低估。")


def main() -> None:
    """运行全部实验。"""
    exp1_loading()
    exp2_select_filter()
    exp3_groupby()
    exp4_pivot()
    exp5_merge_concat()
    exp6_window()
    exp7_when_not_pandas()

    print("\n" + "=" * 74)
    print("本课要点")
    print("=" * 74)
    for line in [
        "1. 把 DataFrame 当「带标签的二维数组 + 索引对齐引擎」，不是 Excel",
        "2. ID 类字段必须 dtype=str，否则前导零静默丢失",
        "3. 缺失值标记要显式列举：'', N/A, -, null, 暂无 全都常见",
        "4. 布尔索引用 & | ~ 并加括号，不能用 and/or（会报 ambiguous）",
        "5. sort_values 默认不稳定排序，要保持原序得用 kind='mergesort'",
        "6. iterrows 比向量化慢 1000-10000 倍，能用向量化就别循环",
        "7. agg 降维、transform 保持行数、filter 整组取舍 —— 三兄弟别混用",
        "8. pivot_table 的 index/columns/values/aggfunc 是透视四要素",
        "9. merge 默认 how='inner' 会静默删数据，爬虫场景默认改 'left'",
        "10. 所有 merge 加 indicator=True，主动发现未匹配的行",
        "11. 右表有重复键会让 merge 行数爆炸（笛卡尔积），merge 前先断言唯一性",
        "12. 分组算环比必须先 groupby，否则跨组算出无意义的数字",
        "13. 低基数字符串列转 category，内存降一个数量级",
        "14. 超过 5000 万行换 Polars/DuckDB；能 SQL 聚合就别捞出来算",
    ]:
        print("  " + line)


if __name__ == "__main__":
    main()
