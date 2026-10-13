# Getting Started

这份文档只解决一件事：把 WorkBuddy Toolkit 安装好，并完成第一次可验证的使用。

> 如果你只是想知道项目解决什么问题，请先看仓库根目录的 [README](../README.md)。

## 1. 系统要求

- macOS、Linux 或 Windows 10/11
- Python 3.8+
- 已安装并至少登录过一次 WorkBuddy 客户端
- 不要求额外的 Python 第三方依赖

## 2. 推荐安装方式

### macOS / Linux / Windows 通用

```bash
git clone https://github.com/FlapPearLabs/workbuddy-toolkit.git ~/.workbuddy/toolkit
cd ~/.workbuddy/toolkit
python3 install.py
```

Windows 如果命令是 `python`：

```powershell
git clone https://github.com/FlapPearLabs/workbuddy-toolkit.git $HOME\.workbuddy\toolkit
cd $HOME\.workbuddy\toolkit
python install.py
```

### 系统原生安装脚本

macOS / Linux：

```bash
cd ~/.workbuddy/toolkit
./install.sh
```

Windows PowerShell：

```powershell
cd $HOME\.workbuddy\toolkit
.\install.ps1
```

## 3. 第一次初始化

安装完成后，先运行：

```bash
workbuddy init
workbuddy doctor
workbuddy status
```

这三步分别用于：

1. 初始化 Toolkit 需要的本地结构与工作区共享机制。
2. 检查当前 WorkBuddy 版本、凭据格式和本地运行时兼容性。
3. 查看当前账号与已保存 Profile 的状态。

如果 `doctor` 报告不兼容，不要继续强行切换账号。先按诊断结果处理。

## 4. 添加和切换账号

添加一个新账号：

```bash
wb-login
```

查看账号：

```bash
wb-list
```

切换账号：

```bash
wb-switch
```

Toolkit 会在切换前同步当前登录态，降低旧 Profile 覆盖新 Token 的风险。

## 5. 自动签到与账号巡检

手动运行一次签到：

```bash
wb-checkin
```

检查凭据健康度：

```bash
wb-audit
```

安装程序可按系统配置原生后台调度：

- macOS：`launchd`
- Linux：`systemd --user`，必要时降级到 `cron`
- Windows：任务计划程序

如果你不希望后台自动运行，可以关闭系统调度，保留手动命令。

## 6. 让 AI Coding Agent 帮你安装

如果你使用 Codex、Claude Code、Cursor、Hermes、OpenCode 或其他能执行终端命令的 Agent，可以把下面的任务交给它：

```text
请帮我安装并验证 WorkBuddy Toolkit：

1. 克隆 https://github.com/FlapPearLabs/workbuddy-toolkit.git 到 ~/.workbuddy/toolkit。
2. 根据当前操作系统运行 install.py；如果 Python 命令不同，请自行适配。
3. 依次运行 workbuddy init、workbuddy doctor、workbuddy status。
4. 不要修改 WorkBuddy 应用包本体。
5. 如果 doctor 报告不兼容，停止后续写操作并告诉我具体诊断结果。
6. 如果环境支持任务调度，再询问我是否启用每日 wb-checkin；不要擅自创建我没有确认的后台任务。
7. 最后汇报：安装位置、doctor 结果、当前账号状态、后台调度是否启用。
```

这个版本刻意比旧 README 中的长 Prompt 更短。Agent 应以仓库当前实现和 `workbuddy doctor` 的实际输出为准，而不是依赖一段长期可能过期的安装说明。

## 7. 常用命令

```text
wb-help       查看命令帮助
wb-login      添加账号
wb-list       查看账号列表
wb-status     查看当前状态
wb-switch     切换账号
wb-audit      检查账号健康度
wb-checkin    签到与每日任务
wb-usage      查看积分消耗与流速
wb-chat       从终端调用模型
wb-router     管理容灾路由
wb-models     查看模型与倍率信息
wb-doctor     兼容性与故障诊断
```

完整说明见 [Command Reference](command-reference.md)。

## 8. 安全回退

需要退出 Toolkit 时，可以运行：

```bash
python3 uninstall.py
```

或使用对应平台的卸载脚本。卸载前如果你依赖自动签到或后台健康巡检，请先确认这些任务不再需要。

## 下一步

- [Feature Reference](feature-reference.md) — 完整能力索引
- [Command Reference](command-reference.md) — 命令和使用场景
- [Architecture](architecture.md) — 系统边界与设计原则
- [Engineering Notes](engineering-notes.md) — 为什么项目会做成现在这样
- [Incident Reports](incidents/README.md) — 真实故障、证据与修复
