# Termux 命令速查表

> 手机端快速查阅版 · 2026-09-23
> 按使用频率分类,常打星标的是**日常最常用**的

---

## 目录

- [0. 你的自定义别名(最常用)](#0-你的自定义别名最常用)
- [1. 环境与健康检查](#1-环境与健康检查)
- [2. 包管理 pkg](#2-包管理-pkg)
- [3. 文件与目录](#3-文件与目录)
- [4. 文本编辑器 nano](#4-文本编辑器-nano)
- [5. Git 完整命令](#5-git-完整命令)
- [6. SSH 与远程](#6-ssh-与远程)
- [7. 网络与下载](#7-网络与下载)
- [8. Python / Node](#8-python--node)
- [9. 进程与服务](#9-进程与服务)
- [10. Termux 专属命令](#10-termux-专属命令)
- [11. 快捷技巧与键盘](#11-快捷技巧与键盘)
- [12. 报错速查](#12-报错速查)

---

## 0. 你的自定义别名(最常用)

这些是你 `~/.bashrc` 里已经配好的,打完重开 Termux 生效。

| 别名 | 等于 | 用途 |
|---|---|---|
| ⭐ `proj` | `cd ~/projects` | 跳到项目目录 |
| ⭐ `gs` | `git status` | 看当前状态 |
| ⭐ `ga` | `git add -A` | 暂存所有改动 |
| ⭐ `gc "说明"` | `git commit -m "说明"` | 提交 |
| ⭐ `gp` | `git push` | 推送 |
| `gpl` | `git pull` | 拉取更新 |
| `gd` | `git diff` | 看改了什么 |
| `gl` | `git log --oneline --graph --decorate -15` | 看提交历史 |
| `ll` | `ls -alh` | 列文件(含隐藏、带大小) |

**日常三连**:

```bash
ga && gc "改了啥" && gp
```

> `&&` 的意思是"前一条成功才执行下一条",所以如果 `ga` 失败,后面不会瞎跑。

**想加新别名**:

```bash
nano ~/.bashrc          # 在文件末尾加一行
source ~/.bashrc        # 立即生效
```

---

## 1. 环境与健康检查

```bash
termux-info             # 系统+Termux 完整信息
pkg list-installed      # 已装的包
df -h                   # 磁盘剩余空间
df -h $PREFIX           # 只看 Termux 占用(它装在私有目录)
du -sh ~                # 家目录多大
uname -m                # 架构(arm64 等)
echo $PREFIX            # /data/data/com.termux/files/usr
echo $HOME              # /data/data/com.termux/files/home
whoami                  # 当前用户(Termux 里是 u0_aXXX)
```

**空间不够时**:

```bash
pkg clean               # 清 apt 缓存
du -sh ~/*               # 看哪个目录大
```

---

## 2. 包管理 pkg

`pkg` 是 `apt` 的封装,优先用 `pkg`。

```bash
pkg update              # 更新索引
pkg upgrade             # 升级已装的包
pkg update && pkg upgrade   # 常用组合
pkg install 包名         # 安装
pkg install -y 包名      # 安装(跳过确认)
pkg uninstall 包名       # 卸载
pkg search 关键词        # 搜包
pkg show 包名            # 看包详情
pkg clean               # 清缓存
pkg list-installed      # 已装列表
pkg list-all            # 所有可用包(很长)
```

**常用包一键装**:

```bash
pkg install git openssh nano termux-tools    # 基础四件套(你已有)
pkg install python                           # Python
pkg install nodejs-lts                       # Node.js
pkg install tmux                             # 会话保持 ⭐推荐
pkg install curl wget jq                     # 网络+JSON处理
pkg install ffmpeg                           # 音视频处理
pkg install sqlite                           # 数据库
pkg install aria2                            # 多线程下载器
pkg install unzip zip tar rsync              # 压缩/同步
pkg install termux-api                       # 调手机硬件
```

**换源**:

```bash
termux-change-repo          # 交互式选镜像(推荐)
# 或手动改:$PREFIX/etc/apt/sources.list
```

> ⚠️ **别装 Google Play 版 Termux**,`pkg` 会失败。

---

## 3. 文件与目录

```bash
pwd                     # 当前路径
ls                      # 列文件
ls -a                   # 含隐藏文件(看有没有 .git 用这个)
ls -alh                 # 详细列表(别名 ll)
cd 目录                 # 进目录
cd ..                   # 上一级
cd ~                    # 回家目录
cd -                    # 回上一个目录
mkdir 目录名             # 建目录
mkdir -p a/b/c          # 递归建目录
cp 源 目标               # 复制
cp -r 目录1 目录2        # 复制目录
mv 源 目标               # 移动/重命名
rm 文件                 # 删文件
rm -rf 目录             # 删目录 ⚠️不可恢复
touch 文件名             # 建空文件
cat 文件                # 看文件内容
head -20 文件           # 看前20行
tail -20 文件           # 看后20行
less 文件               # 分页看(q 退出)
```

**搜索**:

```bash
grep "关键词" 文件       # 在文件里找
grep -rn "关键词" .      # 递归搜索当前目录
find . -name "*.py"     # 按名字找文件
```

**手机存储交互**:

```bash
termux-setup-storage    # 开权限(首次)
ls ~/storage            # 看有哪些映射
cd ~/storage/shared     # 手机内部存储根目录
cd ~/storage/downloads  # 下载文件夹
cp 文件 ~/storage/downloads/    # 导出到下载
cp ~/storage/downloads/文件 .   # 从下载导入
```

**重定向**:

```bash
echo "内容" > 文件       # 写入(覆盖)
echo "内容" >> 文件      # 追加
命令 > 输出.txt          # 把输出存文件
命令 2>&1 | tee log.txt  # 同时显示并存档
```

---

## 4. 文本编辑器 nano

**最核心的两个操作**:

| 操作 | 按键 |
|---|---|
| ⭐ 保存 | `CTRL+O` → 回车 |
| ⭐ 退出 | `CTRL+X` |
| 搜索 | `CTRL+W` → 输关键词 → 回车 |
| 跳到行号 | `CTRL+_` → 输行号 |
| 撤销 | `ALT+U` |
| 复制整行 | `ALT+6` |
| 剪切整行 | `CTRL+K` |
| 粘贴 | `CTRL+U` |

> **Termux 键盘上方那排快捷键行**就是干这个的:点一下 `CTRL`,再按字母键。

**退出时被问 `Save modified buffer?`**:
- 按 `Y` = 保存
- 按 `N` = 不保存
- 按 `CTRL+C` = 取消(留在编辑器里)

**其他编辑方式**:

```bash
nano 文件               # 最推荐
# 或用外部 App(改同一份文件,不冲突):Acode、Markor
```

---

## 5. Git 完整命令

### 配置(一次性)

```bash
git config --global user.name "你的名字"
git config --global user.email "你的邮箱"
git config --global --list          # 查看所有配置
git config --global core.editor nano
git config --global init.defaultBranch main
```

**漏填了随时补**,和脚本无关。

### 克隆与初始化

```bash
git clone git@github.com:用户名/仓库.git   # SSH 方式 ⭐
git clone https://github.com/用户名/仓库.git  # HTTPS(需 Token)
git init                                # 把现有目录变成仓库
git remote add origin git@github.com:用户名/仓库.git
git remote -v                           # 看远程地址
```

### 日常循环

```bash
git status              # 看状态(别名 gs)
git pull                # 拉取(别名 gpl)
git add 文件名           # 暂存单个文件
git add -A              # 暂存全部(别名 ga)
git commit -m "说明"     # 提交(别名 gc)
git push                # 推送(别名 gp)
git push -u origin main # 首次推送并绑定
```

### 查看

```bash
git log --oneline --graph -15   # 最近15条(别名 gl)
git diff                        # 未暂存的改动
git diff --staged               # 已暂存的改动
git show 提交号                  # 看某次提交详情
git blame 文件                   # 看每行谁改的
```

### 撤销

```bash
git restore 文件                # 丢弃未暂存的改动
git restore --staged 文件       # 取消暂存(保留改动)
git reset --hard HEAD~1         # 撤销最近1次提交 ⚠️
git reset --soft HEAD~1         # 撤销提交但保留改动
git checkout 提交号 -- 文件      # 恢复到某版本
```

### 分支

```bash
git branch                      # 看本地分支
git branch -a                   # 看所有分支(含远程)
git checkout -b feature/xxx     # 新建并切换分支
git checkout main               # 切回 main
git merge feature/xxx           # 合并分支
git branch -d feature/xxx       # 删分支
```

### 常见情况处理

```bash
# push 被拒(远程有新提交)
git pull --rebase && git push

# 冲突了:手动改文件后
git add 冲突文件 && git commit

# 临时保存工作区
git stash                       # 存起来
git stash pop                   # 取回来

# 改错了刚提交的说明
git commit --amend -m "新说明"
```

---

## 6. SSH 与远程

```bash
ssh user@服务器                  # 连服务器
ssh -p 2222 user@服务器          # 指定端口
exit                            # 断开(回到 Termux)
```

**密钥**:

```bash
ssh-keygen -t ed25519 -C "备注"  # 生成密钥
cat ~/.ssh/id_ed25519.pub        # 看公钥(贴到 GitHub)
ls -la ~/.ssh/                   # 看密钥文件
chmod 600 ~/.ssh/id_ed25519      # 修正权限
```

**测试 GitHub 连接**:

```bash
ssh -T git@github.com
# 成功:Hi 用户名! You've successfully authenticated...
```

**配置文件** `~/.ssh/config`(走 443 端口,你已配):

```
Host github.com
  HostName ssh.github.com
  Port 443
  User git
```

**文件传输**:

```bash
scp 文件 user@服务器:/路径/              # 传上去
scp user@服务器:/路径/文件 .             # 拉下来
scp -r 目录 user@服务器:/路径/           # 传目录
rsync -av --progress ./本地/ user@服务器:/远程/   # 同步目录
```

**端口转发**:

```bash
ssh -L 8080:127.0.0.1:8080 user@服务器   # 服务器端口映射到本地
```

---

## 7. 网络与下载

```bash
ping 网址                       # 测连通
curl 网址                       # 发请求看内容
curl -O 文件地址                 # 下载文件
curl -L -o 文件名 地址           # 跟随跳转下载
curl -I 网址                    # 只看响应头
wget 文件地址                    # 下载
```

**GitHub 加速**(国内必用):

```bash
curl -L -o 文件 "https://ghproxy.net/https://github.com/..."
# 可换:gh-proxy.com、ghfast.top
```

**JSON 处理**(配合 `jq`):

```bash
pkg install jq
curl -s 接口地址 | jq .         # 格式化 JSON
curl -s 接口地址 | jq '.key'    # 取某个字段
```

**本机启动服务**:

```bash
python -m http.server 8080      # 起个静态网页服务器
# 浏览器打开 http://127.0.0.1:8080

ip addr show                    # 看本机 IP(手机连同一 Wi-Fi 时用)
```

> ⚠️ `--bind 0.0.0.0` 会让**同 Wi-Fi 的其他人**都能访问,公共网络别用。

---

## 8. Python / Node

**Python**:

```bash
pkg install python
python --version
python 脚本.py
python -i 脚本.py        # 跑完进交互模式

pip install 包名          # 装包
pip install -r requirements.txt
pip list                 # 已装包

python -m venv .venv     # 建虚拟环境
source .venv/bin/activate # 激活
deactivate               # 退出
```

**Node.js**:

```bash
pkg install nodejs-lts
node --version
node 脚本.js
npm install              # 装依赖
npm install 包名
npm run dev              # 跑开发服务器
npx 命令                 # 临时执行
```

> ⚠️ 有些 npm/pip 包依赖 glibc,在 Termux 上装不上。别硬刚,换服务器或电脑跑。

---

## 9. 进程与服务

```bash
ps aux                  # 看所有进程
ps aux | grep python    # 找特定进程
kill PID                # 结束进程(温和)
kill -9 PID             # 强制结束
top                     # 实时看资源占用(q 退出)
jobs                    # 看后台任务
```

**后台运行**:

```bash
命令 &                  # 后台跑
nohup 命令 &            # 关终端也不停
命令 > log.txt 2>&1 &   # 后台跑并存日志
```

**tmux 会话(强烈推荐)**:

```bash
pkg install tmux
tmux new -s work        # 新建会话
tmux ls                 # 列出会话
tmux attach -t work     # 回到会话
tmux kill-session -t work  # 删会话
```

会话内快捷键(先按 `CTRL+B` 松开,再按):

| 按键 | 作用 |
|---|---|
| `CTRL+B` 然后 `D` | 脱离(会话继续在后台跑) |
| `CTRL+B` 然后 `C` | 新建窗口 |
| `CTRL+B` 然后 `N` | 下一个窗口 |
| `CTRL+B` 然后 `%` | 左右分屏 |
| `CTRL+B` 然后 `方向键` | 切换分屏 |

> **手机切到微信再回来,普通会话可能被回收;tmux 里的不会丢。** 值得装。

---

## 10. Termux 专属命令

```bash
termux-setup-storage        # 申请存储权限(首次)
termux-wake-lock            # 防止被系统杀后台
termux-wake-unlock          # 释放
termux-change-repo          # 交互式换源
termux-info                 # 环境信息
termux-open 文件             # 用其他 App 打开文件
termux-reload-settings      # 重载配置
```

**需要 `termux-api` 包 + Termux:API App**(同签名来源):

```bash
termux-notification --title "标题" --content "内容"   # 发通知
termux-vibrate -d 200                                 # 震动200ms
termux-battery-status                                 # 读电量
termux-clipboard-set "文本"                            # 写剪贴板
termux-clipboard-get                                  # 读剪贴板
termux-tts-speak "要说的话"                            # 语音播报
termux-camera-photo 照片.jpg                           # 拍照
termux-location                                       # 获取位置
```

**定时任务**(需 Termux:Boot 插件 + 关电池优化):

```bash
termux-job-scheduler --help
```

---

## 11. 快捷技巧与键盘

### 键盘快捷键行

Termux 键盘上方那排:`ESC` `CTRL` `ALT` `←` `↑` `↓` `→` `TAB`

**用法**:点一下修饰键(它变亮),再按目标键。

| 想输入 | 怎么做 |
|---|---|
| Ctrl+C(中断) | 点 `CTRL` → 按 `c` |
| Ctrl+O(保存) | 点 `CTRL` → 按 `o` |
| Ctrl+X(退出) | 点 `CTRL` → 按 `x` |
| Tab 补全 | 直接点 `TAB` |

### 补全与历史

- **按 `TAB`**:自动补全路径/命令。打 `cd ~/pro` 后按 TAB,会自动补成 `~/projects/`
- **按 `↑` / `↓`**:翻找之前打过的命令
- **`CTRL+R`**:搜索历史命令
- **`!!`**:重复上一条命令
- **`history`**:看所有历史

### 组合命令

```bash
命令1 && 命令2      # 1成功才执行2
命令1 || 命令2      # 1失败才执行2
命令1 ; 命令2       # 都执行,不管前面结果
命令 > 文件         # 输出到文件
命令 | grep xxx     # 过滤输出
```

---

## 12. 报错速查

| 报错 | 原因 | 解法 |
|---|---|---|
| `command not found` | 没装这个包 | `pkg install 包名` |
| `Permission denied` (访问 /sdcard) | 没开存储权限 | 用 `~/storage/shared`,别用 `/sdcard` |
| `Permission denied (publickey)` | SSH 密钥没配好 | `ssh -T git@github.com` 测;公钥贴到 GitHub |
| `Connection closed by ... port 22` | 网络屏蔽 22 端口 | 配 `~/.ssh/config` 走 443 |
| `not a git repository` | 不在仓库目录里 | `ls -a` 看有没有 `.git`;`cd` 到正确目录 |
| `src refspec main does not match any` | 本地无 main 分支 | `git checkout -b main` |
| `Please tell me who you are` | 没配 git 身份 | `git config --global user.name/email` |
| `fatal: refusing to merge unrelated histories` | 两个不相关的历史 | 加 `--allow-unrelated-histories` |
| `! [rejected] ... (fetch first)` | 远程有新提交 | `git pull --rebase` 再 push |
| `detected dubious ownership` | 文件归属异常 | 按提示 `git config --global --add safe.directory 路径` |
| `No space left on device` | 空间不足 | `pkg clean`;`df -h` 看占用 |
| `Unable to locate package` | 源里没这个包 | `pkg update` 后重试;检查包名 |
| `Cannot open display` | 需要图形界面 | Termux 不支持 GUI,换命令行工具 |
| `error: cannot open lock file` | 有另一个 pkg 在跑 | 等它结束;别同时开多个安装 |
| `Host key verification failed` | known_hosts 脏了 | `ssh-keygen -R github.com` 重连 |
| `bash: xxx.sh: No such file` | 路径不对 | 用绝对路径 `bash ~/xxx.sh` |

### 万能排错思路

1. **看报错第一行** —— 通常直接说了原因
2. **`pkg update`** —— 很多问题源于索引过期
3. **换源** —— `termux-change-repo`,国内直连官方源经常失败
4. **确认在正确目录** —— `pwd` + `ls -a`
5. **检查权限** —— 脚本要 `chmod +x`,密钥要 `chmod 600`

---

## 附:最常用 20 条(打印卡片版)

```bash
proj                        # 进项目目录
cd ~/projects/仓库名         # 进某个仓库
ls -alh                     # 看文件
gs                          # git 状态
ga && gc "说明" && gp        # 提交三连
gpl                         # 拉取
gl                          # 看历史
nano 文件                    # 改文件
pkg install 包名             # 装软件
pkg update && pkg upgrade   # 更新系统
termux-change-repo          # 换源
termux-wake-lock            # 防杀后台
termux-setup-storage        # 开存储权限
ssh -T git@github.com       # 测 GitHub 连接
cat ~/.ssh/id_ed25519.pub   # 看公钥
df -h                       # 看空间
pkg clean                   # 清缓存
tmux new -s work            # 开会话
python -m http.server 8080  # 起网页服务
grep -rn "关键词" .          # 搜索
```

---

*配合《Termux 手机开发环境 · 完整手册》使用,那份讲原理和踩坑,这份讲命令。*
