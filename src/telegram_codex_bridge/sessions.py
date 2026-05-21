from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re


@dataclass(slots=True)
class SavedCodexThread:
    session_id: str
    thread_name: str
    updated_at: str
    cwd: Path | None = None

    @property
    def display_name(self) -> str:
        return self.thread_name or self.session_id


@dataclass(slots=True)
class SavedCodexProject:
    name: str
    path: Path
    thread_count: int
    updated_at: str


@dataclass(slots=True)
class SavedCodexMessage:
    role: str
    text: str
    timestamp: str


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

    def list_threads(
        self,
        *,
        limit: int | None = 10,
        project_cwd: str | Path | None = None,
        include_metadata: bool = False,
    ) -> list[SavedCodexThread]:
        if not self.index_path.exists():
            return []
        project_path = Path(project_cwd).expanduser().resolve() if project_cwd else None
        by_session_id: dict[str, SavedCodexThread] = {}
        for raw_line in self.index_path.read_text().splitlines():
            if not raw_line.strip():
                continue
            try:
                payload = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
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
        if project_path is not None or include_metadata:
            threads = [self._attach_metadata(thread) for thread in threads]
        if project_path is not None:
            threads = [thread for thread in threads if thread.cwd == project_path]
        if limit is None:
            return threads
        return threads[:limit]

    def list_projects(self, *, limit: int | None = 10) -> list[SavedCodexProject]:
        grouped: dict[Path, SavedCodexProject] = {}
        for thread in self.list_threads(limit=None, include_metadata=True):
            if thread.cwd is None:
                continue
            current = grouped.get(thread.cwd)
            if current is None:
                grouped[thread.cwd] = SavedCodexProject(
                    name=thread.cwd.name or str(thread.cwd),
                    path=thread.cwd,
                    thread_count=1,
                    updated_at=thread.updated_at,
                )
                continue
            grouped[thread.cwd] = SavedCodexProject(
                name=current.name,
                path=current.path,
                thread_count=current.thread_count + 1,
                updated_at=max(current.updated_at, thread.updated_at),
            )
        projects = sorted(grouped.values(), key=lambda item: item.updated_at, reverse=True)
        if limit is None:
            return projects
        return projects[:limit]

    def recent_thread_messages(self, session_id: str, *, limit: int = 8) -> list[SavedCodexMessage]:
        session_file = self._session_file(session_id)
        if session_file is None:
            return []
        messages: list[SavedCodexMessage] = []
        with session_file.open() as handle:
            for raw_line in handle:
                try:
                    row = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") != "response_item":
                    continue
                payload = row.get("payload", {})
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                if role not in {"user", "assistant"}:
                    continue
                if role == "assistant" and payload.get("phase") == "commentary":
                    continue
                text = self._clean_message_preview(self._message_text(payload.get("content", [])))
                if not text:
                    continue
                messages.append(
                    SavedCodexMessage(
                        role=role,
                        text=text,
                        timestamp=row.get("timestamp", ""),
                    )
                )
        return messages[-limit:]

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
        session_file = self._session_file(session_id)
        if session_file is None:
            return None
        with session_file.open() as handle:
            first_line = handle.readline().strip()
        if not first_line:
            return None
        try:
            payload = json.loads(first_line)
        except json.JSONDecodeError:
            return None
        if payload.get("type") != "session_meta":
            return None
        cwd = payload.get("payload", {}).get("cwd")
        return Path(cwd).expanduser().resolve() if cwd else None

    def _session_file(self, session_id: str) -> Path | None:
        if not self.sessions_root.exists():
            return None
        matches = sorted(self.sessions_root.rglob(f"*{session_id}.jsonl"))
        return matches[-1] if matches else None

    @staticmethod
    def _message_text(content: object) -> str:
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        chunks = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") not in {"input_text", "output_text", "text"}:
                continue
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
        return "\n".join(chunks)

    @staticmethod
    def _clean_message_preview(text: str) -> str:
        text = text.strip()
        if not text:
            return ""
        if text.startswith(("<environment_context>", "<permissions instructions>", "<app-context>")):
            return ""
        if "## My request for Codex:" in text:
            text = text.split("## My request for Codex:", 1)[1]
        text = re.sub(r"<image\b[^>]*>.*?</image>", "[图片]", text, flags=re.DOTALL)
        text = re.sub(r"# Files mentioned by the user:.*?(?=My request for Codex:|$)", "", text, flags=re.DOTALL)
        return re.sub(r"\s+", " ", text).strip()
