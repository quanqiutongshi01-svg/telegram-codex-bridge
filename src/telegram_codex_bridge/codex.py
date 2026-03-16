from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable


LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class TaskInput:
    prompt: str
    workspace_name: str
    workspace_path: Path
    chat_id: int
    model: str
    reasoning_effort: str
    plan_mode: bool
    thread_name: str | None = None
    image_paths: list[Path] = field(default_factory=list)
    file_paths: list[Path] = field(default_factory=list)
    dangerous: bool = False


@dataclass(slots=True)
class CodexEvent:
    kind: str
    payload: dict


def build_prompt(task: TaskInput) -> str:
    prompt = task.prompt.strip() or "Please review the provided Telegram input and help the user."
    lines = [
        "Request source: Telegram Codex Bridge.",
        f"Workspace profile: {task.workspace_name}",
        f"Execution root: {task.workspace_path}",
    ]
    if task.thread_name:
        lines.append(f"Selected Codex thread: {task.thread_name}")
    if task.plan_mode:
        lines.append(
            "Planning mode is ON. Do not edit files or run mutating commands. Produce a concrete plan first."
        )
    if task.file_paths:
        lines.append("Local staged files:")
        lines.extend(f"- {path}" for path in task.file_paths)
    if task.image_paths:
        lines.append("Attached image paths:")
        lines.extend(f"- {path}" for path in task.image_paths)
    lines.extend(["", prompt])
    return "\n".join(lines)


def build_command(binary: str, task: TaskInput, session_id: str | None) -> list[str]:
    command = [binary, "exec"]
    if session_id:
        command.append("resume")
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "-m",
                task.model,
                "-c",
                f'model_reasoning_effort="{task.reasoning_effort}"',
            ]
        )
    else:
        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "-C",
                str(task.workspace_path),
                "-m",
                task.model,
                "-c",
                f'model_reasoning_effort="{task.reasoning_effort}"',
            ]
        )
    if task.dangerous:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    for image_path in task.image_paths:
        command.extend(["-i", str(image_path)])
    prompt = build_prompt(task)
    if session_id:
        command.extend([session_id, prompt])
    else:
        command.append(prompt)
    return command


class CodexRunner:
    def __init__(self, binary: str):
        self.binary = binary

    async def stream_task(
        self,
        task: TaskInput,
        session_id: str | None,
        *,
        on_event: Callable[[CodexEvent], Awaitable[None]],
        on_process_started: Callable[[asyncio.subprocess.Process], None] | None = None,
    ) -> int:
        command = build_command(self.binary, task, session_id)
        LOGGER.info("Starting Codex task", extra={"workspace": task.workspace_name, "chat_id": task.chat_id})
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(task.workspace_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if on_process_started:
            on_process_started(process)

        async def read_stdout() -> None:
            assert process.stdout is not None
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    LOGGER.debug("Ignoring non-JSON stdout line: %s", raw)
                    continue
                await self._dispatch_payload(payload, on_event)

        async def read_stderr() -> None:
            assert process.stderr is not None
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                message = line.decode("utf-8", errors="replace").strip()
                if message:
                    await on_event(CodexEvent(kind="stderr", payload={"message": message}))

        await asyncio.gather(read_stdout(), read_stderr())
        returncode = await process.wait()
        await on_event(CodexEvent(kind="process_exit", payload={"returncode": returncode}))
        return returncode

    async def _dispatch_payload(
        self,
        payload: dict,
        on_event: Callable[[CodexEvent], Awaitable[None]],
    ) -> None:
        event_type = payload.get("type", "")
        if event_type == "thread.started":
            await on_event(CodexEvent(kind="session_started", payload={"session_id": payload.get("thread_id")}))
            return
        if event_type == "turn.started":
            await on_event(CodexEvent(kind="turn_started", payload={}))
            return
        if event_type == "turn.completed":
            await on_event(CodexEvent(kind="turn_completed", payload=payload.get("usage", {})))
            return
        if "approval" in event_type:
            await on_event(CodexEvent(kind="approval_requested", payload=payload))
            return
        item = payload.get("item")
        if payload.get("type") == "item.started" and item and item.get("type") == "command_execution":
            await on_event(
                CodexEvent(
                    kind="command_started",
                    payload={"command": item.get("command", ""), "id": item.get("id")},
                )
            )
            return
        if payload.get("type") == "item.completed" and item:
            item_type = item.get("type")
            if item_type == "command_execution":
                await on_event(
                    CodexEvent(
                        kind="command_completed",
                        payload={
                            "command": item.get("command", ""),
                            "exit_code": item.get("exit_code"),
                            "output": item.get("aggregated_output", ""),
                            "id": item.get("id"),
                        },
                    )
                )
                return
            if item_type == "agent_message":
                await on_event(
                    CodexEvent(
                        kind="agent_message",
                        payload={"text": item.get("text", ""), "id": item.get("id")},
                    )
                )
                return
