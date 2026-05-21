from pathlib import Path
import subprocess

from telegram_codex_bridge.codex import CodexRunner, TaskInput, build_command, build_prompt


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
    assert "--disable" in command
    assert "plugins" in command
    assert '-c' in command
    assert 'model_reasoning_effort="low"' in command
    assert 'web_search="disabled"' in command
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


def test_build_command_maps_legacy_minimal_effort_to_low(tmp_path: Path) -> None:
    task = TaskInput(
        prompt="Continue",
        workspace_name="main",
        workspace_path=tmp_path,
        chat_id=1,
        model="gpt-5.4",
        reasoning_effort="minimal",
        plan_mode=False,
    )

    command = build_command("codex", task, session_id=None)

    assert 'model_reasoning_effort="low"' in command
    assert 'model_reasoning_effort="minimal"' not in command


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


def test_list_models_parses_codex_debug_catalog(monkeypatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '{"models":['
                '{"slug":"gpt-5.5","display_name":"GPT-5.5","visibility":"list",'
                '"default_reasoning_level":"medium",'
                '"supported_reasoning_levels":[{"effort":"low"},{"effort":"xhigh"}]},'
                '{"slug":"hidden","display_name":"Hidden","visibility":"hidden"}'
                ']}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    models = CodexRunner("codex").list_models()

    assert [model.slug for model in models] == ["gpt-5.5"]
    assert models[0].display_name == "GPT-5.5"
    assert models[0].supported_reasoning_efforts == ("low", "xhigh")
