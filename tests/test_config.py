from pathlib import Path

from telegram_codex_bridge.config import BridgeConfig, WorkspaceConfig, load_config, write_config


def test_write_and_load_config_round_trip(tmp_path: Path) -> None:
    config = BridgeConfig(
        bot_token="123:abc",
        workspaces=[WorkspaceConfig(name="main", path=tmp_path)],
        allowed_user_ids=[1],
        allowed_chat_ids=[-2],
        default_model="gpt-5.4",
        default_reasoning_effort="medium",
        default_plan_mode=True,
        quick_models=["gpt-5.4", "gpt-5.4-mini"],
        runtime_dir=tmp_path / "runtime",
    )
    path = write_config(config, tmp_path / "runtime" / "config.toml")
    loaded = load_config(path)

    assert loaded.bot_token == "123:abc"
    assert loaded.allowed_user_ids == [1]
    assert loaded.allowed_chat_ids == [-2]
    assert loaded.default_plan_mode is True
    assert [workspace.name for workspace in loaded.workspaces] == ["main"]
    assert loaded.workspaces[0].path == tmp_path.resolve()
