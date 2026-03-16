#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


LAUNCHD_LABEL = "com.openai.codex.telegram-bridge"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start, stop, restart, or inspect the Telegram Codex Bridge service.")
    parser.add_argument("action", choices=("start", "stop", "restart", "status"))
    return parser.parse_args()


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def domain() -> str:
    return f"gui/{os.getuid()}"


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def start_service() -> int:
    plist = plist_path()
    if not plist.exists():
        print(f"未找到 LaunchAgent 配置：{plist}", file=sys.stderr)
        print("请先运行 scripts/install.py 完成安装。", file=sys.stderr)
        return 1
    launch_domain = domain()
    bootstrap = run(["launchctl", "bootstrap", launch_domain, str(plist)], check=False)
    if bootstrap.returncode not in (0, 5):
        print(bootstrap.stderr.strip() or bootstrap.stdout.strip(), file=sys.stderr)
        return bootstrap.returncode
    run(["launchctl", "enable", f"{launch_domain}/{LAUNCHD_LABEL}"])
    run(["launchctl", "kickstart", "-k", f"{launch_domain}/{LAUNCHD_LABEL}"])
    print("Telegram Codex Bridge 已启动。")
    return 0


def stop_service() -> int:
    plist = plist_path()
    launch_domain = domain()
    result = run(["launchctl", "bootout", launch_domain, str(plist)], check=False)
    if result.returncode not in (0, 3):
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return result.returncode
    print("Telegram Codex Bridge 已停止。")
    return 0


def status_service() -> int:
    launch_domain = domain()
    result = run(["launchctl", "print", f"{launch_domain}/{LAUNCHD_LABEL}"], check=False)
    if result.returncode != 0:
        print("Telegram Codex Bridge 当前未加载。")
        return 0
    lines = result.stdout.splitlines()
    state_line = next((line.strip() for line in lines if "state =" in line), "state = unknown")
    pid_line = next((line.strip() for line in lines if line.strip().startswith("pid =")), "pid = n/a")
    print(f"Telegram Codex Bridge 状态：{state_line.split('=', 1)[1].strip()}")
    print(f"Telegram Codex Bridge 进程：{pid_line.split('=', 1)[1].strip()}")
    return 0


def main() -> int:
    args = parse_args()
    if args.action == "start":
        return start_service()
    if args.action == "stop":
        return stop_service()
    if args.action == "restart":
        stop_service()
        return start_service()
    return status_service()


if __name__ == "__main__":
    raise SystemExit(main())
