from pathlib import Path

from telegram_codex_bridge.codex import TaskInput, build_command, build_prompt


def test_build_command_includes_telegram_only_overrides(tmp_path: Path) -> None:
    task = TaskInput(
        prompt="Fix the bug",
        workspace_name="main",
        workspace_path=tmp_path,
        chat_id=1,
        model="gpt-5.4",
        reasoning_effort="low",
        plan_mode=True,
        image_paths=[tmp_path / "image.png"],
        file_paths=[tmp_path / "notes.txt"],
    )
    command = build_command("codex", task, session_id="thread-123")

    assert command[:3] == ["codex", "exec", "resume"]
    assert '-c' in command
    assert 'model_reasoning_effort="low"' in command
    assert "-m" in command
    assert "thread-123" in command
    assert str(tmp_path / "image.png") in command


def test_resume_command_omits_workspace_only_flags(tmp_path: Path) -> None:
    task = TaskInput(
        prompt="Continue",
        workspace_name="main",
        workspace_path=tmp_path,
        chat_id=1,
        model="gpt-5.4",
        reasoning_effort="medium",
        plan_mode=False,
    )
    command = build_command("codex", task, session_id="thread-123")

    assert command[:3] == ["codex", "exec", "resume"]
    assert "-C" not in command
    assert "--skip-git-repo-check" in command
    assert "thread-123" in command


def test_build_prompt_mentions_selected_thread_and_execution_root(tmp_path: Path) -> None:
    task = TaskInput(
        prompt="Continue the plan",
        workspace_name="main",
        workspace_path=tmp_path,
        chat_id=1,
        model="gpt-5.4",
        reasoning_effort="medium",
        plan_mode=False,
        thread_name="新媒体矩阵运行",
    )

    prompt = build_prompt(task)

    assert "Workspace profile: main" in prompt
    assert f"Execution root: {tmp_path}" in prompt
    assert "Selected Codex thread: 新媒体矩阵运行" in prompt
