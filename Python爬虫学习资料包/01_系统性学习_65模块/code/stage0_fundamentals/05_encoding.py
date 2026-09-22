"""阶段 0.5 · 字符编码：亲手复现乱码，才能真正理解它。

运行：python3 05_encoding.py

编码是初学者最大的困惑来源。这个脚本让你亲眼看到：
  - 同一个字节序列，用不同编码解读会得到完全不同的文字
  - 乱码不是"坏了"，是"解读方式错了"
  - 为什么 UTF-8 能统一世界，而 GBK 不行

理解了这一节，你以后遇到任何乱码都能自己推理出原因，
而不是靠背 "utf-8-sig" 这种咒语。
"""

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
# 实验 1：字符串在内存中 ≠ 字符串在磁盘上
# ============================================================
print("=" * 60)
print("实验 1：编码的本质是「翻译规则」")
print("=" * 60)

text = "中文"

# 同一个字符串，用不同编码转成字节，结果完全不同
utf8_bytes = text.encode("utf-8")
gbk_bytes = text.encode("gbk")

print(f"原始字符串: {text!r}")
print(f"  UTF-8 编码后的字节: {utf8_bytes.hex(' ')}  （{len(utf8_bytes)} 字节）")
print(f"  GBK   编码后的字节: {gbk_bytes.hex(' ')}  （{len(gbk_bytes)} 字节）")

print()
print("→ 结论：UTF-8 用 3 字节表示一个汉字，GBK 用 2 字节。")
print("  同样两个字'中文'，UTF-8 占 6 字节，GBK 只占 4 字节。")
print("  所以编码不同，字节长度就不同 —— 这是解码时必须知道编码的原因。")


# ============================================================
# 实验 2：故意用错编码解码，制造乱码
# ============================================================
print()
print("=" * 60)
print("实验 2：乱码是怎么产生的")
print("=" * 60)

print(f"正确的解读（UTF-8 解 UTF-8）: {utf8_bytes.decode('utf-8')}")
print(f"错误的解读（GBK 解 UTF-8）  : {utf8_bytes.decode('gbk', errors='replace')}")
print()
print("→ 这就是乱码的真相：字节是对的，只是用错了翻译规则。")
print("  爬虫抓到的数据本来就是字节流，你必须知道它该用哪种编码解读。")


# ============================================================
# 实验 3：ASCII 是万物的起点
# ============================================================
print()
print("=" * 60)
print("实验 3：为什么会有这么多种编码")
print("=" * 60)

# ASCII 只能表示 128 个字符，最初只够英文用
print("ASCII 的局限：")
print(f"  字符 'A'  → ASCII 编码 = {ord('A')}  （1 字节足够）")
print(f"  字符 '中' → ASCII 编码 = ？（超出范围，无法表示）")
print()
print("  ASCII 只有 1 字节（0-127），最多表示 128 个字符。")
print("  英文够用，但中文、日文、阿拉伯文全都放不下。")

# 各国于是各自发明编码
print()
print("于是各国各自发明了编码：")
encodings = {
    "GBK / GB2312": "简体中文，2 字节，但和日文、韩文冲突",
    "Big5": "繁体中文，2 字节",
    "Shift-JIS": "日文，2 字节",
    "EUC-KR": "韩文，2 字节",
}
for name, desc in encodings.items():
    print(f"  {name:<14} {desc}")

print()
print("→ 问题来了：同一串字节，用 GBK 解是中文，用 Shift-JIS 解可能变成日文乱码。")
print("  这就是跨国网站经常显示乱码的历史原因。")


# ============================================================
# 实验 4：UTF-8 如何统一世界
# ============================================================
print()
print("=" * 60)
print("实验 4：UTF-8 的设计智慧")
print("=" * 60)

samples = ["A", "中", "🎉", "é"]
print(f"{'字符':<6} {'UTF-8 字节':<16} {'字节数':<8} {'说明'}")
print("-" * 55)
for ch in samples:
    b = ch.encode("utf-8")
    desc = {1: "兼容 ASCII，省空间", 3: "常用汉字",
            4: "emoji，扩展平面", 2: "拉丁扩展"}.get(len(b), "")
    print(f"{ch:<6} {b.hex(' '):<16} {len(b):<8} {desc}")

print()
print("→ UTF-8 是变长编码：")
print("  ASCII 字符仍占 1 字节（对英文网站零成本，所以能被广泛接受）")
print("  汉字占 3 字节，emoji 占 4 字节")
print("  所有语言都能表示，且向后兼容 ASCII —— 这就是它胜出的原因。")


# ============================================================
# 实验 5：BOM —— 为什么 Excel 会乱码
# ============================================================
print()
print("=" * 60)
print("实验 5：BOM 与 Excel 的恩怨")
print("=" * 60)

content = "书名,价格"

plain = content.encode("utf-8")
with_bom = content.encode("utf-8-sig")

print(f"普通 UTF-8:       {plain.hex(' ')}")
print(f"带 BOM 的 UTF-8:  {with_bom.hex(' ')}")
print()
print("→ BOM 就是开头多出的 3 个字节 EF BB BF（一个隐藏的标记）。")
print()
print("为什么需要它？")
print("  微软的 Excel 打开 CSV 时，不会自动判断编码。")
print("  如果没有 BOM，它默认按系统本地编码（中文系统是 GBK）解读，")
print("  而文件实际是 UTF-8 —— 于是中文全变乱码。")
print("  有了 BOM，Excel 就知道'这是 UTF-8'，正常显示。")
print()
print("→ 所以爬虫写 CSV 时用 encoding='utf-8-sig'，专门为了讨好 Excel。")

# 验证 BOM 的存在
print()
print("验证：解读开头 3 个字节")
print(f"  带 BOM 的文件开头: {with_bom[:3].hex(' ')}  （BOM 标记）")
print(f"  去掉 BOM 后开头:   {with_bom[3:].hex(' ')}  （真正的内容）")


# ============================================================
# 实验 6：爬虫中的实战判断逻辑
# ============================================================
print()
print("=" * 60)
print("实验 6：爬虫里如何判断编码")
print("=" * 60)

print("""
当 requests 抓到页面后，response.content 是原始字节。
该用哪种编码解读？按优先级判断：

  1) 看 HTTP 响应头 Content-Type
     如 Content-Type: text/html; charset=utf-8
     → response.encoding 会自动取到这个值

  2) 看 HTML 里的 meta 标签
     如 <meta charset="gbk">
     → requests 也会尝试识别

  3) 都不明确时，用 apparent_encoding 让库自己猜
     → response.encoding = response.apparent_encoding

  4) 还猜错，就直接指定（国内老站常见 gbk）
     → response.encoding = "gbk"

为什么必须在访问 .text 之前设置 encoding？
  因为 .text 第一次被访问时会解码并【缓存】结果。
  之后再改 encoding，读到的是旧缓存 —— 依然乱码。
""")


# ============================================================
# 实验 7：动手验证编码缓存行为
# ============================================================
print("=" * 60)
print("实验 7：动手验证「先设置再读取」的必要性")
print("=" * 60)

import requests

url = "https://books.toscrape.com/"
resp = requests.get(url, timeout=15)

print(f"requests 自动识别的编码: {resp.encoding}")
resp.encoding = resp.apparent_encoding
print(f"修正后的编码:            {resp.encoding}")
print(f"修正后取到的内容长度:    {len(resp.text)} 字符")

print()
print("→ 顺序很重要。真实项目中，务必在访问 .text 前设置好 encoding。")
print("  这也是为什么我在核心教程里把这两行紧挨着写。")

print()
print("=" * 60)
print("本节要点回顾")
print("=" * 60)
print("""
1. 编码是"字符串 ↔ 字节"的翻译规则，解码用错规则就产生乱码
2. ASCII 只有 1 字节，放不下中文，所以各国各自发明了编码
3. UTF-8 是变长编码，兼容 ASCII 且能表示所有语言，因此成为标准
4. BOM 是 UTF-8 文件开头的隐藏标记，用来提示 Excel 等软件
5. 爬虫判断编码的顺序：HTTP 头 → meta 标签 → 猜测 → 手动指定
6. 必须在访问 .text 之前设置 encoding，否则读到的是错误缓存
""")
