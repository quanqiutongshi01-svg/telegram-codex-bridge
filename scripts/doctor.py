#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import sys
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from telegram_codex_bridge.config import load_config  # noqa: E402


def check(name: str, ok: bool, detail: str) -> tuple[bool, str]:
    status = "OK" if ok else "FAIL"
    return ok, f"[{status}] {name}: {detail}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a Telegram Codex Bridge installation.")
    parser.add_argument("--runtime-dir", default=str(Path.home() / ".codex" / "telegram-bridge"))
    parser.add_argument("--ping-telegram", action="store_true", help="Call Telegram getMe using the configured bot token")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    config_path = runtime_dir / "config.toml"
    results: list[tuple[bool, str]] = []

    results.append(check("runtime-dir", runtime_dir.exists(), str(runtime_dir)))
    results.append(check("config", config_path.exists(), str(config_path)))
    if config_path.exists():
        config = load_config(config_path)
        results.append(check("codex", shutil.which(config.codex_binary) is not None or Path(config.codex_binary).exists(), config.codex_binary))
        results.append(check("ffmpeg", shutil.which(config.ffmpeg_binary) is not None or Path(config.ffmpeg_binary).exists(), config.ffmpeg_binary))
        results.append(check("workspaces", all(item.path.exists() for item in config.workspaces), ", ".join(str(item.path) for item in config.workspaces)))
        results.append(check("state-db", (runtime_dir / "state.db").exists(), str(runtime_dir / "state.db")))
        if args.ping_telegram:
            try:
                with urllib.request.urlopen(f"https://api.telegram.org/bot{config.bot_token}/getMe", timeout=10) as response:
                    payload = response.read().decode("utf-8", errors="replace")
                results.append(check("telegram", '"ok":true' in payload, "getMe"))
            except Exception as exc:  # noqa: BLE001
                results.append(check("telegram", False, str(exc)))

    success = True
    for ok, message in results:
        print(message)
        success = success and ok
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
