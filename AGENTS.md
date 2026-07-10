# Repository Guidelines

## Project Structure & Module Organization

This Python 3.14 package uses a `src/` layout. `src/sidekick_usages/cli/`
contains the Typer composition root and cohesive command owners; `providers/`
contains Claude and Codex adapters; and `heartbeat/`, `daemon.py`, and
`maintenance.py` own scheduled maintenance. Tests in `tests/` generally mirror
package modules. Keep operational docs in `docs/`, Homebrew packaging in
`packaging/homebrew/`, and automation in `.github/workflows/`. Do not commit
generated distributions, caches, coverage, or credential files.

## Build, Test, and Development Commands

- `uv sync --all-groups`: install locked development and quality tools.
- `uv pip install -e .`: install the CLI from the working tree.
- `uv run sidekick-usages --help`: exercise the local CLI.
- `uv run pytest --cov=sidekick_usages`: run tests with branch coverage.
- `uv run ruff check src/ tests/` and `uv run ty check src/ tests/`: lint and
  type-check.
- `uv run pre-commit run --all-files`: reproduce the CI quality gate.
- `npm run lint:markdown`: validate `README.md` and `docs/**/*.md`.
- `uv build`: create the wheel and source distribution in `dist/`.

## Coding Style & Naming Conventions

Use four-space indentation, double quotes, LF endings, and the 79-column Ruff
format. Use `snake_case` for modules and functions, `PascalCase` for classes,
and `UPPER_SNAKE_CASE` for constants. Prefer PEP 604 unions and Python 3.14's
native deferred annotations; do not add the legacy stringizing
`from __future__ import annotations` behavior, which is scheduled for future
deprecation. Write Sphinx-style docstrings. Keep provider-specific logic in
adapters, not shared services.

## Testing Guidelines

Pytest discovers `tests/test_*.py` and `test_*` functions under strict config.
Name tests after observable behavior. Inject fakes at provider, HTTP,
filesystem, and scheduler boundaries; never require real credentials. Iterate
with `uv run pytest tests/test_daemon.py`, then run the full suite. CI tests
Python 3.14 on Linux, macOS, and Windows. No minimum coverage is configured.

## Commit & Pull Request Guidelines

Use Conventional Commits: `feat(render): ...`, `fix(cli): ...`, `test: ...`,
`docs: ...`, `chore: ...`, or `ci: ...`. Install pre-commit hooks and do not
commit directly to `main`. Pull requests should explain behavior, list
verification commands, link issues, and update relevant docs and tests. Include
before/after terminal captures for CLI or TUI changes.

## Security & Configuration

Never commit OAuth tokens, account exports, or files from
`~/.config/sidekick-usages/`. Redact credentials and provider identifiers from
logs and fixtures, require HTTPS for provider traffic, and never let
saved-account maintenance overwrite the user's active CLI login.
