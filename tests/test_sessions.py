import json

import pytest

from telegram_codex_bridge.sessions import AmbiguousThreadError, SessionCatalog


def test_session_catalog_lists_latest_threads(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    index_path = codex_home / "session_index.jsonl"
    index_path.write_text(
        "\n".join(
            [
                json.dumps({"id": "aaa", "thread_name": "旧名称", "updated_at": "2026-03-14T00:00:00Z"}),
                json.dumps({"id": "aaa", "thread_name": "新媒体矩阵运行", "updated_at": "2026-03-16T00:00:00Z"}),
                json.dumps({"id": "bbb", "thread_name": "第二个线程", "updated_at": "2026-03-15T00:00:00Z"}),
            ]
        )
    )

    threads = SessionCatalog(codex_home).list_threads(limit=10)

    assert [thread.display_name for thread in threads] == ["新媒体矩阵运行", "第二个线程"]


def test_session_catalog_resolves_thread_and_loads_cwd(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "03" / "16"
    sessions_root.mkdir(parents=True)
    (codex_home / "session_index.jsonl").write_text(
        json.dumps({"id": "abc123", "thread_name": "新媒体矩阵运行", "updated_at": "2026-03-16T00:00:00Z"})
    )
    (sessions_root / "rollout-2026-03-16T00-00-00-abc123.jsonl").write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"cwd": str(tmp_path / "workspace")},
            }
        )
        + "\n"
    )

    thread = SessionCatalog(codex_home).resolve_thread("新媒体矩阵")

    assert thread.session_id == "abc123"
    assert thread.cwd == (tmp_path / "workspace").resolve()


def test_session_catalog_groups_projects_and_filters_threads(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    sessions_root = codex_home / "sessions" / "2026" / "03" / "16"
    sessions_root.mkdir(parents=True)
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    (codex_home / "session_index.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "aaa", "thread_name": "A 线程", "updated_at": "2026-03-16T00:00:00Z"}),
                json.dumps({"id": "bbb", "thread_name": "B 线程", "updated_at": "2026-03-17T00:00:00Z"}),
                json.dumps({"id": "ccc", "thread_name": "A 新线程", "updated_at": "2026-03-18T00:00:00Z"}),
            ]
        )
    )
    for session_id, cwd in {"aaa": workspace_a, "bbb": workspace_b, "ccc": workspace_a}.items():
        (sessions_root / f"rollout-{session_id}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"cwd": str(cwd)}}) + "\n"
        )

    catalog = SessionCatalog(codex_home)
    projects = catalog.list_projects(limit=None)
    threads = catalog.list_threads(project_cwd=workspace_a, include_metadata=True, limit=None)

    assert [(project.name, project.thread_count) for project in projects] == [("workspace-a", 2), ("workspace-b", 1)]
    assert [thread.display_name for thread in threads] == ["A 新线程", "A 线程"]


def test_session_catalog_reports_ambiguous_match(tmp_path) -> None:
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"id": "aaa", "thread_name": "新媒体矩阵A", "updated_at": "2026-03-16T00:00:00Z"}),
                json.dumps({"id": "bbb", "thread_name": "新媒体矩阵B", "updated_at": "2026-03-16T00:00:01Z"}),
            ]
        )
    )

    with pytest.raises(AmbiguousThreadError):
        SessionCatalog(codex_home).resolve_thread("新媒体矩阵")
