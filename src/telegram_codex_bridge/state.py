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
  active_thread_cwd TEXT
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
        }
        for column_name, column_type in required_columns.items():
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE chat_settings ADD COLUMN {column_name} {column_type}")
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
                  active_thread_cwd
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
                      active_thread_cwd
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
                  active_thread_cwd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                  workspace_name=excluded.workspace_name,
                  model=excluded.model,
                  reasoning_effort=excluded.reasoning_effort,
                  plan_mode=excluded.plan_mode,
                  active_session_id=excluded.active_session_id,
                  active_thread_name=excluded.active_thread_name,
                  active_thread_cwd=excluded.active_thread_cwd
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
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO task_history (chat_id, workspace_name, prompt, status, dangerous, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, workspace_name, prompt, status, int(dangerous), time.time()),
            )
