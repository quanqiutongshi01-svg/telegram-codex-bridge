from telegram_codex_bridge.state import ChatSettings, StateStore


def test_state_store_persists_chat_settings(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    settings = state.get_chat_settings(
        42,
        default_workspace="main",
        default_model="gpt-5.4",
        default_effort="high",
        default_plan_mode=False,
    )
    assert settings.workspace_name == "main"

    settings.model = "gpt-5.4-mini"
    state.update_chat_settings(settings)
    loaded = state.get_chat_settings(
        42,
        default_workspace="ignored",
        default_model="ignored",
        default_effort="ignored",
        default_plan_mode=True,
    )
    assert loaded.model == "gpt-5.4-mini"
    assert loaded.active_session_id is None


def test_state_store_persists_active_thread_context(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    settings = state.get_chat_settings(
        7,
        default_workspace="main",
        default_model="gpt-5.4",
        default_effort="high",
        default_plan_mode=False,
    )
    settings.active_session_id = "session-7"
    settings.active_thread_name = "新媒体矩阵运行"
    settings.active_thread_cwd = tmp_path / "workspace"
    state.update_chat_settings(settings)

    loaded = state.get_chat_settings(
        7,
        default_workspace="ignored",
        default_model="ignored",
        default_effort="ignored",
        default_plan_mode=False,
    )
    assert loaded.active_session_id == "session-7"
    assert loaded.active_thread_name == "新媒体矩阵运行"
    assert loaded.active_thread_cwd == (tmp_path / "workspace").resolve()


def test_workspace_sessions_round_trip(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    assert state.get_session_id("main") is None
    state.set_session_id("main", "session-1")
    assert state.get_session_id("main") == "session-1"
