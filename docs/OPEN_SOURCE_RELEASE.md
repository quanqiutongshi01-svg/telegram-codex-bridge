# Open Source Release Guide

This checklist is for publishing `telegram-codex-bridge` to GitHub without leaking personal or machine-local data.

## 1. Review Secrets

Never publish:

- Telegram bot tokens
- Telegram user ids or private group ids if you do not want them public
- `~/.codex/telegram-bridge/config.toml`
- `~/.codex/telegram-bridge/state.db`
- downloaded Telegram media
- local logs

If a bot token has ever been pasted into chat, screenshots, commit history, or shell history, revoke it in `@BotFather` and create a new one before public release.

## 2. Review Machine-Specific Data

Replace or avoid publishing:

- absolute personal paths such as `/Users/your-name/...`
- local usernames
- screenshots containing personal chats, ids, or tokens
- local runtime output

This repository already uses generic example paths in the published reference docs.

## 3. Confirm Ignored Files

Before committing, check that these stay untracked:

- `build/`
- `dist/`
- `*.egg-info/`
- `*.db`
- `.env`
- logs and downloads

Run:

```bash
git status --short
```

## 4. Prepare the Repository

Suggested steps:

```bash
git init
git add .
git commit -m "Initial open source release"
git branch -M main
git remote add origin git@github.com:<your-name>/telegram-codex-bridge.git
git push -u origin main
```

If you already have a GitHub repository created in the browser, attach it as `origin` and push.

## 5. Add Repository Metadata

Before going public, set:

- repository description
- topics such as `telegram`, `codex`, `openai`, `python`, `macos`
- a license

Recommended license choices:

- MIT: simplest for broad reuse
- Apache-2.0: explicit patent grant
- GPL-3.0: stronger copyleft

## 6. Verify the Public README

The public README should explain:

- what the project does
- how to install it
- how to control the background service
- how Telegram commands work
- how to use it as a Codex skill

That is already covered in `README.md`.

## 7. Publish Carefully

Before switching the GitHub repo to public:

1. Re-run `git status --short`
2. Re-run `python3 -m pytest`
3. Re-run `python3 -m compileall src scripts`
4. Open `README.md` and verify all examples look generic
5. Confirm no screenshots or logs with personal data are committed

## 8. Optional Follow-Ups

Good next additions after the first public release:

- `LICENSE`
- GitHub Actions for tests
- release tags
- issue templates
- a demo GIF or sanitized screenshots
