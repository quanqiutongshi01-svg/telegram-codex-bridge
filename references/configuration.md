# Telegram Codex Bridge Reference

## Runtime layout

- `~/.codex/telegram-bridge/config.toml`: bridge-only configuration.
- `~/.codex/telegram-bridge/state.db`: chat settings, workspace sessions, media index, task history.
- `~/.codex/telegram-bridge/downloads/`: Telegram media cached for Codex input or operator download.
- `~/.codex/telegram-bridge/logs/`: service stdout/stderr logs.
- `~/Library/LaunchAgents/com.openai.codex.telegram-bridge.plist`: macOS launchd job.
- `scripts/service_control.py`: start, stop, restart, or inspect the installed launchd service.

## Config schema

```toml
bot_token = "123:abc"
codex_binary = "/Applications/Codex.app/Contents/Resources/codex"
ffmpeg_binary = "/opt/homebrew/bin/ffmpeg"
default_model = "gpt-5.4"
default_reasoning_effort = "high"
default_plan_mode = false
quick_models = ["gpt-5.4", "gpt-5.4-mini"]
allowed_user_ids = [123456789]
allowed_chat_ids = [-1001234567890]
log_level = "INFO"
polling_timeout = 30
whisper_model = "base"
whisper_language = "zh"

[[workspaces]]
name = "main"
path = "/Users/your-name/projects/telegram-codex-bridge"
```

## Telegram commands

- `/menu`: open the Telegram control panel.
- `/status`: current workspace, Telegram-only model/effort/plan mode, queue state.
- `/doctor`: quick bridge self-check.
- `/model [id]`: show or change the Telegram-only default model for this chat.
- `/effort [minimal|low|medium|high]`: show or change Telegram-only reasoning effort.
- `/plan [on|off]`: toggle bridge-managed planning mode for Telegram tasks.
- `/workspaces`: list registered workspaces.
- `/workspace [name]`: show or change the chat's active workspace.
- `/threads`: list recent local Codex threads from this Mac.
- `/thread [name|id|clear]`: attach Telegram to a desktop-created Codex thread or clear the current binding.
- `/new`: clear the active workspace's persisted Codex session.
- `/stop`: stop the current workspace task.
- `/help`: command summary.

## Media behavior

- Photos are downloaded and passed to Codex with `-i`.
- Voice notes are transcribed locally with Whisper after ffmpeg normalization.
- Generic documents are staged locally and mentioned in the prompt as local files.
- Videos currently return a placeholder note instead of full video understanding.
- Absolute file paths mentioned in Codex replies are inspected; existing files are sent back to Telegram as photos or documents.

## Service notes

- The bridge uses `codex exec --json` for new tasks and `codex exec resume --json` for follow-up messages.
- Each workspace has a single FIFO queue and one persisted session id.
- Telegram-only model and reasoning overrides are injected via CLI flags; global Codex defaults stay unchanged.
