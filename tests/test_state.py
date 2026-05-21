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
    settings.active_project_name = "workspace"
    settings.active_project_cwd = tmp_path / "workspace"
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
    assert loaded.active_project_name == "workspace"
    assert loaded.active_project_cwd == (tmp_path / "workspace").resolve()


def test_workspace_sessions_round_trip(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    assert state.get_session_id("main") is None
    state.set_session_id("main", "session-1")
    assert state.get_session_id("main") == "session-1"


def test_project_and_thread_preferences_round_trip(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    project_path = tmp_path / "project"

    state.record_project_usage(7, project_path, "project")
    state.set_project_favorite(7, project_path, "project", True)
    state.set_thread_favorite(
        7,
        session_id="session-1",
        thread_name="新媒体矩阵",
        thread_cwd=project_path,
        favorite=True,
    )

    preferences = state.project_preferences(7)
    assert preferences[str(project_path.resolve())]["favorite"] == 1
    assert "session-1" in state.thread_favorites(7)

    state.set_thread_favorite(
        7,
        session_id="session-1",
        thread_name="新媒体矩阵",
        thread_cwd=project_path,
        favorite=False,
    )
    assert "session-1" not in state.thread_favorites(7)


def test_pending_voice_round_trip(tmp_path) -> None:
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    local_path = tmp_path / "voice.ogg"

    state.set_pending_voice(7, "pending-1", "你好", local_path)
    pending = state.get_pending_voice(7, "pending-1")

    assert pending["transcript"] == "你好"
    assert pending["local_path"] == str(local_path)
    state.clear_pending_voice(7)
    assert state.get_pending_voice(7) is None
