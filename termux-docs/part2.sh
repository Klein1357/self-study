#!/data/data/com.termux/files/usr/bin/bash
# ============================================
# 第 2 段:存储权限 + 防后台杀 + git 身份
# 粘贴完执行: bash part2.sh
# ============================================

echo "=== 第 2 段:存储权限 + git 身份 ==="

# --- 存储权限 ---
if [ -d "$HOME/storage" ]; then
    echo "[OK] 存储权限已有"
else
    echo "[..] 即将弹出权限框,请点「允许」"
    termux-setup-storage 2>/dev/null
    sleep 3
    if [ -d "$HOME/storage" ]; then
        echo "[OK] 存储权限已配置"
    else
        echo "[!] 没拿到权限,可稍后手动: 设置 > 应用 > Termux > 权限 > 允许"
    fi
fi

# --- 防止后台被杀 ---
termux-wake-lock 2>/dev/null && echo "[OK] 已锁定后台" || echo "[!] wake lock 失败,不影响"

# --- git 身份 ---
echo ""
echo "--- 配置 git 身份 ---"
read -r -p "你的名字(回车跳过): " GN
if [ -n "$GN" ]; then
    git config --global user.name "$GN"
    echo "[OK] 名字已设为: $GN"
fi

read -r -p "你的邮箱(回车跳过): " GE
if [ -n "$GE" ]; then
    git config --global user.email "$GE"
    echo "[OK] 邮箱已设为: $GE"
fi

# 基础偏好设置
git config --global init.defaultBranch main 2>/dev/null
git config --global core.editor nano 2>/dev/null
echo "[OK] 默认分支 main / 编辑器 nano"

echo ""
echo "--- 当前配置 ---"
git config --global user.name 2>/dev/null
git config --global user.email 2>/dev/null

echo ""
echo ">>> 第 2 段完成,继续粘第 3 段 <<<"
