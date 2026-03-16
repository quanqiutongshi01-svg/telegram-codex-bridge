# Telegram Codex Bridge / Telegram Codex 桥接器

[English](README.md) | [简体中文](README.zh-CN.md)

一个可安装的 macOS Telegram 桥接器，让本地 Codex 会话可以持续通过 Telegram 访问。
An installable macOS bridge that keeps a local Codex session reachable through Telegram.

它支持：

- Telegram 文本任务
- 图片和文档输入
- 图片与文件回传到 Telegram
- 本地 Whisper 语音转写
- 仅对 Telegram 生效的模型和推理精度覆盖
- 带按钮的 Telegram 控制面板
- 在新的 Telegram 线程和已有 Codex Desktop 线程之间切换
- 基于 macOS `launchd` 的后台常驻管理

这个仓库同时也是一个 Codex skill，可以挂载到 `$CODEX_HOME/skills` 中使用。

## 视觉方案

仓库里带了 3 版原创图标概念稿，位于 [`docs/assets/`](docs/assets)：

- `logo-option-a.svg`：指挥环徽章
- `logo-option-b.svg`：六边形护盾
- `logo-option-c.svg`：战术横版标识

## 环境要求

- macOS
- Python 3.11+
- `codex`
- `ffmpeg`
- 从 `@BotFather` 获取的 Telegram Bot Token

## 安装

运行安装脚本，并传入 bot token、允许访问的 Telegram 用户或群聊，以及工作区路径：

```bash
python3 scripts/install.py \
  --bot-token "<telegram-bot-token>" \
  --allow-user "<your-telegram-user-id>" \
  --workspace main=/Users/your-name/projects/telegram-codex-bridge
```

常用可选参数：

- `--allow-chat <group-chat-id>`：允许某个 Telegram 群使用
- `--default-model <model>`：设置默认模型
- `--default-effort <minimal|low|medium|high>`：设置默认推理精度
- `--quick-model <model>`：重复传入可增加快捷模型选项

## 服务控制

桥接器会以 `launchd` 后台服务运行，可以使用：

```bash
python3 scripts/service_control.py start
python3 scripts/service_control.py stop
python3 scripts/service_control.py restart
python3 scripts/service_control.py status
```

## Telegram 命令

- `/menu`：打开控制面板
- `/status`：查看当前状态
- `/doctor`：运行快速自检
- `/threads`：查看最近的本地 Codex 线程
- `/thread <name|id|clear>`：切换到已有线程，或清空当前线程绑定
- `/workspaces`：查看工作区
- `/workspace <name>`：切换工作区
- `/model <id>`：设置仅 Telegram 生效的模型
- `/effort <minimal|low|medium|high>`：设置仅 Telegram 生效的推理精度
- `/plan <on|off>`：切换仅 Telegram 生效的计划模式
- `/new`：新建一个 Telegram 线程
- `/stop`：停止当前执行任务
- `/help`：查看帮助

## 作为 Codex Skill 使用

如果你想把这个仓库暴露为 Codex skill，可以执行：

```bash
ln -sfn /path/to/telegram-codex-bridge ~/.codex/skills/telegram-codex-bridge
```

然后重启 Codex。

## 安全说明

- 不要提交 `~/.codex/telegram-bridge/config.toml`
- 不要提交 bot token、chat id 或运行时数据库
- 如果 bot token 曾被公开暴露，请到 `@BotFather` 撤销并重新生成
- 开源前请查看 [`docs/OPEN_SOURCE_RELEASE.md`](docs/OPEN_SOURCE_RELEASE.md)

## 开发

运行测试：

```bash
python3 -m pytest
```

运行快速编译检查：

```bash
python3 -m compileall src scripts
```
