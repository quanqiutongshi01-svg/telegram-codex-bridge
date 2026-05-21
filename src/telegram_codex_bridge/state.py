from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_settings (
  chat_id INTEGER PRIMARY KEY,
  workspace_name TEXT NOT NULL,
  model TEXT NOT NULL,
  reasoning_effort TEXT NOT NULL,
  plan_mode INTEGER NOT NULL DEFAULT 0,
  active_session_id TEXT,
  active_thread_name TEXT,
  active_thread_cwd TEXT,
  active_project_name TEXT,
  active_project_cwd TEXT,
  voice_confirm_mode INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workspace_sessions (
  workspace_name TEXT PRIMARY KEY,
  session_id TEXT
);

CREATE TABLE IF NOT EXISTS media_files (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  telegram_file_id TEXT NOT NULL,
  media_kind TEXT NOT NULL,
  local_path TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS task_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL,
  workspace_name TEXT NOT NULL,
  prompt TEXT NOT NULL,
  status TEXT NOT NULL,
  dangerous INTEGER NOT NULL DEFAULT 0,
  project_path TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS project_preferences (
  chat_id INTEGER NOT NULL,
  project_path TEXT NOT NULL,
  project_name TEXT NOT NULL,
  favorite INTEGER NOT NULL DEFAULT 0,
  last_used_at REAL,
  PRIMARY KEY (chat_id, project_path)
);

CREATE TABLE IF NOT EXISTS thread_favorites (
  chat_id INTEGER NOT NULL,
  session_id TEXT NOT NULL,
  thread_name TEXT NOT NULL,
  thread_cwd TEXT NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY (chat_id, session_id)
);

CREATE TABLE IF NOT EXISTS pending_voice (
  chat_id INTEGER PRIMARY KEY,
  pending_id TEXT NOT NULL,
  transcript TEXT NOT NULL,
  local_path TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


@dataclass(slots=True)
class ChatSettings:
    chat_id: int
    workspace_name: str
    model: str
    reasoning_effort: str
    plan_mode: bool
    active_session_id: str | None = None
    active_thread_name: str | None = None
    active_thread_cwd: Path | None = None
    active_project_name: str | None = None
    active_project_cwd: Path | None = None
    voice_confirm_mode: bool = False


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(chat_settings)")
        }
        required_columns = {
            "active_session_id": "TEXT",
            "active_thread_name": "TEXT",
            "active_thread_cwd": "TEXT",
            "active_project_name": "TEXT",
            "active_project_cwd": "TEXT",
            "voice_confirm_mode": "INTEGER NOT NULL DEFAULT 0",
        }
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE chat_settings ADD COLUMN {column_name} {column_type}")
        task_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(task_history)")
        }
        if "project_path" not in task_columns:
            connection.execute("ALTER TABLE task_history ADD COLUMN project_path TEXT")
        connection.execute(
            """
            UPDATE chat_settings
            SET active_session_id = (
              SELECT workspace_sessions.session_id
              FROM workspace_sessions
              WHERE workspace_sessions.workspace_name = chat_settings.workspace_name
            )
            WHERE active_session_id IS NULL
            """
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def get_chat_settings(
        self,
        chat_id: int,
        *,
        default_workspace: str,
        default_model: str,
        default_effort: str,
        default_plan_mode: bool,
    ) -> ChatSettings:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                  chat_id,
                  workspace_name,
                  model,
                  reasoning_effort,
                  plan_mode,
                  active_session_id,
                  active_thread_name,
                  active_thread_cwd,
                  active_project_name,
                  active_project_cwd,
                  voice_confirm_mode
                FROM chat_settings
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                settings = ChatSettings(
                    chat_id=chat_id,
                    workspace_name=default_workspace,
                    model=default_model,
                    reasoning_effort=default_effort,
                    plan_mode=default_plan_mode,
                )
                connection.execute(
                    """
                    INSERT INTO chat_settings (
                      chat_id,
                      workspace_name,
                      model,
                      reasoning_effort,
                      plan_mode,
                      active_session_id,
                      active_thread_name,
                      active_thread_cwd,
                      active_project_name,
                      active_project_cwd,
                      voice_confirm_mode
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        settings.chat_id,
                        settings.workspace_name,
                        settings.model,
                        settings.reasoning_effort,
                        int(settings.plan_mode),
                        settings.active_session_id,
                        settings.active_thread_name,
                        str(settings.active_thread_cwd) if settings.active_thread_cwd else None,
                        settings.active_project_name,
                        str(settings.active_project_cwd) if settings.active_project_cwd else None,
                        int(settings.voice_confirm_mode),
                    ),
                )
                return settings
            return ChatSettings(
                chat_id=row["chat_id"],
                workspace_name=row["workspace_name"],
                model=row["model"],
                reasoning_effort=row["reasoning_effort"],
                plan_mode=bool(row["plan_mode"]),
                active_session_id=row["active_session_id"],
                active_thread_name=row["active_thread_name"],
                active_thread_cwd=Path(row["active_thread_cwd"]).resolve() if row["active_thread_cwd"] else None,
                active_project_name=row["active_project_name"],
                active_project_cwd=Path(row["active_project_cwd"]).resolve() if row["active_project_cwd"] else None,
                voice_confirm_mode=bool(row["voice_confirm_mode"]),
            )

    def update_chat_settings(self, settings: ChatSettings) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_settings (
                  chat_id,
                  workspace_name,
                  model,
                  reasoning_effort,
                  plan_mode,
                  active_session_id,
                  active_thread_name,
                  active_thread_cwd,
                  active_project_name,
                  active_project_cwd,
                  voice_confirm_mode
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  workspace_name=excluded.workspace_name,
                  model=excluded.model,
                  reasoning_effort=excluded.reasoning_effort,
                  plan_mode=excluded.plan_mode,
                  active_session_id=excluded.active_session_id,
                  active_thread_name=excluded.active_thread_name,
                  active_thread_cwd=excluded.active_thread_cwd,
                  active_project_name=excluded.active_project_name,
                  active_project_cwd=excluded.active_project_cwd,
                  voice_confirm_mode=excluded.voice_confirm_mode
                """,
                (
                    settings.chat_id,
                    settings.workspace_name,
                    settings.model,
                    settings.reasoning_effort,
                    int(settings.plan_mode),
                    settings.active_session_id,
                    settings.active_thread_name,
                    str(settings.active_thread_cwd) if settings.active_thread_cwd else None,
                    settings.active_project_name,
                    str(settings.active_project_cwd) if settings.active_project_cwd else None,
                    int(settings.voice_confirm_mode),
                ),
            )

    def set_active_session(self, chat_id: int, session_id: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE chat_settings
                SET active_session_id = ?
                WHERE chat_id = ?
                """,
                (session_id, chat_id),
            )

    def get_session_id(self, workspace_name: str) -> str | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM workspace_sessions WHERE workspace_name = ?",
                (workspace_name,),
            ).fetchone()
            return row["session_id"] if row and row["session_id"] else None

    def set_session_id(self, workspace_name: str, session_id: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO workspace_sessions (workspace_name, session_id)
                VALUES (?, ?)
                ON CONFLICT(workspace_name) DO UPDATE SET session_id=excluded.session_id
                """,
                (workspace_name, session_id),
            )

    def add_media(self, chat_id: int, telegram_file_id: str, media_kind: str, local_path: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO media_files (chat_id, telegram_file_id, media_kind, local_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, telegram_file_id, media_kind, local_path, time.time()),
            )

    def add_task(
        self,
        *,
        chat_id: int,
        workspace_name: str,
        prompt: str,
        status: str,
        dangerous: bool,
        project_path: str | Path | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_history (chat_id, workspace_name, prompt, status, dangerous, project_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    workspace_name,
                    prompt,
                    status,
                    int(dangerous),
                    str(Path(project_path).expanduser().resolve()) if project_path else None,
                    time.time(),
                ),
            )

    def record_project_usage(self, chat_id: int, project_path: str | Path, project_name: str) -> None:
        path = str(Path(project_path).expanduser().resolve())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_preferences (chat_id, project_path, project_name, favorite, last_used_at)
                VALUES (?, ?, ?, 0, ?)
                ON CONFLICT(chat_id, project_path) DO UPDATE SET
                  project_name=excluded.project_name,
                  last_used_at=excluded.last_used_at
                """,
                (chat_id, path, project_name, time.time()),
            )

    def set_project_favorite(self, chat_id: int, project_path: str | Path, project_name: str, favorite: bool) -> None:
        path = str(Path(project_path).expanduser().resolve())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO project_preferences (chat_id, project_path, project_name, favorite, last_used_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, project_path) DO UPDATE SET
                  project_name=excluded.project_name,
                  favorite=excluded.favorite
                """,
                (chat_id, path, project_name, int(favorite), time.time()),
            )

    def project_preferences(self, chat_id: int) -> dict[str, dict[str, float | int | str | None]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT project_path, project_name, favorite, last_used_at
                FROM project_preferences
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchall()
        return {
            row["project_path"]: {
                "name": row["project_name"],
                "favorite": int(row["favorite"]),
                "last_used_at": row["last_used_at"],
            }
            for row in rows
        }

    def set_thread_favorite(
        self,
        chat_id: int,
        *,
        session_id: str,
        thread_name: str,
        thread_cwd: str | Path,
        favorite: bool,
    ) -> None:
        with self._lock, self._connect() as connection:
            if favorite:
                connection.execute(
                    """
                    INSERT INTO thread_favorites (chat_id, session_id, thread_name, thread_cwd, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, session_id) DO UPDATE SET
                      thread_name=excluded.thread_name,
                      thread_cwd=excluded.thread_cwd
                    """,
                    (chat_id, session_id, thread_name, str(Path(thread_cwd).expanduser().resolve()), time.time()),
                )
                return
            connection.execute(
                "DELETE FROM thread_favorites WHERE chat_id = ? AND session_id = ?",
                (chat_id, session_id),
            )

    def thread_favorites(self, chat_id: int) -> set[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id FROM thread_favorites WHERE chat_id = ?",
                (chat_id,),
            ).fetchall()
        return {row["session_id"] for row in rows}

    def set_pending_voice(self, chat_id: int, pending_id: str, transcript: str, local_path: str | Path) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO pending_voice (chat_id, pending_id, transcript, local_path, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  pending_id=excluded.pending_id,
                  transcript=excluded.transcript,
                  local_path=excluded.local_path,
                  created_at=excluded.created_at
                """,
                (chat_id, pending_id, transcript, str(local_path), time.time()),
            )

    def get_pending_voice(self, chat_id: int, pending_id: str | None = None) -> sqlite3.Row | None:
        with self._lock, self._connect() as connection:
            if pending_id is None:
                return connection.execute(
                    "SELECT pending_id, transcript, local_path, created_at FROM pending_voice WHERE chat_id = ?",
                    (chat_id,),
                ).fetchone()
            return connection.execute(
                """
                SELECT pending_id, transcript, local_path, created_at
                FROM pending_voice
                WHERE chat_id = ? AND pending_id = ?
                """,
                (chat_id, pending_id),
            ).fetchone()

    def clear_pending_voice(self, chat_id: int) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM pending_voice WHERE chat_id = ?", (chat_id,))

    def recent_tasks(self, chat_id: int, *, limit: int = 10, project_path: str | Path | None = None) -> list[sqlite3.Row]:
        with self._lock, self._connect() as connection:
            if project_path is not None:
                return connection.execute(
                    """
                    SELECT workspace_name, prompt, status, dangerous, project_path, created_at
                    FROM task_history
                    WHERE chat_id = ? AND project_path = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (chat_id, str(Path(project_path).expanduser().resolve()), limit),
                ).fetchall()
            return connection.execute(
                """
                SELECT workspace_name, prompt, status, dangerous, project_path, created_at
                FROM task_history
                WHERE chat_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
