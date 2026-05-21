# Telegram Codex Bridge Reference

## Runtime layout

- `~/.codex/telegram-bridge/config.toml`: bridge-only configuration.
- `~/.codex/telegram-bridge/state.db`: chat settings, workspace sessions, media index, task history, favorites, recent projects, pending voice confirmations.
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
- `/logs`: show recent service stdout/stderr snippets.
- `/tasks`: show recent task history for the current chat.
- `/tasks project`: show recent task history filtered to the current project path.
- `/model [id]`: show or change the Telegram-only default model for this chat. Button choices come from `codex debug models`.
- `/effort [level]`: show or change Telegram-only reasoning effort. Choices follow the selected model's supported levels.
- `/plan [on|off]`: toggle bridge-managed planning mode for Telegram tasks.
- `/voiceconfirm [on|off]`: require user confirmation before sending voice transcripts to Codex.
- `/projects`: list local Codex projects discovered from desktop session metadata, plus configured workspaces.
- `/project [name|path|clear]`: show, change, or clear the current project selection.
- Project switches return a project overview card with recent conversations, project-scoped Telegram task progress, and a lightweight Git status summary.
- `/threads`: list recent local Codex conversations for the current project.
- `/thread [name|id|clear]`: attach Telegram to a desktop-created Codex conversation or clear the current conversation binding.
- `/search [query]`: search project names, paths, and conversation titles.
- `/favorite`: favorite or unfavorite the current project/conversation.
- `/workspaces`: compatibility alias for project selection.
- `/workspace [name]`: switch a configured workspace as the current project.
- `/new`: clear the current conversation while keeping the selected project.
- `/stop`: stop the current project task.
- `/help`: command summary.

## Media behavior

- Photos are downloaded and passed to Codex with `-i`.
- Voice notes are transcribed locally with Whisper after ffmpeg normalization. Optional confirmation mode lets the user approve or cancel the transcript before execution.
- Generic documents are staged locally and mentioned in the prompt as local files.
- Videos currently return a placeholder note instead of full video understanding.
- Absolute file paths mentioned in Codex replies are inspected; existing files are sent back to Telegram as photos or documents.

## Service notes

- The bridge uses `codex exec --json` for new tasks and `codex exec resume --json` for follow-up messages.
- Each project execution path has a single FIFO queue. Selecting a project controls where new Telegram conversations start.
- Project favorites and recent-use order are chat-specific and stored only in the bridge state database.
- Task cards expose details, rerun, cancel, and a simple risk label. Dangerous Codex approval requests still require explicit Telegram approval.
- Telegram-only model and reasoning overrides are injected via CLI flags; global Codex defaults stay unchanged.
