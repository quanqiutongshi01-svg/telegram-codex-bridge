#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import venv


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from telegram_codex_bridge.config import BridgeConfig, write_config, workspace_choices  # noqa: E402


LAUNCHD_LABEL = "com.openai.codex.telegram-bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install the Telegram Codex Bridge runtime.")
    parser.add_argument("--bot-token", required=True, help="Telegram bot token from BotFather")
    parser.add_argument("--allow-user", action="append", default=[], help="Authorized Telegram user id")
    parser.add_argument("--allow-chat", action="append", default=[], help="Authorized Telegram chat id")
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        help="Workspace in NAME=/abs/path form. Repeat for multiple workspaces.",
    )
    parser.add_argument("--runtime-dir", default=str(Path.home() / ".codex" / "telegram-bridge"))
    parser.add_argument("--default-model", default="gpt-5.4")
    parser.add_argument("--default-effort", default="high")
    parser.add_argument("--quick-model", action="append", default=[])
    parser.add_argument("--plan-mode-default", action="store_true")
    parser.add_argument("--codex-binary", default=shutil.which("codex") or "codex")
    parser.add_argument("--ffmpeg-binary", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--whisper-language", default="zh")
    parser.add_argument("--polling-timeout", type=int, default=30)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--no-launchctl", action="store_true", help="Skip launchctl load/start")
    return parser.parse_args()


def build_plist(runtime_dir: Path, venv_python: Path) -> str:
    stdout_path = runtime_dir / "logs" / "service.log"
    stderr_path = runtime_dir / "logs" / "service.err.log"
    return textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
          <dict>
            <key>Label</key>
            <string>{LAUNCHD_LABEL}</string>
            <key>ProgramArguments</key>
            <array>
              <string>{venv_python}</string>
              <string>-m</string>
              <string>telegram_codex_bridge.service</string>
              <string>--runtime-dir</string>
              <string>{runtime_dir}</string>
            </array>
            <key>RunAtLoad</key>
            <true/>
            <key>KeepAlive</key>
            <true/>
            <key>WorkingDirectory</key>
            <string>{runtime_dir}</string>
            <key>StandardOutPath</key>
            <string>{stdout_path}</string>
            <key>StandardErrorPath</key>
            <string>{stderr_path}</string>
          </dict>
        </plist>
        """
    )


def run(cmd: list[str], *, check: bool = True) -> None:
    subprocess.run(cmd, check=check)


def main() -> int:
    args = parse_args()
    if not args.workspace:
        print("At least one --workspace NAME=/abs/path entry is required.", file=sys.stderr)
        return 1

    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "logs").mkdir(parents=True, exist_ok=True)
    (runtime_dir / "downloads").mkdir(parents=True, exist_ok=True)

    config = BridgeConfig(
        bot_token=args.bot_token,
        workspaces=workspace_choices(args.workspace),
        allowed_user_ids=[int(value) for value in args.allow_user],
        allowed_chat_ids=[int(value) for value in args.allow_chat],
        default_model=args.default_model,
        default_reasoning_effort=args.default_effort,
        default_plan_mode=args.plan_mode_default,
        quick_models=args.quick_model or [args.default_model],
        polling_timeout=args.polling_timeout,
        log_level=args.log_level,
        whisper_model=args.whisper_model,
        whisper_language=args.whisper_language,
        codex_binary=args.codex_binary,
        ffmpeg_binary=args.ffmpeg_binary,
        runtime_dir=runtime_dir,
    )
    write_config(config, runtime_dir / "config.toml")

    venv_dir = runtime_dir / "venv"
    if not venv_dir.exists():
        venv.EnvBuilder(with_pip=True).create(venv_dir)
    venv_python = venv_dir / "bin" / "python"
    run([str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    run([str(venv_python), "-m", "pip", "install", f"{REPO_ROOT}[runtime]"])

    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(build_plist(runtime_dir, venv_python))

    if not args.no_launchctl:
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False)
        run(["launchctl", "bootstrap", domain, str(plist_path)])
        run(["launchctl", "enable", f"{domain}/{LAUNCHD_LABEL}"])
        run(["launchctl", "kickstart", "-k", f"{domain}/{LAUNCHD_LABEL}"])

    print(f"Installed Telegram Codex Bridge at {runtime_dir}")
    print(f"Config file: {runtime_dir / 'config.toml'}")
    print(f"LaunchAgent: {plist_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
