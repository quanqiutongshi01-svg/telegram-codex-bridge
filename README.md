# Telegram Codex Bridge / Telegram Codex 桥接器

[English](README.md) | [简体中文](README.zh-CN.md)

[![CI](https://github.com/quanqiutongshi01-svg/telegram-codex-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/quanqiutongshi01-svg/telegram-codex-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-E5534B.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-1F6FEB.svg)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-macOS-111827.svg)](README.md)

An installable macOS bridge that keeps a local Codex session reachable through Telegram.  
一个可安装的 macOS 桥接器，让本地 Codex 会话可以持续通过 Telegram 访问。

## Highlights

- Telegram text tasks
- Image and document input
- File and image return back to Telegram
- Local Whisper voice transcription
- Telegram-only model and reasoning overrides
- Telegram control panel with buttons
- Project-aware switching between new Telegram conversations and existing Codex Desktop conversations
- Project/conversation search, favorites, and recent-project ordering
- Project overview cards after switching, with recent conversations, recent Telegram task progress, and Git status
- Task cards with details, cancel, rerun, risk labels, and recent task history
- Optional voice transcription confirmation before sending to Codex
- Telegram-accessible recent error logs
- `launchd` service management on macOS

This repository is also structured as a Codex skill, so it can be linked into `$CODEX_HOME/skills`.

## Quick Start

1. Create a Telegram bot with `@BotFather`
2. Install the bridge locally with `scripts/install.py`
3. Open Telegram and use `/menu` to control Codex

```bash
python3 scripts/install.py \
  --bot-token "<telegram-bot-token>" \
  --allow-user "<your-telegram-user-id>" \
  --workspace main=/Users/your-name/projects/telegram-codex-bridge
```

## How It Works

```mermaid
flowchart LR
    A["Telegram Chat"] --> B["Telegram Codex Bridge"]
    B --> C["Local Codex CLI"]
    B --> D["Local Whisper"]
    B --> E["launchd Service"]
    C --> F["Workspace Files"]
    D --> B
    F --> B
    B --> A
```

## Official Icon

The project now uses the following official icon:

![Telegram Codex Bridge icon](assets/brand-icon-large.png)

The Codex skill metadata also points to this icon through [`agents/openai.yaml`](agents/openai.yaml).

## Requirements

- macOS
- Python 3.11+
- `codex`
- `ffmpeg`
- A Telegram bot token from `@BotFather`

## Install

Useful optional flags:

- `--allow-chat <group-chat-id>`: allow a Telegram group
- `--default-model <model>`
- `--default-effort <low|medium|high|xhigh>`
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
- `/logs`: show recent bridge logs
- `/tasks`: show recent task history
- `/projects`: list local Codex projects discovered from desktop session history
- `/project <name|path|clear>`: switch project or clear project selection
- `/threads`: list conversations under the current project
- `/thread <name|id|clear>`: switch to a saved conversation or clear selection
- `/tasks project`: show recent task progress for the current project only
- `/search <query>`: search projects, paths, and conversation titles
- `/favorite`: favorite or unfavorite the current project/conversation
- `/workspaces`: compatibility alias for project selection
- `/workspace <name>`: switch to a registered workspace as the current project
- `/model <id>`: set the Telegram-only model from the local Codex model catalog
- `/effort <level>`: set a reasoning effort supported by the selected model
- `/plan <on|off>`: toggle Telegram-only plan mode
- `/voiceconfirm <on|off>`: require confirmation before voice transcripts are submitted
- `/new`: start a fresh Telegram conversation in the current project
- `/stop`: stop the current running task
- `/help`: show command help

## Release Assets

Build a release zip and optional GitHub release with:

```bash
python3 scripts/release.py v0.1.1
python3 scripts/release.py v0.1.1 --publish
```

## Repository Layout

- `src/telegram_codex_bridge/`: bridge runtime
- `scripts/`: install, uninstall, doctor, service control
- `tests/`: pytest suite
- `references/`: configuration and operator reference
- `SKILL.md`: Codex skill entry
- `agents/openai.yaml`: skill UI metadata

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

## FAQ

### Why does the bot not reply in a group?

Make sure the bot is allowed in that group, privacy settings are configured correctly, and the group chat id is in the bridge allowlist.

### Why do Telegram model changes not affect desktop Codex?

That is intentional. Telegram-only model and reasoning overrides are isolated from the global Codex config.

### Can I continue an existing desktop thread from Telegram?

Yes. Use `/projects` or the **Project** button first, then use `/threads` or the **Conversation** button to attach Telegram to a saved desktop conversation in that project.

### What should I do if voice transcription fails?

Check `ffmpeg`, Whisper dependencies, and bridge logs under `~/.codex/telegram-bridge/logs/`.

## Development

Run tests:

```bash
python3 -m pytest
```

Run a quick compile check:

```bash
python3 -m compileall src scripts
```
