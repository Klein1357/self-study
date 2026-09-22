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
