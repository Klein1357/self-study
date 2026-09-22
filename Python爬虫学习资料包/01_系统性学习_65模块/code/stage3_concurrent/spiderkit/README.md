# spiderkit

> 工程化异步爬虫工具包 —— 《Python 爬虫系统性学习》阶段 3 毕业项目

一个具备**并发、限速、重试、断点续爬、日志、配置管理、测试、CLI** 八大特性的异步爬虫框架，全部代码经过真实靶站实测。

---

## 快速开始

```bash
# 1. 安装依赖
pip install httpx parsel pydantic pydantic-settings typer
pip install pytest pytest-asyncio ruff    # 开发依赖

# 2. 复制配置模板
cp .env.example .env

# 3. 查看当前配置（密钥自动脱敏）
python -m spiderkit.cli info

# 4. 开始采集（8 并发，每秒最多 6 个请求，抓 3 页）
python -m spiderkit.cli crawl -c 8 -r 6 -p 3

# 5. 查看进度
python -m spiderkit.cli status
```

---

## CLI 命令

| 命令 | 作用 | 示例 |
|------|------|------|
| `crawl` | 执行采集 | `crawl -c 8 -r 6 -p 3` |
| `crawl --resume` | 跳过列表页，从断点续爬 | `crawl --resume` |
| `status` | 查看进度与失败清单 | `status` |
| `reset` | 重置任务（默认只重置失败项） | `reset --only-failed` |
| `reset --only-failed=false` | 全部重来 | `reset -y` |
| `info` | 显示当前配置（脱敏） | `info` |

参数覆盖优先级：**CLI 参数 > 环境变量 > .env 文件 > 代码默认值**

---

## 项目结构

```
spiderkit/
├── spiderkit/
│   ├── config.py     # 分层配置 + 类型校验 + 密钥保护（3.8）
│   ├── fetcher.py    # 并发控制 + 限速 + 重试 + 熔断（3.3/3.4/3.5）
│   ├── state.py      # SQLite 断点续爬状态库（3.6）
│   ├── models.py     # 数据模型与解析函数（2.5）
│   ├── writer.py     # 边爬边写，CSV/JSONL（2.7）
│   ├── spider.py     # 主编排 + 优雅退出（3.9）
│   └── cli.py        # typer 命令行入口（3.9）
├── tests/
│   └── test_spiderkit.py   # 55 个测试，1.3 秒跑完
├── pyproject.toml
└── .env.example
```

---

## 八大特性

| 特性 | 实现位置 | 说明 |
|------|----------|------|
| **并发采集** | `fetcher.py` | asyncio + httpx 连接池 |
| **限速控制** | `fetcher.py` `TokenBucket` | 令牌桶，精确控制每秒请求数 |
| **并发上限** | `fetcher.py` `Semaphore` | 防止瞬间打爆对方 |
| **失败重试** | `fetcher.py` `backoff_delay` | 指数退避 + 全抖动，错误分类 |
| **熔断保护** | `fetcher.py` `CircuitBreaker` | 连续失败自动跳闸 |
| **断点续爬** | `state.py` | SQLite 状态机，崩溃可恢复 |
| **优雅退出** | `spider.py` | SIGINT 释放任务，不留悬空状态 |
| **可观测性** | `config.py` `setup_logging` | 分级日志 + 轮转 + 配置脱敏 |

---

## 实测数据

针对 `books.toscrape.com` 3 页 60 本书记录，与阶段 2 的同步实现对比：

| 指标 | 阶段 2（requests 串行） | 阶段 3（httpx 异步） |
|------|------------------------|---------------------|
| 总耗时 | 44.2 秒 | **11.8 秒** |
| 平均每请求 | 702 ms | **188 ms** |
| 提速 | — | **3.7x** |
| 重试机制 | 无 | 指数退避 + 抖动 |
| 断点续爬 | 无 | SQLite 状态库 |
| 测试覆盖 | 无 | 55 个测试 |

数据质量（60 本，12 个字段）：

```
库存分布: {19: 23, 16: 16, 18: 11, 17: 5, 20: 4, 22: 1}
价格区间: £12.84 - £57.31
UPC 全为16位: True
分类数: 25
空值: 0
```

---

## 测试

```bash
pytest -v                    # 全部测试
pytest tests/ -q             # 简洁输出
ruff check spiderkit/        # 代码检查
ruff format spiderkit/       # 代码格式化
```

```
55 passed in 1.33s
```

测试策略：
- **解析器**：内联 HTML 片段，不依赖网络
- **配置**：`monkeypatch` 注入环境变量，不碰真实 `.env`
- **状态库**：`tmp_path` 临时数据库
- **抓取器**：`httpx.MockTransport` 模拟响应，不发真实请求

---

## 断点续爬原理

```python
# 启动时：把上次崩溃留下的悬空任务打回待处理
state.reset_stale()

# 循环领取（原子事务，多 worker 安全）
urls = state.claim_batch(batch_size)      # pending → running
for url in urls:
    if html := await fetcher.get(url):
        state.mark_done(url, parse(html))  # running → done
    else:
        state.mark_failed(url, "抓取失败")   # running → failed
```

关键设计：
1. `INSERT OR IGNORE` 初始化 → 重复启动不重置进度
2. URL 作主键 → 天然幂等，重复运行不产生重复数据
3. `reset_stale()` → 处理进程被 kill 时悬空的 `running` 任务
4. WAL 模式 → 崩溃后数据不丢

---

## 配置约束

| 字段 | 范围 | 说明 |
|------|------|------|
| `concurrency` | 1-64 | 并发上限 |
| `rate_limit` | (0, 10000] | 每秒请求数 |
| `max_retries` | 0-10 | 重试次数 |
| `timeout_connect` | > 0 | 连接超时（秒） |
| `timeout_read` | > 0 | 读取超时（秒） |
| `log_level` | DEBUG/INFO/WARNING/ERROR/CRITICAL | 日志级别 |
| `env` | dev/prod | prod 时强制要求代理配置 |

所有约束在**启动时**校验，配置错误立即报错，不会跑到一半才炸。

---

## 安全说明

- 密钥字段使用 `pydantic.SecretStr`，`print()` / 日志输出均为 `********`
- `.env` 已加入 `.gitignore`，只提交 `.env.example`
- `settings.safe_dump()` 生成脱敏摘要，可安全外发

---

## 靶站说明

`books.toscrape.com` 是 Scrapy 官方提供的**专门用于练习爬虫的公开靶站**，允许采集。请勿将此代码直接用于未经授权的站点。

一旦换成真实站点，请先确认：
- 目标站点的 `robots.txt` 与服务条款
- 采集频率是否会影响对方正常服务
- 数据用途是否合规（尤其涉及个人信息时）

详见教程阶段 4 的「合规红线」章节。
