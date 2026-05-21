#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=REPO_ROOT, check=check, text=True, capture_output=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and optionally publish a GitHub release.")
    parser.add_argument("version", help="Release version, for example 0.1.1 or v0.1.1")
    parser.add_argument("--publish", action="store_true", help="Create a GitHub release with gh.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow releasing from a dirty worktree.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest and compileall.")
    return parser.parse_args()


def normalized_tag(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def ensure_clean_worktree(allow_dirty: bool) -> None:
    status = run(["git", "status", "--short"]).stdout.strip()
    if status and not allow_dirty:
        raise SystemExit("Worktree is dirty. Commit changes or pass --allow-dirty.")


def run_checks(skip_tests: bool) -> None:
    if skip_tests:
        return
    run([sys.executable, "-m", "compileall", "src", "scripts", "tests"])
    run([sys.executable, "-m", "pytest"])


def build_asset(tag: str) -> Path:
    dist_dir = REPO_ROOT / "dist"
    dist_dir.mkdir(exist_ok=True)
    asset_path = dist_dir / f"telegram-codex-bridge-{tag}.zip"
    if asset_path.exists():
        asset_path.unlink()
    excluded_roots = {".git", ".pytest_cache", "__pycache__", "build", "dist", ".venv", "runtime", "logs", "downloads"}
    with zipfile.ZipFile(asset_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in REPO_ROOT.rglob("*"):
            relative = path.relative_to(REPO_ROOT)
            if any(part in excluded_roots for part in relative.parts):
                continue
            if path.name == ".DS_Store" or path.name.startswith(".env") or path.suffix == ".db":
                continue
            if path.is_file():
                archive.write(path, relative)
    return asset_path


def release_notes(tag: str) -> str:
    previous_tag = run(["git", "describe", "--tags", "--abbrev=0"], check=False).stdout.strip()
    if previous_tag:
        log_range = f"{previous_tag}..HEAD"
        commits = run(["git", "log", "--oneline", log_range], check=False).stdout.strip()
    else:
        commits = run(["git", "log", "--oneline", "-10"], check=False).stdout.strip()
    body = [
        f"Telegram Codex Bridge {tag}",
        "",
        "Highlights:",
        commits or "- Initial release asset.",
        "",
        "Install with `scripts/install.py`; see README.md and README.zh-CN.md.",
    ]
    return "\n".join(body)


def publish_release(tag: str, asset_path: Path) -> None:
    if shutil.which("gh") is None:
        raise SystemExit("GitHub CLI `gh` is required for --publish.")
    existing = run(["git", "tag", "--list", tag]).stdout.strip()
    if not existing:
        run(["git", "tag", tag])
        run(["git", "push", "origin", tag])
    notes_path = REPO_ROOT / "dist" / f"{tag}-notes.md"
    notes_path.write_text(release_notes(tag))
    run(
        [
            "gh",
            "release",
            "create",
            tag,
            str(asset_path),
            "--title",
            f"Telegram Codex Bridge {tag}",
            "--notes-file",
            str(notes_path),
        ]
    )


def main() -> int:
    args = parse_args()
    tag = normalized_tag(args.version)
    ensure_clean_worktree(args.allow_dirty)
    run_checks(args.skip_tests)
    asset_path = build_asset(tag)
    print(
        textwrap.dedent(
            f"""\
            Built release asset:
              {asset_path}

            Publish command:
              python3 scripts/release.py {tag} --publish
            """
        ).strip()
    )
    if args.publish:
        publish_release(tag, asset_path)
        print(f"Published GitHub release {tag}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
