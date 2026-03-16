from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shlex
import tomllib


def _expand_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass(slots=True)
class WorkspaceConfig:
    name: str
    path: Path


@dataclass(slots=True)
class BridgeConfig:
    bot_token: str
    workspaces: list[WorkspaceConfig]
    allowed_user_ids: list[int] = field(default_factory=list)
    allowed_chat_ids: list[int] = field(default_factory=list)
    default_model: str = "gpt-5.4"
    default_reasoning_effort: str = "high"
    default_plan_mode: bool = False
    quick_models: list[str] = field(default_factory=lambda: ["gpt-5.4"])
    polling_timeout: int = 30
    log_level: str = "INFO"
    whisper_model: str = "base"
    whisper_language: str = "zh"
    codex_binary: str = "codex"
    ffmpeg_binary: str = "ffmpeg"
    runtime_dir: Path = field(default_factory=lambda: Path.home() / ".codex" / "telegram-bridge")

    @property
    def downloads_dir(self) -> Path:
        return self.runtime_dir / "downloads"

    @property
    def logs_dir(self) -> Path:
        return self.runtime_dir / "logs"

    @property
    def state_db_path(self) -> Path:
        return self.runtime_dir / "state.db"

    @property
    def config_path(self) -> Path:
        return self.runtime_dir / "config.toml"

    def ensure_workspace(self, name: str) -> WorkspaceConfig:
        for workspace in self.workspaces:
            if workspace.name == name:
                return workspace
        raise KeyError(f"Unknown workspace: {name}")

    @property
    def default_workspace(self) -> WorkspaceConfig:
        if not self.workspaces:
            raise ValueError("At least one workspace is required")
        return self.workspaces[0]


def load_config(path: str | Path) -> BridgeConfig:
    config_path = _expand_path(path)
    data = tomllib.loads(config_path.read_text())
    workspaces = [
        WorkspaceConfig(name=entry["name"], path=_expand_path(entry["path"]))
        for entry in data.get("workspaces", [])
    ]
    runtime_dir = _expand_path(data.get("runtime_dir", config_path.parent))
    return BridgeConfig(
        bot_token=data["bot_token"],
        workspaces=workspaces,
        allowed_user_ids=[int(value) for value in data.get("allowed_user_ids", [])],
        allowed_chat_ids=[int(value) for value in data.get("allowed_chat_ids", [])],
        default_model=data.get("default_model", "gpt-5.4"),
        default_reasoning_effort=data.get("default_reasoning_effort", "high"),
        default_plan_mode=bool(data.get("default_plan_mode", False)),
        quick_models=list(data.get("quick_models", ["gpt-5.4"])),
        polling_timeout=int(data.get("polling_timeout", 30)),
        log_level=data.get("log_level", "INFO"),
        whisper_model=data.get("whisper_model", "base"),
        whisper_language=data.get("whisper_language", "zh"),
        codex_binary=data.get("codex_binary", "codex"),
        ffmpeg_binary=data.get("ffmpeg_binary", "ffmpeg"),
        runtime_dir=runtime_dir,
    )


def write_config(config: BridgeConfig, path: str | Path) -> Path:
    config_path = _expand_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        f'bot_token = {_quote(config.bot_token)}',
        f'codex_binary = {_quote(config.codex_binary)}',
        f'ffmpeg_binary = {_quote(config.ffmpeg_binary)}',
        f'default_model = {_quote(config.default_model)}',
        f'default_reasoning_effort = {_quote(config.default_reasoning_effort)}',
        f"default_plan_mode = {'true' if config.default_plan_mode else 'false'}",
        f"quick_models = [{', '.join(_quote(model) for model in config.quick_models)}]",
        f"allowed_user_ids = [{', '.join(str(item) for item in config.allowed_user_ids)}]",
        f"allowed_chat_ids = [{', '.join(str(item) for item in config.allowed_chat_ids)}]",
        f'log_level = {_quote(config.log_level)}',
        f"polling_timeout = {config.polling_timeout}",
        f'whisper_model = {_quote(config.whisper_model)}',
        f'whisper_language = {_quote(config.whisper_language)}',
        f'runtime_dir = {_quote(str(config.runtime_dir))}',
        "",
    ]
    for workspace in config.workspaces:
        lines.extend(
            [
                "[[workspaces]]",
                f'name = {_quote(workspace.name)}',
                f'path = {_quote(str(workspace.path))}',
                "",
            ]
        )
    config_path.write_text("\n".join(lines))
    return config_path


def workspace_choices(value: list[str]) -> list[WorkspaceConfig]:
    workspaces: list[WorkspaceConfig] = []
    for item in value:
        name, raw_path = item.split("=", 1)
        workspaces.append(WorkspaceConfig(name=name.strip(), path=_expand_path(raw_path.strip())))
    return workspaces


def render_shell_exports(config_path: str | Path) -> str:
    config_path = _expand_path(config_path)
    return f"export TELEGRAM_CODEX_BRIDGE_CONFIG={shlex.quote(str(config_path))}"
