# Termux 手机开发环境 · 完整手册

> 记录时间:2026-09-23
> 环境:Android + Termux 0.118.3 (GitHub 版) + SSH 走 443 端口

---

## 目录

- [一、Termux 能做什么](#一termux-能做什么)
- [二、从零安装流程](#二从零安装流程)
- [三、日常 Git 工作流](#三日常-git-工作流)
- [四、命令速查表](#四命令速查表)
- [五、踩坑记录(重要)](#五踩坑记录重要)
- [六、进阶方向](#六进阶方向)

---

## 一、Termux 能做什么

Termux 不是"手机上的玩具终端",它是一个**完整的 Linux 环境**(不需要 root)。它可以分成这几类用途:

### 1. 随身 Git 客户端(你已配好)

最基础的用法。地铁上、床上改点代码提交上去,不用开电脑。

- clone / commit / push / pull、开分支、解决冲突
- 配合 GitHub 的 SSH 免密,一次性配好终身受用
- **手机上特别建议小步提交**——手机容易被中途打断(来电、切 App、电量),小 commit 是你的安全网

### 2. 远程服务器管理

这是 Termux 被低估的能力。它是一个**极好的 SSH 客户端**:

```bash
ssh user@你的服务器
```

连上后就能重启服务、看日志、改配置。**注意区分环境**:

- 连上服务器后,`dnf`/`systemctl` 这些是**服务器**上的命令
- `pkg` 是 **Termux 本地**的命令
- 输 `exit` 回到手机

端口转发也很实用,能把服务器上的服务映射到手机本地:

```bash
ssh -L 8080:127.0.0.1:8080 user@服务器
```

文件互传:

```bash
scp file.zip user@服务器:/home/user/     # 传上去
scp user@服务器:/home/user/report.txt .  # 拉下来
rsync -av --progress ./site/ user@服务器:/var/www/site/   # 同步目录
```

### 3. 跑脚本和自动化

装 Python / Node.js 后,可以做:

- **爬虫**:你仓库名是 `self-study`,里面有「Python爬虫学习资料包」,那这正好用得上
- **定时任务**:配合 `termux-job-scheduler` 定时跑脚本
- **文件整理**:批量重命名、格式转换
- **Telegram/微信机器人**:用 `curl` + `jq` 调 API

```bash
pkg install python
python 你的脚本.py
```

### 4. 本地 Web 服务 / 预览

```bash
cd ~/projects/你的项目
python -m http.server 8080
```

然后在手机浏览器打开 `http://127.0.0.1:8080`。**做网页时在手机上直接预览效果**,很方便。

> ⚠️ **安全提醒**:不要随便用 `--bind 0.0.0.0`,那会让**同一 Wi-Fi 下的其他人**都能访问你的服务。在公共 Wi-Fi 上尤其危险。只在明确需要局域网测试时才用。

### 5. 调用手机硬件(Termux:API)

装 `termux-api` 后,脚本能操作手机:

```bash
pkg install termux-api
termux-notification --title "任务完成"    # 发通知
termux-vibrate -d 200                     # 震动
termux-battery-status                     # 读电量
termux-tts-speak "构建完成"                # 语音播报
termux-clipboard-set "文本"               # 写剪贴板
```

> 需要**额外装 Termux:API 这个 App**,且必须和 Termux 本体**同签名来源**(你是 GitHub 版,就要下 GitHub 版的 Termux:API)。

### 6. 其他实用工具

| 用途 | 装什么 | 干什么 |
|---|---|---|
| 网络排查 | `pkg install nmap dnsutils` | ping、端口扫描、DNS 查询 |
| 音视频处理 | `pkg install ffmpeg` | 转格式、裁剪、提取音频 |
| 数据库 | `pkg install sqlite` | 本地 SQLite(手机首选) |
| 下载 | `pkg install aria2` | 多线程下载 |
| 编译 | `pkg install clang make cmake` | C/C++ 编译 |
| 压缩 | `pkg install zip unzip tar` | 压缩解压 |

### 不适合做的事

- **长时间跑的服务**:Android 会杀后台
- **生产数据库**:Android 进程生命周期和存储限制让手机不适合做长期数据库宿主
- **依赖 glibc/systemd 的重型项目**:Termux 不是标准 GNU/Linux,遇到刚不动的项目就换 PRoot 或服务器

---

## 二、从零安装流程

### 第 1 步:下载 APK

**别用 Google Play 版**——已弃用,`pkg` 会失败。

国内下载慢的话用 GitHub 代理加速:

```bash
# 用手机浏览器打开这个链接下载
https://ghproxy.net/https://github.com/termux/termux-app/releases/download/v0.118.3/termux-app_v0.118.3+github-debug_arm64-v8a.apk
```

- 文件大小约 **33.5 MB**(不对就是没下完整)
- 选 `arm64-v8a`;不确定就选 `universal`
- 备选代理:`gh-proxy.com`、`ghfast.top`
- 备选镜像:清华 `mirrors.tuna.tsinghua.edu.cn/fdroid/repo/`

**装之前先彻底卸载旧版 Termux 和所有插件**,否则会「应用未安装」。

### 第 2 步:初始化(三段脚本)

> 完整脚本有 3 个 heredoc 块 + 4 行超长命令,手机粘贴极易截断。
> **拆成三段,每段最长 78 行,无 heredoc。**

#### part1.sh — 换源 + 装基础包

```bash
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
```

#### part2.sh — 存储权限 + 防后台杀 + git 身份

```bash
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
```

> **如果名字那步误按回车跳过**,随时用这两条补上:
> ```bash
> git config --global user.name "你的名字"
> git config --global user.email "你的邮箱"
> ```

#### part3.sh — SSH 密钥 + 目录 + 别名

```bash
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
```

**执行方式**(每段):

1. `nano part1.sh` → 长按粘贴 → `CTRL+O` 回车保存 → `CTRL+X` 退出
2. `bash part1.sh`

### 第 3 步:把公钥加到 GitHub

1. 手机浏览器打开 `github.com/settings/keys`
2. **New SSH key** → Title 随便填(如 `android-phone`)
3. Key type 选 **Authentication Key**
4. 粘贴 `part3.sh` 打印出来的**整行公钥** → 保存

### 第 4 步:解决 22 端口被封(重要)

国内不少网络屏蔽 SSH 的 22 端口,报错长这样:

```
ssh: connect to host github.com port 22: Connection closed by 20.205.243.166
```

**解法**:走 443 端口。建 `~/.ssh/config`:

```bash
nano ~/.ssh/config
```

粘进:

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

> **这个文件别删**,删了就又连不上。

### 第 5 步:验证

```bash
ssh -T git@github.com
```

首次会问 `Are you sure you want to continue connecting?`,**要输完整的 `yes`**(不能只按 y)。

成功标志:

```
Hi 你的用户名! You've successfully authenticated, but GitHub does not provide shell access.
```

---

## 三、日常 Git 工作流

### 首次克隆

```bash
cd ~/projects
git clone git@github.com:用户名/仓库名.git
```

**必须用 `git@github.com:` 开头的 SSH 地址**,不是网页上的 `https://`。端口 443 的事由 config 自动处理,不用在地址里写。

### 日常循环

```bash
cd ~/projects/仓库名

git pull                    # 先拉最新,避免冲突
nano 文件名                  # 改代码
git status                  # 看改了啥
git add -A                  # 暂存所有改动
git commit -m "修改说明"      # 提交
git push                    # 推上去
```

**用别名可以压缩成三连**:

```bash
ga          # git add -A
gc "改了啥"  # git commit -m
gp          # git push
```

### nano 操作要点

| 操作 | 按键 |
|---|---|
| 保存 | `CTRL+O` → 回车 |
| 退出 | `CTRL+X` |
| 搜索 | `CTRL+W` |
| 有改动时退出 | 会问 `Save modified buffer?` 按 `Y` 保存 |

> Termux 键盘上方那排 `CTRL` `ESC` `TAB` 就是干这个的:**点一下 CTRL,再按字母**。

### 开分支干活

```bash
git checkout -b feature/新功能
# 改代码、提交
git push -u origin feature/新功能
```

然后去 GitHub 上提 Pull Request。

### 撤销操作

```bash
git restore 文件名            # 丢弃单个文件的改动(未暂存)
git restore --staged 文件名   # 取消暂存
git reset --hard HEAD~1      # 撤销最近一次提交(本地)
```

---

## 四、命令速查表

### 你已有的别名

| 别名 | 完整命令 |
|---|---|
| `proj` | `cd ~/projects` |
| `gs` | `git status` |
| `gd` | `git diff` |
| `gl` | `git log --oneline --graph --decorate -15` |
| `ga` | `git add -A` |
| `gc` | `git commit -m` |
| `gp` | `git push` |
| `gpl` | `git pull` |
| `ll` | `ls -alh` |

### pkg 包管理

```bash
pkg update && pkg upgrade   # 更新(建议经常跑)
pkg install 包名             # 安装
pkg uninstall 包名           # 卸载
pkg search 关键词            # 搜索
pkg list-installed          # 已装列表
pkg clean                   # 清缓存(占空间时用)
termux-change-repo          # 交互式换源
```

### 常用安装

```bash
pkg install python          # Python
pkg install nodejs-lts      # Node.js
pkg install sqlite          # 数据库
pkg install ffmpeg          # 音视频
pkg install aria2           # 下载器
pkg install tmux            # 会话保持(切 App 不丢)
pkg install termux-api      # 调手机硬件
```

### 实用技巧

```bash
termux-wake-lock            # 防后台被杀
termux-wake-unlock          # 释放
termux-setup-storage        # 开存储权限
ls ~/storage                # 访问手机文件
termux-info                 # 看环境信息
```

> **tmux 很值得装**:手机切到微信再回来,普通会话可能就断了。`tmux new -s work` 开会话,`CTRL+B` 再按 `D` 脱离,`tmux attach -t work` 回来。

### 存储路径

| 路径 | 是什么 |
|---|---|
| `~` | Termux 私有目录(快,推荐放仓库) |
| `~/projects` | 你的项目目录 |
| `~/storage/shared` | 手机内部存储根目录 |
| `~/storage/downloads` | 下载文件夹 |

> 仓库建议放 `~/projects`(快),共享存储只用来和别的 App 交换文件。
> Android 11+ 下 `Android/data/` 目录 Termux 进不去,这是系统限制。

---

## 五、踩坑记录(重要)

按遇到顺序记录,附原因和解法。

### 坑 1:APK 下载慢

**现象**:从 f-droid.org 或 GitHub 直连下载龟速。

**解法**:用 GitHub 代理加速前缀:

```
https://ghproxy.net/https://github.com/...
```

备选:`gh-proxy.com`、`ghfast.top`。

**自查**:文件应约 33.5 MB;过小或后缀变成 `.html` 说明被拦了。

### 坑 2:脚本粘贴被截断

**现象**:报错

```
setup.sh: line 178: unexpected EOF while looking for matching `"'
```

**原因**:脚本有 3 个 heredoc 块(`cat > ... <<EOF`)和 4 行超长命令(最长 101 字符)。手机终端粘贴长文本时容易丢内容,**heredoc 一旦断裂,后面的括号配对全乱**。

**解法**:拆成 3 段,每段独立执行,去掉所有 heredoc。

**通用经验**:**超过 200 行的脚本别往手机终端粘**,改用 `curl` 下载,或拆段。

### 坑 3:Google Play 版 Termux

**现象**:`pkg update` 直接失败。

**原因**:Play 版自 2020 年起停止更新,指向的软件源已失效。

**解法**:卸载,改从 F-Droid 或 GitHub Releases 安装。

### 坑 4:插件和本体签名不一致

**现象**:装 Termux:API 时提示「应用未安装」。

**原因**:Termux 本体和插件的签名必须同源。

**解法**:**GitHub 版 Termux 就配 GitHub 版 Termux:API**,不要混用 F-Droid 版。切换来源需要先卸载全部相关 App。

### 坑 5:存储权限拿不到

**现象**:`termux-setup-storage` 不弹框,或报 `inaccessible or not found`(Android 13+ 常见)。

**解法**(三步):

1. 设置 → 应用 → 应用管理 → **特殊应用权限** → **所有文件访问权限** → 找到 Termux 打开
2. 回到 Termux 执行 `pkg install termux-am`
3. 重开 Termux,再跑 `termux-setup-storage`

### 坑 6:SSH 22 端口被封(最容易卡住的一个)

**现象**:

```
ssh: connect to host github.com port 22: Connection closed by 20.205.243.166
```

**原因**:**不是配置错误**,是网络环境屏蔽了 22 端口。

**解法**:建 `~/.ssh/config` 走 443:

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

改完首次连接会问 host key 确认,输完整 `yes` 即可。

### 坑 7:误删密钥后 part3.sh 找不到

**现象**:

```
$ rm ~/.ssh/id_ed25519 ~/.ssh/id_ed25519.pub
$ bash part3.sh
bash: part3.sh: No such file or directory
```

**原因**:当前在 `~/projects`,而 part3.sh 在家目录。

**解法**:用**绝对路径**:

```bash
bash ~/part3.sh
```

**通用经验**:脚本放 `~/` 下,从任何地方都用 `bash ~/脚本名` 调用,不会找不到。

### 坑 8:cd 打错导致 "not a git repository"

**现象**:

```
fatal: not a git repository (or any parent up to mount point /)
Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).
```

**原因**:目录打错了(比如打成 `d ~/projects/...`),人还在上级目录,那里不是仓库。

**判据**:`ls -a` 看有没有 **`.git`** 目录,有它才是仓库。

### 坑 9:git 身份漏填

**现象**:脚本里名字那步误按回车,配置为空。

**解法**:随时补,和脚本无关:

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
```

**验证**:

```bash
git config --global user.name
git config --global user.email
```

> 邮箱建议用 GitHub 账号绑定的;不想暴露真实邮箱可用 `github.com/settings/emails` 里的 noreply 地址。
> 注意:**这个邮箱只用于 commit 署名,和 SSH 认证无关**。

---

## 六、进阶方向

### 1. 用 tmux 保命

手机切 App 时会话可能被回收。tmux 让会话在后台活着:

```bash
pkg install tmux
tmux new -s work      # 建会话
# CTRL+B 然后按 D  → 脱离
tmux attach -t work   # 回来
tmux ls               # 列出所有会话
```

### 2. 把常用脚本变成命令

```bash
mkdir -p ~/bin
# 把脚本放进去并 chmod +x
echo 'export PATH="$HOME/bin:$PATH"' >> ~/.bashrc
```

以后 `你的脚本名` 就能直接调用,像原生命令一样。

### 3. 定时任务

配合 `termux-job-scheduler` 可以定时跑脚本。注意需要 **Termux:Boot** 插件 + 关闭电池优化,否则定时任务会被系统杀掉。

### 4. 外接键盘

**这是提升体验最明显的一件事**。蓝牙键盘让手机接近笔记本的手感,配合 Termux 的快捷键行,写代码不再痛苦。

### 5. 多设备协作

- **手机写,电脑跑**:手机上 `nano` 改,电脑上 `git pull` 后运行调试
- **SSH 反连**:电脑上 `ssh` 连手机(需先 `sshd`),用电脑键盘编辑手机上的文件
- **Codespaces 兜底**:需要完整 VS Code 环境时,用 GitHub Codespaces 网页版(免费额度 120 core-hours/月)

### 6. 遇到搞不定的项目

Termux 不是标准 GNU/Linux,有些项目依赖 glibc/systemd 会刚不动。**别硬刚**,换这些:

- PRoot 装个完整发行版
- 自己的 VPS
- GitHub Actions / CI
- 电脑

---

## 常用参考

| 资源 | 地址 |
|---|---|
| GitHub SSH 密钥管理 | `github.com/settings/keys` |
| GitHub 邮箱设置 | `github.com/settings/emails` |
| Termux 官方 Releases | `github.com/termux/termux-app/releases` |
| F-Droid | `f-droid.org` |
| 清华 Termux 镜像 | `mirrors.tuna.tsinghua.edu.cn/help/termux/` |
| Termux Wiki | `wiki.termux.com` |

---

*本文档记录了一次完整的 Termux 从零配置过程,含所有实际踩到的坑。换手机重来时照着走即可。*
