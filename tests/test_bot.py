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
