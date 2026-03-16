from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from .bot import TelegramCodexBridge
from .config import load_config
from .state import StateStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Telegram Codex Bridge service.")
    parser.add_argument(
        "--runtime-dir",
        default=str(Path.home() / ".codex" / "telegram-bridge"),
        help="Runtime directory containing config.toml and state.db",
    )
    return parser.parse_args()


async def _main() -> None:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    config = load_config(runtime_dir / "config.toml")
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    state = StateStore(config.state_db_path)
    state.initialize()
    bridge = TelegramCodexBridge(config=config, state=state)
    await bridge.run()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
