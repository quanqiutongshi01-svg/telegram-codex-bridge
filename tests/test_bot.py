import asyncio

import pytest

pytest.importorskip("telegram")

from telegram_codex_bridge.bot import QueuedTask, TelegramCodexBridge, WorkspaceWorker
from telegram_codex_bridge.codex import CodexModelOption, TaskInput
from telegram_codex_bridge.config import BridgeConfig, WorkspaceConfig
from telegram_codex_bridge.state import ChatSettings, StateStore


def make_bridge(tmp_path) -> TelegramCodexBridge:
    workspace_path = tmp_path / "workspace"
    workspace_path.mkdir()
    state = StateStore(tmp_path / "state.db")
    state.initialize()
    config = BridgeConfig(
        bot_token="token",
        workspaces=[WorkspaceConfig(name="main", path=workspace_path)],
        runtime_dir=tmp_path / "runtime",
    )
    return TelegramCodexBridge(config=config, state=state)


def make_job(tmp_path, source_description: str = "text") -> QueuedTask:
    workspace_path = (tmp_path / "workspace").resolve()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
    )
    task = TaskInput(
        prompt="你好",
        workspace_name="main",
        workspace_path=workspace_path,
        chat_id=7,
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
    )
    return QueuedTask(
        task=task,
        settings_snapshot=settings,
        worker_key=str(workspace_path),
        context_label="main",
        reply_to_message_id=12,
        source_description=source_description,
    )


def test_render_task_status_for_queued_job_shows_queue_position(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    queued_job = make_job(tmp_path)
    active_job = make_job(tmp_path, source_description="voice")
    worker = WorkspaceWorker(
        key="main",
        name="main",
        path=(tmp_path / "workspace").resolve(),
        queue=asyncio.Queue(),
    )
    worker.active_job = active_job
    asyncio.run(worker.queue.put(queued_job))

    text = bridge._render_task_status(queued_job, worker, "排队中")

    assert "任务状态：排队中" in text
    assert "任务ID：" in text
    assert "来源：文字" in text
    assert "风险等级：低" in text
    assert "前方任务：1" in text


def test_render_task_status_for_running_job_shows_progress_detail(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    job = make_job(tmp_path, source_description="approval")
    worker = WorkspaceWorker(
        key="main",
        name="main",
        path=(tmp_path / "workspace").resolve(),
        queue=asyncio.Queue(),
    )

    text = bridge._render_task_status(job, worker, "执行中", "pytest -q")

    assert "任务状态：执行中" in text
    assert "来源：授权重试" in text
    assert "进度：pytest -q" in text
    assert "前方任务" not in text


def test_detect_existing_paths_supports_spaces(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    output_dir = tmp_path / "video outputs"
    output_dir.mkdir()
    video_path = output_dir / "demo clip.mp4"
    video_path.write_bytes(b"fake video")

    paths = bridge._detect_existing_paths(f"已生成视频：{video_path}，可以回传。")

    assert paths == [video_path.resolve()]


def test_model_options_use_codex_catalog(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    bridge.codex.list_models = lambda: [
        CodexModelOption(
            slug="gpt-5.5",
            display_name="GPT-5.5",
            default_reasoning_effort="medium",
            supported_reasoning_efforts=("low", "medium", "high", "xhigh"),
        )
    ]

    options = bridge._model_options()

    assert [option.slug for option in options] == ["gpt-5.5"]
    assert bridge._effort_choices("gpt-5.5") == ("low", "medium", "high", "xhigh")


def test_normalize_effort_uses_selected_model_default(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    bridge.codex.list_models = lambda: [
        CodexModelOption(
            slug="gpt-custom",
            display_name="Custom",
            default_reasoning_effort="medium",
            supported_reasoning_efforts=("medium", "high"),
        )
    ]

    assert bridge._normalize_effort_for_model("gpt-custom", "xhigh") == "medium"


def test_selecting_project_changes_execution_target_without_global_config(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "other-project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
    )

    bridge._select_project(settings, project_path)
    target = bridge._resolve_target(settings)

    assert settings.workspace_name == "main"
    assert settings.active_project_name == "other-project"
    assert target.path == project_path
    assert target.context_label == "other-project"


def test_threads_keyboard_filters_by_selected_project(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="project",
        active_project_cwd=project_path,
    )
    bridge.session_catalog.list_threads = lambda **kwargs: [
        type(
            "Thread",
            (),
            {
                "session_id": "session-1",
                "display_name": "项目对话",
            },
        )()
    ]

    keyboard = bridge._threads_keyboard(settings)

    assert keyboard.inline_keyboard[0][0].text == "项目对话"


def test_project_favorites_sort_first(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    favorite_path = (tmp_path / "favorite-project").resolve()
    favorite_path.mkdir()
    bridge.session_catalog.list_projects = lambda **kwargs: [
        type(
            "Project",
            (),
            {
                "name": "favorite-project",
                "path": favorite_path,
                "thread_count": 1,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )()
    ]
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
    )
    bridge.state.set_project_favorite(7, favorite_path, "favorite-project", True)

    keyboard = bridge._projects_keyboard(settings)

    assert "★ favorite-project" in keyboard.inline_keyboard[0][0].text


def test_voice_confirm_keyboard_marks_current_choice(tmp_path) -> None:
    bridge = make_bridge(tmp_path)

    keyboard = bridge._voice_confirm_keyboard(True)

    assert keyboard.inline_keyboard[0][0].text == "* 开启"


def test_project_overview_includes_threads_and_project_tasks(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="project",
        active_project_cwd=project_path,
    )
    bridge.session_catalog.list_threads = lambda **kwargs: [
        type(
            "Thread",
            (),
            {
                "session_id": "session-1",
                "display_name": "项目对话",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )()
    ]
    bridge.session_catalog.recent_thread_messages = lambda session_id, limit=8: [
        type("Message", (), {"role": "user", "text": "可以，你做吧"})(),
        type("Message", (), {"role": "assistant", "text": "已经做了第一版 v0.1"})(),
    ]
    bridge.state.add_task(
        chat_id=7,
        workspace_name="project",
        prompt="继续完善项目",
        status="completed",
        dangerous=False,
        project_path=project_path,
    )

    text = bridge._project_overview_text(settings)

    assert "项目概览：project" in text
    assert "项目对话" in text
    assert "继续完善项目" in text


def test_status_text_includes_project_activity(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="project",
        active_project_cwd=project_path,
        active_session_id="session-1",
    )
    bridge.session_catalog.list_threads = lambda **kwargs: [
        type(
            "Thread",
            (),
            {
                "session_id": "session-1",
                "display_name": "规划双网切换应用",
                "cwd": project_path,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )()
    ]
    bridge.session_catalog.recent_thread_messages = lambda session_id, limit=8: [
        type("Message", (), {"role": "user", "text": "可以，你做吧"})(),
        type("Message", (), {"role": "assistant", "text": "已经做了第一版 v0.1"})(),
    ]
    bridge.state.add_task(
        chat_id=7,
        workspace_name="project",
        prompt="实现网卡状态检测",
        status="completed",
        dangerous=False,
        project_path=project_path,
    )

    text = bridge._status_text(settings)

    assert "当前项目：project" in text
    assert "最近对话：" in text
    assert "* 规划双网切换应用" in text
    assert "当前项目最近任务：" in text
    assert "实现网卡状态检测" in text


def test_thread_switch_text_includes_project_activity(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="project",
        active_project_cwd=project_path,
        active_session_id="session-1",
    )
    bridge.session_catalog.list_threads = lambda **kwargs: [
        type(
            "Thread",
            (),
            {
                "session_id": "session-1",
                "display_name": "规划双网切换应用",
                "cwd": project_path,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )()
    ]
    bridge.session_catalog.recent_thread_messages = lambda session_id, limit=8: [
        type("Message", (), {"role": "user", "text": "可以，你做吧"})(),
        type("Message", (), {"role": "assistant", "text": "已经做了第一版 v0.1"})(),
    ]
    bridge.state.add_task(
        chat_id=7,
        workspace_name="project",
        prompt="继续 UI 菜单",
        status="queued",
        dangerous=False,
        project_path=project_path,
    )

    text = bridge._thread_switch_text(settings, "规划双网切换应用", project_path)

    assert "对话续聊：规划双网切换应用" in text
    assert "当前对话最近内容：" in text
    assert "你：可以，你做吧" in text
    assert "Codex：已经做了第一版 v0.1" in text
    assert "最近对话：" in text
    assert "当前项目最近任务：" in text
    assert "继续 UI 菜单" in text


def test_project_overview_falls_back_to_legacy_task_rows(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "legacy-project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="legacy-project",
        active_project_cwd=project_path,
    )
    bridge.session_catalog.list_threads = lambda **kwargs: []
    bridge.state.add_task(
        chat_id=7,
        workspace_name="legacy-project",
        prompt="旧任务记录",
        status="completed",
        dangerous=False,
    )

    text = bridge._project_overview_text(settings)

    assert "旧记录按名称匹配" in text
    assert "旧任务记录" in text


def test_project_overview_does_not_use_generic_main_legacy_rows(tmp_path) -> None:
    bridge = make_bridge(tmp_path)
    project_path = (tmp_path / "project").resolve()
    project_path.mkdir()
    settings = ChatSettings(
        chat_id=7,
        workspace_name="main",
        model="gpt-5.4",
        reasoning_effort="high",
        plan_mode=False,
        active_project_name="project",
        active_project_cwd=project_path,
    )
    bridge.session_catalog.list_threads = lambda **kwargs: []
    bridge.state.add_task(
        chat_id=7,
        workspace_name="main",
        prompt="你好啊",
        status="completed",
        dangerous=False,
    )

    text = bridge._project_overview_text(settings)

    assert "你好啊" not in text
    assert "还没有能确认属于这个项目的任务记录" in text
