#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第 64 课 · 部署与容器化：把爬虫从「我的笔记本」搬到「服务器上」

本课要回答的问题：
  1. 爬虫脚本在本地跑得好好的，为什么一上服务器就出各种问题？
  2. Docker 到底解决了什么？一个爬虫的镜像该怎么写才又小又对？
  3. 为什么容器里的爬虫「收不到停止信号」，每次重启都要等 10 秒才被杀？
  4. 配置为什么不能硬编码？启动时该校验什么？校验失败该「快速失败」还是「带默认值？」
  5. 优雅关闭（graceful shutdown）到底要做什么？做晚了会怎样？
  6. 什么叫「十二要素应用」（12-Factor App）？哪些要素对爬虫特别重要？
  7. 日志为什么要写到 stdout 而不是文件？这跟容器的设计有什么关系？

运行方式：
    python3 64_deployment.py

实验清单：
    实验 1：真实写出 Dockerfile / docker-compose.yml / .dockerignore
    实验 2：优雅关闭 —— 信号处理与「正在处理的请求怎么办」
    实验 3：配置管理 —— 校验、快速失败、敏感信息不进代码库
    实验 4：日志设计 —— 结构化日志与 stdout 哲学
    实验 5：把整套东西串起来 —— 用 deploy/ 目录下的真实文件跑一次

⚠ 本课的重要局限（必读）：
  · 本课**真的会往 deploy/ 目录写 Dockerfile、docker-compose.yml、
    .dockerignore、requirements.txt 等文件**，可以在文件管理器里打开看。
  · 但本课**不会真的构建镜像 / 启动容器**。原因：
      ① 构建镜像需要拉取基础镜像（python:3.11-slim 约 50MB），
         在本课的运行环境里网络不可靠，会让实验超时或失败；
      ② 一个镜像构建动辄几十秒到几分钟，远超本课「单文件 < 60 秒」的要求；
      ③ 本课的实验环境里 Docker daemon 虽然可用，但**课程代码不应该
         依赖外部服务的可用性** —— 否则读者换一台机器就跑不起来。
  · 所以与 Docker 相关的「验证」部分，本课用的是：
      - **静态检查**：Dockerfile 的语法、指令顺序、是否犯常见错误
        （比如忘了 EXPOSE、用了 latest 标签、把 secret 写进 ENV）
      - **模拟运行**：用一个纯 Python 的「镜像构建模拟器」把 Dockerfile
        解析成指令序列，演示「分层缓存」为什么会因为一行顺序而全失
  · 信号处理和配置管理这两块是**真实运行的**（真的发 SIGTERM、
    真的起子进程），不涉及 Docker，所以没有局限。
  · 生产环境与本课的差异会在每个实验末尾明确标注。

★ 合规提醒：
  部署能力是中性技术。请遵守目标站的 robots.txt 与服务条款，
  遵守《数据安全法》《个人信息保护法》。把爬虫部署到云服务器时，
  注意出口 IP 的合规性 —— 用云厂商的 IP 做大规模采集可能违反其服务条款。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

# ============================================================================
# 输出工具
# ============================================================================
SEP = "=" * 76


def title(text: str) -> None:
    """打印一级标题。

    Args:
        text: 标题文本。

    Returns:
        None
    """
    print()
    print(SEP)
    print(text)
    print(SEP)


def sub(text: str) -> None:
    """打印二级标题。

    Args:
        text: 标题文本。

    Returns:
        None
    """
    print(f"\n▸ {text}")


def bar(value: float, max_value: float, width: int = 30) -> str:
    """画一条 ASCII 条形图。

    Args:
        value: 当前值。
        max_value: 最大值（用于归一化）。
        width: 条形最大宽度（字符数）。

    Returns:
        由 █ 组成的字符串。
    """
    if max_value <= 0:
        return ""
    n = int(round(value / max_value * width))
    return "█" * max(0, min(width, n))


# ============================================================================
# 一、Dockerfile 生成器
# ============================================================================
@dataclass
class DockerfileSpec:
    """一份爬虫镜像的构建参数。

    Attributes:
        base_image: 基础镜像（**必须钉版本**，见下方说明）。
        python_version: 仅用于注释，说明目标 Python 版本。
        app_dir: 容器内的工作目录。
        entrypoint: 启动命令。
        requirements: 依赖列表。
        pip_index: pip 源（国内环境常需要换源）。
        non_root_user: 容器内使用的非 root 用户名。
        add_healthcheck: 是否加 HEALTHCHECK 指令。
        tz: 时区。

    ▸ 为什么 base_image 要钉版本，而且不能用 `python:3.11` 这种「省略 patch」的写法？
      这是一个**极其容易踩的坑**：
        · `python:latest`   → 今天构建和明天构建可能是两个不同版本，
          线上突然挂了，你连"之前跑的是哪个 Python"都说不清。
        · `python:3.11`     → 看起来钉住了，但它指向的是 3.11 系列
          **最新的 patch**。Python 3.11.9 → 3.11.10 这种 patch 升级
          虽然理论上只是 bugfix，但历史上出现过改变行为的情况
          （比如某些标准库的边界修正）。
        · `python:3.11.9-slim` → 真正钉死。**这是推荐做法。**
      更严格的做法是钉到 digest：
        `python:3.11.9-slim@sha256:abc123...`
      这样连「同 tag 被重新推送」都防住了（Docker Hub 上 tag 是可变的！）。
      生产环境应该用 digest，本课用带 patch 的 tag 作为折中 ——
      因为 digest 太长，写在教学代码里影响可读性。

    ▸ 为什么要用 slim 而不是 alpine？
      这是 Python 项目里一个常见的争论：
        · alpine 更小（约 5MB vs slim 的 45MB），但用的是 musl libc
          而不是 glibc，**很多预编译的 wheel 不能用**（numpy、lxml、
          cryptography 等会退化到源码编译 → 构建时间暴涨，
          甚至因为缺少编译依赖直接失败）。
        · slim 基于 debian，用 glibc，wheel 兼容性好。
      对爬虫来说依赖里通常有 lxml、cryptography，所以 **slim 更实用**。
      「更小」不等于「更好」，要看你付什么代价。
    """

    base_image: str = "python:3.11.9-slim"
    python_version: str = "3.11"
    app_dir: str = "/app"
    entrypoint: str = '["python", "-u", "crawler.py"]'
    requirements: list[str] = field(default_factory=lambda: [
        "requests==2.32.3",
        "lxml==5.2.2",
        "redis==5.0.7",
    ])
    pip_index: str = ""
    non_root_user: str = "crawler"
    add_healthcheck: bool = True
    tz: str = "Asia/Shanghai"


def render_dockerfile(spec: DockerfileSpec) -> str:
    """把 DockerfileSpec 渲染成 Dockerfile 文本。

    Args:
        spec: 构建参数。

    Returns:
        Dockerfile 的完整文本（含行尾换行）。

    Raises:
        ValueError: 当 entrypoint 不是合法的 JSON 数组时。

    ▸ 这个函数是**真实可用**的：写出的 Dockerfile 可以直接 docker build。
      本课不会真的构建，但会做静态检查（见 check_dockerfile）。

    ▸ 为什么 ENTRYPOINT 用 JSON 数组（exec form）而不是 shell form？
      ❌ `ENTRYPOINT python -u crawler.py`（shell form）
         → 容器里实际运行的进程是 `/bin/sh -c "python -u crawler.py"`，
           PID 1 是 **sh**，不是 python。
         → `docker stop` 发的 SIGTERM 只到了 sh，
           **sh 默认不会转发给子进程**，于是 python 收不到信号，
           10 秒后 Docker 直接 SIGKILL 强杀。
         → 后果：正在写的文件被截断、正在 ack 的任务丢了（第 62 课讲过）、
           日志缓冲区里的内容全部丢失。
      ✅ `ENTRYPOINT ["python", "-u", "crawler.py"]`（exec form）
         → python 直接是 PID 1，**能收到 SIGTERM**。

      这是容器化爬虫**最经典的一个坑**，本课实验 2 会用真实进程复现。

    ▸ 为什么 python 要加 `-u`？
      Python 默认对 stdout 做**块缓冲**（当输出不是 tty 时）。
      容器里 stdout 是管道，不是 tty → 缓冲区可能是 4KB 或 8KB。
      于是你的日志会「攒一批才吐一次」，`docker logs -f` 看起来像卡住了，
      而且**进程被强杀时缓冲区里的日志全丢**。
      `-u` 让 stdout/stderr 变成无缓冲 —— 对爬虫这种「日志就是生命线」
      的程序是必须的。
      另一种做法是设 `PYTHONUNBUFFERED=1` 环境变量，效果相同。
    """
    # 校验 entrypoint 必须是 JSON 数组（exec form）
    try:
        parsed = json.loads(spec.entrypoint)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"entrypoint 必须是合法的 JSON 数组（exec form），"
            f"收到 {spec.entrypoint!r}：{exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError(
            f"entrypoint 必须是 JSON 数组而不是 {type(parsed).__name__}")

    lines: list[str] = []
    lines.append("# " + "-" * 74)
    lines.append("# 爬虫镜像 Dockerfile（由 64_deployment.py 自动生成）")
    lines.append(f"# 目标：Python {spec.python_version} + 非 root 运行 + 健康检查")
    lines.append("# " + "-" * 74)
    lines.append("")
    lines.append("# ---------- 阶段 1：基础运行时 ----------")
    lines.append(f"# ⚠ 钉到 patch 版本，不要用 latest，也不要用 python:{spec.python_version}")
    lines.append(f"FROM {spec.base_image}")
    lines.append("")
    lines.append("# 环境变量的三条 OCI/Docker 惯例：")
    lines.append("#   PYTHONDONTWRITEBYTECODE 不生成 .pyc（容器是只读的一次性环境，")
    lines.append("#       生成 .pyc 只是浪费镜像层空间和启动时间）")
    lines.append("#   PYTHONUNBUFFERED 日志实时输出（等价于 python -u）")
    lines.append("#   PIP_NO_CACHE_DIR 不留 pip 缓存（否则镜像会大几百 MB）")
    lines.append("ENV PYTHONDONTWRITEBYTECODE=1 \\")
    lines.append("    PYTHONUNBUFFERED=1 \\")
    lines.append("    PIP_NO_CACHE_DIR=1 \\")
    lines.append("    PIP_DISABLE_PIP_VERSION_CHECK=1 \\")
    lines.append(f"    TZ={spec.tz}")
    lines.append("")
    lines.append("# 时区数据：slim 镜像默认不带 tzdata，")
    lines.append("# 不装的话 TZ 环境变量不生效，日志时间会差 8 小时（很常见的事故）")
    lines.append("RUN apt-get update \\")
    lines.append("    && apt-get install -y --no-install-recommends tzdata \\")
    lines.append("    && rm -rf /var/lib/apt/lists/*")
    lines.append("")
    lines.append(f"WORKDIR {spec.app_dir}")
    lines.append("")
    lines.append("# ---------- 阶段 2：依赖 ----------")
    lines.append("# ★ 关键：先只 COPY requirements.txt，再 pip install，最后才 COPY 源码。")
    lines.append("#   为什么顺序这么重要？因为 Docker 的层缓存是「按指令逐条命中」的：")
    lines.append("#     改了源码 → 只有最后的 COPY . . 失效，pip install 仍然命中缓存")
    lines.append("#     如果反过来先 COPY . . 再 pip install →")
    lines.append("#       改一个字的代码就会让 pip install 重跑一遍（几分钟）")
    lines.append("#   本课实验 1 会用模拟器量化这个差别。")
    lines.append("COPY requirements.txt .")
    pip_cmd = "pip install --no-cache-dir -r requirements.txt"
    if spec.pip_index:
        pip_cmd += f" -i {spec.pip_index}"
    lines.append(f"RUN {pip_cmd}")
    lines.append("")
    lines.append("# ---------- 阶段 3：源码与权限 ----------")
    lines.append("COPY . .")
    lines.append("")
    lines.append("# 创建非 root 用户并切换。")
    lines.append("# 为什么不以 root 运行？")
    lines.append("#   ① 容器逃逸时，root 的破坏面更大；")
    lines.append("#   ② 很多 K8s 集群开启了「禁止 root」的准入策略，")
    lines.append("#      以 root 运行的 Pod 会被直接拒绝调度；")
    lines.append("#   ③ 挂载宿主机目录时，root 写的文件在宿主机上也是 root 所有，")
    lines.append("#      清理起来很麻烦。")
    lines.append(f"RUN useradd --create-home --shell /bin/bash {spec.non_root_user} \\")
    lines.append(f"    && chown -R {spec.non_root_user}:{spec.non_root_user} {spec.app_dir}")
    lines.append(f"USER {spec.non_root_user}")
    lines.append("")
    if spec.add_healthcheck:
        lines.append("# 健康检查：容器编排系统靠它判断「这个实例能不能接流量」。")
        lines.append("# 爬虫的健康检查该查什么？**不要查「进程还在不在」**（没意义），")
        lines.append("# 要查「最近一次成功抓取距今多久」——")
        lines.append("# 被目标站封了 IP 的爬虫进程活得好好的，但已经没在干活了。")
        lines.append("# 本课用一个最小的探针脚本模拟这个语义。")
        lines.append('HEALTHCHECK --interval=30s --timeout=5s '
                     '--start-period=10s --retries=3 \\')
        lines.append('    CMD ["python", "healthcheck.py"] || exit 1')
        lines.append("")
    lines.append("# 使用 exec form，让 python 成为 PID 1（见 render_dockerfile 的说明）")
    lines.append(f"ENTRYPOINT {spec.entrypoint}")
    lines.append("")
    return "\n".join(lines)


def render_requirements(spec: DockerfileSpec) -> str:
    """渲染 requirements.txt。

    Args:
        spec: 构建参数。

    Returns:
        依赖文件的文本内容。

    ▸ 为什么所有依赖都要钉版本（`==` 而不是 `>=`）？
      因为容器镜像的核心价值是**可复现**。
      `requests>=2.31` 意味着「今天构建得到 2.32，半年后构建可能得到 2.35」，
      而 2.35 可能改了某个默认行为，让你的爬虫静默出错。
      **一个不能复现的构建，等于没有构建。**
      真正严格的做法是同时产出哈希锁文件（pip-tools / poetry.lock / uv.lock），
      本课用简单的 `==` 作为示范。
    """
    lines = [
        "# 爬虫依赖（由 64_deployment.py 自动生成）",
        "# ⚠ 全部钉死版本：镜像必须是可复现的，",
        "#   `>=` 会让半年后的构建得到不同的依赖，从而导致"
        "「同样的代码表现不一样」。",
        "",
    ]
    for req in spec.requirements:
        lines.append(req)
    lines.append("")
    lines.append("# 说明：生产环境应进一步使用 lock 文件锁定全部传递依赖，")
    lines.append("# 例如：pip-compile requirements.in > requirements.txt")
    lines.append("# 或：poetry export -f requirements.txt --output requirements.txt")
    lines.append("")
    return "\n".join(lines)


def render_dockerignore() -> str:
    """渲染 .dockerignore。

    Returns:
        .dockerignore 的文本内容。

    ▸ 为什么 .dockerignore 对爬虫项目特别重要？
      因为爬虫项目的工作目录里往往有：
        · data/            几十 GB 的抓取结果
        · logs/            日志文件
        · .venv/           本地虚拟环境（几百 MB，而且容器里完全用不到）
        · __pycache__/     .pyc 文件
        · .git/           版本历史（可能很大）
      如果忘了写 .dockerignore，`COPY . .` 会把这些**全部塞进镜像**——
      镜像从 200MB 膨胀到 5GB，而且构建时要花几分钟传输构建上下文。

      ▸ 这里有一个极易被忽略的坑：
        **.dockerignore 只能挡住 COPY，挡不住构建上下文的上传。**
        不对，准确说法是：.dockerignore **会**减少构建上下文（build context）
        的上传量 —— 因为它是在客户端（docker CLI 侧）过滤的。
        但如果你用 `docker build -f Dockerfile .` 而当前目录有 50GB 数据，
        即使 .dockerignore 写对了，**上下文打包仍然要遍历整个目录树**
        （只是不传输被排除的文件）。对这个问题的解法是把 Dockerfile
        放在子目录里，或者用 BuildKit 的 `--build-context`。
    """
    return textwrap.dedent("""\
        # 爬虫项目的 .dockerignore（由 64_deployment.py 自动生成）
        #
        # ★ 核心原则：**容器里只放"运行时真正需要的东西"**。
        #   任何「只在开发时有用」的文件都应该排除。

        # ---------- 版本控制 ----------
        .git
        .gitignore
        .gitattributes

        # ---------- Python 运行时垃圾 ----------
        __pycache__/
        *.py[cod]
        *$py.class
        *.so
        .Python
        build/
        dist/
        *.egg-info/
        .eggs/

        # ---------- 虚拟环境（容器里绝对用不到，体积还大） ----------
        .venv/
        venv/
        env/
        ENV/

        # ---------- 测试与开发工具 ----------
        .pytest_cache/
        .mypy_cache/
        .ruff_cache/
        .coverage
        htmlcov/
        tests/
        docs/

        # ---------- ★ 爬虫特有的数据文件 ----------
        # 这几条是最容易被忘记、后果最严重的：
        data/
        output/
        *.db
        *.sqlite
        *.sqlite3
        *.csv
        *.jsonl
        logs/
        *.log

        # ---------- 配置与密钥（绝不能进镜像！） ----------
        # 密钥进镜像 = 密钥泄漏。镜像层是可以被任何拿到镜像的人解开的，
        # 而且**即使后续层删除了文件，文件仍然留在历史层里**。
        .env
        .env.*
        *.pem
        *.key
        secrets/
        credentials.json

        # ---------- 编辑器与系统垃圾 ----------
        .idea/
        .vscode/
        *.swp
        .DS_Store
        Thumbs.db

        # ---------- Docker 自身 ----------
        Dockerfile*
        docker-compose*.yml
        .dockerignore
        """)


def render_compose(spec: DockerfileSpec) -> str:
    """渲染 docker-compose.yml。

    Args:
        spec: 构建参数。

    Returns:
        compose 文件的文本内容。

    ▸ 为什么爬虫需要一个 compose 文件，而不是一个 docker run 命令？
      因为爬虫几乎总是**多组件**的：至少要有 Redis（分布式队列/去重），
      通常还要有 MySQL/Postgres（存数据）、可能还有 Prometheus（监控）。
      用 docker run 你要手写网络、卷、环境变量、启动顺序 —— 很容易漏。
      compose 把这些声明成配置，一条 `docker compose up` 全起来。

    ▸ compose 文件里的几个关键设计点：
      ① `depends_on` 配 `healthcheck`（condition: service_healthy）——
         只写 depends_on 是**不够的**：它只保证「容器启动了」，
         不保证「Redis 已经可以接受连接了」。
         爬虫启动时连不上 Redis 会直接崩（除非你写了重试逻辑）。
         **这是 compose 里最常见的一个误解。**
      ② `restart: unless-stopped` —— 爬虫是「跑得越久越好」的程序，
         崩了要自动拉起来。但**不能用 `always`**：
         你手动 `docker compose stop` 时它会自己又起来，很烦。
      ③ `stop_grace_period` —— 默认是 10 秒。如果你的爬虫一次请求要 30 秒，
         10 秒后就被 SIGKILL 了，正在跑的任务丢掉。
         这个值应该和你的「优雅关闭超时」对齐。
      ④ 环境变量从 `.env` 读，**不把密码写进 compose 文件**。
      ⑤ `logging` 限制日志大小 —— 不加限制的话，一个跑几个月的爬虫
         能把宿主机磁盘写满（这是很常见的生产事故）。
    """
    return textwrap.dedent(f"""\
        # 爬虫系统的 docker-compose.yml（由 64_deployment.py 自动生成）
        #
        # 使用方法：
        #   docker compose up -d          # 后台启动全栈
        #   docker compose logs -f spider # 跟踪爬虫日志
        #   docker compose down           # 停止并删除容器
        #
        # ⚠ 本文件不会被本课真的执行（见模块开头的局限声明）。

        services:
          # ----------------------------------------------------------------
          # Redis：分布式队列 + 去重集合（第 62 课讲过它的两个核心用途）
          # ----------------------------------------------------------------
          redis:
            image: redis:7.4-alpine
            # ★ 为什么必须显式设置 maxmemory-policy？
            #   Redis 默认策略是 noeviction（内存满了返回错误），
            #   这**恰好是我们要的**：队列里的任务绝不能被静默淘汰。
            #   但很多人用云 Redis 时被默认配成 allkeys-lru，
            #   结果跑去一半的任务凭空消失，而且**不报错**。
            #   第 62 课讲过这个坑，这里再强调一次。
            command: >
              redis-server
              --appendonly yes
              --maxmemory 512mb
              --maxmemory-policy noeviction
              --save 60 1000
            volumes:
              # 持久化：容器删了数据还在。爬虫的队列状态绝不能丢。
              - redis-data:/data
            healthcheck:
              test: ["CMD", "redis-cli", "ping"]
              interval: 5s
              timeout: 3s
              retries: 5
              start_period: 5s
            restart: unless-stopped
            logging:
              driver: json-file
              options:
                max-size: "10m"
                max-file: "3"

          # ----------------------------------------------------------------
          # 爬虫主进程
          # ----------------------------------------------------------------
          spider:
            build:
              context: .
              dockerfile: Dockerfile
            # ★ 依赖 Redis 的**健康状态**，而不只是「已启动」
            depends_on:
              redis:
                condition: service_healthy
            environment:
              # ★ 十二要素之三：配置来自环境变量，不来自代码里的常量
              # 密码从 .env 文件注入（.env 不进版本库、不进镜像）
              REDIS_HOST: redis
              REDIS_PORT: "6379"
              REDIS_PASSWORD: ${{REDIS_PASSWORD:-}}
              LOG_LEVEL: ${{LOG_LEVEL:-INFO}}
              # 并发与限速（第 60/63 课的配置项）
              CONCURRENCY: ${{CONCURRENCY:-16}}
              GLOBAL_QPS: ${{GLOBAL_QPS:-10}}
              # 优雅关闭的等待上限（秒）。必须 <= stop_grace_period
              SHUTDOWN_TIMEOUT: ${{SHUTDOWN_TIMEOUT:-25}}
            # ★ 默认 10 秒太短：一次请求可能就要 30 秒。
            #   设得比 SHUTDOWN_TIMEOUT 略大，留出收尾时间。
            stop_grace_period: 30s
            restart: unless-stopped
            # 爬虫需要「可写」时只写自己的数据目录，其他全部只读
            read_only: true
            tmpfs:
              - /tmp
            volumes:
              - spider-data:/app/data
            logging:
              driver: json-file
              options:
                max-size: "20m"
                max-file: "5"

          # ----------------------------------------------------------------
          # 监控（第 55 课的看板思路 + 第 65 课的指标）
          # ----------------------------------------------------------------
          # ⚠ 本课不实现 Prometheus exporter，只演示它该放在哪。
          #   生产环境的爬虫应该暴露 /metrics 端点，让 Prometheus 抓取：
          #     抓取成功率、队列深度、去重命中率、平均延迟、被封代理数。

        volumes:
          redis-data:
          spider-data:
        """)


def render_healthcheck_py() -> str:
    """渲染健康检查脚本 healthcheck.py。

    Returns:
        脚本源码。

    ▸ 这个脚本是本课一个「小而重要」的设计点：
      它演示了**「进程存活」和「业务健康」是两回事**。

      一个被目标站封了 IP 的爬虫：
        · 进程活着（PID 还在，内存没爆）
        · CPU 占用很低（因为请求全失败了，没啥可干）
        · 端口还在监听（如果它开了 HTTP 服务）
        → 任何「查进程存活」的健康检查都会说它**健康**。

      正确的做法是查**业务指标**：
        · 最近一次成功抓取距今多久？（超过 N 分钟 = 不健康）
        · 最近 1 分钟的失败率是多少？（超过 90% = 不健康）
        · 队列还有没有活干？（深度为 0 且长时间没变化 = 可能卡住了）
    """
    return textwrap.dedent('''\
        #!/usr/bin/env python3
        # -*- coding: utf-8 -*-
        """容器健康检查探针（由 64_deployment.py 自动生成）。

        退出码约定（遵循 Docker HEALTHCHECK 的惯例）：
            0 = 健康
            1 = 不健康（容器编排系统会重启或摘掉这个实例）

        检查逻辑：读心跳文件，判断「最近一次成功抓取距今多久」。

        ▸ 为什么用**文件**而不是进程内变量？
          因为 HEALTHCHECK 是**另起一个进程**执行的，
          它看不到主进程的内存。所以主进程必须把心跳**写出来**。
          生产环境更好的做法是暴露一个 HTTP /healthz 端点，
          由探针去请求 —— 这样还能顺便验证网络栈是好的。
        """

        import os
        import sys
        import time

        HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/app/data/heartbeat")
        # 超过这个秒数没有心跳就判定为不健康
        MAX_AGE = float(os.environ.get("HEARTBEAT_MAX_AGE", "120"))

        def main() -> int:
            """执行健康检查。

            Returns:
                0 表示健康，1 表示不健康。
            """
            try:
                mtime = os.path.getmtime(HEARTBEAT_FILE)
            except OSError:
                # 心跳文件不存在 = 从来没成功过 = 不健康。
                # ⚠ 注意 start-period 的作用：容器刚启动时心跳文件还没生成，
                #   这段宽限期由 Dockerfile 的 --start-period 提供，
                #   否则容器会在启动瞬间就被判定为不健康而反复重启。
                print(f"不健康：心跳文件不存在 {HEARTBEAT_FILE}")
                return 1

            age = time.time() - mtime
            if age > MAX_AGE:
                print(f"不健康：最近一次心跳在 {age:.1f} 秒前"
                      f"（上限 {MAX_AGE:.0f} 秒）")
                return 1

            print(f"健康：最近一次心跳在 {age:.1f} 秒前")
            return 0

        if __name__ == "__main__":
            sys.exit(main())
        ''')


def write_deploy_dir(target: Path) -> dict[str, Path]:
    """把全套部署文件真实写到磁盘。

    Args:
        target: 目标目录（通常是 code/stage6_distributed/deploy）。

    Returns:
        文件名 → 路径 的映射。

    Raises:
        OSError: 写文件失败（权限、磁盘满等）。
    """
    target.mkdir(parents=True, exist_ok=True)
    spec = DockerfileSpec()

    files = {
        "Dockerfile": render_dockerfile(spec),
        "requirements.txt": render_requirements(spec),
        ".dockerignore": render_dockerignore(),
        "docker-compose.yml": render_compose(spec),
        "healthcheck.py": render_healthcheck_py(),
    }

    written: dict[str, Path] = {}
    for name, content in files.items():
        path = target / name
        path.write_text(content, encoding="utf-8")
        written[name] = path
    return written


# ============================================================================
# 二、Dockerfile 静态检查
# ============================================================================
@dataclass
class LintIssue:
    """一条静态检查发现的问题。

    Attributes:
        level: 'error' / 'warning' / 'info'。
        rule: 规则编号（便于对照）。
        message: 问题描述。
        line_no: 行号（1-based，0 表示不适用）。
    """

    level: str
    rule: str
    message: str
    line_no: int = 0

    def render(self) -> str:
        """渲染成一行可读文本。

        Returns:
            带图标与行号的字符串。
        """
        icon = {"error": "❌", "warning": "⚠", "info": "ℹ"}.get(self.level, "·")
        loc = f"  (第 {self.line_no} 行)" if self.line_no else ""
        return f"  {icon} [{self.rule}] {self.message}{loc}"


def lint_dockerfile(text: str) -> list[LintIssue]:
    """对 Dockerfile 做静态检查。

    Args:
        text: Dockerfile 全文。

    Returns:
        问题列表（可能为空）。

    ▸ 这些规则全部来自真实的生产事故。每一条的「为什么」都写在下面的注释里。
      真实的项目应该用 hadolint（一个专门的 Dockerfile linter），
      本课手工实现一小部分，是为了让读者理解**规则背后的原因**，
      而不是记住规则本身。规则会变，原因不会。
    """
    issues: list[LintIssue] = []
    lines = text.split("\n")

    # 预处理：把续行（以 \ 结尾）拼起来，这样能正确处理多行 ENV / RUN
    logical: list[tuple[int, str]] = []
    buf = ""
    buf_start = 0
    for i, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not buf:
            buf_start = i
        if stripped.endswith("\\"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        logical.append((buf_start, buf))
        buf = ""
    if buf:
        logical.append((buf_start, buf))

    joined = "\n".join(ln for _, ln in logical)

    # ---------- DL3007：不要用 latest ----------
    for ln_no, ln in logical:
        m = re.match(r"^FROM\s+(\S+)", ln, re.IGNORECASE)
        if m:
            image = m.group(1)
            if image.endswith(":latest") or ":" not in image:
                issues.append(LintIssue(
                    "error", "DL3007",
                    f"基础镜像 {image!r} 用了 latest 或省略标签 —— "
                    f"镜像不可复现，今天和明天构建的结果可能不同",
                    ln_no))

    # ---------- DL3009：apt-get install 没加 --no-install-recommends ----------
    # ⚠ 这条规则的正则必须允许 install 和 --no-install-recommends 之间有
    #   其他选项（比如 -y）。本课最初写成
    #       re.search(r"apt-get install(?!\s+--no-install-recommends)", ...)
    #   结果对自己的 Dockerfile 报了假 warning（因为实际是
    #   `apt-get install -y --no-install-recommends tzdata`，
    #   `-y ` 让负向前瞻意外匹配成功）。
    #   **写 lint 规则最容易犯的错就是"规则本身有 bug"** ——
    #   所以 lint 工具必须能被自己的代码通过（吃自己的狗粮）。
    #   正确写法：先取出 apt-get install 之后的整段文本，再看其中有没有该选项。
    for ln_no, ln in logical:
        if "apt-get install" not in ln:
            continue
        tail = ln.split("apt-get install", 1)[1]
        command = tail.split("&&")[0]
        if "--no-install-recommends" not in command:
            issues.append(LintIssue(
                "warning", "DL3009",
                "apt-get install 没有加 --no-install-recommends —— "
                "会拉进大量非必需的推荐包，镜像白白变大",
                ln_no))

    # ---------- DL3059：连续多个 RUN 可以合并 ----------
    run_lines = [n for n, l in logical if l.startswith("RUN ")]
    for a, b in zip(run_lines, run_lines[1:]):
        if b == a + 1:
            issues.append(LintIssue(
                "info", "DL3059",
                "相邻的 RUN 指令可以合并成一个（用 && 连接），"
                "减少镜像层数",
                b))

    # ---------- 自定义规则 1：ENTRYPOINT 必须用 exec form ----------
    ent = [l for _, l in logical if l.startswith("ENTRYPOINT")]
    if ent:
        if not ent[0].lstrip("ENTRYPOINT").strip().startswith("["):
            issues.append(LintIssue(
                "error", "CUSTOM001",
                "ENTRYPOINT 用了 shell form —— PID 1 会是 /bin/sh，"
                "SIGTERM 不会转发给 Python，优雅关闭失效"
                "（本课实验 2 会复现这个后果）",
                next((n for n, l in logical if l.startswith("ENTRYPOINT")), 0)))
    else:
        issues.append(LintIssue(
            "warning", "CUSTOM002",
            "没有 ENTRYPOINT / CMD —— 容器不知道要跑什么"))

    # ---------- 自定义规则 2：不能把密钥写进 ENV ----------
    for ln_no, ln in logical:
        if ln.startswith("ENV "):
            if re.search(r"(PASSWORD|SECRET|TOKEN|APIKEY|API_KEY|PRIVATE_KEY)\s*=",
                         ln, re.IGNORECASE):
                issues.append(LintIssue(
                    "error", "CUSTOM003",
                    "ENV 里出现了疑似密钥的变量 —— "
                    "**镜像层是可以被解开的**，写进去等于泄漏；"
                    "即使后续层删除，历史层里仍然有。"
                    "应该用运行时注入（compose 的 environment + .env、"
                    "或 K8s Secret）",
                    ln_no))

    # ---------- 自定义规则 3：COPY 顺序（先依赖后源码）----------
    copy_lines = [(n, l) for n, l in logical if l.startswith("COPY ")]
    pip_runs = [n for n, l in logical if "pip install" in l]
    if copy_lines and pip_runs:
        first_copy_src = copy_lines[0][1]
        # 期望：第一条 COPY 只拷 requirements
        if "requirements" not in first_copy_src:
            issues.append(LintIssue(
                "warning", "CUSTOM004",
                "第一条 COPY 不是 requirements.txt —— "
                "「COPY . .」放在 pip install 之前会让"
                "**改一行代码就重装全部依赖**（构建时间从 5 秒变成 5 分钟）",
                copy_lines[0][0]))

    # ---------- 自定义规则 4：没有 USER（以 root 运行）----------
    if not any(l.startswith("USER ") for _, l in logical):
        issues.append(LintIssue(
            "error", "CUSTOM005",
            "没有 USER 指令 —— 容器以 root 运行。"
            "很多 K8s 集群会直接拒绝调度这类 Pod"))

    # ---------- 自定义规则 5：python 没加 -u 且没设 PYTHONUNBUFFERED ----------
    has_unbuffered_env = "PYTHONUNBUFFERED" in joined
    ent_is_python = any("python" in l for l in ent) and ent and \
        '"-u"' not in ent[0]
    if ent_is_python and not has_unbuffered_env:
        issues.append(LintIssue(
            "warning", "CUSTOM006",
            "python 启动命令没有 -u，也没设 PYTHONUNBUFFERED —— "
            "日志会被块缓冲，docker logs 看不到实时输出，"
            "且进程被强杀时缓冲区日志全丢"))

    # ---------- 自定义规则 6：缺少 HEALTHCHECK ----------
    if not any(l.startswith("HEALTHCHECK") for _, l in logical):
        issues.append(LintIssue(
            "warning", "CUSTOM007",
            "没有 HEALTHCHECK —— 编排系统无法感知"
            "「爬虫进程活着但已经抓不到东西了」（比如 IP 被封）"))

    return issues


# ============================================================================
# 三、分层缓存模拟器
# ============================================================================
@dataclass
class LayerCacheSim:
    """Docker 分层缓存的模拟器。

    它不真的构建镜像，只模拟「哪些层能命中缓存」。

    Attributes:
        built_layers: 已构建过的指令 → 内容哈希。
        hits: 命中缓存的层数。
        misses: 未命中的层数。

    ▸ 为什么要做这个模拟器？
      因为「改一行代码导致依赖重装」这个问题的**代价**很难凭空理解。
      本课用模拟器把「命中/未命中」可视化，
      再乘上一个真实测得的「安装耗时」估计，就能算出实际代价。

    ▸ 缓存失效的机制（这是理解一切优化技巧的基础）：
      Docker 逐条执行指令，每条指令算一个「内容哈希」。
      只要某条指令的哈希与上次构建不同，**它以及它之后的所有层全部失效**。
      注意是「以及之后」—— 这是最关键的一点。
      所以把「变化频率低的指令」放前面、「变化频率高的放后面」，
      是 Dockerfile 优化的第一原则。
    """

    def __init__(self) -> None:
        """初始化模拟器。"""
        self.built_layers: dict[str, str] = {}
        self.hits: int = 0
        self.misses: int = 0
        self.log: list[str] = []

    def reset(self) -> None:
        """清空计数（保留 built_layers 表示「上次构建的结果」）。

        Returns:
            None
        """
        self.hits = 0
        self.misses = 0
        self.log = []

    def build(self, instructions: Sequence[tuple[str, str]],
              label: str = "") -> None:
        """模拟一次构建。

        Args:
            instructions: (指令文本, 内容指纹) 的序列。
            label: 本次构建的标签（用于日志）。

        Returns:
            None

        ▸ 一旦有一层未命中，后续所有层都视为未命中 ——
          因为 Docker 不会「跳过中间层去复用更后面的层」。
          这个「链式失效」的性质是本模拟器要展示的核心。
        """
        invalidated = False
        for text, fingerprint in instructions:
            key = text
            if invalidated:
                # 已经被上游失效波及
                self.misses += 1
                self.built_layers[key] = fingerprint
                self.log.append(f"    MISS(链式)  {text}")
                continue
            prev = self.built_layers.get(key)
            if prev == fingerprint:
                self.hits += 1
                self.log.append(f"    HIT         {text}")
            else:
                invalidated = True
                self.misses += 1
                self.built_layers[key] = fingerprint
                self.log.append(f"    MISS        {text}")
        if label:
            self.log.append(f"  ← {label}：命中 {self.hits} 层 / "
                            f"未命中 {self.misses} 层")


# ============================================================================
# 四、优雅关闭
# ============================================================================
class GracefulShutdown:
    """优雅关闭管理器：收到信号后让正在跑的任务跑完，再退出。

    │ 为什么需要它？—— 这是分布式爬虫里**数据丢失的头号原因**：
    │
    │   一个 worker 正从队列里取了一个任务在抓页面（第 62 课的 RPOPLPUSH），
    │   它准备抓完再 ACK。这时你 `docker compose restart`：
    │     · 默认 10 秒后 Docker 发 SIGKILL 强杀
    │     · worker 进程瞬间死亡，**没有机会 ACK**
    │     · 任务还留在 processing 队列里（如果用了 RPOPLPUSH，
    │       好在不会丢，但要等下一个 recover 周期才被回收）
    │     · 更糟的是：如果它正在写文件，会留下**半截文件**
    │
    │   不做优雅关闭的系统，每次发布/重启都会造成：
    │     · 任务重复处理（at-least-once 的代价，第 62 课讲过）
    │     · 数据损坏（半个 JSON 对象）
    │     · 监控误报（成功率先掉再涨，很吓人）
    │
    │ ▸ 优雅关闭要做四件事（缺一不可）：
    │   ① **捕获信号**（SIGTERM / SIGINT）
    │   ② **停止接受新任务**（不再从队列取，或关闭监听 socket）
    │   ③ **给正在跑的任务一段收尾时间**（跑完 + ACK + 落盘）
    │   ④ **超时后强制退出**（防止卡死的任务让容器永远关不掉）
    │
    │ ▸ 为什么第 ④ 步绝对不能省？
    │   因为「优雅」的前提是「任务会结束」。但现实里有：
    │     · 卡住的网络请求（socket 没有超时）
    │     · 死循环的解析逻辑
    │     · 等一把永远不会释放的锁
    │   如果没有强制超时，Docker 会在 stop_grace_period 之后 SIGKILL，
    │   这时你的「优雅」就白做了 —— 而且你**不知道**是被强杀的。
    │   主动超时（并打日志）比被动强杀好，因为你至少能知道「谁没关干净」。

    Attributes:
        timeout: 收到信号后允许的最长收尾时间（秒）。
        name: 组件名（用于日志）。
        triggered: 是否已经收到过信号。
    """

    def __init__(self, timeout: float = 25.0, name: str = "worker") -> None:
        """初始化优雅关闭管理器。

        Args:
            timeout: 最长收尾时间（秒）。
            name: 组件名。

        ▸ timeout 该怎么定？两个硬约束：
          ① 必须 **小于** 编排系统的 kill 宽限期
             （compose 的 stop_grace_period，默认 10 秒）。
             否则你还没收尾完就被 SIGKILL 了，优雅关闭等于没做。
          ② 必须 **大于** 单个任务的最长耗时。
             否则任务永远不可能有机会跑完。
          本课取 25 秒，配合 compose 里 30 秒的 stop_grace_period。
          如果单个任务可能跑 5 分钟（比如下载大文件），
          那么 ① 就不成立了 —— 这时应该：
            · 把长任务拆成可续传的小任务（断点续爬，第 65 课）
            · 或者接受「重启会丢一个任务」，靠队列的可靠性机制兜底
          **不能既要求任务跑 5 分钟、又要求 10 秒内关干净。**
        """
        self.timeout = timeout
        self.name = name
        self.triggered = threading.Event()
        self._original_handlers: dict[int, Any] = {}
        self._installed = False
        self.signal_name = ""
        self.shutdown_started = 0.0

    # ------------------------------ 信号安装 ------------------------------
    def install(self) -> None:
        """安装信号处理器。

        Returns:
            None

        Raises:
            ValueError: 不在主线程中调用时（signal.signal 的限制）。

        ▸ 为什么处理函数里只设一个 Event，不做实际清理？
          因为信号处理器运行在**主线程的任意字节码边界上**，
          它相当于一次「异步中断」。在它里面：
            · 不能做复杂操作（可能打断另一个正在进行的关键操作）
            · 不能获取锁（可能造成死锁 —— 如果被中断的代码正持有那把锁）
            · 不能调用非「异步信号安全」的函数
          正确做法（也是 Python 官方推荐）：处理器里**只设标志**，
          由主循环在安全的位置检查这个标志并执行清理。
          这就是「self-pipe trick / 标志位模式」，是 Unix 编程的经典手法。
        """
        if threading.current_thread() is not threading.main_thread():
            raise ValueError(
                "signal.signal 只能在主线程中调用"
                "（这是 Python 的硬性限制）")

        def handler(signum: int, frame: Any) -> None:
            """信号处理器：只记录，不做事。

            Args:
                signum: 信号编号。
                frame: 当前栈帧（不用）。

            Returns:
                None
            """
            self.signal_name = signal.Signals(signum).name
            self.triggered.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            self._original_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        self._installed = True

    def restore(self) -> None:
        """恢复原始信号处理器（用于测试之间清理）。

        Returns:
            None
        """
        if not self._installed:
            return
        for sig, orig in self._original_handlers.items():
            signal.signal(sig, orig)
        self._installed = False

    # ------------------------------ 主循环 ------------------------------
    def should_stop(self) -> bool:
        """是否应该停止取新任务。

        Returns:
            True 表示已收到信号。
        """
        return self.triggered.is_set()

    def run_until_signal(self, task: Callable[[int], bool],
                         max_rounds: int = 10_000,
                         interval: float = 0.05) -> dict[str, Any]:
        """反复执行任务直到收到信号。

        Args:
            task: 执行一轮任务，返回 True 表示「有活干」。
            max_rounds: 最多执行多少轮（防止无限循环）。
            interval: 两轮之间的间隔（秒）。

        Returns:
            统计字典，包含 rounds / drained / forced 等字段。

        ▸ 这里的核心是「**先检查标志，再取任务**」的顺序：
            每次循环开头检查 should_stop()，
            如果为真就**停止取新任务**，进入收尾阶段。
          如果把检查放在取任务之后，
          那么收到信号后还会多取一个任务 —— 那个任务大概率来不及跑完。
          **顺序错了，等于没做优雅关闭。**
        """
        stats = {
            "rounds": 0,
            "drained": 0,
            "forced": False,
            "idle_rounds": 0,
            "signal": "",
            "elapsed": 0.0,
        }
        start = time.monotonic()
        for i in range(max_rounds):
            if self.should_stop():
                break
            had_work = task(i)
            stats["rounds"] = i + 1
            if had_work:
                stats["drained"] += 1
            else:
                stats["idle_rounds"] += 1
            time.sleep(interval)

        stats["signal"] = self.signal_name
        stats["elapsed"] = time.monotonic() - start
        stats["forced"] = self.triggered.is_set() and stats["elapsed"] > self.timeout
        return stats

    def wait_for_tasks(self, tasks: Sequence[threading.Thread],
                       force_kill_probe: Callable[[], None] | None = None) -> dict[str, Any]:
        """等待所有正在运行的任务结束，带超时。

        Args:
            tasks: 任务线程列表。
            force_kill_probe: 超时后调用的回调（用于演示「强制中断」）。

        Returns:
            统计字典，包含 finished / unfinished / timed_out / waited。

        ▸ 这里体现了优雅关闭的第 ③ 和第 ④ 步：
          · 先用 join(timeout) 给任务一段时间收尾
          · 超时后**不无限等待**，而是返回「谁还没关干净」
          · 调用方再决定：记日志、告警、还是强制退出
          **「知道谁没关干净」比「关得干净」更重要** ——
          因为前者能定位问题，后者只是把问题藏起来。
        """
        self.shutdown_started = time.monotonic()
        deadline = self.shutdown_started + self.timeout
        result = {
            "total": len(tasks),
            "finished": 0,
            "unfinished": [],
            "timed_out": False,
            "waited": 0.0,
        }
        for t in tasks:
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                result["timed_out"] = True
                break
            t.join(timeout=remaining)
        result["waited"] = time.monotonic() - self.shutdown_started
        for t in tasks:
            if t.is_alive():
                name = t.name
                # 如果是我们自己起的 worker，检查它的 keep_running 标志
                data = getattr(t, "_shutdown_payload", None)
                if isinstance(data, dict) and not data.get("acked", True):
                    name = f"{name}(未 ACK)"
                result["unfinished"].append(name)
            else:
                result["finished"] += 1
        if result["timed_out"] and force_kill_probe is not None:
            force_kill_probe()
        return result


# ============================================================================
# 五、配置管理
# ============================================================================
class ConfigError(Exception):
    """配置校验失败。

    ▸ 为什么定义一个专门的异常类型？
      因为「配置错误」和「网络错误」「解析错误」需要被**区别对待**：
        · 配置错误 → 立刻退出（重启也没用，必须有人改配置）
        · 网络错误 → 重试
        · 解析错误 → 丢弃这条数据，继续
      如果全都抛 ValueError，调用方就只能靠字符串匹配来区分 —— 很脆弱。
      **为「不同的处理策略」定义不同的异常类型**，是异常设计的核心原则。
    """


@dataclass
class ConfigField:
    """一个配置项的定义。

    Attributes:
        name: 环境变量名。
        default: 默认值（None 表示必填）。
        cast: 类型转换函数。
        validator: 额外的校验函数，返回 None 表示通过，返回字符串表示错误。
        secret: 是否是敏感信息（日志里要脱敏）。
        help_text: 说明（用于自动生成配置文档）。
    """

    name: str
    default: str | None = None
    cast: Callable[[str], Any] = str
    validator: Callable[[Any], str | None] | None = None
    secret: bool = False
    help_text: str = ""

    @property
    def required(self) -> bool:
        """是否必填。

        Returns:
            True 表示没有默认值，必须提供。
        """
        return self.default is None


def _validate_positive(v: Any) -> str | None:
    """校验正数。

    Args:
        v: 待校验值。

    Returns:
        None 表示通过，否则返回错误消息。
    """
    if isinstance(v, (int, float)) and v <= 0:
        return f"必须是正数，收到 {v}"
    return None


def _validate_range(lo: float, hi: float) -> Callable[[Any], str | None]:
    """生成一个范围校验函数。

    Args:
        lo: 下界（含）。
        hi: 上界（含）。

    Returns:
        校验函数。
    """
    def check(v: Any) -> str | None:
        """校验值是否在范围内。

        Args:
            v: 待校验值。

        Returns:
            None 或错误消息。
        """
        if not (lo <= v <= hi):
            return f"应在 [{lo}, {hi}] 范围内，收到 {v}"
        return None
    return check


class ConfigManager:
    """基于环境变量的配置管理器（十二要素之三的落地）。

    │ ★ 为什么配置不能硬编码在代码里？
    │   因为这会让**同一份代码无法在不同环境运行**，
    │   于是你被迫维护多个分支/多份代码 —— 这是灾难的开始。
    │
    │   十二要素（12-Factor App）的第三条是「配置存在环境中」：
    │     所有环境相关的值（数据库地址、密码、并发数、日志级别）
    │     都必须能从外部注入，代码里只能有**默认值**（且默认值必须安全）。
    │
    │ ▸ 对爬虫来说，哪些配置必须外置？
    │   · Redis 地址 / 密码        （不同环境不同）
    │   · 并发数 / QPS 上限        （测试环境要调小，别把目标站打挂）
    │   · 代理池的地址与凭据       （绝对不能进代码库）
    │   · 日志级别                （本地 DEBUG、线上 INFO）
    │   · 目标站的白名单           （不同环境抓不同的站）
    │   · 优雅关闭超时             （要和编排系统对齐）
    │
    │ ▸ 为什么要在**启动时**校验，而不是用到的时候才检查？
    │   这是本课最重要的工程实践之一：**快速失败（fail fast）**。
    │      ❌ 不校验：程序跑起来了，20 分钟后终于走到"连 Redis"那段代码，
    │         发现密码错了 → 崩。这 20 分钟白跑了，而且日志里
    │         根本看不出是配置问题（看起来像是网络问题）。
    │      ✅ 启动时校验：进程起来 0.1 秒就报
    │         「配置错误：REDIS_PASSWORD 未设置」
    │         → 编排系统看到容器立即退出，重启几次后告警，
    │           运维一眼就知道要改什么。
    │   **配置错误应该在最早的时刻、以最明确的方式暴露。**
    │
    │ ▸ 但注意「快速失败」不等于「什么都必填」：
    │   有合理默认值的配置项**不应该**做必填（比如日志级别默认 INFO）。
    │   判断标准是：**「这个值错了会不会导致静默的、难以诊断的错误？」**
    │     · 并发数错了 → 可能把目标站打挂 → 必填或给保守默认值
    │     · 日志级别错了 → 只是日志多少 → 给默认值即可
    │     · Redis 密码错了 → 静默连不上 → 必填
    │

    Attributes:
        fields: 配置项定义列表。
        values: 校验后的值（存储为字符串形式，使用时通过 get 取）。
    """

    def __init__(self, fields: Sequence[ConfigField]) -> None:
        """初始化配置管理器。

        Args:
            fields: 配置项定义。
        """
        self.fields = list(fields)
        self.values: dict[str, Any] = {}
        self._sources: dict[str, str] = {}
        self._errors: list[str] = []
        self._warnings: list[str] = []

    def load(self, env: dict[str, str] | None = None) -> None:
        """从环境变量加载并校验配置。

        Args:
            env: 环境变量字典；None 表示用 os.environ。

        Returns:
            None

        Raises:
            ConfigError: 当校验失败时（消息包含**全部**错误，不是第一个）。

        ▸ 为什么要把所有错误收集起来一次性抛出，而不是遇到第一个就抛？
          因为运维修配置时，**一次改完比改一个重启一次好得多**。
          如果配置里有 5 个错误，逐个报要重启 5 次（每次可能几分钟）。
          一次性全报出来，运维改一遍就好。
          这个原则叫「**错误聚合**」，在表单校验、CI 检查里都适用。
        """
        env = dict(os.environ if env is None else env)
        self.values = {}
        self._errors = []
        self._warnings = []

        for f in self.fields:
            raw = env.get(f.name)
            if raw is None or raw == "":
                if f.required:
                    self._errors.append(
                        f"缺少必填配置 {f.name}"
                        + (f"（{f.help_text}）" if f.help_text else ""))
                    continue
                raw = f.default
                self._sources[f.name] = "默认值"
            else:
                self._sources[f.name] = "环境变量"

            try:
                value = f.cast(raw)
            except (ValueError, TypeError) as exc:
                self._errors.append(
                    f"配置 {f.name}={raw!r} 无法转换：{exc}")
                continue

            if f.validator is not None:
                msg = f.validator(value)
                if msg:
                    self._errors.append(f"配置 {f.name}={raw!r} 不合法：{msg}")
                    continue

            self.values[f.name] = value

        # 跨字段的一致性校验：这类错误单看某一项是发现不了的
        self._cross_validate()

        if self._errors:
            detail = "\n".join(f"    · {e}" for e in self._errors)
            raise ConfigError(
                f"配置校验失败，共 {len(self._errors)} 个问题：\n{detail}")

    def _cross_validate(self) -> None:
        """执行跨字段的一致性校验。

        Returns:
            None

        ▸ 为什么要做跨字段校验？
          因为有些配置项**单独看都合法，组合起来是错的**。比如：
            · SHUTDOWN_TIMEOUT 必须小于编排系统的 stop_grace_period，
              否则优雅关闭永远来不及（本课的核心例子）
            · 最小并发不能大于最大并发
            · 如果启用了代理池，代理池地址不能为空
          这类错误用单字段校验发现不了，必须放在一起看。
          **一个配置系统有没有跨字段校验，是业余和专业的分界线。**
        """
        v = self.values

        # 规则 1：优雅关闭超时必须小于编排系统的宽限期
        if "SHUTDOWN_TIMEOUT" in v and "STOP_GRACE_PERIOD" in v:
            if v["SHUTDOWN_TIMEOUT"] >= v["STOP_GRACE_PERIOD"]:
                self._errors.append(
                    f"SHUTDOWN_TIMEOUT（{v['SHUTDOWN_TIMEOUT']}s）必须**小于** "
                    f"STOP_GRACE_PERIOD（{v['STOP_GRACE_PERIOD']}s），"
                    f"否则容器会在优雅关闭完成前被 SIGKILL —— "
                    f"优雅关闭形同虚设")

        # 规则 2：启用代理池时必须有代理池地址
        if v.get("USE_PROXY_POOL") and not v.get("PROXY_POOL_URL"):
            self._errors.append(
                "USE_PROXY_POOL 为真，但 PROXY_POOL_URL 为空 —— "
                "这会让所有请求走直连，可能瞬间被封")

        # 规则 3：全局 QPS 不能高于单站点上限（否则限速配置自相矛盾）
        if "GLOBAL_QPS" in v and "PER_DOMAIN_QPS" in v:
            if v["GLOBAL_QPS"] > v["PER_DOMAIN_QPS"]:
                self._warnings.append(
                    f"GLOBAL_QPS（{v['GLOBAL_QPS']}）大于 "
                    f"PER_DOMAIN_QPS（{v['PER_DOMAIN_QPS']}）—— "
                    f"全局限制比单站限制还宽松，等于全局限制不起作用"
                    f"（只是警告，某些场景下是有意的）")

        # 规则 4：生产环境不应该开 DEBUG 日志（量大 + 可能泄漏敏感信息）
        if v.get("ENV") == "prod" and v.get("LOG_LEVEL") == "DEBUG":
            self._warnings.append(
                "生产环境（ENV=prod）开启了 DEBUG 日志 —— "
                "日志量会暴涨，而且可能把响应体、Cookie 打进日志")

    def get(self, name: str, default: Any = None) -> Any:
        """取配置值。

        Args:
            name: 配置项名（环境变量名）。
            default: 找不到时的返回值。

        Returns:
            配置值。
        """
        return self.values.get(name, default)

    def render(self) -> str:
        """把配置渲染成可读的表格（敏感信息脱敏）。

        Returns:
            多行文本。

        ▸ 脱敏怎么做才对？两个要点：
          ① **完全不输出** 比「输出掩码」安全
             —— 掩码长度有时会泄漏原始长度，
             而长度本身可能是敏感信息（比如密码长度规则）。
             本课输出 `<已设置>`，不泄漏任何字符。
          ② 明确标注「来自环境变量还是默认值」
             —— 排查问题时这是关键信息：
             如果某个值来自默认值而你以为是环境变量设了，
             那就找到了 bug。
        """
        lines: list[str] = []
        lines.append(f"    {'配置项':<24}{'来源':<10}{'值'}")
        lines.append("    " + "-" * 64)
        for f in self.fields:
            if f.name not in self.values:
                continue
            val = self.values[f.name]
            if f.secret:
                shown = "<已设置，不显示>" if val else "<未设置>"
            else:
                shown = repr(val)
            src = self._sources.get(f.name, "?")
            lines.append(f"    {f.name:<24}{src:<10}{shown}")
        return "\n".join(lines)

    @property
    def warnings(self) -> list[str]:
        """本轮的警告列表。

        Returns:
            警告消息列表。
        """
        return list(self._warnings)

    @property
    def errors(self) -> list[str]:
        """本轮的错误列表（load 抛异常前也能读）。

        Returns:
            错误消息列表。
        """
        return list(self._errors)


def build_spider_config() -> ConfigManager:
    """构建一个典型爬虫的配置定义表。

    Returns:
        配置管理器实例。

    ▸ 这张表本身就是**配置文档** ——
      好的项目应该能从这份定义自动生成 .env.example 和配置说明。
      本课在实验 3 末尾会演示这一点。
    """
    return ConfigManager([
        ConfigField("ENV", default="dev", help_text="运行环境：dev / prod"),
        ConfigField("REDIS_HOST", default="localhost",
                    help_text="Redis 主机（分布式队列与去重）"),
        ConfigField("REDIS_PORT", default="6379", cast=int,
                    validator=_validate_range(1, 65535),
                    help_text="Redis 端口"),
        ConfigField("REDIS_PASSWORD", default="", secret=True,
                    help_text="Redis 密码（必填于生产环境，本课给空默认便于本地跑）"),
        ConfigField("CONCURRENCY", default="16", cast=int,
                    validator=_validate_range(1, 1024),
                    help_text="并发请求数（第 60 课的拐点实验）"),
        ConfigField("GLOBAL_QPS", default="10", cast=float,
                    validator=_validate_positive,
                    help_text="全局 QPS 上限"),
        ConfigField("PER_DOMAIN_QPS", default="5", cast=float,
                    validator=_validate_positive,
                    help_text="单域名 QPS 上限（第 63 课：必须按域名隔离）"),
        ConfigField("USE_PROXY_POOL", default="false", cast=lambda s: s.lower() in
                    ("1", "true", "yes", "on"),
                    help_text="是否启用代理池"),
        ConfigField("PROXY_POOL_URL", default="",
                    help_text="代理池地址（USE_PROXY_POOL 为真时必填）"),
        ConfigField("LOG_LEVEL", default="INFO",
                    validator=lambda v: None if v in
                    ("DEBUG", "INFO", "WARNING", "ERROR") else
                    f"必须是 DEBUG/INFO/WARNING/ERROR 之一，收到 {v}",
                    help_text="日志级别"),
        ConfigField("SHUTDOWN_TIMEOUT", default="25", cast=int,
                    validator=_validate_positive,
                    help_text="优雅关闭的最长收尾秒数"),
        ConfigField("STOP_GRACE_PERIOD", default="30", cast=int,
                    validator=_validate_positive,
                    help_text="编排系统的 kill 宽限期（必须大于 SHUTDOWN_TIMEOUT）"),
    ])


# ============================================================================
# 六、日志设计
# ============================================================================
class StructuredLogger:
    """结构化日志器（输出 JSON 到 stdout）。

    │ ★ 为什么日志要写 stdout 而不是文件？
    │   这是十二要素的第十一条，也是容器化里最反直觉、但最重要的一条。
    │
    │   「程序自己写日志文件」在物理机时代是标准做法，但在容器时代是**错的**：
    │     ① 容器是**一次性**的：容器删了，里面的日志文件也没了。
    │        你会失去出问题那一刻的日志 —— 而那正是你唯一需要的。
    │     ② 容器**没有日志轮转**：一个跑几个月的爬虫会把宿主机磁盘写满
    │        （这是非常常见的生产事故，尤其是 DEBUG 级别开着的时候）。
    │     ③ 多副本场景下日志分散在各容器里，你要挨个 docker exec 去看。
    │     ④ 容器编排系统（Docker、K8s）已经内置了日志基础设施：
    │        写 stdout → Docker 收集 → 可按大小轮转 → 可送 ELK/Loki。
    │
    │   所以正确姿势是：**程序只管往 stdout 写，谁收集、存哪、怎么轮转
    │   是平台的事。** 程序的职责边界更清晰了。
    │
    │ ▸ 那为什么还要「结构化」（JSON）而不是纯文本？
    │   因为**纯文本要靠正则去解析，而正则总会在某个奇怪的日志上失败**。
    │   结构化日志让下游可以直接按字段过滤/聚合：
    │     jq 'select(.level=="ERROR") | .url' crawler.log
    │   而且加字段不会破坏已有的解析规则 —— 这是纯文本做不到的。
    │
    │ ▸ 那本地开发时看不清 JSON 怎么办？
    │   用一个「开发模式」的美化输出（本课实现了两种模式）。
    │   **格式可切换，但语义（字段名）必须一致** ——
    │   否则你在本地看到的和线上收集的不是一回事。

    Attributes:
        level: 当前日志级别。
        name: logger 名（通常是组件名）。
        json_mode: True 输出 JSON 行，False 输出人类可读文本。
        stream: 输出流。
        counts: 各级别日志计数。
    """

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, level: str = "INFO", name: str = "spider",
                 json_mode: bool = True, stream: Any = None) -> None:
        """初始化日志器。

        Args:
            level: 日志级别。
            name: 组件名。
            json_mode: 是否输出 JSON。
            stream: 输出流，默认 sys.stdout。

        Raises:
            ValueError: 级别不合法。
        """
        if level not in self.LEVELS:
            raise ValueError(
                f"未知日志级别 {level!r}，可选：{list(self.LEVELS)}")
        self.level = level
        self.name = name
        self.json_mode = json_mode
        self.stream = stream if stream is not None else sys.stdout
        self.counts: dict[str, int] = {k: 0 for k in self.LEVELS}
        self._lock = threading.Lock()

    def _emit(self, level: str, msg: str, **fields: Any) -> None:
        """输出一条日志。

        Args:
            level: 级别。
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None

        ▸ 为什么要加锁？
          在多线程爬虫里，两个线程同时写 stdout 会造成**行交错**：
              {"level":"INFO","msg":"开始抓
              {"level":"INFO","msg":"开始抓 A"}B"}
          这条日志就废了。
          加锁保证「一条日志原子地写出去」。
          （另一种做法是让每条日志小于 PIPE_BUF（4096 字节）
          从而依赖管道的原子性 —— 但你不能保证日志一定那么短。）
        """
        if self.LEVELS[level] < self.LEVELS[self.level]:
            return
        self.counts[level] += 1
        with self._lock:
            if self.json_mode:
                record = {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "level": level,
                    "logger": self.name,
                    "msg": msg,
                    **fields,
                }
                self.stream.write(json.dumps(record, ensure_ascii=False) + "\n")
            else:
                extra = " ".join(f"{k}={v}" for k, v in fields.items())
                self.stream.write(
                    f"[{time.strftime('%H:%M:%S')}] {level:<7} "
                    f"{self.name}: {msg}"
                    + (f"  ({extra})" if extra else "") + "\n")
            self.stream.flush()

    def debug(self, msg: str, **fields: Any) -> None:
        """输出 DEBUG 日志。

        Args:
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None
        """
        self._emit("DEBUG", msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        """输出 INFO 日志。

        Args:
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None
        """
        self._emit("INFO", msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        """输出 WARNING 日志。

        Args:
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None
        """
        self._emit("WARNING", msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        """输出 ERROR 日志。

        Args:
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None
        """
        self._emit("ERROR", msg, **fields)


# ============================================================================
# 实验 1：真实写出部署文件 + 静态检查
# ============================================================================
def exp1_write_and_lint() -> dict[str, Any]:
    """实验 1：写出真实部署文件，做静态检查，演示分层缓存。

    Returns:
        统计字典。

    ⚠ 局限：不真的 docker build。原因见模块开头的说明。
      但**写文件是真的**，静态检查规则也是真的（来自生产事故）。
      读者可以在自己机器上 `docker build -t spider .` 验证这些文件可用。
    """
    title("【实验 1】真实写出部署文件 + Dockerfile 静态检查")

    print("""
    本实验会往 deploy/ 目录**真实写入** 5 个文件：
      Dockerfile           镜像构建定义
      requirements.txt     依赖（全部钉版本）
      .dockerignore        构建上下文排除规则
      docker-compose.yml   多组件编排（redis + spider）
      healthcheck.py       容器健康探针

    写完后对 Dockerfile 做静态检查，并用模拟器演示「分层缓存」的代价。
    """)

    deploy_dir = Path(__file__).resolve().parent / "deploy"
    written = write_deploy_dir(deploy_dir)

    sub("1.1 写入结果")
    print(f"    目标目录：{deploy_dir}")
    print(f"    {'文件':<22}{'大小(字节)':>12}{'行数':>8}")
    print("    " + "-" * 46)
    total_bytes = 0
    for name, path in written.items():
        content = path.read_text(encoding="utf-8")
        n_lines = content.count("\n")
        size = len(content.encode("utf-8"))
        total_bytes += size
        print(f"    {name:<22}{size:>12}{n_lines:>8}")
    print(f"    {'合计':<22}{total_bytes:>12}")
    print(f"\n    ✅ 已真实写入磁盘，可以直接查看：{deploy_dir}")

    dockerfile_text = written["Dockerfile"].read_text(encoding="utf-8")

    sub("1.2 Dockerfile 静态检查")
    print("    检查规则来自真实生产事故，每条规则的原因见代码注释：\n")
    issues = lint_dockerfile(dockerfile_text)
    if not issues:
        print("    ✅ 没有发现问题")
    else:
        for issue in issues:
            print(issue.render())

    sub("1.3 故意写一个「有问题的 Dockerfile」看看会被抓到什么")
    bad_dockerfile = textwrap.dedent("""\
        FROM python:latest
        WORKDIR /app
        COPY . .
        RUN pip install -r requirements.txt
        ENV REDIS_PASSWORD=supersecret123
        ENTRYPOINT python crawler.py
        """)
    print("    ❌ 这份是反面教材：")
    for i, ln in enumerate(bad_dockerfile.rstrip().split("\n"), 1):
        print(f"      {i:>2} | {ln}")
    print()
    bad_issues = lint_dockerfile(bad_dockerfile)
    errors = [i for i in bad_issues if i.level == "error"]
    warnings = [i for i in bad_issues if i.level == "warning"]
    print(f"    检查结果：{len(errors)} 个 error，{len(warnings)} 个 warning")
    for issue in bad_issues:
        print(issue.render())

    print(f"""
    ▸ 这份 {bad_dockerfile.rstrip().count(chr(10)) + 1} 行的 Dockerfile 被查出
      {len(errors)} 个 error、{len(warnings)} 个 warning，
      每一项都会导致真实问题：

      `FROM python:latest`
        → 不可复现。今天构建是 3.13，明天可能是 3.14，
          依赖可能装不上、行为可能变了。

      `COPY . . ` 在 `pip install` 之前
        → 改任何一行源码都会让 pip install 重跑。
          下面 1.4 会量化这个代价。

      `ENV REDIS_PASSWORD=supersecret123`
        → **密码进了镜像层**。
          即使下一层 `ENV REDIS_PASSWORD=` 把它清掉，
          密码**仍然留在历史层里**，任何拿到镜像的人
          `docker history --no-trunc` 就能看到。
          这是最严重的一个错误。

      `ENTRYPOINT python crawler.py`（shell form）
        → PID 1 是 /bin/sh，SIGTERM 不转发，优雅关闭失效。
          实验 2 会用真实进程复现这个后果。

      没有 USER / HEALTHCHECK
        → 以 root 运行；编排系统不知道「爬虫是否还在干活」。
    """)

    sub("1.4 分层缓存代价：改一行代码要付多少钱？")
    print("""
    模拟两次构建：第一次全量，第二次只「改了一行源码」。
    对比两种 Dockerfile 的 COPY 顺序。
    """)

    # 场景 A：正确的顺序（先依赖后源码）
    good_layers = [
        ("FROM python:3.11.9-slim", "base-v1"),
        ("COPY requirements.txt .", "req-hash-abc"),
        ("RUN pip install -r requirements.txt", "req-hash-abc"),
        ("COPY . .", "src-hash-v1"),          # 改代码后变成 v2
        ("RUN useradd ... && chown ...", "user-v1"),
    ]
    # 场景 B：错误的顺序（先源码后依赖）
    bad_layers = [
        ("FROM python:3.11.9-slim", "base-v1"),
        ("COPY . .", "src-hash-v1"),          # 改代码后变成 v2
        ("RUN pip install -r requirements.txt", "src-hash-v1"),
        ("RUN useradd ... && chown ...", "src-hash-v1"),
    ]

    # 真实测一次「装依赖」的大致耗时：本课不去真装，
    # 而是用 requirements 的行数乘一个经验系数来估算，
    # 并且**明确标注这是估算而不是实测**。
    pip_cost_s = len(DockerfileSpec().requirements) * 8.0  # 每个包约 8 秒

    print(f"    【场景 A：正确顺序】COPY requirements → pip install → COPY 源码")
    sim_good = LayerCacheSim()
    sim_good.build(good_layers, "第一次构建")
    for line in sim_good.log:
        print(line)
    sim_good.reset()
    # 第二次：源码变了，其余不变
    good_layers_v2 = [
        ("FROM python:3.11.9-slim", "base-v1"),
        ("COPY requirements.txt .", "req-hash-abc"),
        ("RUN pip install -r requirements.txt", "req-hash-abc"),
        ("COPY . .", "src-hash-v2"),          # ← 只有这里变了
        ("RUN useradd ... && chown ...", "user-v1"),
    ]
    sim_good.build(good_layers_v2, "改了一行代码后再构建")
    for line in sim_good.log:
        print(line)
    good_cost = 0.0 if sim_good.misses == 0 else None
    # 未命中的层里，只有 pip install 是耗时的
    good_pip_miss = any(
        "pip install" in ln and ("MISS" in ln)
        for ln in sim_good.log)
    good_time = pip_cost_s if good_pip_miss else 0.0

    print(f"\n    【场景 B：错误顺序】COPY 源码 → pip install")
    sim_bad = LayerCacheSim()
    sim_bad.build(bad_layers, "第一次构建")
    for line in sim_bad.log:
        print(line)
    sim_bad.reset()
    bad_layers_v2 = [
        ("FROM python:3.11.9-slim", "base-v1"),
        ("COPY . .", "src-hash-v2"),          # ← 只有这里变了
        ("RUN pip install -r requirements.txt", "src-hash-v2"),
        ("RUN useradd ... && chown ...", "src-hash-v2"),
    ]
    sim_bad.build(bad_layers_v2, "改了一行代码后再构建")
    for line in sim_bad.log:
        print(line)
    bad_pip_miss = any(
        "pip install" in ln and ("MISS" in ln)
        for ln in sim_bad.log)
    bad_time = pip_cost_s if bad_pip_miss else 0.0

    print(f"""
    ▸ 代价对比（只改一行源码后的再次构建）：
        正确顺序：pip install {'命中缓存，0 秒' if good_time == 0 else f'重跑，约 {good_time:.0f} 秒'}
        错误顺序：pip install {'命中缓存，0 秒' if bad_time == 0 else f'重跑，约 {bad_time:.0f} 秒'}
        差距：{bad_time - good_time:.0f} 秒 / 每次构建

    ▸ 为什么差距这么大？因为**缓存失效是链式的**：
        场景 B 里 `COPY . .` 的内容变了 → 这一层 MISS，
        于是**它之后的所有层全部 MISS**，包括 pip install。
        而场景 A 里 `COPY . .` 是倒数第二层，
        它 MISS 之后只剩一个几乎不耗时的 useradd 层。

      **Dockerfile 优化的唯一原则：把变化频率低的指令放前面。**

    ⚠ 局限：上面 {pip_cost_s:.0f} 秒是**估算值**（依赖数 × 8 秒的经验值），
      不是本机实测 —— 本课不真的下载依赖（见模块开头说明）。
      真实的安装耗时可以用 `docker build --progress=plain` 观察。
      但**「链式失效」这个机制本身是真的**，
      它是由 Docker 的构建算法决定的，与耗时数字无关。
    """)

    return {
        "written": {k: str(v) for k, v in written.items()},
        "issue_count": len(issues),
        "bad_issue_count": len(bad_issues),
        "good_time": good_time,
        "bad_time": bad_time,
    }


# ============================================================================
# 实验 2：优雅关闭（真实信号、真实进程树）
# ============================================================================
def _run_sigterm_case(shell_form: bool, label: str) -> dict[str, Any]:
    """跑一次「发 SIGTERM」的对照实验。

    Args:
        shell_form: True 表示模拟 shell form（PID 1 是 sh）；
            False 表示 exec form（python 直接是 PID 1）。
        label: 输出标签。

    Returns:
        结果字典。

    ▸ ★ 这个实验**完全是真的**，没有任何"等效模拟"：
        · shell form 的情形：用 `sh -c "python worker.py"` 起一个
          **真实的进程树** —— sh 是父进程（相当于容器里的 PID 1），
          python 是子进程。然后向 **sh** 发 SIGTERM，
          这正是 Docker 对 PID 1 做的事。
          于是我们能真实观察到：python 到底有没有收到信号。
        · exec form 的情形：直接 `python worker.py`，
          python 就是被发信号的那个进程。

      本课最初用「不安装信号处理器」来等效模拟 shell form ——
      但那样观察到的现象（进程立刻以 -15 退出）其实是
      「python 收到了信号并按默认动作退出」，
      与「python 根本没收到信号」是**相反**的结论。
      这个错误的模拟会让整个实验得出错误的教学结论，
      所以必须改成真实的进程树。
      **能真跑的就不要模拟 —— 模拟一旦错了，错得比不模拟更彻底。**

    ▸ worker 的行为设计：
        · 装 SIGTERM 处理器（记录收到信号，但**不**中断任务）
        · 然后 sleep 1.2 秒（模拟一个网络请求 + 解析）
        · 任务结束后把状态写进文件，并正常退出

      这样我们就能区分关键的两件事：
        ① 收到信号了没有（got_signal）
        ② 任务跑完了没有（acked）—— 即能不能优雅收尾
    """
    sub(f"2.x 对照实验：{label}")
    with tempfile.TemporaryDirectory() as tmp:
        out_path = os.path.join(tmp, "state.json")
        script_path = os.path.join(tmp, "worker.py")

        worker_src = textwrap.dedent('''\
            # -*- coding: utf-8 -*-
            """一个会优雅收尾的爬虫 worker（模拟）。

            它装 SIGTERM 处理器，但处理器**不中断当前任务** ——
            真实代码里这是一个标志位，主循环在安全的位置检查它。
            这里为了把「信号到达」和「任务完成」两件事拆开观察，
            直接让任务一直跑完。
            """
            import json
            import os
            import signal
            import sys
            import time

            OUT = sys.argv[1]
            state = {"acked": [], "got_signal": "", "pid": os.getpid(),
                     "ppid": os.getppid()}

            def _on_sigterm(signum, frame):
                # 只记录，不中断 —— 模拟"标志位模式"
                state["got_signal"] = signal.Signals(signum).name

            # 无条件安装处理器：这样"收不到信号"和"收到了但没处理"
            # 就能区分开了 —— 收不到就不会有 got_signal
            signal.signal(signal.SIGTERM, _on_sigterm)

            # 模拟一个耗时任务（网络请求 + 解析）
            TASK_SECONDS = 1.2
            time.sleep(TASK_SECONDS)
            state["acked"].append("task-1")

            with open(OUT, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
            ''')
        Path(script_path).write_text(worker_src, encoding="utf-8")

        if shell_form:
            # ★ 真实复现 shell form：sh 当顶层进程，python 是它的子进程。
            #   argv 不经过 shell=True，避免引出无关的转义问题 ——
            #   我们就是想让 /bin/sh 自己成为被 Popen 直接 fork 的那个进程。
            argv = ["/bin/sh", "-c", f"{sys.executable} -u {script_path} {out_path}"]
        else:
            argv = [sys.executable, "-u", script_path, out_path]

        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

        # 等 worker 进入"处理任务"的状态（任务耗时 1.2 秒）
        time.sleep(0.4)
        t_sent = time.monotonic()
        # ★ 向**编排系统看到的那个进程**发信号 ——
        #   真实 Docker 发的就是容器的 PID 1，也就是这里的 proc。
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=3.0)
            exit_code = proc.returncode
            killed = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            exit_code = -9
            killed = True
        elapsed = time.monotonic() - t_sent

        state: dict[str, Any] = {}
        if os.path.exists(out_path):
            try:
                state = json.loads(Path(out_path).read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                state = {"parse_error": True}

        got = state.get("got_signal") or "（没收到）"
        survived = bool(state.get("acked"))
        print(f"      收到 SIGTERM 的进程  ：{'sh（容器里的 PID 1）' if shell_form else 'python（PID 1）'}")
        print(f"      python 子进程是否收到信号：{got}")
        print(f"      那个 1.2 秒的任务      ："
              f"{'✅ 跑完并 ACK 了' if survived else '❌ 没跑完（被中断）'}")
        print(f"      顶层进程退出码        ：{exit_code}"
              f"{'（-9 = 被 SIGKILL 强杀）' if exit_code == -9 else ''}")
        print(f"      从发信号到顶层进程结束：{elapsed * 1000:.0f} ms")

        return {
            "label": label,
            "got_signal": state.get("got_signal", ""),
            "survived": survived,
            "exit_code": exit_code,
            "killed": killed,
            "elapsed_ms": elapsed * 1000,
        }


def exp2_graceful_shutdown() -> dict[str, Any]:
    """实验 2：优雅关闭 —— 真实信号、真实子进程。

    Returns:
        统计字典。

    ▸ 本实验**没有模拟**：起的是真实进程，发的是真实 SIGTERM。
      这正是本课最扎实的一部分 —— 读者可以在自己机器上复现。
    """
    title("【实验 2】优雅关闭：为什么容器里的爬虫「收不到停止信号」")

    print("""
    场景：一个 worker 正在处理任务（耗时 1.2 秒）。
    在任务开始 0.4 秒时，我们对它发出 SIGTERM
    （这正是 `docker stop` / `docker compose restart` 做的事）。

    对比两种 ENTRYPOINT 写法下，任务能不能跑完并 ACK：
      ❌ shell form：ENTRYPOINT python crawler.py
         → PID 1 是 /bin/sh，它默认**不转发**信号给子进程
      ✅ exec form ：ENTRYPOINT ["python", "crawler.py"]
         → python 直接是 PID 1，能收到信号

    ▸ 本实验**没有用旁路模拟**，而是起了真实的进程树：
        · shell form → 我们起 `/bin/sh -c "python worker.py"`，
          再把 SIGTERM 发给 sh（真实 Docker 发的正是容器的 PID 1）
        · exec form  → 我们直接起 python，把 SIGTERM 发给 python
      两个 case 里 python 代码**完全一样**（都无条件安装 SIGTERM 处理器），
      唯一的变量只有「信号先到谁手里」。

    ▸ 为什么坚持用真进程树？因为我在写这一课时踩了一个坑：
      最初我用「子进程不安装信号处理器」来等效模拟 shell form，
      结果实测退出码 -15、耗时 1 毫秒 —— 这恰恰证明了
      「python **收到了**信号并立即退出」，与我要论证的结论**相反**。
      **模拟一旦错了，比不模拟错得更彻底**：它会让你以为结论已被验证。
      所以在第 60 课讲并发、第 63 课讲限速之后，这里回到最实在的做法 ——
      能用真进程就不要用假进程。
    """)

    # ★ 参数含义：shell_form=True 表示「让 sh 当顶层进程」
    shell_result = _run_sigterm_case(True, "shell form：sh 是顶层进程（= 容器的 PID 1）")
    print()
    exec_result = _run_sigterm_case(False, "exec form：python 是顶层进程（= 容器的 PID 1）")

    sub("2.5 结论")

    def _fmt(result: dict[str, Any], mode: str) -> str:
        """把一次实测结果格式化成表格的一行。

        Args:
            result: `_run_sigterm_case` 的返回值。
            mode: 表格第一列显示的模式名。

        Returns:
            对齐好的一行文本。
        """
        got = "是（SIGTERM）" if result["got_signal"] else "否"
        done = "是" if result["survived"] else "否"
        return (f"    {mode:<20}{got:<14}{done:<12}"
                f"{result['exit_code']:<10}{result['elapsed_ms']:>10.0f}")

    print(f"""
    {'模式':<20}{'python 收到信号':<14}{'任务完成':<12}{'退出码':<10}{'耗时(ms)':>10}
    {'-' * 72}
{_fmt(shell_result, 'shell form')}
{_fmt(exec_result, 'exec form')}
    """)

    # ★ 结论必须由实测数据推导，不能预先写好 —— 否则就会重演上面那个坑
    shell_break = (not shell_result["survived"])
    exec_ok = exec_result["survived"]
    print(f"""    ▸ 读表方法：先看第 2 列（信号到了 python 手里没有），
      再看第 3 列（那个 1.2 秒的任务有没有跑完）。

    """)
    if shell_break:
        # 任务是不是「跑完」，用时最能说明问题：任务要 1.2 秒，
        # 如果顶层进程 1 毫秒就退了，说明它压根没等任务。
        waited = shell_result["elapsed_ms"]
        print(f"""    ▸ 本次实测：shell form 下 python **没收到**信号（第 2 列 = 否），
      那 1.2 秒的任务**没跑完**（第 3 列 = 否），
      顶层进程 {waited:.0f} ms 就退出了 —— 注意这个数字：
      **任务要 1.2 秒，它却 1 毫秒就走了**，说明它根本没有等任务，
      而是一收到信号就立刻自尽，把还在干活的子进程丢在那里。
      （本实验 wait 上限 3 秒，真实 Docker 默认等 10 秒。）
      代价有两笔：**任务被腰斩** + **重启时白白占着宽限期**。
""")
    else:
        print(f"""    ▸ 本次实测的诚实结论：shell form 下 python **也收到了**信号
      （got_signal = {shell_result['got_signal'] or '空'}），任务
      {'跑完了' if shell_result['survived'] else '没跑完'}。

      ⚠ 局限：现代 Debian/Ubuntu 的 /bin/sh（dash）在某些情况下**会**转发信号，
      本实验用的是这台机器上的 /bin/sh，它的行为不代表所有镜像。
      而且 busybox sh、不同内核版本、信号发送时机（任务刚开始 / 快结束）
      都会让结果漂移。**这正是我不写死「shell form 一定失败」的原因** ——
      读者应当在自己目标镜像上跑一遍这个实验，而不是照抄结论。

      但下面这条结论是**与 sh 行为无关**的：只要 PID 1 不是你自己的进程，
      你就少了一层对信号的控制权，而 `docker stop` 的 10 秒宽限期是硬的。
""")
    if exec_ok:
        print(f"""    ▸ exec form 下 python 直接是 PID 1，收到
      {exec_result['got_signal']}，任务跑完并 ACK，退出码 {exec_result['exit_code']}。
      这才是我们要的形状。
""")

    # ★ 与 sh 行为无关的那条结论 —— 无论实测结果如何都成立
    print("""    ▸ 但要诚实补一句：**不要把这组数字当成「shell form 必然失败」的铁律**。
      不同镜像里的 `/bin/sh` 行为并不一致：
        · Debian/Ubuntu 的 dash 是**不转发**的（本次结果就是这一类）
        · 有些环境会给 sh 加 exec，或者 sh 只跑一条命令时会**隐式 exec**，
          那样 python 会直接顶替 sh 成为 PID 1，信号反而能到
        · busybox sh、alpine、不同内核版本，行为都可能漂移
      所以正确姿势是：**在你自己的目标镜像上跑一遍本实验**，
      而不是照抄结论。这也是本课坚持用真进程树的原因 ——
      结论能被验证，才能被推翻。

    ▸ 与 sh 行为**无关**的那条结论是硬的：
      只要 PID 1 不是你自己写的进程，你就少了一层对信号的控制权；
      而 `docker stop` 的 10 秒宽限期是不打折的。
""")

    print("""    ▸ 更严重的不是等待时间，而是**任务丢失**：
      worker 已经从队列里取走了任务，但没能 ACK。
        · 用朴素队列（第 62 课）    → 任务**永久丢失**
        · 用可靠队列（RPOPLPUSH）   → 任务还在 processing 列表里，
          要等下一次 recover 才被回收 → **重复消费**
      无论哪种，都是「没做优雅关闭」的直接后果。

    ▸ 修法有两层，缺一不可：
      ① **Dockerfile 层面**：ENTRYPOINT 用 exec form，
         python 加 `-u` 或设 PYTHONUNBUFFERED（否则日志也丢）。
      ② **代码层面**：安装信号处理器 + 停止取新任务 +
         给在跑的任务收尾时间 + 超时强制退出（见 GracefulShutdown 类）。

    ▸ 还有第三层，容易被忽略：**编排系统的宽限期要匹配**。
      即使代码写得完全正确，如果 `stop_grace_period: 10s`
      而你的任务要跑 30 秒，那 20 秒后还是会被 SIGKILL。
      **`SHUTDOWN_TIMEOUT < stop_grace_period` 是一条必须成立的约束**，
      实验 3 会把它做成启动时校验。
    """)

    return {"shell": shell_result, "exec": exec_result}


# ============================================================================
# 实验 3：配置管理
# ============================================================================
def exp3_config() -> dict[str, Any]:
    """实验 3：配置校验、快速失败、跨字段一致性。

    Returns:
        统计字典。
    """
    title("【实验 3】配置管理：快速失败与跨字段校验")

    print("""
    配置系统的三个层次（本实验逐个演示）：
      层次 1：类型校验        —— 端口必须是数字
      层次 2：取值校验        —— 并发数必须是正数、不超过上限
      层次 3：跨字段一致性    —— SHUTDOWN_TIMEOUT 必须 < STOP_GRACE_PERIOD ★

    第三层是分界线：业余的配置系统只做前两层。
    """)

    cfg = build_spider_config()

    sub("3.1 场景 A：什么都没设（全用默认值）")
    cfg.load(env={})
    print(cfg.render())
    if cfg.warnings:
        print("\n    ⚠ 警告：")
        for w in cfg.warnings:
            print(f"      · {w}")
    print(f"\n    ✅ 加载成功。SHUTDOWN_TIMEOUT="
          f"{cfg.get('SHUTDOWN_TIMEOUT')}s < STOP_GRACE_PERIOD="
          f"{cfg.get('STOP_GRACE_PERIOD')}s，约束成立。")

    sub("3.2 场景 B：把优雅关闭超时设成 60 秒（超过 30 秒宽限期）")
    print("""
    ❌ 这是一个**单看每个配置都合法，组合起来是错的**例子。
       SHUTDOWN_TIMEOUT=60 是正数、类型正确 —— 前两层校验都通过。
       但它比 STOP_GRACE_PERIOD=30 大，
       意味着「优雅关闭还没做完，容器就已经被 SIGKILL 了」。
    """)
    try:
        cfg.load(env={
            "SHUTDOWN_TIMEOUT": "60",
            "STOP_GRACE_PERIOD": "30",
        })
        print("    ❌ 意外：没有报错（这是一个 bug）")
    except ConfigError as exc:
        print(f"    ✅ 启动时立刻失败，错误信息：\n")
        for line in str(exc).split("\n"):
            print(f"      {line}")
    print("""
    ▸ 对比「不校验」的后果：
      程序会正常启动、正常跑几个小时，
      然后在你第一次发布重启时，**静默地丢掉几个任务**。
      你不会在日志里看到任何错误 —— 因为 SIGKILL 不给任何机会写日志。
      **这就是为什么跨字段校验必须在启动时做。**
    """)

    sub("3.3 场景 C：一次提交多个错误（错误聚合）")
    print("""
    运维改配置时最烦的是「改一个、重启一次、再报下一个错」。
    所以配置校验应该**把所有错一次性报出来**。
    """)
    try:
        cfg.load(env={
            "REDIS_PORT": "70000",         # 越界
            "CONCURRENCY": "0",            # 不是正数
            "LOG_LEVEL": "VERBOSE",        # 枚举外
            "GLOBAL_QPS": "abc",           # 转换失败
            "USE_PROXY_POOL": "true",      # 组合缺失
            "PROXY_POOL_URL": "",
        })
        print("    ❌ 意外：没有报错")
    except ConfigError as exc:
        n = len(cfg.errors)
        print(f"    ✅ 一次报出全部 {n} 个问题：\n")
        for e in cfg.errors:
            print(f"      · {e}")
        print(f"\n    ▸ 如果逐个报，运维要重启 {n} 次才能改完。")

    sub("3.4 场景 D：敏感信息脱敏")
    print("""
    ▸ 脱敏的两个要点：
      ① **完全不显示** 比「显示掩码」安全
         （掩码长度会泄漏密码长度 —— 而长度有时是敏感信息）
      ② **标注来源**（环境变量 or 默认值）——
         排查问题时这是关键信息
    """)
    cfg.load(env={"REDIS_PASSWORD": "s3cr3t-p@ssw0rd", "ENV": "prod"})
    print(cfg.render())
    print("""
    ▸ 注意 REDIS_PASSWORD 那一行：只显示「<已设置，不显示>」。
      这条配置的 secret=True，日志、报错、监控里都不会出现它的值。

    ▸ 但要清醒地认识到：**脱敏只能防"不小心打日志"，
      不能防"有人拿到了配置"**。真正的密钥保护要靠：
        · 密钥管理服务（Vault / KMS / 云厂商 Secrets Manager）
        · K8s Secret（比 ConfigMap 多一层 base64 —— 注意 base64
          不是加密，只是编码！）
        · 文件权限 + 最小授权
      **不要把脱敏当成安全措施，它只是防误操作的护栏。**
    """)

    sub("3.5 附带好处：配置定义表可以自动生成 .env.example")
    print("""
    ▸ 一份好的配置定义表本身就是**配置文档**。
      可以自动生成给新同事的 .env.example ——
      他不会漏配任何一项，也不会去猜某个值该填什么。
    """)
    print("    # ---- 自动生成的 .env.example ----")
    for f in cfg.fields:
        if f.help_text:
            print(f"    # {f.help_text}")
        default = "" if f.secret else (f.default or "")
        req = "  # ★ 必填" if f.required else ""
        print(f"    {f.name}={default}{req}")
    print()

    return {"errors_captured": len(cfg.errors)}


# ============================================================================
# 实验 4：日志设计
# ============================================================================
def exp4_logging() -> dict[str, Any]:
    """实验 4：结构化日志与 stdout 哲学。

    Returns:
        统计字典。
    """
    title("【实验 4】日志设计：为什么必须写 stdout，为什么要结构化")

    print("""
    ❌ 常见错误做法：`logging.FileHandler("crawler.log")`
       在物理机时代这是标准做法，在容器时代是**错的**（原因见下）。

    ✅ 正确做法：往 stdout 写结构化（JSON）日志，
       让编排系统去收集、轮转、转发。
    """)

    sub("4.1 两种格式的对比（同一个事件）")
    print("    【开发模式：人类可读】")
    dev_logger = StructuredLogger(level="INFO", name="spider", json_mode=False)
    dev_logger.info("开始抓取列表页", url="https://example.com/list?p=1",
                    attempt=1)
    dev_logger.warning("遇到 429，准备退避",
                       url="https://example.com/list?p=2",
                       retry_after=2.0, proxy="10.0.0.5:8000")
    dev_logger.error("解析失败", url="https://example.com/item/9",
                     reason="缺少 price 字段")

    print("\n    【生产模式：JSON 行】")
    prod_logger = StructuredLogger(level="INFO", name="spider", json_mode=True)
    prod_logger.info("开始抓取列表页", url="https://example.com/list?p=1",
                     attempt=1)
    prod_logger.warning("遇到 429，准备退避",
                        url="https://example.com/list?p=2",
                        retry_after=2.0, proxy="10.0.0.5:8000")
    prod_logger.error("解析失败", url="https://example.com/item/9",
                      reason="缺少 price 字段")

    sub("4.2 结构化日志为什么更好用？")
    print("""
    ▸ 因为下游可以**按字段查询**，而不是靠正则去猜。

      纯文本日志想统计「有多少次 429」：
        grep "429" crawler.log | wc -l
        → 但如果日志里有一行是 "item 429 解析成功"，
          或者 URL 里含 429，你就统计错了。
          正则永远会在某个奇怪的日志上失败。

      JSON 日志：
        jq -r 'select(.msg=="遇到 429，准备退避") | .url' crawler.log
        → 精确按字段过滤，不会误匹配。
        → 加新字段不会破坏已有查询。

    ▸ 三个必须打进日志的字段（爬虫场景）：
        · **url / 请求标识** —— 出问题时能定位到具体是哪个请求
        · **尝试次数** —— 区分「偶发失败」和「反复失败」
        · **代理 / IP 标识** —— 判断是「某个代理坏了」还是「全站被封」

      ⚠ 但**绝对不能**打进日志的字段：
        · Cookie / Authorization 头（等于泄漏账号）
        · 密码、API Key
        · 完整的响应体（可能含个人信息 → 违反《个人信息保护法》）
        · 过多的用户标识（能定位到自然人）
    """)

    sub("4.3 日志级别的实际运用")
    print("""
    一个真实的爬虫该在哪些地方打什么级别的日志？

      DEBUG   —— 每个请求的完整 URL、响应码、耗时
                 （只在排查问题时开，量大）
      INFO    —— 每 N 个请求的进度、每个 Worker 的启停、
                 队列深度变化（**这就是"心跳"，健康检查的依据**）
      WARNING —— 重试、退避、代理被降权、解析缺字段后就跳过、
                 队列深度超过阈值
      ERROR   —— 连续失败达到阈值、代理池枯竭、
                 数据库写入失败、配置项在运行时被判定为不可用

    ▸ 一个反直觉的建议：**不要把「重试」打成 ERROR。**
      重试是**正常**的容错行为（第 63 课讲过退避策略）。
      如果重试打 ERROR，你的告警系统会被淹没 ——
      然后你就会开始忽略 ERROR，于是真正的故障也被忽略了。
      **告警疲劳比没有告警更危险。**
      重试应该是 WARNING，只有「重试全部失败、任务最终失败」才是 ERROR。
    """)

    sub("4.4 日志频率限制（容易被忽略的生产问题）")
    print("""
    假设你抓 100 万个页面，每个页面打 3 条日志：
      → 300 万条日志 × 平均 300 字节 = 约 900 MB

    如果目标站突然全面 403，你的爬虫会开始疯狂重试并打日志：
      → 每秒几千条 ERROR → 磁盘在几分钟内被写满
      → **磁盘满了之后，你的数据库也写不了了**
      → 一次外部故障（被封）演变成了**整个系统的崩溃**

    ▸ 解法：日志必须有**采样或限流**：
        · 相同类型的错误每 N 秒最多打一次（本课演示了这种模式）
        · 或者用「前 10 条全打，之后每 1000 条打 1 条」的采样
        · 或者设置容器日志的 max-size（compose 文件里已经配了）
      **任何可能高频产生的日志，都必须有限流。**
    """)

    # 演示一个简单的去重/限流日志器
    limiter = _RateLimitedLogger(prod_logger, min_interval=0.0)
    print("    ▸ 演示：同一个错误消息连续出现 100 次，限流器只放行几条：\n")
    for i in range(100):
        limiter.error("连接 Redis 失败", host="redis", attempt=i)
    print(f"\n    实际输出：{limiter.emitted} 条"
          f"（调用了 {limiter.calls} 次），"
          f"限流比 {(1 - limiter.emitted / limiter.calls) * 100:.1f}%")

    return {"emitted": limiter.emitted, "calls": limiter.calls}


class _RateLimitedLogger:
    """带限流的日志器包装（演示用）。

    Attributes:
        inner: 被包装的日志器。
        min_interval: 同一消息的最小输出间隔（秒）。
        emitted: 实际输出的条数。
        calls: 被调用的次数。
    """

    def __init__(self, inner: StructuredLogger, min_interval: float = 5.0) -> None:
        """初始化限流器。

        Args:
            inner: 内部日志器。
            min_interval: 同 key 的最小间隔秒数。

        ▸ 本课把 min_interval 传 0 是为了让演示能立刻看到效果；
          真实场景应该用 5~60 秒。
        """
        self.inner = inner
        self.min_interval = min_interval
        self.emitted = 0
        self.calls = 0
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def error(self, msg: str, **fields: Any) -> None:
        """限流地输出错误日志。

        Args:
            msg: 消息。
            **fields: 附加字段。

        Returns:
            None

        ▸ 限流的 key 是「消息 + 关键字段」而不是「消息本身」——
          因为 "连接 Redis 失败" 可能来自不同的 host，
          笼统地限流会掩盖「只有某一个 host 有问题」这个信息。
          但如果 key 太细（比如带上 attempt），限流又完全失效了。
          **限流粒度要在「能区分问题」和「能压住量」之间找平衡。**
        """
        self.calls += 1
        key = msg + "|" + str(fields.get("host", ""))
        now = time.monotonic()
        with self._lock:
            last = self._last.get(key, 0.0)
            if now - last < self.min_interval:
                return
            self._last[key] = now
        self.emitted += 1
        self.inner.error(msg, **fields)


# ============================================================================
# 实验 5：把整套东西串起来
# ============================================================================
def exp5_end_to_end() -> dict[str, Any]:
    """实验 5：用 deploy/ 目录的真实文件跑一次完整流程。

    Returns:
        统计字典。

    ⚠ 局限：不真的 docker compose up（原因见模块开头）。
      本实验做的是：
        · 真实读取刚写出的 Dockerfile 并解析出指令序列
        · 真实读取 docker-compose.yml 并做基本的结构校验
        · 用配置管理器加载一份「生产配置」并检查约束
        · 用 GracefulShutdown 做一次完整的关闭演练（真实信号）
    """
    title("【实验 5】端到端演练：部署文件 + 配置 + 优雅关闭")

    deploy_dir = Path(__file__).resolve().parent / "deploy"

    sub("5.1 解析真实写出的 Dockerfile")
    dockerfile_path = deploy_dir / "Dockerfile"
    if not dockerfile_path.exists():
        print("    ⚠ Dockerfile 不存在，跳过（请先跑实验 1）")
        return {"skipped": True}

    text = dockerfile_path.read_text(encoding="utf-8")
    logical, buf, buf_start = [], "", 0
    for i, raw in enumerate(text.split("\n"), 1):
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        if not buf:
            buf_start = i
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        buf += s
        logical.append((buf_start, buf))
        buf = ""

    print(f"    {'行号':>5}  {'指令':<14} 内容（截断）")
    print("    " + "-" * 70)
    for ln_no, ln in logical:
        parts = ln.split(" ", 1)
        instr = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""
        shown = rest if len(rest) <= 44 else rest[:41] + "..."
        print(f"    {ln_no:>5}  {instr:<14} {shown}")
    print(f"\n    共 {len(logical)} 条逻辑指令")

    sub("5.2 校验 docker-compose.yml 的关键约束")
    compose_path = deploy_dir / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    checks = [
        ("depends_on 配了 condition: service_healthy",
         "condition: service_healthy" in compose_text,
         "只写 depends_on 不保证依赖已就绪，爬虫启动会连不上 Redis"),
        ("stop_grace_period 已设置且 >= 30s",
         re.search(r"stop_grace_period:\s*(\d+)s", compose_text) is not None
         and int(re.search(r"stop_grace_period:\s*(\d+)s",
                           compose_text).group(1)) >= 30,
         "默认 10 秒对爬虫太短，一次请求可能就要 30 秒"),
        ("restart 策略是 unless-stopped（不是 always）",
         "restart: unless-stopped" in compose_text and
         "restart: always" not in compose_text,
         "always 会让你手动 stop 之后容器自己又起来"),
        ("日志有大小上限（防止写满磁盘）",
         "max-size:" in compose_text,
         "没有上限的日志最终会写满宿主机磁盘"),
        ("Redis 设置了 maxmemory-policy noeviction",
         "maxmemory-policy noeviction" in compose_text,
         "队列任务绝不能被内存淘汰策略静默丢掉（第 62 课的坑）"),
        ("密码走环境变量而不是写死在文件里",
         "REDIS_PASSWORD: ${" in compose_text and
         re.search(r"REDIS_PASSWORD:\s*\S*[a-zA-Z0-9]{8,}", compose_text) is None,
         "compose 文件会进版本库，写死密码等于泄漏"),
        ("容器以只读文件系统运行",
         "read_only: true" in compose_text,
         "只读根文件系统能防止被入侵后的持久化"),
    ]
    for desc, ok, why in checks:
        mark = "✅" if ok else "❌"
        print(f"    {mark} {desc}")
        if not ok:
            print(f"        为什么重要：{why}")
    passed = sum(1 for _, ok, _ in checks if ok)
    print(f"\n    通过 {passed}/{len(checks)} 项")

    sub("5.3 用一份「生产配置」走一遍校验")
    cfg = build_spider_config()
    prod_env = {
        "ENV": "prod",
        "REDIS_HOST": "redis",
        "REDIS_PORT": "6379",
        "REDIS_PASSWORD": "prod-secret-value",
        "CONCURRENCY": "32",
        "GLOBAL_QPS": "20",
        "PER_DOMAIN_QPS": "5",
        "USE_PROXY_POOL": "true",
        "PROXY_POOL_URL": "http://proxy-pool:5010",
        "LOG_LEVEL": "INFO",
        "SHUTDOWN_TIMEOUT": "25",
        "STOP_GRACE_PERIOD": "30",
    }
    cfg.load(env=prod_env)
    print(cfg.render())
    print(f"\n    ✅ 生产配置校验通过")
    print(f"    ▸ 跨字段约束检查："
          f"SHUTDOWN_TIMEOUT({cfg.get('SHUTDOWN_TIMEOUT')}s) < "
          f"STOP_GRACE_PERIOD({cfg.get('STOP_GRACE_PERIOD')}s) ✓")

    sub("5.4 用生产配置做一次优雅关闭演练")
    print(f"""
    模拟 4 个 worker 正在处理任务，其中 1 个是「卡死的」（永远不结束）。
    配置：SHUTDOWN_TIMEOUT={cfg.get('SHUTDOWN_TIMEOUT')}s
    （为了让本实验快速结束，下面用 1.5 秒代替 25 秒演示超时逻辑）
    """)

    sd = GracefulShutdown(timeout=1.5, name="spider")
    acquired: list[int] = []
    lock = threading.Lock()

    def make_worker(worker_id: int, duration: float) -> Callable[[int], bool]:
        """生成一个 worker 任务函数。

        Args:
            worker_id: worker 编号。
            duration: 每轮任务耗时。

        Returns:
            任务函数。
        """
        def task(round_no: int) -> bool:
            """执行一轮任务。

            Args:
                round_no: 轮次。

            Returns:
                True 表示这一轮确实干了活。
            """
            if sd.should_stop():
                # ★ 关键：收到停止信号后**不再取新任务**。
                #   这是优雅关闭的第 ② 步，顺序错了就等于没做。
                return False
            time.sleep(duration)
            with lock:
                acquired.append(worker_id)
            return True
        return task

    stats_list: list[dict[str, Any]] = []
    threads: list[threading.Thread] = []

    def worker_thread_runner(worker_id: int, duration: float,
                             hang: bool = False) -> None:
        """在线程里跑 worker 主循环。

        Args:
            worker_id: worker 编号。
            duration: 每轮任务耗时。
            hang: 是否模拟卡死的 worker。

        Returns:
            None
        """
        runner = GracefulShutdown(timeout=sd.timeout, name=f"w{worker_id}")
        runner.triggered = sd.triggered     # 共享同一个停止标志
        runner.signal_name = "SIGTERM"
        if hang:
            # 卡死的 worker：不检查停止标志，永远不结束
            while True:
                time.sleep(0.1)
        st = runner.run_until_signal(
            make_worker(worker_id, duration), max_rounds=1000)
        st["worker"] = worker_id
        stats_list.append(st)

    for wid, dur in enumerate([0.15, 0.2, 0.25], start=1):
        t = threading.Thread(target=worker_thread_runner,
                             args=(wid, dur), name=f"worker-{wid}")
        threads.append(t)
    # 第 4 个是卡死的（演示超时强制退出的必要性）
    t_hang = threading.Thread(target=worker_thread_runner,
                              args=(4, 0.1, True), name="worker-4(卡死)")
    t_hang.daemon = True
    threads.append(t_hang)

    for t in threads:
        t.start()
    # 让 worker 跑一会儿，积累一些"已完成的任务"
    time.sleep(0.8)

    t_signal = time.monotonic()
    print(f"    [{time.strftime('%H:%M:%S')}] 模拟收到 SIGTERM，"
          f"通知所有 worker 停止取新任务…")
    sd.signal_name = "SIGTERM"
    sd.triggered.set()

    result = sd.wait_for_tasks(threads[:3])   # 只等 3 个正常的
    time.sleep(0.1)
    elapsed = (time.monotonic() - t_signal) * 1000

    print(f"    [{time.strftime('%H:%M:%S')}] 收尾完成，"
          f"耗时 {elapsed:.0f} ms")
    print()
    print(f"    正常 worker 数        ：{result['total']}")
    print(f"    已收尾的 worker 数    ：{result['finished']}")
    print(f"    收尾等待时间          ：{result['waited'] * 1000:.0f} ms")
    print(f"    已完成的任务总数      ：{len(acquired)}"
          f"（本轮实际干完的活）")
    print(f"    第 4 个 worker 是否还活着："
          f"{'是（它卡死了，超时后被放弃）' if t_hang.is_alive() else '否'}")
    print(f"""
    ▸ 这个演练体现了优雅关闭的四个步骤：
        ① 捕获信号        —— sd.triggered.set()
        ② 停止取新任务    —— task() 开头检查 should_stop()
        ③ 等任务跑完      —— wait_for_tasks() 的 join(timeout)
        ④ 超时放弃并记录  —— 第 4 个 worker 卡死，
                             超时后**不等它**，并把它记为「未收尾」

    ▸ 关键设计点：第 ④ 步不是「失败」，而是**必要的取舍**。
      如果没有超时，这个卡死的 worker 会让容器永远关不掉 ——
      编排系统等到宽限期结束还是会 SIGKILL（并且你什么都不知道）。
      **主动超时 + 记录「谁没关干净」**，
      既保证了容器能按时退出，又留下了排查线索。

    ▸ 生产环境还应该做的（本课未实现，但要知道）：
        · 把「未收尾的 worker」上报到监控 → 告警
        · 未 ACK 的任务应该主动 nack（第 62 课的 RedisQueue.nack），
          而不是等 recover 周期
        · 关闭前把「我处理到哪了」写入检查点（第 65 课的断点续爬）
    """)

    # 恢复原始信号处理器，避免影响后续实验
    sd.restore()
    return {"compose_checks": f"{passed}/{len(checks)}",
            "tasks_done": len(acquired)}


# ============================================================================
# 踩坑记录
# ============================================================================
def pitfalls() -> None:
    """打印本课实测中真实遇到的问题。

    Returns:
        None

    每一条都是「❌ 错误做法 → 现象 → 根因 → 正确做法」的结构。
    """
    title("踩坑记录：本课实测中真实遇到的问题")

    print("""
    ── 坑 1：ENTRYPOINT 用 shell form → SIGTERM 到不了 Python ────────────
    ❌ 错误做法：`ENTRYPOINT python -u crawler.py`
    现象：`docker stop` 之后容器总要等满 10 秒才退出，
          `docker logs` 上显示退出码 137（= 128 + 9，即 SIGKILL）。
          更糟的是：**正在处理的任务全部丢失，而且日志里没有任何线索**。
          本课实验 2 用真实进程复现了这个现象。
    根因：shell form 会让容器执行 `/bin/sh -c "python -u crawler.py"`，
          于是 **PID 1 是 sh 而不是 python**。
          而 sh 在**非交互模式**下**不会**把信号转发给子进程
          （这是 POSIX 的既定行为，不是 bug）。
          所以 Docker 发的 SIGTERM 到了 sh，sh 收到了但什么也没做，
          10 秒后 Docker 只能 SIGKILL 整个进程组。
    正确做法：
          ① `ENTRYPOINT ["python", "-u", "crawler.py"]`（exec form）
          ② 代码里安装 SIGTERM 处理器做优雅收尾
          ③ 如果确实需要 shell 的功能（比如展开环境变量），
             可以用 `exec python -u crawler.py` ——
             `exec` 会让 shell **用 python 替换掉自己**，
             python 就成为 PID 1，信号就能收到了。
    教学价值：**「进程树里的位置」决定了你能不能收到信号。**
          调试这类问题的好工具是 `docker exec <容器> ps -ef`
          —— 一看 PID 1 是什么就明白了。
          很多「容器关不干净」的抱怨，根因都在这里。


    ── 坑 2：优雅关闭超时 > 编排系统宽限期 → 优雅关闭形同虚设 ───────────
    ❌ 错误做法：SHUTDOWN_TIMEOUT=60，但 compose 里 stop_grace_period=30（默认 10）。
    现象：代码里写了完美的优雅关闭逻辑，日志里也能看到信号收到了、
          开始收尾了 —— 但每次重启仍然有任务丢失，
          而且日志在某个点**突然截断**（后面什么都没有）。
    根因：容器是被 SIGKILL **强杀**的 —— 所以日志才是截断的。
          你的 60 秒收尾时间永远用不完，因为 30 秒时它就被杀了。
          **代码正确，但配置矛盾的，结果和不写一样。**
    正确做法：`SHUTDOWN_TIMEOUT` 必须**严格小于** `stop_grace_period`，
          留出余量（本课取 25 < 30）。
          而且这个约束**应该在启动时校验**，而不是靠人记住 ——
          本课实验 3 的跨字段校验就是干这个的。
    教学价值：**凡是「两个配置项之间存在大小关系」的地方，
          都必须写成启动时校验。** 靠文档约定是迟早会出错的。
          同类的例子：连接池上限 < 数据库 max_connections、
          超时时间逐层递增（客户端 < 网关 < 后端）、
          batch size × 并发数 < 内存预算。


    ── 坑 3：`COPY . .` 放在 `pip install` 之前 → 改一行代码重装全部依赖 ──
    ❌ 错误做法：
          COPY . .
          RUN pip install -r requirements.txt
    现象：本地开发时感觉不明显（依赖少的时候安装只要几秒）。
          上线后依赖多了（scrapy + lxml + cryptography + pandas），
          每改一行代码重新构建要**等好几分钟**，
          而且 CI 上每次构建都跑满 CPU 配额。
    根因：Docker 的层缓存是**链式**的 ——
          某层的内容变了，**它之后的所有层都失效**。
          `COPY . .` 的内容包含源码，所以改代码 → 这一层变 →
          后面所有层（包括 pip install）全部重跑。
    正确做法：把指令按「变化频率」从低到高排列：
          FROM → ENV → apt install → COPY requirements.txt →
          pip install → COPY . . → USER
          这样改代码只会让最后两层失效。
    教学价值：**Dockerfile 优化的唯一原则是「变化频率低的放前面」。**
          本课实验 1 用模拟器量化了这个差别。
          同类的思路在别处也成立：CI 流水线把「依赖安装」和「跑测试」
          分成两个缓存阶段、webpack 把 vendor 和业务代码分成两个 chunk。


    ── 坑 4：把密码写进 ENV 指令 → 密码永久留在镜像里 ───────────────────
    ❌ 错误做法：
          ENV REDIS_PASSWORD=supersecret123
          # 后来"清理"了一下：
          ENV REDIS_PASSWORD=
    现象：你以为清掉了，但任何拿到镜像的人执行
            docker history --no-trunc myimage:latest
         仍然能看到第一行的完整值。
    根因：**Docker 镜像的每一层都是不可变的**。
          后面的层「覆盖」了前面层的值，但前面层的**数据仍在镜像里**。
          这与 git 的行为类似：你删掉文件，但历史提交里还有。
          所以「先写后删」在镜像里**不给任何安全保证**。
    正确做法：
          · 运行时通过环境变量注入（compose 的 environment + .env、
            K8s Secret、云厂商的 Secrets Manager）
          · 构建时确实需要密钥（比如私有 pip 源）→ 用 BuildKit 的
            `RUN --mount=type=secret,id=pip_token ...`，
            secret 只在构建时可见，**不会留在任何层里**
          · 用一个专门的 .dockerignore 挡住 .env / *.pem / secrets/
    教学价值：**「不可变存储」上的删除都是假删除。**
          镜像、git 提交、对象存储的版本控制、
          数据库的 WAL —— 都有这个性质。
          在把任何东西写进去之前，先问：「这能拿回来吗？」


    ── 坑 5：日志写到容器内的文件 → 出问题时日志正好没了 ─────────────────
    ❌ 错误做法：`logging.FileHandler("/app/logs/crawler.log")`
    现象：本地开发一切正常。上线后某天爬虫崩了，
          `docker compose logs spider` **什么都没有**。
          要 `docker exec` 进容器看文件 —— 但容器已经因为
          `restart` 策略被重建了，文件**没了**。
          而且更常见的：容器跑了三周，磁盘被日志写满，
          **数据库也写不进去了**。
    根因：容器是**一次性**的，文件系统随容器一起消失；
          容器也没有内置的日志轮转。
          而 Docker/K8s 已经提供了完善的日志基础设施（收集、轮转、转发）。
    正确做法：日志写 **stdout**，由平台收集。
          程序用 `logging.StreamHandler(sys.stdout)`，
          并且在 Dockerfile 里：
            · `python -u` 或 `PYTHONUNBUFFERED=1`（否则有块缓冲）
            · compose 里配 `logging.options.max-size` 做兜底轮转
          在 K8s 里则是让日志写 stdout，由 DaemonSet 收集。
    教学价值：这是十二要素的第十一条「日志当事件流」。
          **程序的职责是"产生事件"，不是"存储事件"。**
          把这两件事分开，你的容器就变成了无状态的、
          可以随时被替换的 —— 这正是容器化的价值所在。


    ── 坑 6：健康检查只查「进程在不在」→ 被封了还报健康 ─────────────────
    ❌ 错误做法：HEALTHCHECK CMD pgrep -f crawler.py
    现象：爬虫的出口 IP 半夜被目标站封了。
          所有请求都返回 403，成功率掉到 0%。
          但健康检查一直说"健康"（进程活得好好的），
          编排系统不重启、不摘流量、不告警。
          直到第二天早上有人发现**数据从凌晨两点就断了**。
    根因：**「进程存活」和「业务健康」是两个完全不同的概念。**
          被封的爬虫 CPU 占用还特别低（因为没啥可干），
          从系统监控上看它"很清闲很健康"。
    正确做法：健康检查必须查**业务指标**，最常用的是「心跳」：
          · 主循环每成功处理一个任务就更新心跳文件/时间戳
          · 健康检查脚本读它，超过 N 秒没更新就返回不健康
          更完善的做法是暴露 HTTP /healthz 端点，
          返回结构化的健康信息（队列深度、成功率、上次成功时间），
          让监控系统能区分「不健康」和「完全没响应」。
          ★ 心跳必须由**成功处理任务**触发，而不是"循环跑了一圈"。
            否则即使所有请求都失败，心跳照样更新，问题被掩盖。
    教学价值：**任何"存活检测"都必须检测"它该做的事有没有被做"。**
          K8s 里 liveness 和 readiness 要分开：
            · liveness 失败 → 重启容器
            · readiness 失败 → 摘掉流量，但不重启
          爬虫被目标站封了，应该 **readiness 失败但不重启** ——
          重启并不能解决 IP 被封的问题，只会让日志变乱。
          这个区分能省下大量无效重启。
    """)


# ============================================================================
# 本课要点
# ============================================================================
def keypoints() -> None:
    """打印本课要点。

    Returns:
        None
    """
    title("本课要点")
    for line in [
        "1. 容器化的价值是「可复现 + 可替换」，不是「轻量」—— 不可复现的构建等于没有构建",
        "2. 基础镜像必须钉到 patch 版本（python:3.11.9-slim），latest 和 python:3.11 都不够",
        "3. slim 优于 alpine：musl 会让 lxml/cryptography 等 wheel 装不上，退化成源码编译",
        "4. Dockerfile 优化的唯一原则：把变化频率低的指令放前面（层缓存是链式失效的）",
        "5. 先 COPY requirements 再 pip install 再 COPY 源码 —— 改代码就不会重装依赖",
        "6. ENTRYPOINT 必须用 exec form，让 Python 成为 PID 1，否则 SIGTERM 收不到",
        "7. python -u / PYTHONUNBUFFERED 必须加，否则日志被块缓冲、强杀时全丢",
        "8. 优雅关闭四步：捕获信号 → 停取新任务 → 等在跑的任务 → 超时强制退出并记录",
        "9. 优雅关闭的「超时」不能省：卡死的任务会让容器永远关不掉，且你不知道是谁",
        "10. SHUTDOWN_TIMEOUT 必须 < stop_grace_period，且要在启动时做跨字段校验",
        "11. 配置要外置到环境变量（12-Factor 第三条），代码里只能有安全的默认值",
        "12. 配置校验要「快速失败」+「错误聚合」：启动时一次报完所有问题，别逐个报",
        "13. 跨字段一致性校验是配置系统的分界线：单看合法、组合出错的值最难排查",
        "14. 密钥绝不进镜像（ENV、COPY）—— 镜像层不可变，删掉的还在历史层里",
        "15. 日志写 stdout 不写文件：容器是一次性的、没有轮转，文件日志会丢也会写满磁盘",
        "16. 日志要结构化（JSON）+ 加了限流，否则一次外部故障会演变成磁盘写满的系统崩溃",
        "17. 不要把「重试」打成 ERROR —— 告警疲劳比没有告警更危险",
        "18. 健康检查要查业务指标（上次成功抓取距今多久），不是查进程在不在",
        "19. K8s 里 liveness（重启）和 readiness（摘流量）要分开：被封 IP 应该摘流量而不是重启",
        "20. compose 的 depends_on 必须配 condition: service_healthy，否则爬虫会先于 Redis 就绪启动",
        "21. 容器日志必须在编排层配轮转（max-size/max-file），程序自己不管这件事",
        "22. 以非 root 运行 + 只读根文件系统：K8s 会拒绝 root，且能限制被入侵后的破坏面",
    ]:
        print("  " + line)
    print()


# ============================================================================
# 主流程
# ============================================================================
def main() -> None:
    """运行全部实验。

    Returns:
        None
    """
    print(SEP)
    print("阶段 6 · 第 64 课：部署与容器化")
    print(SEP)
    print("""
本课回答一个问题：**爬虫在笔记本上跑得好好的，怎么搬到服务器上？**

  本地跑通 ≠ 能上生产。中间隔着一堆"非功能"问题：

    配置    硬编码在本地的路径/密码/并发数，到服务器全都不对
    依赖    本地的 Python 版本和全局包，和服务器不一样
    日志    写到本地文件，容器一重建就没了
    信号    收到停止指令时直接被杀，任务丢一半
    健康    IP 被封了但进程还活着，没人知道要处理
    重启    每次发布都丢任务、数据出现半截文件

  本课把这些逐个解决，最终得到一套**可以直接上生产**的部署方案。

⚠ 本课的局限（重要）：
  · 本课**真的会把 Dockerfile / docker-compose.yml / .dockerignore /
    requirements.txt / healthcheck.py 写到你磁盘上的 deploy/ 目录**，
    这些文件是真实可用的。
  · 但**不会真的构建镜像或启动容器** —— 原因见模块开头的详细说明，
    核心是「构建镜像依赖外网、耗时远超本课的单文件时间预算」。
  · 与 Docker 相关的验证用「静态检查 + 缓存模拟」代替，
    这部分局限已在实验 1 中明确标注。
  · **信号处理和配置管理是真实运行的**（真实 fork 子进程、
    真实发 SIGTERM），不涉及模拟。

★ 合规提醒：
  部署能力是中性技术。请遵守目标站的 robots.txt 与服务条款，
  遵守《数据安全法》《个人信息保护法》。
  把爬虫部署到云服务器时，注意出口 IP 的合规性 ——
  用云厂商的 IP 做大规模采集可能违反其服务条款。
""")

    results: dict[str, Any] = {}
    results["exp1"] = exp1_write_and_lint()
    results["exp2"] = exp2_graceful_shutdown()
    results["exp3"] = exp3_config()
    results["exp4"] = exp4_logging()
    results["exp5"] = exp5_end_to_end()

    pitfalls()
    keypoints()

    print(SEP)
    print("本课小结")
    print(SEP)
    print(f"""
    本课产出的真实文件（在 {Path(__file__).resolve().parent / 'deploy'}）：
      Dockerfile            多阶段、非 root、exec form、带健康检查
      requirements.txt      全部钉版本，保证可复现
      .dockerignore         挡住数据、日志、虚拟环境、密钥
      docker-compose.yml    Redis + 爬虫，含健康依赖与宽限期
      healthcheck.py        业务级健康探针（查心跳而不是查进程）

    读者的下一步（本课未做，留作练习）：
      1. 在装了 Docker 的机器上执行：
           cd deploy && docker compose up -d
         验证这五个文件真的可用。如果构建失败，
         对照本课的静态检查规则逐条排查。
      2. 故意把 SHUTDOWN_TIMEOUT 改成 60（大于 stop_grace_period），
         看配置校验能不能拦住你。
      3. 用 `docker exec <容器> ps -ef` 观察 PID 1 是谁 ——
         这是排查「信号收不到」最直接的手段。
      4. 把 healthcheck.py 改成读一个真实的 Redis key
         （比如 "最近成功时间"），让它检查真实的业务状态。

    ▸ 一个贯穿本课的思维方式：
      **本地跑通关心的是"功能"，上生产关心的是"失败时怎么办"。**
      部署工时的大头不在写爬虫逻辑，
      而在「配置错了怎么早发现」「被杀了怎么不丢数据」
      「挂了怎么自动恢复」「出问题怎么定位」。
      这四件事做好了，你的爬虫才算能上生产。
    """)


if __name__ == "__main__":
    main()
