# Telegram Codex Bridge / Telegram Codex 桥接器

[English](README.md) | [简体中文](README.zh-CN.md)

An installable macOS bridge that keeps a local Codex session reachable through Telegram.
一个可安装的 macOS 桥接器，让本地 Codex 会话可以持续通过 Telegram 访问。

It supports:
它支持：

- Telegram text tasks
- Image and document input
- File and image return back to Telegram
- Local Whisper voice transcription
- Telegram-only model and reasoning overrides
- Telegram control panel with buttons
- Switching between new Telegram threads and existing Codex Desktop threads
- `launchd` service management on macOS

This repository is also structured as a Codex skill, so it can be linked into `$CODEX_HOME/skills`.

## Visual Direction

Three original icon concepts live in [`docs/assets/`](docs/assets):

- `logo-option-a.svg`: command-ring emblem
- `logo-option-b.svg`: hex shield mark
- `logo-option-c.svg`: tactical wordmark badge

## Requirements

- macOS
- Python 3.11+
- `codex`
- `ffmpeg`
- A Telegram bot token from `@BotFather`

## Install

Run the installer and provide your bot token, allowed chat ids, and workspace path:

```bash
python3 scripts/install.py \
  --bot-token "<telegram-bot-token>" \
  --allow-user "<your-telegram-user-id>" \
  --workspace main=/Users/your-name/projects/telegram-codex-bridge
```

Useful optional flags:

- `--allow-chat <group-chat-id>`: allow a Telegram group
- `--default-model <model>`
- `--default-effort <minimal|low|medium|high>`
- `--quick-model <model>`: repeat to add more model shortcuts

## Service Control

The bridge runs as a `launchd` agent. Use:

```bash
python3 scripts/service_control.py start
python3 scripts/service_control.py stop
python3 scripts/service_control.py restart
python3 scripts/service_control.py status
```

## Telegram Commands

- `/menu`: open the control panel
- `/status`: show current status
- `/doctor`: run a quick self-check
- `/threads`: list recent local Codex threads
- `/thread <name|id|clear>`: switch to a saved thread or clear selection
- `/workspaces`: list workspaces
- `/workspace <name>`: switch workspace
- `/model <id>`: set the Telegram-only model
- `/effort <minimal|low|medium|high>`: set Telegram-only reasoning effort
- `/plan <on|off>`: toggle Telegram-only plan mode
- `/new`: start a fresh Telegram thread
- `/stop`: stop the current running task
- `/help`: show command help

## Codex Skill Use

To expose this repository as a Codex skill:

```bash
ln -sfn /path/to/telegram-codex-bridge ~/.codex/skills/telegram-codex-bridge
```

Then restart Codex.

## Security Notes

- Do not commit `~/.codex/telegram-bridge/config.toml`
- Do not commit bot tokens, chat ids, or runtime databases
- Revoke and replace any Telegram bot token that has ever been shared publicly
- Review [docs/OPEN_SOURCE_RELEASE.md](docs/OPEN_SOURCE_RELEASE.md) before publishing

## Development

Run tests:

```bash
python3 -m pytest
```

Run a quick compile check:

```bash
python3 -m compileall src scripts
```
