from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


@dataclass(slots=True)
class SavedCodexThread:
    session_id: str
    thread_name: str
    updated_at: str
    cwd: Path | None = None

    @property
    def display_name(self) -> str:
        return self.thread_name or self.session_id


class ThreadLookupError(LookupError):
    pass


class AmbiguousThreadError(ThreadLookupError):
    def __init__(self, query: str, matches: list[SavedCodexThread]):
        self.query = query
        self.matches = matches
        super().__init__(query)


class SessionCatalog:
    def __init__(self, codex_home: str | Path):
        self.codex_home = Path(codex_home).expanduser().resolve()
        self.index_path = self.codex_home / "session_index.jsonl"
        self.sessions_root = self.codex_home / "sessions"

    def list_threads(self, *, limit: int | None = 10) -> list[SavedCodexThread]:
        if not self.index_path.exists():
            return []
        by_session_id: dict[str, SavedCodexThread] = {}
        for raw_line in self.index_path.read_text().splitlines():
            if not raw_line.strip():
                continue
            payload = json.loads(raw_line)
            session_id = payload.get("id")
            if not session_id:
                continue
            candidate = SavedCodexThread(
                session_id=session_id,
                thread_name=payload.get("thread_name", "").strip(),
                updated_at=payload.get("updated_at", ""),
            )
            current = by_session_id.get(session_id)
            if current is None or candidate.updated_at >= current.updated_at:
                by_session_id[session_id] = candidate
        threads = sorted(by_session_id.values(), key=lambda item: item.updated_at, reverse=True)
        if limit is None:
            return threads
        return threads[:limit]

    def resolve_thread(self, query: str) -> SavedCodexThread:
        normalized = query.strip()
        if not normalized:
            raise ThreadLookupError("empty thread query")
        threads = self.list_threads(limit=None)
        for thread in threads:
            if thread.session_id == normalized:
                return self._attach_metadata(thread)
        exact_name_matches = [thread for thread in threads if thread.thread_name == normalized]
        if len(exact_name_matches) == 1:
            return self._attach_metadata(exact_name_matches[0])
        if len(exact_name_matches) > 1:
            raise AmbiguousThreadError(normalized, exact_name_matches[:5])
        lowered = normalized.casefold()
        partial_matches = [
            thread
            for thread in threads
            if lowered in thread.thread_name.casefold() or lowered in thread.session_id.casefold()
        ]
        if len(partial_matches) == 1:
            return self._attach_metadata(partial_matches[0])
        if partial_matches:
            raise AmbiguousThreadError(normalized, partial_matches[:5])
        raise ThreadLookupError(normalized)

    def _attach_metadata(self, thread: SavedCodexThread) -> SavedCodexThread:
        if thread.cwd is not None:
            return thread
        return SavedCodexThread(
            session_id=thread.session_id,
            thread_name=thread.thread_name,
            updated_at=thread.updated_at,
            cwd=self._load_session_cwd(thread.session_id),
        )

    def _load_session_cwd(self, session_id: str) -> Path | None:
        if not self.sessions_root.exists():
            return None
        matches = sorted(self.sessions_root.rglob(f"*{session_id}.jsonl"))
        if not matches:
            return None
        with matches[-1].open() as handle:
            first_line = handle.readline().strip()
        if not first_line:
            return None
        payload = json.loads(first_line)
        if payload.get("type") != "session_meta":
            return None
        cwd = payload.get("payload", {}).get("cwd")
        return Path(cwd).expanduser().resolve() if cwd else None
