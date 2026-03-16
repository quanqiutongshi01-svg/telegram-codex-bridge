---
name: telegram-codex-bridge
description: Install, configure, diagnose, or remove a macOS Telegram bridge that keeps a local Codex session reachable through Telegram with text, images, file return, local Whisper voice transcription, per-Telegram model and reasoning overrides, launchd management, and workspace-aware session persistence.
---

# Telegram Codex Bridge

Use this skill when the user wants Codex reachable from Telegram, wants to install or repair the local bridge service, or needs to adjust Telegram-only runtime settings such as model, reasoning effort, workspaces, or plan mode.

## Workflow

1. Read [references/configuration.md](references/configuration.md) if you need the config schema, launchd layout, or Telegram command surface.
2. Use `scripts/install.py` to install or update the runtime in `~/.codex/telegram-bridge/`.
3. Use `scripts/service_control.py start|stop|restart|status` when the user wants a simple on/off switch for the installed bridge.
4. Use `scripts/doctor.py` to validate `codex`, `ffmpeg`, launchd, config, and Telegram connectivity.
5. Use `scripts/uninstall.py` to stop and remove the service. Add `--purge` only if the user wants runtime state deleted.

## Guardrails

- Keep Telegram-only model, reasoning, and plan settings isolated from the global `~/.codex/config.toml`.
- Treat the bridge as single-user plus explicitly authorized chats; do not broaden access unless asked.
- Prefer editing the Python package in `src/telegram_codex_bridge/` and re-running `scripts/doctor.py` over hand-editing runtime files under `~/.codex/telegram-bridge/`.
- When changing the Telegram command surface or config schema, update [references/configuration.md](references/configuration.md) too.

## Resources

- `scripts/install.py`: install venv, write config, register launchd.
- `scripts/service_control.py`: start, stop, restart, or inspect the launchd service.
- `scripts/uninstall.py`: stop launchd and optionally purge runtime data.
- `scripts/doctor.py`: health checks for binaries, config, runtime, and Telegram.
- `src/telegram_codex_bridge/`: bridge implementation.
- `references/configuration.md`: config schema, commands, runtime layout.
