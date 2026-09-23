#!/data/data/com.termux/files/usr/bin/bash
# ============================================
# 第 3 段:SSH 密钥 + 目录 + 别名
# 粘贴完执行: bash part3.sh
# ============================================

echo "=== 第 3 段:SSH 密钥 ==="

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

KEY="$HOME/.ssh/id_ed25519"

if [ -f "$KEY" ]; then
    echo "[OK] SSH 密钥已存在,直接复用"
else
    echo "[..] 正在生成密钥(不设密码,一路回车)..."
    ssh-keygen -t ed25519 -N "" -C "termux@android" -f "$KEY" >/dev/null 2>&1
    if [ -f "$KEY" ]; then
        chmod 600 "$KEY"
        chmod 644 "$KEY.pub"
        echo "[OK] 密钥已生成"
    else
        echo "[x] 生成失败,手动执行: ssh-keygen -t ed25519"
    fi
fi

# 预置 github.com 的 host key
if [ -f "$KEY" ]; then
    ssh-keyscan -t rsa,ecdsa,ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
    echo "[OK] 已预置 github.com host key"
fi

# --- 工作目录 ---
mkdir -p "$HOME/projects"
echo "[OK] 已创建 ~/projects"

# --- 别名 ---
BR="$HOME/.bashrc"
if grep -q "termux-setup aliases" "$BR" 2>/dev/null; then
    echo "[OK] 别名已存在,跳过"
else
    echo "" >> "$BR"
    echo "# >>> termux-setup aliases >>>" >> "$BR"
    echo "alias gs='git status'" >> "$BR"
    echo "alias gd='git diff'" >> "$BR"
    echo "alias gl='git log --oneline --graph --decorate -15'" >> "$BR"
    echo "alias gp='git push'" >> "$BR"
    echo "alias gpl='git pull'" >> "$BR"
    echo "alias ga='git add -A'" >> "$BR"
    echo "alias gc='git commit -m'" >> "$BR"
    echo "alias proj='cd ~/projects'" >> "$BR"
    echo "alias ll='ls -alh'" >> "$BR"
    echo "# <<< termux-setup aliases <<<" >> "$BR"
    echo "[OK] 已添加别名(重开 Termux 或 source ~/.bashrc 生效)"
fi

# --- 打印公钥 ---
echo ""
echo "============================================"
echo "  复制下面这一整行,这是你的公钥"
echo "============================================"
echo ""
if [ -f "$KEY.pub" ]; then
    cat "$KEY.pub"
else
    echo "(密钥不存在,请先解决上面的报错)"
fi
echo ""
echo "============================================"
echo "  下一步:"
echo "  1. 长按选中上面那行公钥并复制"
echo "  2. 手机浏览器打开 github.com/settings/keys"
echo "  3. 点 New SSH key,粘贴进去,保存"
echo "  4. 回来验证: ssh -T git@github.com"
echo "============================================"
echo ""
echo ">>> 全部完成 <<<"
