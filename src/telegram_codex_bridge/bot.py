from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import logging
import mimetypes
from pathlib import Path
import re
import shutil
import time
import uuid

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .codex import CodexEvent, CodexModelOption, CodexRunner, TaskInput
from .config import BridgeConfig, WorkspaceConfig
from .sessions import AmbiguousThreadError, SavedCodexProject, SessionCatalog, ThreadLookupError
from .state import ChatSettings, StateStore
from .transcribe import WhisperTranscriber


LOGGER = logging.getLogger(__name__)
FALLBACK_EFFORT_CHOICES = ("low", "medium", "high", "xhigh")
FILE_PATH_PATTERN = re.compile(r"(/[^ \n\r\t\]\)\"']+)")


@dataclass(slots=True)
class QueuedTask:
    task: TaskInput
    settings_snapshot: ChatSettings
    worker_key: str
    context_label: str
    reply_to_message_id: int | None
    source_description: str
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status_message_id: int | None = None
    cancelled: bool = False
    created_at: float = field(default_factory=time.time)

    def rerun_dangerous(self) -> "QueuedTask":
        return QueuedTask(
            task=replace(self.task, dangerous=True),
            settings_snapshot=self.settings_snapshot,
            worker_key=self.worker_key,
            context_label=self.context_label,
            reply_to_message_id=self.reply_to_message_id,
            source_description=self.source_description,
            task_id=uuid.uuid4().hex[:10],
            status_message_id=self.status_message_id,
        )


@dataclass(slots=True)
class WorkspaceWorker:
    key: str
    name: str
    path: Path
    queue: asyncio.Queue[QueuedTask]
    worker_task: asyncio.Task | None = None
    active_process: asyncio.subprocess.Process | None = None
    active_job: QueuedTask | None = None
    last_progress_message: str | None = None


@dataclass(slots=True)
class ResolvedChatTarget:
    workspace: str
    path: Path
    session_id: str | None
    thread_name: str | None
    worker_key: str
    context_label: str


class TelegramCodexBridge:
    def __init__(self, *, config: BridgeConfig, state: StateStore):
        self.config = config
        self.state = state
        self.codex = CodexRunner(config.codex_binary)
        self.transcriber = WhisperTranscriber(
            ffmpeg_binary=config.ffmpeg_binary,
            model_name=config.whisper_model,
            language=config.whisper_language or None,
        )
        self.session_catalog = SessionCatalog(config.runtime_dir.parent)
        self.application: Application | None = None
        self.bot_username: str | None = None
        self.pending_approvals: dict[str, QueuedTask] = {}
        self.workers: dict[str, WorkspaceWorker] = {}
        self._model_options_cache: list[CodexModelOption] | None = None
        self._project_tokens: dict[str, Path] = {}
        self.recent_tasks: dict[str, QueuedTask] = {}
        for workspace in config.workspaces:
            self._ensure_worker(workspace.name, workspace.path)

    async def run(self) -> None:
        application = ApplicationBuilder().token(self.config.bot_token).build()
        self.application = application
        self._register_handlers(application)
        async with application:
            me = await application.bot.get_me()
            self.bot_username = me.username
            await self._configure_bot_commands(application)
            for worker in self.workers.values():
                worker.worker_task = asyncio.create_task(self._workspace_loop(worker))
            await application.start()
            assert application.updater is not None
            await application.updater.start_polling(timeout=self.config.polling_timeout)
            try:
                await asyncio.Event().wait()
            finally:
                await application.updater.stop()
                await application.stop()
                for worker in self.workers.values():
                    if worker.worker_task:
                        worker.worker_task.cancel()
                await asyncio.gather(
                    *(worker.worker_task for worker in self.workers.values() if worker.worker_task),
                    return_exceptions=True,
                )

    async def _configure_bot_commands(self, application: Application) -> None:
        commands = [
            BotCommand("menu", "打开控制面板"),
            BotCommand("status", "查看当前状态"),
            BotCommand("doctor", "查看桥接器自检"),
            BotCommand("logs", "查看最近错误日志"),
            BotCommand("tasks", "查看最近任务"),
            BotCommand("projects", "查看 Codex 项目"),
            BotCommand("project", "切换 Codex 项目"),
            BotCommand("threads", "查看当前项目的对话"),
            BotCommand("thread", "切换到指定对话"),
            BotCommand("search", "搜索项目和对话"),
            BotCommand("favorite", "收藏当前项目或对话"),
            BotCommand("workspaces", "查看项目"),
            BotCommand("workspace", "切换项目"),
            BotCommand("model", "查看或切换模型"),
            BotCommand("effort", "查看或切换推理精度"),
            BotCommand("plan", "查看或切换计划模式"),
            BotCommand("voiceconfirm", "切换语音确认模式"),
            BotCommand("new", "新建一个 Telegram 对话"),
            BotCommand("stop", "停止当前任务"),
            BotCommand("help", "查看帮助"),
        ]
        await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    def _register_handlers(self, application: Application) -> None:
        application.add_handler(CommandHandler("help", self.help_command))
        application.add_handler(CommandHandler("menu", self.menu_command))
        application.add_handler(CommandHandler("status", self.status_command))
        application.add_handler(CommandHandler("doctor", self.doctor_command))
        application.add_handler(CommandHandler("logs", self.logs_command))
        application.add_handler(CommandHandler("tasks", self.tasks_command))
        application.add_handler(CommandHandler("projects", self.projects_command))
        application.add_handler(CommandHandler("project", self.project_command))
        application.add_handler(CommandHandler("workspaces", self.workspaces_command))
        application.add_handler(CommandHandler("workspace", self.workspace_command))
        application.add_handler(CommandHandler("threads", self.threads_command))
        application.add_handler(CommandHandler("thread", self.thread_command))
        application.add_handler(CommandHandler("search", self.search_command))
        application.add_handler(CommandHandler("favorite", self.favorite_command))
        application.add_handler(CommandHandler("model", self.model_command))
        application.add_handler(CommandHandler("effort", self.effort_command))
        application.add_handler(CommandHandler("plan", self.plan_command))
        application.add_handler(CommandHandler("voiceconfirm", self.voiceconfirm_command))
        application.add_handler(CommandHandler("new", self.new_command))
        application.add_handler(CommandHandler("stop", self.stop_command))
        application.add_handler(CallbackQueryHandler(self.callback_query))
        application.add_handler(MessageHandler(filters.VOICE, self.voice_message))
        application.add_handler(MessageHandler(filters.PHOTO | (filters.Document.ALL & filters.Document.IMAGE), self.photo_message))
        application.add_handler(MessageHandler(filters.VIDEO | (filters.Document.ALL & filters.Document.VIDEO), self.video_message))
        application.add_handler(
            MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE & ~filters.Document.VIDEO, self.document_message)
        )
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))

    def _is_authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        return bool(
            (user_id is not None and user_id in self.config.allowed_user_ids)
            or (chat_id is not None and chat_id in self.config.allowed_chat_ids)
        )

    def _is_directed_to_bot(self, update: Update) -> bool:
        chat = update.effective_chat
        message = update.effective_message
        if chat is None or message is None:
            return False
        if chat.type == ChatType.PRIVATE:
            return True
        if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.username == self.bot_username:
            return True
        mention = f"@{self.bot_username}" if self.bot_username else None
        haystack = (message.text or message.caption or "")
        return bool(mention and mention in haystack)

    async def _ensure_allowed(self, update: Update) -> bool:
        if self._is_authorized(update):
            return True
        if update.effective_message:
            await update.effective_message.reply_text("这个聊天还没有被授权使用 Telegram Codex Bridge。")
        return False

    def _chat_settings(self, chat_id: int) -> ChatSettings:
        settings = self.state.get_chat_settings(
            chat_id,
            default_workspace=self.config.default_workspace.name,
            default_model=self.config.default_model,
            default_effort=self.config.default_reasoning_effort,
            default_plan_mode=self.config.default_plan_mode,
        )
        normalized_effort = self._normalize_effort_for_model(settings.model, settings.reasoning_effort)
        if settings.reasoning_effort != normalized_effort:
            settings.reasoning_effort = normalized_effort
            self.state.update_chat_settings(settings)
        return settings

    def _worker_key(self, path: Path) -> str:
        return str(path.expanduser().resolve())

    def _workspace_for_path(self, path: Path) -> WorkspaceConfig | None:
        resolved_path = path.expanduser().resolve()
        for workspace in self.config.workspaces:
            if workspace.path == resolved_path:
                return workspace
        return None

    def _project_display_name(self, path: Path) -> str:
        workspace = self._workspace_for_path(path)
        if workspace is not None:
            return workspace.name
        return path.name or str(path)

    def _selected_project_path(self, settings: ChatSettings) -> Path:
        if settings.active_project_cwd:
            return settings.active_project_cwd
        if settings.active_thread_cwd:
            return settings.active_thread_cwd
        return self.config.ensure_workspace(settings.workspace_name).path

    def _project_token(self, path: Path) -> str:
        resolved_path = path.expanduser().resolve()
        token = hashlib.sha1(str(resolved_path).encode("utf-8")).hexdigest()[:12]
        self._project_tokens[token] = resolved_path
        return token

    def _resolve_project_token(self, token: str) -> Path | None:
        if token in self._project_tokens:
            return self._project_tokens[token]
        for project in self._available_projects(limit=None):
            candidate_token = hashlib.sha1(str(project.path).encode("utf-8")).hexdigest()[:12]
            if candidate_token == token:
                self._project_tokens[token] = project.path
                return project.path
        return None

    def _available_projects(self, settings: ChatSettings | None = None, *, limit: int | None = 10) -> list[SavedCodexProject]:
        by_path: dict[Path, SavedCodexProject] = {}
        for workspace in self.config.workspaces:
            by_path[workspace.path] = SavedCodexProject(
                name=workspace.name,
                path=workspace.path,
                thread_count=0,
                updated_at="",
            )
        for project in self.session_catalog.list_projects(limit=None):
            workspace = self._workspace_for_path(project.path)
            by_path[project.path] = SavedCodexProject(
                name=workspace.name if workspace is not None else project.name,
                path=project.path,
                thread_count=project.thread_count,
                updated_at=project.updated_at,
            )
        preferences = self.state.project_preferences(settings.chat_id) if settings is not None else {}

        def sort_key(project: SavedCodexProject) -> tuple[int, float, str, str]:
            preference = preferences.get(str(project.path), {})
            favorite = int(preference.get("favorite") or 0)
            last_used = float(preference.get("last_used_at") or 0)
            return (favorite, last_used, project.updated_at, project.name)

        projects = sorted(by_path.values(), key=sort_key, reverse=True)
        if limit is None:
            return projects
        return projects[:limit]

    def _ensure_worker(self, name: str, path: Path) -> WorkspaceWorker:
        resolved_path = path.expanduser().resolve()
        key = self._worker_key(resolved_path)
        worker = self.workers.get(key)
        if worker is None:
            worker = WorkspaceWorker(
                key=key,
                name=name,
                path=resolved_path,
                queue=asyncio.Queue(),
            )
            self.workers[key] = worker
            if self.application is not None and worker.worker_task is None:
                worker.worker_task = asyncio.create_task(self._workspace_loop(worker))
        return worker

    def _resolve_target(self, settings: ChatSettings) -> ResolvedChatTarget:
        workspace = self.config.ensure_workspace(settings.workspace_name)
        if settings.active_session_id and settings.active_thread_cwd:
            path = settings.active_thread_cwd
            context_label = settings.active_thread_name or settings.active_session_id[:8]
            return ResolvedChatTarget(
                workspace=settings.workspace_name,
                path=path,
                session_id=settings.active_session_id,
                thread_name=settings.active_thread_name,
                worker_key=self._worker_key(path),
                context_label=context_label,
            )
        if settings.active_project_cwd:
            path = settings.active_project_cwd
            context_label = settings.active_project_name or self._project_display_name(path)
            return ResolvedChatTarget(
                workspace=settings.workspace_name,
                path=path,
                session_id=settings.active_session_id,
                thread_name=settings.active_thread_name,
                worker_key=self._worker_key(path),
                context_label=context_label,
            )
        context_label = settings.active_thread_name or settings.workspace_name
        return ResolvedChatTarget(
            workspace=settings.workspace_name,
            path=workspace.path,
            session_id=settings.active_session_id,
            thread_name=settings.active_thread_name,
            worker_key=self._worker_key(workspace.path),
            context_label=context_label,
        )

    def _current_project_summary(self, settings: ChatSettings) -> str:
        path = self._selected_project_path(settings)
        name = settings.active_project_name or self._project_display_name(path)
        return f"{name}\n项目路径：{path}"

    def _current_thread_summary(self, settings: ChatSettings) -> str:
        if settings.active_thread_name:
            return settings.active_thread_name
        if settings.active_session_id:
            return f"Telegram 对话 {settings.active_session_id[:8]}"
        return "新的 Telegram 对话"

    def _threads_keyboard(self, settings: ChatSettings) -> InlineKeyboardMarkup | None:
        recent_threads = self.session_catalog.list_threads(
            limit=6,
            project_cwd=self._selected_project_path(settings),
            include_metadata=True,
        )
        favorites = self.state.thread_favorites(settings.chat_id)
        if not recent_threads:
            return InlineKeyboardMarkup([[InlineKeyboardButton("切换项目", callback_data="menu:projects")]])
        recent_threads = sorted(
            recent_threads,
            key=lambda thread: (thread.session_id in favorites, getattr(thread, "updated_at", "")),
            reverse=True,
        )
        buttons = []
        for thread in recent_threads:
            marker = "* " if thread.session_id == settings.active_session_id else ""
            favorite_marker = "★ " if thread.session_id in favorites else ""
            buttons.append(
                [
                    InlineKeyboardButton(
                        (marker + favorite_marker + thread.display_name)[:48],
                        callback_data=f"thread:{thread.session_id}",
                    ),
                    InlineKeyboardButton(
                        "取消收藏" if thread.session_id in favorites else "收藏",
                        callback_data=f"favthread:{thread.session_id}",
                    ),
                ]
            )
        buttons.append(
            [
                InlineKeyboardButton("新对话", callback_data="menu:new"),
                InlineKeyboardButton("清空对话", callback_data="thread:clear"),
            ]
        )
        buttons.append([InlineKeyboardButton("切换项目", callback_data="menu:projects")])
        return InlineKeyboardMarkup(buttons)

    def _projects_keyboard(self, settings: ChatSettings) -> InlineKeyboardMarkup:
        current_path = self._selected_project_path(settings)
        preferences = self.state.project_preferences(settings.chat_id)
        rows = []
        for project in self._available_projects(settings, limit=8):
            token = self._project_token(project.path)
            preference = preferences.get(str(project.path), {})
            favorite = bool(preference.get("favorite"))
            label = project.name
            if project.thread_count:
                label = f"{label} ({project.thread_count} 个对话)"
            if favorite:
                label = f"★ {label}"
            if project.path == current_path:
                label = f"* {label}"
            rows.append(
                [
                    InlineKeyboardButton(label[:48], callback_data=f"project:{token}"),
                    InlineKeyboardButton("取消收藏" if favorite else "收藏", callback_data=f"favproject:{token}"),
                ]
            )
        rows.append([InlineKeyboardButton("清空项目选择", callback_data="project:clear")])
        return InlineKeyboardMarkup(rows)

    def _menu_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("状态", callback_data="menu:status"),
                    InlineKeyboardButton("自检", callback_data="menu:doctor"),
                ],
                [
                    InlineKeyboardButton("最近错误", callback_data="menu:logs"),
                    InlineKeyboardButton("任务", callback_data="menu:tasks"),
                ],
                [
                    InlineKeyboardButton("项目", callback_data="menu:projects"),
                    InlineKeyboardButton("对话", callback_data="menu:threads"),
                ],
                [
                    InlineKeyboardButton("搜索", callback_data="menu:search"),
                    InlineKeyboardButton("收藏当前", callback_data="favorite:current"),
                ],
                [
                    InlineKeyboardButton("模型", callback_data="menu:model"),
                    InlineKeyboardButton("精度", callback_data="menu:effort"),
                ],
                [
                    InlineKeyboardButton("计划模式", callback_data="menu:plan"),
                    InlineKeyboardButton("语音确认", callback_data="menu:voiceconfirm"),
                ],
                [
                    InlineKeyboardButton("新对话", callback_data="menu:new"),
                    InlineKeyboardButton("停止", callback_data="menu:stop"),
                ],
            ]
        )

    def _with_back_button(self, keyboard: InlineKeyboardMarkup | None) -> InlineKeyboardMarkup:
        rows = list(keyboard.inline_keyboard) if keyboard else []
        rows.append([InlineKeyboardButton("返回", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _workspace_keyboard(self, current_name: str) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton(
                    ("* " if workspace.name == current_name else "") + workspace.name,
                    callback_data=f"workspace:{workspace.name}",
                )
            ]
            for workspace in self.config.workspaces
        ]
        return InlineKeyboardMarkup(buttons)

    def _model_keyboard(self, current_model: str) -> InlineKeyboardMarkup:
        rows = []
        for model in self._model_options():
            label = model.display_name
            if model.display_name != model.slug:
                label = f"{model.display_name} ({model.slug})"
            label = ("* " if model.slug == current_model else "") + label
            rows.append([InlineKeyboardButton(label[:60], callback_data=f"model:{model.slug}")])
        return InlineKeyboardMarkup(rows)

    def _effort_keyboard(self, current_effort: str, current_model: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(("* " if effort == current_effort else "") + effort, callback_data=f"effort:{effort}")]
                for effort in self._effort_choices(current_model)
            ]
        )

    def _plan_keyboard(self, enabled: bool) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(("* " if enabled else "") + "开启", callback_data="plan:on"),
                    InlineKeyboardButton(("* " if not enabled else "") + "关闭", callback_data="plan:off"),
                ]
            ]
        )

    def _status_text(self, settings: ChatSettings) -> str:
        target = self._resolve_target(settings)
        worker = self._ensure_worker(target.context_label, target.path)
        return (
            f"当前项目：{settings.active_project_name or self._project_display_name(self._selected_project_path(settings))}\n"
            f"当前对话：{self._current_thread_summary(settings)}\n"
            f"工作区配置：{settings.workspace_name}\n"
            f"执行目录：{target.path}\n"
            f"模型：{settings.model}\n"
            f"推理精度：{settings.reasoning_effort}\n"
            f"计划模式：{'开启' if settings.plan_mode else '关闭'}\n"
            f"语音确认：{'开启' if settings.voice_confirm_mode else '关闭'}\n"
            f"排队任务：{worker.queue.qsize()}\n"
            f"运行中：{'是' if worker.active_process else '否'}"
        )

    def _doctor_text(self, settings: ChatSettings) -> str:
        target = self._resolve_target(settings)
        checks = [
            ("桥接服务", True, "bot 轮询中"),
            ("codex", self._binary_available(self.config.codex_binary), self.config.codex_binary),
            ("ffmpeg", self._binary_available(self.config.ffmpeg_binary), self.config.ffmpeg_binary),
            ("当前项目", target.path.exists(), str(target.path)),
            ("执行目录", target.path.exists(), str(target.path)),
            ("状态数据库", self.config.state_db_path.exists(), str(self.config.state_db_path)),
            ("会话索引", self.session_catalog.index_path.exists(), str(self.session_catalog.index_path)),
        ]
        lines = ["快速自检："]
        for label, ok, detail in checks:
            lines.append(f"- {'正常' if ok else '异常'} {label}：{detail}")
        return "\n".join(lines)

    def _projects_text(self, settings: ChatSettings) -> str:
        projects = self._available_projects(settings, limit=8)
        if not projects:
            return "这台 Mac 上暂时还没有找到可选 Codex 项目。"
        current_path = self._selected_project_path(settings)
        preferences = self.state.project_preferences(settings.chat_id)
        lines = ["可选 Codex 项目："]
        for project in projects:
            prefix = "* " if project.path == current_path else "- "
            favorite = "★ " if preferences.get(str(project.path), {}).get("favorite") else ""
            count = f"，{project.thread_count} 个对话" if project.thread_count else ""
            lines.append(f"{prefix}{favorite}{project.name}{count}\n  {project.path}")
        lines.append("")
        lines.append("选择项目后，“对话”按钮只会显示这个项目下的本地 Codex 对话。")
        return "\n".join(lines)

    def _recent_tasks_text(self, chat_id: int) -> str:
        rows = self.state.recent_tasks(chat_id, limit=8)
        if not rows:
            return "还没有记录到任务。"
        lines = ["最近任务："]
        for row in rows:
            prompt = str(row["prompt"]).replace("\n", " ")[:50]
            danger = "，提权" if row["dangerous"] else ""
            lines.append(f"- {row['status']}｜{row['workspace_name']}{danger}｜{prompt}")
        return "\n".join(lines)

    def _logs_text(self) -> str:
        err_log = self.config.logs_dir / "service.err.log"
        service_log = self.config.logs_dir / "service.log"
        chunks = []
        for label, path in (("错误日志", err_log), ("运行日志", service_log)):
            if not path.exists():
                chunks.append(f"{label}：暂无。")
                continue
            lines = path.read_text(errors="replace").splitlines()[-20:]
            interesting = [line for line in lines if line.strip()]
            if not interesting:
                chunks.append(f"{label}：暂无新内容。")
                continue
            chunks.append(f"{label}最近 20 行：\n" + "\n".join(interesting[-20:]))
        text = "\n\n".join(chunks)
        return text[-3500:] if len(text) > 3500 else text

    def _voice_confirm_keyboard(self, enabled: bool) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(("* " if enabled else "") + "开启", callback_data="voiceconfirm:on"),
                    InlineKeyboardButton(("* " if not enabled else "") + "关闭", callback_data="voiceconfirm:off"),
                ]
            ]
        )

    def _threads_text(self, settings: ChatSettings) -> str:
        project_path = self._selected_project_path(settings)
        recent_threads = self.session_catalog.list_threads(limit=8, project_cwd=project_path, include_metadata=True)
        if not recent_threads:
            return (
                f"当前项目还没有找到本地 Codex 对话。\n"
                f"项目：{settings.active_project_name or self._project_display_name(project_path)}\n"
                f"路径：{project_path}"
            )
        lines = [
            "当前项目下的 Codex 对话：",
            f"项目：{settings.active_project_name or self._project_display_name(project_path)}",
        ]
        for thread in recent_threads:
            prefix = "* " if thread.session_id == settings.active_session_id else "- "
            lines.append(f"{prefix}{thread.display_name} [{thread.session_id[:8]}]")
        lines.append("")
        lines.append("可以发送 /thread <名称或ID>，也可以直接点下面的按钮。")
        return "\n".join(lines)

    def _binary_available(self, binary: str) -> bool:
        return shutil.which(binary) is not None or Path(binary).exists()

    def _model_options(self) -> list[CodexModelOption]:
        if self._model_options_cache is not None:
            return self._model_options_cache
        try:
            options = self.codex.list_models()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Falling back to configured quick models", exc_info=True)
            options = [
                CodexModelOption(
                    slug=model,
                    display_name=model,
                    default_reasoning_effort="medium",
                    supported_reasoning_efforts=FALLBACK_EFFORT_CHOICES,
                )
                for model in self.config.quick_models
            ]
        self._model_options_cache = options
        return options

    def _model_option(self, slug: str) -> CodexModelOption | None:
        for model in self._model_options():
            if model.slug == slug:
                return model
        return None

    def _effort_choices(self, model_slug: str) -> tuple[str, ...]:
        model = self._model_option(model_slug)
        if model is None:
            return FALLBACK_EFFORT_CHOICES
        return model.supported_reasoning_efforts or FALLBACK_EFFORT_CHOICES

    def _normalize_effort_for_model(self, model_slug: str, effort: str) -> str:
        normalized = "low" if effort == "minimal" else effort
        choices = self._effort_choices(model_slug)
        if normalized in choices:
            return normalized
        model = self._model_option(model_slug)
        if model and model.default_reasoning_effort in choices:
            return model.default_reasoning_effort
        return choices[0]

    def _select_project(self, settings: ChatSettings, path: Path) -> None:
        resolved_path = path.expanduser().resolve()
        workspace = self._workspace_for_path(resolved_path)
        if workspace is not None:
            settings.workspace_name = workspace.name
        settings.active_project_name = workspace.name if workspace is not None else self._project_display_name(resolved_path)
        settings.active_project_cwd = resolved_path
        settings.active_session_id = None
        settings.active_thread_name = None
        settings.active_thread_cwd = None
        self.state.record_project_usage(settings.chat_id, resolved_path, settings.active_project_name)

    def _clear_project_selection(self, settings: ChatSettings) -> None:
        settings.workspace_name = self.config.default_workspace.name
        settings.active_project_name = None
        settings.active_project_cwd = None
        settings.active_session_id = None
        settings.active_thread_name = None
        settings.active_thread_cwd = None

    def _resolve_project_query(self, query: str, settings: ChatSettings | None = None) -> SavedCodexProject | None:
        normalized = query.strip()
        if not normalized:
            return None
        projects = self._available_projects(settings, limit=None)
        for project in projects:
            if project.name == normalized or str(project.path) == normalized:
                return project
        lowered = normalized.casefold()
        matches = [
            project
            for project in projects
            if lowered in project.name.casefold() or lowered in str(project.path).casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        return None

    def _source_label(self, source_description: str) -> str:
        labels = {
            "text": "文字",
            "image": "图片",
            "video": "视频",
            "document": "文档",
            "voice": "语音",
            "approval": "授权重试",
        }
        return labels.get(source_description, source_description)

    def _queue_ahead_count(self, worker: WorkspaceWorker, job: QueuedTask) -> int:
        ahead = max(worker.queue.qsize() - 1, 0)
        if worker.active_job is not None and worker.active_job is not job:
            ahead += 1
        return ahead

    def _risk_label(self, job: QueuedTask) -> str:
        if job.task.dangerous:
            return "高，需要提权或已提权"
        prompt = job.task.prompt.lower()
        risky_terms = ("rm ", "sudo", "chmod", "launchctl", "pip install", "npm install", "git push")
        if any(term in prompt for term in risky_terms):
            return "中，可能涉及写入或系统命令"
        return "低，常规任务"

    def _render_task_status(self, job: QueuedTask, worker: WorkspaceWorker, state_label: str, detail: str | None = None) -> str:
        lines = [
            f"任务状态：{state_label}",
            f"任务ID：{job.task_id}",
            f"上下文：{job.context_label}",
            f"来源：{self._source_label(job.source_description)}",
            f"执行目录：{job.task.workspace_path}",
            f"模型：{job.task.model}",
            f"推理精度：{job.task.reasoning_effort}",
            f"计划模式：{'开启' if job.task.plan_mode else '关闭'}",
            f"风险等级：{self._risk_label(job)}",
        ]
        if state_label == "排队中":
            lines.append(f"前方任务：{self._queue_ahead_count(worker, job)}")
        if detail:
            lines.append(f"进度：{detail}")
        return "\n".join(lines)

    def _task_keyboard(self, job: QueuedTask, state_label: str) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton("详情", callback_data=f"task:details:{job.task_id}"),
                InlineKeyboardButton("重新运行", callback_data=f"task:rerun:{job.task_id}"),
            ]
        ]
        if state_label in {"排队中", "执行中", "等待批准"}:
            rows.append([InlineKeyboardButton("取消任务", callback_data=f"task:cancel:{job.task_id}")])
        return InlineKeyboardMarkup(rows)

    def _task_details_text(self, job: QueuedTask) -> str:
        prompt = job.task.prompt.strip()
        if len(prompt) > 1200:
            prompt = prompt[:1200] + "\n..."
        return (
            f"任务详情：{job.task_id}\n"
            f"上下文：{job.context_label}\n"
            f"来源：{self._source_label(job.source_description)}\n"
            f"执行目录：{job.task.workspace_path}\n"
            f"模型：{job.task.model}\n"
            f"推理精度：{job.task.reasoning_effort}\n"
            f"风险等级：{self._risk_label(job)}\n"
            f"创建时间：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(job.created_at))}\n"
            f"提示词：\n{prompt}"
        )

    async def _update_task_status_message(
        self,
        job: QueuedTask,
        worker: WorkspaceWorker,
        state_label: str,
        *,
        detail: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if self.application is None:
            return
        bot = self.application.bot
        text = self._render_task_status(job, worker, state_label, detail)
        if reply_markup is None:
            reply_markup = self._task_keyboard(job, state_label)
        try:
            if job.status_message_id is None:
                message = await bot.send_message(
                    chat_id=job.task.chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    reply_to_message_id=job.reply_to_message_id,
                )
                job.status_message_id = message.message_id
                return
            await bot.edit_message_text(
                chat_id=job.task.chat_id,
                message_id=job.status_message_id,
                text=text,
                reply_markup=reply_markup,
            )
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            LOGGER.debug("Failed to update task status card", exc_info=True)
            message = await bot.send_message(
                chat_id=job.task.chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=job.reply_to_message_id,
            )
            job.status_message_id = message.message_id

    async def _edit_callback_message(
        self,
        query,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        try:
            await query.edit_message_text(text, reply_markup=reply_markup)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            raise

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        text = (
            "/menu - 打开 Telegram 控制面板\n"
            "/status - 查看当前工作状态\n"
            "/doctor - 快速自检桥接器运行状态\n"
            "/logs - 查看最近错误日志\n"
            "/tasks - 查看最近任务记录\n"
            "/model [模型ID] - 查看或切换仅 Telegram 生效的模型\n"
            "/effort [精度] - 查看或切换当前模型支持的推理精度\n"
            "/plan [on|off] - 查看或切换仅 Telegram 生效的计划模式\n"
            "/voiceconfirm [on|off] - 开关语音转写确认模式\n"
            "/projects - 查看这台 Mac 上的 Codex 项目\n"
            "/project [名称|路径片段|clear] - 切换项目，或清空项目选择\n"
            "/threads - 查看当前项目下的 Codex 对话\n"
            "/thread [名称|ID|clear] - 切换到已有对话，或清空当前对话绑定\n"
            "/search <关键词> - 搜索项目、路径和对话标题\n"
            "/favorite - 收藏或取消收藏当前项目/对话\n"
            "/workspaces - 查看已注册工作区（兼容命令）\n"
            "/workspace [名称] - 切换已注册工作区（兼容命令）\n"
            "/new - 在当前项目中新建一个 Telegram 对话\n"
            "/stop - 停止当前正在执行的任务"
        )
        await update.effective_message.reply_text(text)

    async def menu_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            self._status_text(settings),
            reply_markup=self._menu_keyboard(),
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            self._status_text(settings),
            reply_markup=self._menu_keyboard(),
        )

    async def doctor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            self._doctor_text(settings),
            reply_markup=self._menu_keyboard(),
        )

    async def logs_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await update.effective_message.reply_text(self._logs_text(), reply_markup=self._menu_keyboard())

    async def tasks_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        await update.effective_message.reply_text(self._recent_tasks_text(update.effective_chat.id), reply_markup=self._menu_keyboard())

    async def projects_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            self._projects_text(settings),
            reply_markup=self._projects_keyboard(settings),
        )

    async def project_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前项目：{self._current_project_summary(settings)}",
                reply_markup=self._projects_keyboard(settings),
            )
            return
        query = " ".join(context.args).strip()
        if query.lower() in {"clear", "none", "default"}:
            self._clear_project_selection(settings)
            self.state.update_chat_settings(settings)
            await update.effective_message.reply_text("已清空项目选择，回到默认工作区。")
            return
        project = self._resolve_project_query(query, settings)
        if project is None:
            await update.effective_message.reply_text("没有找到匹配的 Codex 项目。请用 /projects 查看可选项目。")
            return
        if not project.path.exists():
            await update.effective_message.reply_text(f"找到了这个项目，但路径不存在：{project.path}")
            return
        self._select_project(settings, project.path)
        self.state.update_chat_settings(settings)
        self._ensure_worker(settings.active_project_name or project.name, project.path)
        await update.effective_message.reply_text(
            f"已切换到项目：{settings.active_project_name}\n路径：{project.path}\n现在可以点“对话”选择这个项目里的历史对话。"
        )

    async def workspaces_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            self._projects_text(settings),
            reply_markup=self._projects_keyboard(settings),
        )

    async def workspace_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前项目：{self._current_project_summary(settings)}",
                reply_markup=self._projects_keyboard(settings),
            )
            return
        target = context.args[0]
        try:
            workspace = self.config.ensure_workspace(target)
        except KeyError:
            await update.effective_message.reply_text(f"未知工作区：{target}")
            return
        self._select_project(settings, workspace.path)
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(f"已切换到项目 {target}，并清空当前对话绑定。")

    def _search_results_keyboard(self, settings: ChatSettings, query: str) -> InlineKeyboardMarkup | None:
        lowered = query.casefold()
        rows = []
        for project in self._available_projects(settings, limit=None):
            if lowered in project.name.casefold() or lowered in str(project.path).casefold():
                rows.append([InlineKeyboardButton(f"项目：{project.name}"[:60], callback_data=f"project:{self._project_token(project.path)}")])
            if len(rows) >= 8:
                break
        for thread in self.session_catalog.list_threads(limit=None, include_metadata=True):
            haystack = f"{thread.display_name} {thread.session_id} {thread.cwd or ''}".casefold()
            if lowered not in haystack:
                continue
            rows.append([InlineKeyboardButton(f"对话：{thread.display_name}"[:60], callback_data=f"thread:{thread.session_id}")])
            if len(rows) >= 12:
                break
        if not rows:
            return None
        rows.append([InlineKeyboardButton("返回", callback_data="menu:main")])
        return InlineKeyboardMarkup(rows)

    def _search_results_text(self, settings: ChatSettings, query: str) -> str:
        lowered = query.casefold()
        project_matches = [
            project
            for project in self._available_projects(settings, limit=None)
            if lowered in project.name.casefold() or lowered in str(project.path).casefold()
        ][:5]
        thread_matches = [
            thread
            for thread in self.session_catalog.list_threads(limit=None, include_metadata=True)
            if lowered in f"{thread.display_name} {thread.session_id} {thread.cwd or ''}".casefold()
        ][:7]
        if not project_matches and not thread_matches:
            return f"没有找到和“{query}”相关的项目或对话。"
        lines = [f"搜索结果：{query}"]
        if project_matches:
            lines.append("项目：")
            for project in project_matches:
                lines.append(f"- {project.name}\n  {project.path}")
        if thread_matches:
            lines.append("对话：")
            for thread in thread_matches:
                lines.append(f"- {thread.display_name} [{thread.session_id[:8]}]")
        return "\n".join(lines)

    async def search_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        query = " ".join(context.args).strip()
        if not query:
            await update.effective_message.reply_text("请发送 /search <关键词>，我会搜索项目名、路径和对话标题。")
            return
        await update.effective_message.reply_text(
            self._search_results_text(settings, query),
            reply_markup=self._search_results_keyboard(settings, query),
        )

    def _toggle_current_favorite(self, settings: ChatSettings) -> str:
        if settings.active_session_id and settings.active_thread_cwd:
            favorites = self.state.thread_favorites(settings.chat_id)
            enabled = settings.active_session_id not in favorites
            self.state.set_thread_favorite(
                settings.chat_id,
                session_id=settings.active_session_id,
                thread_name=settings.active_thread_name or settings.active_session_id[:8],
                thread_cwd=settings.active_thread_cwd,
                favorite=enabled,
            )
            return f"已{'收藏' if enabled else '取消收藏'}当前对话。"
        path = self._selected_project_path(settings)
        name = settings.active_project_name or self._project_display_name(path)
        preferences = self.state.project_preferences(settings.chat_id)
        enabled = not bool(preferences.get(str(path), {}).get("favorite"))
        self.state.set_project_favorite(settings.chat_id, path, name, enabled)
        return f"已{'收藏' if enabled else '取消收藏'}当前项目：{name}"

    async def favorite_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        await update.effective_message.reply_text(self._toggle_current_favorite(settings), reply_markup=self._menu_keyboard())

    async def threads_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        text = self._threads_text(settings)
        await update.effective_message.reply_text(
            text,
            reply_markup=self._threads_keyboard(settings),
        )

    async def thread_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            keyboard = self._threads_keyboard(settings)
            await update.effective_message.reply_text(
                f"当前对话：{self._current_thread_summary(settings)}",
                reply_markup=keyboard,
            )
            return
        query = " ".join(context.args).strip()
        if query.lower() in {"clear", "none", "new"}:
            settings.active_session_id = None
            settings.active_thread_name = None
            settings.active_thread_cwd = None
            self.state.update_chat_settings(settings)
            await update.effective_message.reply_text("已清空对话绑定，下一条消息会在当前项目里开始一个新对话。")
            return
        try:
            thread = self.session_catalog.resolve_thread(query)
        except AmbiguousThreadError as exc:
            choices = "\n".join(f"- {item.display_name} [{item.session_id[:8]}]" for item in exc.matches)
            await update.effective_message.reply_text(f"匹配到多个对话：\n{choices}")
            return
        except ThreadLookupError:
            await update.effective_message.reply_text(f"没有找到匹配的本地 Codex 对话：{query}")
            return
        if thread.cwd is None:
            await update.effective_message.reply_text("找到了这个对话，但无法读取它原始的工作目录。")
            return
        if not thread.cwd.exists():
            await update.effective_message.reply_text(f"找到了这个对话，但它的工作目录已经不存在：{thread.cwd}")
            return
        settings.active_session_id = thread.session_id
        settings.active_thread_name = thread.display_name
        settings.active_thread_cwd = thread.cwd
        settings.active_project_name = self._project_display_name(thread.cwd)
        settings.active_project_cwd = thread.cwd
        workspace = self._workspace_for_path(thread.cwd)
        if workspace is not None:
            settings.workspace_name = workspace.name
        self.state.update_chat_settings(settings)
        self.state.record_project_usage(settings.chat_id, thread.cwd, settings.active_project_name)
        self._ensure_worker(thread.display_name, thread.cwd)
        await update.effective_message.reply_text(f"已切换到对话“{thread.display_name}”。\n执行目录：{thread.cwd}")

    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前仅 Telegram 生效的模型：{settings.model}\n模型列表来自当前 Codex 本地模型目录。",
                reply_markup=self._model_keyboard(settings.model),
            )
            return
        model = self._model_option(context.args[0])
        if model is None:
            await update.effective_message.reply_text("这个模型不在当前 Codex 模型目录里。请用 /model 查看可选模型。")
            return
        settings.model = model.slug
        settings.reasoning_effort = self._normalize_effort_for_model(model.slug, settings.reasoning_effort)
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(
            f"仅 Telegram 生效的模型已切换为：{settings.model}\n推理精度：{settings.reasoning_effort}"
        )

    async def effort_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前仅 Telegram 生效的推理精度：{settings.reasoning_effort}",
                reply_markup=self._effort_keyboard(settings.reasoning_effort, settings.model),
            )
            return
        effort = context.args[0].lower()
        choices = self._effort_choices(settings.model)
        if effort not in choices:
            await update.effective_message.reply_text(f"当前模型支持的推理精度是：{', '.join(choices)}。")
            return
        settings.reasoning_effort = effort
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(f"仅 Telegram 生效的推理精度已切换为：{effort}")

    async def plan_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前仅 Telegram 生效的计划模式：{'开启' if settings.plan_mode else '关闭'}",
                reply_markup=self._plan_keyboard(settings.plan_mode),
            )
            return
        choice = context.args[0].lower()
        if choice not in {"on", "off"}:
            await update.effective_message.reply_text("计划模式只能是：on 或 off。")
            return
        settings.plan_mode = choice == "on"
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(
            f"仅 Telegram 生效的计划模式已切换为：{'开启' if settings.plan_mode else '关闭'}"
        )

    async def voiceconfirm_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"语音确认模式：{'开启' if settings.voice_confirm_mode else '关闭'}",
                reply_markup=self._voice_confirm_keyboard(settings.voice_confirm_mode),
            )
            return
        choice = context.args[0].lower()
        if choice not in {"on", "off"}:
            await update.effective_message.reply_text("语音确认模式只能是：on 或 off。")
            return
        settings.voice_confirm_mode = choice == "on"
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(f"语音确认模式已{'开启' if settings.voice_confirm_mode else '关闭'}。")

    def _build_task_input(
        self,
        *,
        chat_id: int,
        settings: ChatSettings,
        prompt: str,
        image_paths: list[Path] | None = None,
        file_paths: list[Path] | None = None,
    ) -> tuple[TaskInput, ResolvedChatTarget]:
        target = self._resolve_target(settings)
        self._ensure_worker(target.context_label, target.path)
        task = TaskInput(
            prompt=prompt,
            workspace_name=settings.workspace_name,
            workspace_path=target.path,
            chat_id=chat_id,
            model=settings.model,
            reasoning_effort=self._normalize_effort_for_model(settings.model, settings.reasoning_effort),
            plan_mode=settings.plan_mode,
            thread_name=target.thread_name,
            image_paths=image_paths or [],
            file_paths=file_paths or [],
        )
        return task, target

    def _task_session_id(self, job: QueuedTask) -> str | None:
        if job.settings_snapshot.active_session_id:
            return job.settings_snapshot.active_session_id
        latest = self._chat_settings(job.task.chat_id)
        if latest.active_thread_name or latest.active_thread_cwd:
            return None
        if latest.workspace_name != job.settings_snapshot.workspace_name:
            return None
        if latest.active_project_cwd != job.settings_snapshot.active_project_cwd:
            return None
        return latest.active_session_id

    async def new_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        settings.active_session_id = None
        settings.active_thread_name = None
        settings.active_thread_cwd = None
        self.state.update_chat_settings(settings)
        self.state.set_session_id(settings.workspace_name, None)
        await update.effective_message.reply_text("已新建对话。下一条消息会在当前项目里启动新的 Codex 会话。")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        target = self._resolve_target(settings)
        worker = self._ensure_worker(target.context_label, target.path)
        if worker.active_process is None:
            await update.effective_message.reply_text("当前 Telegram 对话没有正在运行的任务。")
            return
        worker.active_process.terminate()
        await update.effective_message.reply_text("已向当前 Telegram 对话发送停止信号。")

    async def callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not await self._ensure_allowed(update):
            await query.answer()
            return
        await query.answer()
        action, _, value = (query.data or "").partition(":")
        settings = self._chat_settings(update.effective_chat.id)
        if action == "menu":
            if value == "main":
                await self._edit_callback_message(query, self._status_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "status":
                await self._edit_callback_message(query, self._status_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "doctor":
                await self._edit_callback_message(query, self._doctor_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "logs":
                await self._edit_callback_message(query, self._logs_text(), reply_markup=self._menu_keyboard())
                return
            if value == "tasks":
                await self._edit_callback_message(query, self._recent_tasks_text(update.effective_chat.id), reply_markup=self._menu_keyboard())
                return
            if value == "threads":
                await self._edit_callback_message(
                    query,
                    self._threads_text(settings),
                    reply_markup=self._with_back_button(self._threads_keyboard(settings)),
                )
                return
            if value in {"projects", "workspaces"}:
                await self._edit_callback_message(
                    query,
                    self._projects_text(settings),
                    reply_markup=self._with_back_button(self._projects_keyboard(settings)),
                )
                return
            if value == "search":
                await self._edit_callback_message(
                    query,
                    "请发送 /search <关键词>。\n例如：/search 新媒体矩阵",
                    reply_markup=self._menu_keyboard(),
                )
                return
            if value == "model":
                await self._edit_callback_message(
                    query,
                    f"当前仅 Telegram 生效的模型：{settings.model}",
                    reply_markup=self._with_back_button(self._model_keyboard(settings.model)),
                )
                return
            if value == "effort":
                await self._edit_callback_message(
                    query,
                    f"当前仅 Telegram 生效的推理精度：{settings.reasoning_effort}",
                    reply_markup=self._with_back_button(self._effort_keyboard(settings.reasoning_effort, settings.model)),
                )
                return
            if value == "plan":
                await self._edit_callback_message(
                    query,
                    f"当前仅 Telegram 生效的计划模式：{'开启' if settings.plan_mode else '关闭'}",
                    reply_markup=self._with_back_button(self._plan_keyboard(settings.plan_mode)),
                )
                return
            if value == "voiceconfirm":
                await self._edit_callback_message(
                    query,
                    f"语音确认模式：{'开启' if settings.voice_confirm_mode else '关闭'}",
                    reply_markup=self._with_back_button(self._voice_confirm_keyboard(settings.voice_confirm_mode)),
                )
                return
            if value == "new":
                settings.active_session_id = None
                settings.active_thread_name = None
                settings.active_thread_cwd = None
                self.state.update_chat_settings(settings)
                self.state.set_session_id(settings.workspace_name, None)
                await self._edit_callback_message(
                    query,
                    "已新建对话。下一条消息会在当前项目里启动新的 Codex 会话。",
                    reply_markup=self._menu_keyboard(),
                )
                return
            if value == "stop":
                target = self._resolve_target(settings)
                worker = self._ensure_worker(target.context_label, target.path)
                if worker.active_process is None:
                    await self._edit_callback_message(query, "当前 Telegram 对话没有正在运行的任务。", reply_markup=self._menu_keyboard())
                    return
                worker.active_process.terminate()
                await self._edit_callback_message(query, "已向当前 Telegram 对话发送停止信号。", reply_markup=self._menu_keyboard())
                return
        if action == "project":
            if value == "clear":
                self._clear_project_selection(settings)
                self.state.update_chat_settings(settings)
                await self._edit_callback_message(query, "已清空项目选择，回到默认工作区。", reply_markup=self._menu_keyboard())
                return
            path = self._resolve_project_token(value)
            if path is None:
                await self._edit_callback_message(query, "这个项目按钮已经过期，请重新打开项目列表。", reply_markup=self._menu_keyboard())
                return
            if not path.exists():
                await self._edit_callback_message(query, f"这个项目路径不存在：{path}", reply_markup=self._menu_keyboard())
                return
            self._select_project(settings, path)
            self.state.update_chat_settings(settings)
            self._ensure_worker(settings.active_project_name or self._project_display_name(path), path)
            await self._edit_callback_message(
                query,
                f"已切换到项目：{settings.active_project_name}\n路径：{path}\n现在可以选择这个项目下的对话。",
                reply_markup=self._with_back_button(self._threads_keyboard(settings)),
            )
            return
        if action == "favproject":
            path = self._resolve_project_token(value)
            if path is None:
                await self._edit_callback_message(query, "这个项目按钮已经过期，请重新打开项目列表。", reply_markup=self._menu_keyboard())
                return
            name = self._project_display_name(path)
            preferences = self.state.project_preferences(settings.chat_id)
            enabled = not bool(preferences.get(str(path), {}).get("favorite"))
            self.state.set_project_favorite(settings.chat_id, path, name, enabled)
            await self._edit_callback_message(
                query,
                f"已{'收藏' if enabled else '取消收藏'}项目：{name}",
                reply_markup=self._with_back_button(self._projects_keyboard(settings)),
            )
            return
        if action == "workspace":
            try:
                workspace = self.config.ensure_workspace(value)
            except KeyError:
                return
            self._select_project(settings, workspace.path)
            self.state.update_chat_settings(settings)
            await self._edit_callback_message(
                query,
                f"已切换到项目 {value}，并清空当前对话绑定。",
                reply_markup=self._with_back_button(self._projects_keyboard(settings)),
            )
            return
        if action == "thread":
            if value == "clear":
                settings.active_session_id = None
                settings.active_thread_name = None
                settings.active_thread_cwd = None
                self.state.update_chat_settings(settings)
                await self._edit_callback_message(query, "已清空当前对话绑定。", reply_markup=self._menu_keyboard())
                return
            try:
                thread = self.session_catalog.resolve_thread(value)
            except ThreadLookupError:
                await self._edit_callback_message(query, "这个已保存的 Codex 对话现在不可用了。", reply_markup=self._menu_keyboard())
                return
            if thread.cwd is None or not thread.cwd.exists():
                await self._edit_callback_message(
                    query,
                    "这个已保存的 Codex 对话仍在，但它原始的工作目录不可用。",
                    reply_markup=self._menu_keyboard(),
                )
                return
            settings.active_session_id = thread.session_id
            settings.active_thread_name = thread.display_name
            settings.active_thread_cwd = thread.cwd
            settings.active_project_name = self._project_display_name(thread.cwd)
            settings.active_project_cwd = thread.cwd
            workspace = self._workspace_for_path(thread.cwd)
            if workspace is not None:
                settings.workspace_name = workspace.name
            self.state.update_chat_settings(settings)
            self.state.record_project_usage(settings.chat_id, thread.cwd, settings.active_project_name)
            self._ensure_worker(thread.display_name, thread.cwd)
            await self._edit_callback_message(
                query,
                f"已切换到对话“{thread.display_name}”。\n执行目录：{thread.cwd}",
                reply_markup=self._with_back_button(self._threads_keyboard(settings)),
            )
            return
        if action == "favthread":
            try:
                thread = self.session_catalog.resolve_thread(value)
            except ThreadLookupError:
                await self._edit_callback_message(query, "这个 Codex 对话现在不可用了。", reply_markup=self._menu_keyboard())
                return
            if thread.cwd is None:
                await self._edit_callback_message(query, "这个对话没有可用的工作目录，暂时不能收藏。", reply_markup=self._menu_keyboard())
                return
            favorites = self.state.thread_favorites(settings.chat_id)
            enabled = thread.session_id not in favorites
            self.state.set_thread_favorite(
                settings.chat_id,
                session_id=thread.session_id,
                thread_name=thread.display_name,
                thread_cwd=thread.cwd,
                favorite=enabled,
            )
            await self._edit_callback_message(
                query,
                f"已{'收藏' if enabled else '取消收藏'}对话：{thread.display_name}",
                reply_markup=self._with_back_button(self._threads_keyboard(settings)),
            )
            return
        if action == "model":
            model = self._model_option(value)
            if model is None:
                await self._edit_callback_message(query, "这个模型不在当前 Codex 模型目录里。", reply_markup=self._menu_keyboard())
                return
            settings.model = value
            settings.reasoning_effort = self._normalize_effort_for_model(value, settings.reasoning_effort)
            self.state.update_chat_settings(settings)
            await self._edit_callback_message(
                query,
                f"仅 Telegram 生效的模型已切换为 {value}\n推理精度：{settings.reasoning_effort}",
                reply_markup=self._with_back_button(self._model_keyboard(value)),
            )
            return
        if action == "effort" and value in self._effort_choices(settings.model):
            settings.reasoning_effort = value
            self.state.update_chat_settings(settings)
            await self._edit_callback_message(
                query,
                f"仅 Telegram 生效的推理精度已切换为 {value}",
                reply_markup=self._with_back_button(self._effort_keyboard(value, settings.model)),
            )
            return
        if action == "plan" and value in {"on", "off"}:
            settings.plan_mode = value == "on"
            self.state.update_chat_settings(settings)
            await self._edit_callback_message(
                query,
                f"仅 Telegram 生效的计划模式已切换为 {'开启' if settings.plan_mode else '关闭'}",
                reply_markup=self._with_back_button(self._plan_keyboard(settings.plan_mode)),
            )
            return
        if action == "voiceconfirm" and value in {"on", "off"}:
            settings.voice_confirm_mode = value == "on"
            self.state.update_chat_settings(settings)
            await self._edit_callback_message(
                query,
                f"语音确认模式已{'开启' if settings.voice_confirm_mode else '关闭'}。",
                reply_markup=self._with_back_button(self._voice_confirm_keyboard(settings.voice_confirm_mode)),
            )
            return
        if action == "favorite" and value == "current":
            await self._edit_callback_message(query, self._toggle_current_favorite(settings), reply_markup=self._menu_keyboard())
            return
        if action == "task":
            task_action, _, task_id = value.partition(":")
            task = self.recent_tasks.get(task_id)
            if task is None:
                await self._edit_callback_message(query, "这个任务记录已经不在内存里了，可以用 /tasks 查看历史摘要。", reply_markup=self._menu_keyboard())
                return
            worker = self._ensure_worker(task.context_label, task.task.workspace_path)
            if task_action == "details":
                await self._edit_callback_message(query, self._task_details_text(task), reply_markup=self._task_keyboard(task, "已完成"))
                return
            if task_action == "cancel":
                task.cancelled = True
                if worker.active_job is task and worker.active_process is not None:
                    worker.active_process.terminate()
                await self._edit_callback_message(query, f"已请求取消任务：{task.task_id}", reply_markup=self._menu_keyboard())
                return
            if task_action == "rerun":
                rerun = QueuedTask(
                    task=replace(task.task, dangerous=False),
                    settings_snapshot=task.settings_snapshot,
                    worker_key=task.worker_key,
                    context_label=task.context_label,
                    reply_to_message_id=query.message.message_id if query.message else task.reply_to_message_id,
                    source_description=task.source_description,
                )
                await self._enqueue_task(rerun, task.task.chat_id)
                await self._edit_callback_message(query, f"已重新加入队列：{rerun.task_id}", reply_markup=self._menu_keyboard())
                return
        if action == "voice":
            voice_action, _, pending_id = value.partition(":")
            pending = self.state.get_pending_voice(update.effective_chat.id, pending_id)
            if pending is None:
                await self._edit_callback_message(query, "这条语音确认已经过期或被处理。", reply_markup=self._menu_keyboard())
                return
            if voice_action == "cancel":
                self.state.clear_pending_voice(update.effective_chat.id)
                await self._edit_callback_message(query, "已取消这条语音任务。", reply_markup=self._menu_keyboard())
                return
            if voice_action == "send":
                settings = self._chat_settings(update.effective_chat.id)
                task, target = self._build_task_input(
                    chat_id=update.effective_chat.id,
                    settings=settings,
                    prompt=pending["transcript"],
                    file_paths=[Path(pending["local_path"])],
                )
                self.state.clear_pending_voice(update.effective_chat.id)
                await self._enqueue_task(
                    QueuedTask(
                        task=task,
                        settings_snapshot=settings,
                        worker_key=target.worker_key,
                        context_label=target.context_label,
                        reply_to_message_id=query.message.message_id if query.message else None,
                        source_description="voice",
                    ),
                    update.effective_chat.id,
                )
                await self._edit_callback_message(query, "已确认语音转写，并发送给 Codex。", reply_markup=self._menu_keyboard())
                return
        if action == "approve" and value in self.pending_approvals:
            task = self.pending_approvals.pop(value).rerun_dangerous()
            if query.message is not None:
                task.status_message_id = query.message.message_id
            task.source_description = "approval"
            await self._enqueue_task(task, query.message.chat_id)
            return
        if action == "reject" and value in self.pending_approvals:
            task = self.pending_approvals.pop(value)
            if query.message is not None:
                task.status_message_id = query.message.message_id
            worker = self._ensure_worker(task.context_label, task.task.workspace_path)
            self.state.add_task(
                chat_id=task.task.chat_id,
                workspace_name=task.context_label,
                prompt=task.task.prompt,
                status="failed",
                dangerous=task.task.dangerous,
            )
            await self._update_task_status_message(task, worker, "已失败", detail="你拒绝了提权请求，任务已取消。")
            return

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not self._is_directed_to_bot(update):
            return
        message = update.effective_message
        settings = self._chat_settings(update.effective_chat.id)
        text = self._strip_mention(message.text or "")
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=text,
        )
        await self._enqueue_task(
            QueuedTask(
                task=task,
                settings_snapshot=settings,
                worker_key=target.worker_key,
                context_label=target.context_label,
                reply_to_message_id=message.message_id,
                source_description="text",
            ),
            update.effective_chat.id,
        )

    async def photo_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not self._is_directed_to_bot(update):
            return
        message = update.effective_message
        settings = self._chat_settings(update.effective_chat.id)
        image_paths: list[Path] = []
        file_paths: list[Path] = []
        if message.photo:
            telegram_file = await message.photo[-1].get_file()
            local_path = await self._download_file(
                telegram_file,
                suffix=".jpg",
                chat_id=update.effective_chat.id,
                media_kind="photo",
            )
            image_paths.append(local_path)
            file_paths.append(local_path)
        elif message.document:
            local_path = await self._download_document(message.document, update.effective_chat.id, "image-document")
            image_paths.append(local_path)
            file_paths.append(local_path)
        prompt = self._strip_mention(message.caption or "") or "Please inspect the attached image."
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=prompt,
            image_paths=image_paths,
            file_paths=file_paths,
        )
        await self._enqueue_task(
            QueuedTask(
                task=task,
                settings_snapshot=settings,
                worker_key=target.worker_key,
                context_label=target.context_label,
                reply_to_message_id=message.message_id,
                source_description="image",
            ),
            update.effective_chat.id,
        )

    async def document_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not self._is_directed_to_bot(update):
            return
        message = update.effective_message
        settings = self._chat_settings(update.effective_chat.id)
        local_path = await self._download_document(message.document, update.effective_chat.id, "document")
        prompt = self._strip_mention(message.caption or "") or "Please use the staged document if relevant."
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=prompt,
            file_paths=[local_path],
        )
        await self._enqueue_task(
            QueuedTask(
                task=task,
                settings_snapshot=settings,
                worker_key=target.worker_key,
                context_label=target.context_label,
                reply_to_message_id=message.message_id,
                source_description="document",
            ),
            update.effective_chat.id,
        )

    async def voice_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not self._is_directed_to_bot(update):
            return
        message = update.effective_message
        assert message.voice is not None
        await message.reply_text("已收到语音，正在用本地 Whisper 转写...")
        local_path = await self._download_file(
            await message.voice.get_file(),
            suffix=".ogg",
            chat_id=update.effective_chat.id,
            media_kind="voice",
        )
        transcript = await self.transcriber.transcribe(local_path)
        settings = self._chat_settings(update.effective_chat.id)
        if settings.voice_confirm_mode:
            pending_id = uuid.uuid4().hex[:10]
            self.state.set_pending_voice(update.effective_chat.id, pending_id, transcript, local_path)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("发送给 Codex", callback_data=f"voice:send:{pending_id}"),
                        InlineKeyboardButton("取消", callback_data=f"voice:cancel:{pending_id}"),
                    ]
                ]
            )
            await message.reply_text(f"语音转写结果：\n{transcript}\n\n确认后我再交给 Codex。", reply_markup=keyboard)
            return
        await self._enqueue_voice_transcript(update, message, settings, transcript, local_path)

    async def _enqueue_voice_transcript(
        self,
        update: Update,
        message,
        settings: ChatSettings,
        transcript: str,
        local_path: Path,
    ) -> None:
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=transcript,
            file_paths=[local_path],
        )
        await message.reply_text(f"语音转写结果：{transcript}")
        await self._enqueue_task(
            QueuedTask(
                task=task,
                settings_snapshot=settings,
                worker_key=target.worker_key,
                context_label=target.context_label,
                reply_to_message_id=message.message_id,
                source_description="voice",
            ),
            update.effective_chat.id,
        )

    async def video_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        if not self._is_directed_to_bot(update):
            return
        message = update.effective_message
        settings = self._chat_settings(update.effective_chat.id)
        if message.video:
            telegram_file = await message.video.get_file()
            local_path = await self._download_file(
                telegram_file,
                suffix=".mp4",
                chat_id=update.effective_chat.id,
                media_kind="video",
            )
        elif message.document:
            local_path = await self._download_document(message.document, update.effective_chat.id, "video-document")
        else:
            await update.effective_message.reply_text("这条视频消息没有拿到可下载的文件。")
            return
        prompt = self._strip_mention(message.caption or "") or (
            "Please use the staged video file if relevant. "
            "You can inspect it locally with ffmpeg or extract frames/audio when needed."
        )
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=prompt,
            file_paths=[local_path],
        )
        await self._enqueue_task(
            QueuedTask(
                task=task,
                settings_snapshot=settings,
                worker_key=target.worker_key,
                context_label=target.context_label,
                reply_to_message_id=message.message_id,
                source_description="video",
            ),
            update.effective_chat.id,
        )

    async def _download_document(self, document, chat_id: int, media_kind: str) -> Path:
        suffix = Path(document.file_name or "document.bin").suffix or ".bin"
        local_path = await self._download_file(await document.get_file(), suffix=suffix, chat_id=chat_id, media_kind=media_kind)
        return local_path

    async def _download_file(self, telegram_file, *, suffix: str, chat_id: int, media_kind: str) -> Path:
        target_dir = self.config.downloads_dir / str(chat_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid.uuid4().hex}{suffix}"
        await telegram_file.download_to_drive(custom_path=str(target_path))
        self.state.add_media(chat_id, telegram_file.file_id, media_kind, str(target_path))
        return target_path

    async def _enqueue_task(self, queued_task: QueuedTask, chat_id: int) -> None:
        worker = self._ensure_worker(queued_task.context_label, queued_task.task.workspace_path)
        self.recent_tasks[queued_task.task_id] = queued_task
        if len(self.recent_tasks) > 100:
            oldest = sorted(self.recent_tasks.values(), key=lambda task: task.created_at)[:20]
            for task in oldest:
                self.recent_tasks.pop(task.task_id, None)
        await worker.queue.put(queued_task)
        self.state.add_task(
            chat_id=chat_id,
            workspace_name=queued_task.context_label,
            prompt=queued_task.task.prompt,
            status="queued",
            dangerous=queued_task.task.dangerous,
        )
        await self._update_task_status_message(queued_task, worker, "排队中")

    async def _workspace_loop(self, worker: WorkspaceWorker) -> None:
        while True:
            job = await worker.queue.get()
            worker.active_job = job
            typing_task = asyncio.create_task(self._typing_loop(job.task.chat_id))
            try:
                if job.cancelled:
                    self.state.add_task(
                        chat_id=job.task.chat_id,
                        workspace_name=job.context_label,
                        prompt=job.task.prompt,
                        status="cancelled",
                        dangerous=job.task.dangerous,
                    )
                    await self._update_task_status_message(job, worker, "已取消")
                    continue
                await self._run_job(worker, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Workspace job failed", extra={"workspace": worker.name})
                self.state.add_task(
                    chat_id=job.task.chat_id,
                    workspace_name=job.context_label,
                    prompt=job.task.prompt,
                    status="failed",
                    dangerous=job.task.dangerous,
                )
                await self._update_task_status_message(job, worker, "已失败", detail="桥接器在执行任务时遇到了意外错误。")
            finally:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
                worker.active_process = None
                worker.active_job = None
                worker.last_progress_message = None
                worker.queue.task_done()

    async def _run_job(self, worker: WorkspaceWorker, job: QueuedTask) -> None:
        assert self.application is not None
        bot = self.application.bot
        chat_id = job.task.chat_id
        session_id = self._task_session_id(job)
        final_messages: list[str] = []
        approval_triggered = False
        await self._update_task_status_message(job, worker, "执行中")

        async def on_event(event: CodexEvent) -> None:
            nonlocal approval_triggered
            if event.kind == "session_started":
                self.state.set_active_session(chat_id, event.payload["session_id"])
                return
            if event.kind == "turn_started":
                return
            if event.kind == "command_started":
                command = event.payload["command"].strip()
                summary = command[:180]
                if summary != worker.last_progress_message:
                    worker.last_progress_message = summary
                    await self._update_task_status_message(job, worker, "执行中", detail=summary)
                return
            if event.kind == "command_completed":
                return
            if event.kind == "agent_message":
                text = event.payload.get("text", "").strip()
                if text:
                    final_messages.append(text)
                    await bot.send_message(chat_id=chat_id, text=text, reply_to_message_id=job.reply_to_message_id)
                return
            if event.kind == "approval_requested":
                approval_triggered = True
                approval_id = uuid.uuid4().hex[:10]
                self.pending_approvals[approval_id] = job
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("批准", callback_data=f"approve:{approval_id}"),
                            InlineKeyboardButton("拒绝", callback_data=f"reject:{approval_id}"),
                        ]
                    ]
                )
                await self._update_task_status_message(
                    job,
                    worker,
                    "等待批准",
                    detail="Codex 请求提权执行。批准后会按高权限重新运行这次任务。",
                    reply_markup=keyboard,
                )
                if worker.active_process:
                    worker.active_process.terminate()
                return
            if event.kind == "stderr":
                message = event.payload.get("message", "")
                if "WARN codex_core::shell_snapshot" not in message:
                    LOGGER.warning("codex stderr: %s", message)

        def on_process_started(process: asyncio.subprocess.Process) -> None:
            worker.active_process = process

        returncode = await self.codex.stream_task(job.task, session_id, on_event=on_event, on_process_started=on_process_started)
        if job.cancelled:
            self.state.add_task(
                chat_id=chat_id,
                workspace_name=job.context_label,
                prompt=job.task.prompt,
                status="cancelled",
                dangerous=job.task.dangerous,
            )
            await self._update_task_status_message(job, worker, "已取消")
            return
        if approval_triggered:
            self.state.add_task(
                chat_id=chat_id,
                workspace_name=job.context_label,
                prompt=job.task.prompt,
                status="approval_requested",
                dangerous=job.task.dangerous,
            )
            return
        if returncode == 0:
            self.state.add_task(
                chat_id=chat_id,
                workspace_name=job.context_label,
                prompt=job.task.prompt,
                status="completed",
                dangerous=job.task.dangerous,
            )
            await self._update_task_status_message(job, worker, "已完成")
            await self._send_detected_files(chat_id, "\n".join(final_messages), job.reply_to_message_id)
            return
        self.state.add_task(
            chat_id=chat_id,
            workspace_name=job.context_label,
            prompt=job.task.prompt,
            status="failed",
            dangerous=job.task.dangerous,
        )
        await self._update_task_status_message(job, worker, "已失败", detail=f"Codex 退出状态码：{returncode}")

    async def _send_detected_files(self, chat_id: int, text: str, reply_to_message_id: int | None) -> None:
        assert self.application is not None
        bot = self.application.bot
        sent_paths: set[Path] = set()
        for path in self._detect_existing_paths(text):
            if not path.is_absolute() or not path.exists() or path in sent_paths or not path.is_file():
                continue
            sent_paths.add(path)
            mime, _ = mimetypes.guess_type(path.name)
            if mime and mime.startswith("image/"):
                with path.open("rb") as handle:
                    await bot.send_photo(chat_id=chat_id, photo=handle, reply_to_message_id=reply_to_message_id)
            elif mime and mime.startswith("video/"):
                with path.open("rb") as handle:
                    await bot.send_video(chat_id=chat_id, video=handle, reply_to_message_id=reply_to_message_id)
            else:
                with path.open("rb") as handle:
                    await bot.send_document(chat_id=chat_id, document=handle, reply_to_message_id=reply_to_message_id)
            if len(sent_paths) >= 5:
                break

    def _detect_existing_paths(self, text: str) -> list[Path]:
        paths: list[Path] = []
        seen: set[Path] = set()

        def add_path(path: Path) -> None:
            if path.is_absolute() and path.exists() and path not in seen:
                seen.add(path)
                paths.append(path)

        for match in FILE_PATH_PATTERN.findall(text):
            add_path(Path(match))

        for line in text.splitlines():
            for match in re.finditer(r"/", line):
                start = match.start()
                if start > 0 and not line[start - 1].isspace() and line[start - 1] not in "([`\"':：":
                    continue
                path = self._existing_path_from_tail(line[start:])
                if path is not None:
                    add_path(path)
        return paths

    def _existing_path_from_tail(self, tail: str) -> Path | None:
        candidate = tail.strip().strip("`\"'")
        for separator in ("，", "。", "；"):
            candidate = candidate.split(separator, 1)[0]
        while candidate:
            candidate = candidate.rstrip("`\"'.,;:：，。；)]}")
            path = Path(candidate)
            if path.exists():
                return path.resolve()
            parts = candidate.rsplit(maxsplit=1)
            if len(parts) == 1:
                return None
            candidate = parts[0]
        return None

    async def _typing_loop(self, chat_id: int) -> None:
        assert self.application is not None
        bot = self.application.bot
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
                await asyncio.sleep(4)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.debug("Typing indicator failed", exc_info=True)
                await asyncio.sleep(4)

    def _strip_mention(self, text: str) -> str:
        if self.bot_username:
            return text.replace(f"@{self.bot_username}", "").strip()
        return text.strip()
