# --------------------------------------------------------------------------
# 爬虫镜像 Dockerfile（由 64_deployment.py 自动生成）
# 目标：Python 3.11 + 非 root 运行 + 健康检查
# --------------------------------------------------------------------------

# ---------- 阶段 1：基础运行时 ----------
# ⚠ 钉到 patch 版本，不要用 latest，也不要用 python:3.11
FROM python:3.11.9-slim

# 环境变量的三条 OCI/Docker 惯例：
#   PYTHONDONTWRITEBYTECODE 不生成 .pyc（容器是只读的一次性环境，
#       生成 .pyc 只是浪费镜像层空间和启动时间）
#   PYTHONUNBUFFERED 日志实时输出（等价于 python -u）
#   PIP_NO_CACHE_DIR 不留 pip 缓存（否则镜像会大几百 MB）
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai

# 时区数据：slim 镜像默认不带 tzdata，
# 不装的话 TZ 环境变量不生效，日志时间会差 8 小时（很常见的事故）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ---------- 阶段 2：依赖 ----------
# ★ 关键：先只 COPY requirements.txt，再 pip install，最后才 COPY 源码。
#   为什么顺序这么重要？因为 Docker 的层缓存是「按指令逐条命中」的：
#     改了源码 → 只有最后的 COPY . . 失效，pip install 仍然命中缓存
#     如果反过来先 COPY . . 再 pip install →
#       改一个字的代码就会让 pip install 重跑一遍（几分钟）
#   本课实验 1 会用模拟器量化这个差别。
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---------- 阶段 3：源码与权限 ----------
COPY . .

# 创建非 root 用户并切换。
# 为什么不以 root 运行？
#   ① 容器逃逸时，root 的破坏面更大；
#   ② 很多 K8s 集群开启了「禁止 root」的准入策略，
#      以 root 运行的 Pod 会被直接拒绝调度；
#   ③ 挂载宿主机目录时，root 写的文件在宿主机上也是 root 所有，
#      清理起来很麻烦。
RUN useradd --create-home --shell /bin/bash crawler \
    && chown -R crawler:crawler /app
USER crawler

# 健康检查：容器编排系统靠它判断「这个实例能不能接流量」。
# 爬虫的健康检查该查什么？**不要查「进程还在不在」**（没意义），
# 要查「最近一次成功抓取距今多久」——
# 被目标站封了 IP 的爬虫进程活得好好的，但已经没在干活了。
# 本课用一个最小的探针脚本模拟这个语义。
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "healthcheck.py"] || exit 1

# 使用 exec form，让 python 成为 PID 1（见 render_dockerfile 的说明）
ENTRYPOINT ["python", "-u", "crawler.py"]
