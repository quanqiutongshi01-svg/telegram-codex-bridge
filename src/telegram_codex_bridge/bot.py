from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import mimetypes
from pathlib import Path
import re
import shutil
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
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .codex import CodexEvent, CodexRunner, TaskInput
from .config import BridgeConfig
from .sessions import AmbiguousThreadError, SessionCatalog, ThreadLookupError
from .state import ChatSettings, StateStore
from .transcribe import WhisperTranscriber


LOGGER = logging.getLogger(__name__)
EFFORT_CHOICES = ("minimal", "low", "medium", "high")
FILE_PATH_PATTERN = re.compile(r"(/[^ \n\r\t\]\)\"']+)")


@dataclass(slots=True)
class QueuedTask:
    task: TaskInput
    settings_snapshot: ChatSettings
    worker_key: str
    context_label: str
    reply_to_message_id: int | None
    source_description: str

    def rerun_dangerous(self) -> "QueuedTask":
        return QueuedTask(
            task=replace(self.task, dangerous=True),
            settings_snapshot=self.settings_snapshot,
            worker_key=self.worker_key,
            context_label=self.context_label,
            reply_to_message_id=self.reply_to_message_id,
            source_description=self.source_description,
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
            BotCommand("threads", "查看最近线程"),
            BotCommand("thread", "切换到指定线程"),
            BotCommand("workspaces", "查看工作区"),
            BotCommand("workspace", "切换工作区"),
            BotCommand("model", "查看或切换模型"),
            BotCommand("effort", "查看或切换推理精度"),
            BotCommand("plan", "查看或切换计划模式"),
            BotCommand("new", "新建一个 Telegram 线程"),
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
        application.add_handler(CommandHandler("workspaces", self.workspaces_command))
        application.add_handler(CommandHandler("workspace", self.workspace_command))
        application.add_handler(CommandHandler("threads", self.threads_command))
        application.add_handler(CommandHandler("thread", self.thread_command))
        application.add_handler(CommandHandler("model", self.model_command))
        application.add_handler(CommandHandler("effort", self.effort_command))
        application.add_handler(CommandHandler("plan", self.plan_command))
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
            await update.effective_message.reply_text("This chat is not authorized for the Telegram Codex Bridge.")
        return False

    def _chat_settings(self, chat_id: int) -> ChatSettings:
        return self.state.get_chat_settings(
            chat_id,
            default_workspace=self.config.default_workspace.name,
            default_model=self.config.default_model,
            default_effort=self.config.default_reasoning_effort,
            default_plan_mode=self.config.default_plan_mode,
        )

    def _worker_key(self, path: Path) -> str:
        return str(path.expanduser().resolve())

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
        context_label = settings.active_thread_name or settings.workspace_name
        return ResolvedChatTarget(
            workspace=settings.workspace_name,
            path=workspace.path,
            session_id=settings.active_session_id,
            thread_name=settings.active_thread_name,
            worker_key=self._worker_key(workspace.path),
            context_label=context_label,
        )

    def _current_thread_summary(self, settings: ChatSettings) -> str:
        if settings.active_thread_name:
            return settings.active_thread_name
        if settings.active_session_id:
            return f"Telegram 会话 {settings.active_session_id[:8]}"
        return "新的 Telegram 线程"

    def _threads_keyboard(self, current_session_id: str | None) -> InlineKeyboardMarkup | None:
        recent_threads = self.session_catalog.list_threads(limit=5)
        if not recent_threads:
            return None
        buttons = [
            [
                InlineKeyboardButton(
                    ("* " if thread.session_id == current_session_id else "") + thread.display_name[:48],
                    callback_data=f"thread:{thread.session_id}",
                )
            ]
            for thread in recent_threads
        ]
        buttons.append([InlineKeyboardButton("清空选择", callback_data="thread:clear")])
        return InlineKeyboardMarkup(buttons)

    def _menu_keyboard(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("状态", callback_data="menu:status"),
                    InlineKeyboardButton("自检", callback_data="menu:doctor"),
                ],
                [
                    InlineKeyboardButton("线程", callback_data="menu:threads"),
                    InlineKeyboardButton("工作区", callback_data="menu:workspaces"),
                ],
                [
                    InlineKeyboardButton("模型", callback_data="menu:model"),
                    InlineKeyboardButton("精度", callback_data="menu:effort"),
                ],
                [
                    InlineKeyboardButton("计划模式", callback_data="menu:plan"),
                    InlineKeyboardButton("新线程", callback_data="menu:new"),
                ],
                [InlineKeyboardButton("停止", callback_data="menu:stop")],
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
        for model in self.config.quick_models:
            label = ("* " if model == current_model else "") + model
            rows.append([InlineKeyboardButton(label, callback_data=f"model:{model}")])
        return InlineKeyboardMarkup(rows)

    def _effort_keyboard(self, current_effort: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(("* " if effort == current_effort else "") + effort, callback_data=f"effort:{effort}")]
                for effort in EFFORT_CHOICES
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
            f"工作区配置：{settings.workspace_name}\n"
            f"当前线程：{self._current_thread_summary(settings)}\n"
            f"执行目录：{target.path}\n"
            f"模型：{settings.model}\n"
            f"推理精度：{settings.reasoning_effort}\n"
            f"计划模式：{'开启' if settings.plan_mode else '关闭'}\n"
            f"排队任务：{worker.queue.qsize()}\n"
            f"运行中：{'是' if worker.active_process else '否'}"
        )

    def _doctor_text(self, settings: ChatSettings) -> str:
        target = self._resolve_target(settings)
        checks = [
            ("桥接服务", True, "bot 轮询中"),
            ("codex", self._binary_available(self.config.codex_binary), self.config.codex_binary),
            ("ffmpeg", self._binary_available(self.config.ffmpeg_binary), self.config.ffmpeg_binary),
            ("执行目录", target.path.exists(), str(target.path)),
            ("状态数据库", self.config.state_db_path.exists(), str(self.config.state_db_path)),
            ("会话索引", self.session_catalog.index_path.exists(), str(self.session_catalog.index_path)),
        ]
        lines = ["快速自检："]
        for label, ok, detail in checks:
            lines.append(f"- {'正常' if ok else '异常'} {label}：{detail}")
        return "\n".join(lines)

    def _threads_text(self, settings: ChatSettings) -> str:
        recent_threads = self.session_catalog.list_threads(limit=8)
        if not recent_threads:
            return "这台 Mac 上暂时还没有找到本地 Codex 线程。"
        lines = ["最近的 Codex 线程："]
        for thread in recent_threads:
            prefix = "* " if thread.session_id == settings.active_session_id else "- "
            lines.append(f"{prefix}{thread.display_name} [{thread.session_id[:8]}]")
        lines.append("")
        lines.append("可以发送 /thread <名称或ID>，也可以直接点下面的按钮。")
        return "\n".join(lines)

    def _binary_available(self, binary: str) -> bool:
        return shutil.which(binary) is not None or Path(binary).exists()

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        text = (
            "/menu - 打开 Telegram 控制面板\n"
            "/status - 查看当前工作状态\n"
            "/doctor - 快速自检桥接器运行状态\n"
            "/model [模型ID] - 查看或切换仅 Telegram 生效的模型\n"
            "/effort [minimal|low|medium|high] - 查看或切换仅 Telegram 生效的推理精度\n"
            "/plan [on|off] - 查看或切换仅 Telegram 生效的计划模式\n"
            "/workspaces - 查看已注册的工作区\n"
            "/workspace [名称] - 切换当前工作区\n"
            "/threads - 查看这台 Mac 上最近的 Codex 线程\n"
            "/thread [名称|ID|clear] - 切换到已有线程，或清空当前线程绑定\n"
            "/new - 新建一个 Telegram 线程\n"
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

    async def workspaces_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        names = ", ".join(workspace.name for workspace in self.config.workspaces)
        await update.effective_message.reply_text(
            f"已注册工作区：{names}",
            reply_markup=self._workspace_keyboard(settings.workspace_name),
        )

    async def workspace_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前工作区：{settings.workspace_name}",
                reply_markup=self._workspace_keyboard(settings.workspace_name),
            )
            return
        target = context.args[0]
        try:
            workspace = self.config.ensure_workspace(target)
        except KeyError:
            await update.effective_message.reply_text(f"未知工作区：{target}")
            return
        settings.workspace_name = workspace.name
        settings.active_session_id = None
        settings.active_thread_name = None
        settings.active_thread_cwd = None
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(f"已切换到工作区 {target}，并清空当前线程绑定。")

    async def threads_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        text = self._threads_text(settings)
        await update.effective_message.reply_text(
            text,
            reply_markup=self._threads_keyboard(settings.active_session_id),
        )

    async def thread_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            keyboard = self._threads_keyboard(settings.active_session_id)
            await update.effective_message.reply_text(
                f"当前线程：{self._current_thread_summary(settings)}",
                reply_markup=keyboard,
            )
            return
        query = " ".join(context.args).strip()
        if query.lower() in {"clear", "none", "new"}:
            settings.active_session_id = None
            settings.active_thread_name = None
            settings.active_thread_cwd = None
            self.state.update_chat_settings(settings)
            await update.effective_message.reply_text("已清空线程绑定，下一条消息会从新的 Telegram 线程开始。")
            return
        try:
            thread = self.session_catalog.resolve_thread(query)
        except AmbiguousThreadError as exc:
            choices = "\n".join(f"- {item.display_name} [{item.session_id[:8]}]" for item in exc.matches)
            await update.effective_message.reply_text(f"匹配到多个线程：\n{choices}")
            return
        except ThreadLookupError:
            await update.effective_message.reply_text(f"没有找到匹配的本地 Codex 线程：{query}")
            return
        if thread.cwd is None:
            await update.effective_message.reply_text("找到了这个线程，但无法读取它原始的工作目录。")
            return
        if not thread.cwd.exists():
            await update.effective_message.reply_text(f"找到了这个线程，但它的工作目录已经不存在：{thread.cwd}")
            return
        settings.active_session_id = thread.session_id
        settings.active_thread_name = thread.display_name
        settings.active_thread_cwd = thread.cwd
        self.state.update_chat_settings(settings)
        self._ensure_worker(thread.display_name, thread.cwd)
        await update.effective_message.reply_text(f"已切换到线程“{thread.display_name}”。\n执行目录：{thread.cwd}")

    async def model_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前仅 Telegram 生效的模型：{settings.model}",
                reply_markup=self._model_keyboard(settings.model),
            )
            return
        settings.model = context.args[0]
        self.state.update_chat_settings(settings)
        await update.effective_message.reply_text(f"仅 Telegram 生效的模型已切换为：{settings.model}")

    async def effort_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        if not context.args:
            await update.effective_message.reply_text(
                f"当前仅 Telegram 生效的推理精度：{settings.reasoning_effort}",
                reply_markup=self._effort_keyboard(settings.reasoning_effort),
            )
            return
        effort = context.args[0].lower()
        if effort not in EFFORT_CHOICES:
            await update.effective_message.reply_text("推理精度只能是：minimal、low、medium、high。")
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
            reasoning_effort=settings.reasoning_effort,
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
        await update.effective_message.reply_text(f"Cleared the active Telegram thread for workspace profile {settings.workspace_name}.")

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._ensure_allowed(update):
            return
        settings = self._chat_settings(update.effective_chat.id)
        target = self._resolve_target(settings)
        worker = self._ensure_worker(target.context_label, target.path)
        if worker.active_process is None:
            await update.effective_message.reply_text("No active task is running for the current Telegram thread.")
            return
        worker.active_process.terminate()
        await update.effective_message.reply_text("Stop signal sent to the active Telegram thread.")

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
                await query.edit_message_text(self._status_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "status":
                await query.edit_message_text(self._status_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "doctor":
                await query.edit_message_text(self._doctor_text(settings), reply_markup=self._menu_keyboard())
                return
            if value == "threads":
                await query.edit_message_text(
                    self._threads_text(settings),
                    reply_markup=self._with_back_button(self._threads_keyboard(settings.active_session_id)),
                )
                return
            if value == "workspaces":
                await query.edit_message_text(
                    f"已注册工作区：{', '.join(workspace.name for workspace in self.config.workspaces)}",
                    reply_markup=self._with_back_button(self._workspace_keyboard(settings.workspace_name)),
                )
                return
            if value == "model":
                await query.edit_message_text(
                    f"当前仅 Telegram 生效的模型：{settings.model}",
                    reply_markup=self._with_back_button(self._model_keyboard(settings.model)),
                )
                return
            if value == "effort":
                await query.edit_message_text(
                    f"当前仅 Telegram 生效的推理精度：{settings.reasoning_effort}",
                    reply_markup=self._with_back_button(self._effort_keyboard(settings.reasoning_effort)),
                )
                return
            if value == "plan":
                await query.edit_message_text(
                    f"当前仅 Telegram 生效的计划模式：{'开启' if settings.plan_mode else '关闭'}",
                    reply_markup=self._with_back_button(self._plan_keyboard(settings.plan_mode)),
                )
                return
            if value == "new":
                settings.active_session_id = None
                settings.active_thread_name = None
                settings.active_thread_cwd = None
                self.state.update_chat_settings(settings)
                self.state.set_session_id(settings.workspace_name, None)
                await query.edit_message_text(
                    f"已清空工作区 {settings.workspace_name} 当前绑定的 Telegram 线程。",
                    reply_markup=self._menu_keyboard(),
                )
                return
            if value == "stop":
                target = self._resolve_target(settings)
                worker = self._ensure_worker(target.context_label, target.path)
                if worker.active_process is None:
                    await query.edit_message_text("当前 Telegram 线程没有正在运行的任务。", reply_markup=self._menu_keyboard())
                    return
                worker.active_process.terminate()
                await query.edit_message_text("已向当前 Telegram 线程发送停止信号。", reply_markup=self._menu_keyboard())
                return
        if action == "workspace":
            try:
                self.config.ensure_workspace(value)
            except KeyError:
                return
            settings.workspace_name = value
            settings.active_session_id = None
            settings.active_thread_name = None
            settings.active_thread_cwd = None
            self.state.update_chat_settings(settings)
            await query.edit_message_text(
                f"已切换到工作区 {value}，并清空当前线程绑定。",
                reply_markup=self._with_back_button(self._workspace_keyboard(value)),
            )
            return
        if action == "thread":
            if value == "clear":
                settings.active_session_id = None
                settings.active_thread_name = None
                settings.active_thread_cwd = None
                self.state.update_chat_settings(settings)
                await query.edit_message_text("已清空当前线程绑定。", reply_markup=self._menu_keyboard())
                return
            try:
                thread = self.session_catalog.resolve_thread(value)
            except ThreadLookupError:
                await query.edit_message_text("这个已保存的 Codex 线程现在不可用了。", reply_markup=self._menu_keyboard())
                return
            if thread.cwd is None or not thread.cwd.exists():
                await query.edit_message_text(
                    "这个已保存的 Codex 线程仍在，但它原始的工作目录不可用。",
                    reply_markup=self._menu_keyboard(),
                )
                return
            settings.active_session_id = thread.session_id
            settings.active_thread_name = thread.display_name
            settings.active_thread_cwd = thread.cwd
            self.state.update_chat_settings(settings)
            self._ensure_worker(thread.display_name, thread.cwd)
            await query.edit_message_text(
                f"已切换到线程“{thread.display_name}”。\n执行目录：{thread.cwd}",
                reply_markup=self._with_back_button(self._threads_keyboard(thread.session_id)),
            )
            return
        if action == "model":
            settings.model = value
            self.state.update_chat_settings(settings)
            await query.edit_message_text(
                f"仅 Telegram 生效的模型已切换为 {value}",
                reply_markup=self._with_back_button(self._model_keyboard(value)),
            )
            return
        if action == "effort" and value in EFFORT_CHOICES:
            settings.reasoning_effort = value
            self.state.update_chat_settings(settings)
            await query.edit_message_text(
                f"仅 Telegram 生效的推理精度已切换为 {value}",
                reply_markup=self._with_back_button(self._effort_keyboard(value)),
            )
            return
        if action == "plan" and value in {"on", "off"}:
            settings.plan_mode = value == "on"
            self.state.update_chat_settings(settings)
            await query.edit_message_text(
                f"仅 Telegram 生效的计划模式已切换为 {'开启' if settings.plan_mode else '关闭'}",
                reply_markup=self._with_back_button(self._plan_keyboard(settings.plan_mode)),
            )
            return
        if action == "approve" and value in self.pending_approvals:
            task = self.pending_approvals.pop(value).rerun_dangerous()
            await self._enqueue_task(task, query.message.chat_id)
            await query.edit_message_text("Approval accepted. Re-running the task with elevated execution.")
            return
        if action == "reject" and value in self.pending_approvals:
            self.pending_approvals.pop(value, None)
            await query.edit_message_text("Approval rejected. The pending task was discarded.")

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
        await message.reply_text("Voice received. Transcribing locally with Whisper...")
        local_path = await self._download_file(
            await message.voice.get_file(),
            suffix=".ogg",
            chat_id=update.effective_chat.id,
            media_kind="voice",
        )
        transcript = await self.transcriber.transcribe(local_path)
        settings = self._chat_settings(update.effective_chat.id)
        task, target = self._build_task_input(
            chat_id=update.effective_chat.id,
            settings=settings,
            prompt=transcript,
            file_paths=[local_path],
        )
        await message.reply_text(f"Transcription: {transcript}")
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
        await update.effective_message.reply_text(
            "Video input is reserved for a later revision. I can still return video files, but I do not analyze video messages yet."
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
        await worker.queue.put(queued_task)
        self.state.add_task(
            chat_id=chat_id,
            workspace_name=queued_task.context_label,
            prompt=queued_task.task.prompt,
            status="queued",
            dangerous=queued_task.task.dangerous,
        )

    async def _workspace_loop(self, worker: WorkspaceWorker) -> None:
        while True:
            job = await worker.queue.get()
            worker.active_job = job
            typing_task = asyncio.create_task(self._typing_loop(job.task.chat_id))
            try:
                await self._run_job(worker, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Workspace job failed", extra={"workspace": worker.name})
                if self.application:
                    await self.application.bot.send_message(
                        chat_id=job.task.chat_id,
                        text="The Telegram bridge hit an unexpected error while running your task.",
                        reply_to_message_id=job.reply_to_message_id,
                    )
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

        async def on_event(event: CodexEvent) -> None:
            nonlocal approval_triggered
            if event.kind == "session_started":
                self.state.set_active_session(chat_id, event.payload["session_id"])
                return
            if event.kind == "turn_started":
                return
            if event.kind == "command_started":
                command = event.payload["command"].strip()
                summary = f"Running: {command[:180]}"
                if summary != worker.last_progress_message:
                    worker.last_progress_message = summary
                    await bot.send_message(chat_id=chat_id, text=summary, reply_to_message_id=job.reply_to_message_id)
                return
            if event.kind == "command_completed":
                exit_code = event.payload.get("exit_code")
                if exit_code not in (None, 0):
                    await bot.send_message(
                        chat_id=chat_id,
                        text=f"Command failed with exit code {exit_code}: {event.payload['command'][:160]}",
                        reply_to_message_id=job.reply_to_message_id,
                    )
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
                            InlineKeyboardButton("Approve", callback_data=f"approve:{approval_id}"),
                            InlineKeyboardButton("Reject", callback_data=f"reject:{approval_id}"),
                        ]
                    ]
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text="Codex requested elevated execution. Approve to rerun this task with dangerous execution enabled.",
                    reply_markup=keyboard,
                    reply_to_message_id=job.reply_to_message_id,
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
            await self._send_detected_files(chat_id, "\n".join(final_messages), job.reply_to_message_id)
            return
        self.state.add_task(
            chat_id=chat_id,
            workspace_name=job.context_label,
            prompt=job.task.prompt,
            status="failed",
            dangerous=job.task.dangerous,
        )
        await bot.send_message(
            chat_id=chat_id,
            text=f"Codex exited with status {returncode}.",
            reply_to_message_id=job.reply_to_message_id,
        )

    async def _send_detected_files(self, chat_id: int, text: str, reply_to_message_id: int | None) -> None:
        assert self.application is not None
        bot = self.application.bot
        sent_paths: set[Path] = set()
        for match in FILE_PATH_PATTERN.findall(text):
            path = Path(match)
            if not path.is_absolute() or not path.exists() or path in sent_paths or not path.is_file():
                continue
            sent_paths.add(path)
            mime, _ = mimetypes.guess_type(path.name)
            if mime and mime.startswith("image/"):
                with path.open("rb") as handle:
                    await bot.send_photo(chat_id=chat_id, photo=handle, reply_to_message_id=reply_to_message_id)
            else:
                with path.open("rb") as handle:
                    await bot.send_document(chat_id=chat_id, document=handle, reply_to_message_id=reply_to_message_id)
            if len(sent_paths) >= 5:
                break

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
