#!/data/data/com.termux/files/usr/bin/bash
# ============================================
# 第 1 段:换源 + 装基础包
# 粘贴完执行: bash part1.sh
# ============================================

echo "=== 第 1 段:换源 + 装基础包 ==="

# 备份原始源配置
SRC=/data/data/com.termux/files/usr/etc/apt/sources.list
if [ -f "$SRC" ] && [ ! -f "$SRC.orig" ]; then
    cp "$SRC" "$SRC.orig"
    echo "[OK] 已备份原始源配置"
fi

# 写入清华源
MIRROR="https://mirrors.tuna.tsinghua.edu.cn/termux/apt/termux-main"
DEB="deb $MIRROR stable main"
APT_DIR=/data/data/com.termux/files/usr/etc/apt
mkdir -p "$APT_DIR/sources.list.d"
echo "$DEB" > "$SRC"
echo "$DEB" > "$APT_DIR/sources.list.d/termux-mirror.list"
echo "[OK] 已切换到清华源"

# 更新索引
echo "[..] 正在更新软件包索引,请稍等..."
pkg update -y
echo "[OK] 索引更新完成"

# 装基础包
echo "[..] 正在安装 git / openssh / nano ..."
pkg install -y git openssh nano termux-tools
echo "[OK] 安装完成"

# 验证
echo ""
echo "=== 验证 ==="
git --version 2>/dev/null && echo "[OK] git 可用" || echo "[x] git 不可用"
ssh -V 2>&1 | head -1 && echo "[OK] ssh 可用" || echo "[x] ssh 不可用"
which nano >/dev/null 2>&1 && echo "[OK] nano 可用" || echo "[x] nano 不可用"

echo ""
echo ">>> 第 1 段完成,继续粘第 2 段 <<<"
