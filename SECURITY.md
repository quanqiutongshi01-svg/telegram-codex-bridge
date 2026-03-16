# Security Policy

## Supported Versions

The project is currently maintained on the latest `main` branch and the latest tagged release.

## Reporting a Vulnerability

Please do not open a public GitHub issue for sensitive security reports.

If you discover a vulnerability, especially one involving:

- Telegram bot tokens
- leaked runtime configuration
- unauthorized chat access
- privilege escalation in the local bridge
- unsafe file handling

please report it privately through GitHub Security Advisories if available, or contact the maintainer directly before public disclosure.

## Immediate Actions for Sensitive Exposure

If a Telegram bot token has been exposed:

1. Revoke it immediately in `@BotFather`
2. Generate a new token
3. Update the local bridge config
4. Restart the bridge service

If runtime files were exposed, rotate any related credentials and review:

- `~/.codex/telegram-bridge/config.toml`
- `~/.codex/telegram-bridge/state.db`
- Telegram chat allowlists

## Safe Publishing Reminder

Never commit:

- bot tokens
- private chat ids unless you want them public
- local runtime databases
- local logs
- downloaded Telegram media

Before publishing, review [`docs/OPEN_SOURCE_RELEASE.md`](docs/OPEN_SOURCE_RELEASE.md).
