#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess


LAUNCHD_LABEL = "com.openai.codex.telegram-bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uninstall the Telegram Codex Bridge runtime.")
    parser.add_argument("--runtime-dir", default=str(Path.home() / ".codex" / "telegram-bridge"))
    parser.add_argument("--purge", action="store_true", help="Remove runtime files after stopping the service")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist_path)], check=False)
    if plist_path.exists():
        plist_path.unlink()
    if args.purge:
        shutil.rmtree(Path(args.runtime_dir).expanduser().resolve(), ignore_errors=True)
    print("Telegram Codex Bridge uninstalled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
