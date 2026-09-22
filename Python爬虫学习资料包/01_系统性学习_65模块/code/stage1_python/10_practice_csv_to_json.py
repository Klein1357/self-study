"""阶段 1 综合练习：把数据从 CSV 处理到 JSON。

运行：python3 10_practice_csv_to_json.py

这是阶段 1 的毕业作业，综合运用：
  · 文件读写（pathlib + csv + json）
  · 数据结构（列表 + 字典）
  · 函数（参数、返回值、类型注解）
  · 异常处理（try/except）
  · 面向对象（dataclass）
  · 列表推导式与排序

完成它，你就具备了进入阶段 2 的基础能力。

场景：你拿到一份商品数据 CSV，需要清洗、计算、导出成 JSON。
      这个场景在真实爬虫项目里每天都会遇到。
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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



# ============================================================
# 1. 数据结构定义（对应 1.7 面向对象）
# ============================================================
@dataclass
class Product:
    """商品数据模型。

    Args:
        name: 商品名称
        category: 分类
        price: 价格（元）
        stock: 库存数量
    """

    name: str
    category: str
    price: float
    stock: int

    @property
    def total_value(self) -> float:
        """库存总货值 = 单价 × 库存。"""
        return round(self.price * self.stock, 2)

    @property
    def is_low_stock(self) -> bool:
        """是否低库存（少于 10 件）。"""
        return self.stock < 10


# ============================================================
# 2. 准备测试数据（对应 1.6 文件操作）
# ============================================================
def create_sample_csv(path: Path) -> None:
    """生成一份含"脏数据"的测试 CSV。

    故意混入几种真实项目常见的问题：
      · 空行
      · 价格带货币符号
      · 库存为空
      · 重复记录

    Args:
        path: 输出文件路径
    """
    rows = [
        ["商品名称", "分类", "价格", "库存"],
        ["机械键盘", "外设", "399.00", "25"],
        ["无线鼠标", "外设", "129.50", "8"],
        ["显示器", "显示设备", "1599.00", "12"],
        ["", "", "", ""],                          # 空行
        ["耳机", "音频", "¥599.00", "5"],          # 带货币符号
        ["摄像头", "外设", "289.00", ""],          # 库存空缺
        ["机械键盘", "外设", "399.00", "25"],      # 重复记录
        ["音箱", "音频", "899.00", "3"],
        ["U盘", "存储", "89.90", "150"],
    ]

    # encoding="utf-8-sig" 让 Excel 能正常打开中文
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"✓ 已生成测试数据: {path}")


# ============================================================
# 3. 读取与清洗（对应 1.5 异常处理 + 1.6 文件操作）
# ============================================================
def clean_price(raw: str) -> float:
    """把各种格式的价格文本转成浮点数。

    处理情况：
      "399.00"    → 399.0
      "¥599.00"   → 599.0
      "￥199"     → 199.0
      "1,299.00"  → 1299.0

    Args:
        raw: 原始价格文本

    Returns:
        清理后的价格

    Raises:
        ValueError: 无法解析为数字时抛出
    """
    # 去掉货币符号和千分位逗号
    cleaned = raw.strip()
    for symbol in ("¥", "￥", "$", "£", ","):
        cleaned = cleaned.replace(symbol, "")

    if not cleaned:
        raise ValueError("价格为空")

    return float(cleaned)


def load_products(csv_path: Path) -> tuple[list[Product], list[str]]:
    """从 CSV 读取商品数据，并做清洗。

    清洗规则：
      · 跳过完全空白的行
      · 价格缺失或非法 → 丢弃该条
      · 库存缺失 → 默认为 0
      · 名称缺失 → 丢弃该条

    Args:
        csv_path: CSV 文件路径

    Returns:
        (有效商品列表, 问题记录列表) 元组
    """
    products: list[Product] = []
    issues: list[str] = []  # 记录被丢弃的数据，便于排查

    with csv_path.open("r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for line_no, row in enumerate(reader, start=2):  # 从 2 开始：跳过表头
            name = (row.get("商品名称") or "").strip()

            # 规则 1：名称为空 → 跳过（可能是空行）
            if not name:
                issues.append(f"第 {line_no} 行：名称为空，已跳过")
                continue

            # 规则 2：价格解析失败 → 丢弃
            try:
                price = clean_price(row.get("价格") or "")
            except ValueError as exc:
                issues.append(f"第 {line_no} 行《{name}》：价格异常（{exc}），已丢弃")
                continue

            # 规则 3：库存缺失 → 默认 0
            stock_raw = (row.get("库存") or "").strip()
            try:
                stock = int(stock_raw) if stock_raw else 0
                if not stock_raw:
                    issues.append(f"第 {line_no} 行《{name}》：库存缺失，按 0 处理")
            except ValueError:
                stock = 0
                issues.append(f"第 {line_no} 行《{name}》：库存格式错误，按 0 处理")

            products.append(Product(
                name=name,
                category=(row.get("分类") or "未分类").strip() or "未分类",
                price=price,
                stock=stock,
            ))

    return products, issues


def deduplicate(products: list[Product]) -> tuple[list[Product], int]:
    """按商品名称去重（保留首次出现的记录）。

    真实项目中，重复数据非常常见（多页采集时的边界重叠、
    或者同一个商品出现在多个分类下）。去重是必要步骤。

    Args:
        products: 原始商品列表

    Returns:
        (去重后列表, 被移除的数量)
    """
    seen: set[str] = set()
    unique: list[Product] = []

    for p in products:
        if p.name in seen:
            continue
        seen.add(p.name)
        unique.append(p)

    return unique, len(products) - len(unique)


# ============================================================
# 4. 分析与统计（对应 1.3 推导式 + 1.8 标准库）
# ============================================================
def analyze(products: list[Product]) -> dict[str, Any]:
    """对商品数据做统计汇总。

    Args:
        products: 商品列表

    Returns:
        统计结果字典
    """
    if not products:
        return {"错误": "没有有效数据"}

    # 列表推导式（1.3 节）：一行完成筛选
    low_stock = [p for p in products if p.is_low_stock]

    # 按分类分组（字典的 setdefault 用法）
    by_category: dict[str, list[Product]] = {}
    for p in products:
        by_category.setdefault(p.category, []).append(p)

    # 每个分类的统计
    category_stats = {
        cat: {
            "商品数": len(items),
            "平均价格": round(sum(i.price for i in items) / len(items), 2),
            "库存总数": sum(i.stock for i in items),
        }
        for cat, items in by_category.items()
    }

    return {
        "商品总数": len(products),
        "分类数": len(by_category),
        "总货值": round(sum(p.total_value for p in products), 2),
        "平均价格": round(sum(p.price for p in products) / len(products), 2),
        "最贵商品": max(products, key=lambda p: p.price).name,
        "库存最紧张": min(products, key=lambda p: p.stock).name,
        "低库存商品": [p.name for p in low_stock],
        "分类统计": category_stats,
    }


# ============================================================
# 5. 导出（对应 1.6 文件操作）
# ============================================================
def export_json(products: list[Product], stats: dict, path: Path) -> None:
    """导出为 JSON 文件。

    Args:
        products: 商品列表
        stats: 统计结果
        path: 输出路径
    """
    payload = {
        "统计汇总": stats,
        "商品明细": [asdict(p) for p in products],
    }

    # ensure_ascii=False 让中文正常显示（否则会变成 \uXXXX）
    # indent=2 让文件格式化，便于人阅读
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"✓ 已导出 JSON: {path}")


def export_csv(products: list[Product], path: Path) -> None:
    """导出清洗后的 CSV（含计算字段）。

    Args:
        products: 商品列表
        path: 输出路径
    """
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["商品名称", "分类", "价格", "库存", "货值", "是否低库存"],
        )
        writer.writeheader()
        for p in products:
            writer.writerow({
                "商品名称": p.name,
                "分类": p.category,
                "价格": p.price,
                "库存": p.stock,
                "货值": p.total_value,
                "是否低库存": "是" if p.is_low_stock else "否",
            })
    print(f"✓ 已导出 CSV:  {path}")


# ============================================================
# 6. 主流程
# ============================================================
def main() -> None:
    """完整流程：生成数据 → 读取清洗 → 去重 → 分析 → 导出。"""
    work_dir = Path(__file__).parent
    source = work_dir / "practice_input.csv"

    print("=" * 62)
    print("阶段 1 毕业作业：从 CSV 到 JSON 的完整数据处理")
    print("=" * 62)
    print()

    # 步骤 1：生成测试数据
    print("【步骤 1】准备数据")
    create_sample_csv(source)
    print()

    # 步骤 2：读取 + 清洗
    print("【步骤 2】读取与清洗")
    try:
        products, issues = load_products(source)
    except FileNotFoundError:
        print(f"✗ 找不到文件: {source}")
        return
    except Exception as exc:
        print(f"✗ 读取失败: {exc}")
        return

    print(f"  读取到有效记录: {len(products)} 条")
    if issues:
        print(f"  发现 {len(issues)} 个问题：")
        for issue in issues:
            print(f"    · {issue}")
    print()

    # 步骤 3：去重
    print("【步骤 3】去重")
    products, removed = deduplicate(products)
    print(f"  移除重复记录: {removed} 条")
    print(f"  剩余: {len(products)} 条")
    print()

    # 步骤 4：分析
    print("【步骤 4】统计分析")
    stats = analyze(products)
    print(f"  商品总数:   {stats['商品总数']}")
    print(f"  分类数:     {stats['分类数']}")
    print(f"  总货值:     ¥{stats['总货值']:,}")
    print(f"  平均价格:   ¥{stats['平均价格']}")
    print(f"  最贵商品:   {stats['最贵商品']}")
    print(f"  库存最紧张: {stats['库存最紧张']}")
    print(f"  低库存商品: {', '.join(stats['低库存商品'])}")
    print()
    print("  按分类统计：")
    print(f"    {'分类':<10} {'商品数':<8} {'平均价格':<12} {'库存总数'}")
    print("    " + "-" * 48)
    for cat, s in sorted(stats["分类统计"].items(),
                         key=lambda kv: kv[1]["平均价格"],
                         reverse=True):
        print(f"    {cat:<10} {s['商品数']:<8} ¥{s['平均价格']:<11} {s['库存总数']}")
    print()

    # 步骤 5：导出
    print("【步骤 5】导出结果")
    export_json(products, stats, work_dir / "practice_output.json")
    export_csv(products, work_dir / "practice_output.csv")
    print()

    print("=" * 62)
    print("完成。检查这两个文件：")
    print(f"  · {work_dir / 'practice_output.json'}")
    print(f"  · {work_dir / 'practice_output.csv'}")
    print()
    print("如果你能不看本文件、独立写出这个过程，阶段 1 就毕业了。")
    print("=" * 62)


if __name__ == "__main__":
    main()
