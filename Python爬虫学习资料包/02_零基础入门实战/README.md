# Python 爬虫从零到接单

零基础 2-4 周实战路线。打开 `index.html` 即可开始学习。

## 文件说明

| 路径 | 内容 |
|------|------|
| `index.html` | 交互式教程主文件，双击用浏览器打开 |
| `code/d01_hello_spider.py` | 第 1 个爬虫：发出请求，取回网页 |
| `code/d02_parse_books.py` | 解析 HTML，抠出书名/价格/评分 |
| `code/d03_pagination_csv.py` | 翻页采集 1000 本书并存 CSV |
| `code/d04_production_template.py` | 工程级模板（重试/断点续爬/日志/校验/优雅退出） |
| `code/d05_dynamic_playwright.py` | 用 Playwright 抓 JS 渲染的动态页面 |
| `code/d06_concurrent.py` | 线程池并发采集，实测提速 4.2 倍 |
| `data/示例输出_1000本书.csv` | 实测采集结果，可直接用 Excel 打开 |

## 环境准备

```bash
# 只需装这三个库，其余都是 Python 标准库
pip install requests beautifulsoup4 lxml

# 第 7 节需要（可选，约 150MB）
pip install playwright && playwright install chromium
```

## 运行示例

```bash
cd code
python d01_hello_spider.py      # 从最简单的开始
python d02_parse_books.py
python d03_pagination_csv.py    # 约 80 秒，产出 1000 本书
```

## 所有代码均已实测验证

本教程中所有代码都在真实网络环境下运行通过，实测数据：

- `d03`：1000 本书完整采集，耗时 81.6 秒
- `d04`：断点续爬验证成功，二次运行秒级恢复进度
- `d05`：Playwright 成功渲染并截图
- `d06`：20 页 400 本书，并发 8.2 秒 vs 单线程约 35 秒

## 学习建议

1. **不要跳过第 0 节（法律边界）** —— 你是奔着接单去的，知道什么不能碰比会写代码更重要。
2. **代码要亲手敲，不要复制粘贴** —— 照着抄能跑通，但学不会。
3. **时间紧就先学完第 1-6 节** —— 这已经足够接市面上大量小单，第 7-9 节是在提升你的报价上限。
4. **练手只用教程附录 B 里的靶场站** —— 不要拿真实商业网站练手。

## 合规提醒

请仅将所学技术用于公开数据的合规采集。遇到涉及个人信息、绕过权限、破坏性操作的需求，无论报酬多少都应拒绝。
