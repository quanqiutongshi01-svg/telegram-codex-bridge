# Contributing

Thanks for contributing to `telegram-codex-bridge`.

## Before You Start

- Read [`README.md`](README.md)
- Read [`README.zh-CN.md`](README.zh-CN.md) if you prefer Chinese
- Review [`references/configuration.md`](references/configuration.md) when changing config or command behavior

## Development Setup

Install development dependencies:

```bash
python3 -m pip install -e ".[dev]"
```

Run tests:

```bash
python3 -m pytest
```

Run a compile check:

```bash
python3 -m compileall src scripts
```

## Contribution Rules

- Keep Telegram-only settings isolated from global Codex config
- Do not commit secrets, tokens, runtime databases, or local logs
- Update docs when changing:
  - Telegram commands
  - config schema
  - installation behavior
  - service management flow
- Preserve macOS support unless a change is explicitly scoped otherwise

## Pull Requests

Please include:

- what changed
- why it changed
- how you tested it
- any user-facing behavior changes

If your PR changes commands or onboarding, update:

- `README.md`
- `README.zh-CN.md`
- `references/configuration.md`
- `SKILL.md` when relevant
