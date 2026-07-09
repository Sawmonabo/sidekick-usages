# sidekick-usages

Inspect Claude Code and Codex CLI usage across multiple saved accounts without
switching the active provider login for every check. The CLI groups rate-limit
windows, reset times, local lifetime output totals, and per-account failures in
one terminal view.

Routine checks do not open a browser. Initial account setup, an explicit
`codex-login`, or recovery from expired credentials can still require the
provider's normal login flow.

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [What it reports](#what-it-reports)
- [How provider access works](#how-provider-access-works)
- [Commands](#commands)
- [Background maintenance and heartbeat](#background-maintenance-and-heartbeat)
- [Security and network access](#security-and-network-access)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)
- [Related](#related)

## Requirements

- macOS, Linux, WSL, or native Windows.
- Python 3.14 or newer. Homebrew and `uv --python 3.14` provision an
  appropriate interpreter when needed.
- A normal Claude Code or Codex ChatGPT login for credential auto-detection.
  Install and sign in to the provider CLI before running `add` or `refresh`.
- `git` and [`uv`](https://docs.astral.sh/uv/) for the Git-tag installation
  path below.

Codex API-key mode is not supported. The Codex integration reads ChatGPT OAuth
credentials from `auth.json` and requires the associated ChatGPT account id.
Claude supports Claude Code OAuth logins and Claude `setup-token` credentials,
not Anthropic API keys.

## Installation

### Homebrew on macOS or Linux

The public tap currently packages release `v0.6.0` and its Python 3.14 runtime:

```bash
brew tap Sawmonabo/tap
brew install sidekick-usages
sidekick-usages --version
```

### uv from the GitHub release tag

The project is not currently published on public PyPI, and release `v0.6.0`
does not attach wheel or source-distribution assets. Install the tagged source
directly instead:

```bash
uv tool install --python 3.14 "git+https://github.com/Sawmonabo/sidekick-usages.git@v0.6.0"
uv tool update-shell
sidekick-usages --version
```

This works on macOS, Linux, WSL, and native Windows with `git` available. To
move a pinned installation to a newer release, replace the tag and reinstall:

```bash
uv tool install --force --python 3.14 "git+https://github.com/Sawmonabo/sidekick-usages.git@vX.Y.Z"
```

Find the current tag on the
[GitHub releases page](https://github.com/Sawmonabo/sidekick-usages/releases/latest).
Until a public PyPI distribution exists, registry-only commands such as
`uv tool install sidekick-usages`, `pipx install sidekick-usages`, and the
checked-in `install.sh` bootstrap are not valid public installation paths.

## Quick start

### Save provider accounts

Log in normally, then import the current provider login under a stable label:

```bash
# Claude Code OAuth login
claude auth login
sidekick-usages add claude --label claude-personal

# Codex ChatGPT login
codex login
sidekick-usages add codex --label codex-personal
```

For another Codex account, use an isolated source home when the normal
`~/.codex` login must remain unchanged:

```bash
sidekick-usages codex-login codex-work --codex-home ~/.codex-work
```

Without `--codex-home`, `codex-login` runs the normal Codex login flow against
`~/.codex`; that provider login changes the active Codex account before
sidekick imports its private copy.

Claude's long-lived setup token is also supported:

```bash
sidekick-usages setup-token claude --label claude-long-lived
```

A setup token has no refresh token and must be replaced manually when rejected.
Its usage check sends a tiny Claude model request to obtain rate-limit headers;
see [How provider access works](#how-provider-access-works).

### Check and manage accounts

```bash
# All accounts, or one provider
sidekick-usages
sidekick-usages --only claude

# Health and saved-account inventory
sidekick-usages doctor
sidekick-usages list

# Account management
sidekick-usages rename claude-personal claude-main
sidekick-usages set-plan claude-long-lived max
sidekick-usages remove old-label
```

`add` is idempotent by access token. It auto-detects provider credentials first,
then falls back to piped stdin or a hidden prompt when no local login is found.
Use `--force` only to replace an existing label intentionally.

## What it reports

The default `check` view provides:

- Provider-grouped panels with account labels and known plan tags.
- Utilization for primary 5-hour and 7-day windows plus named model limits,
  such as Claude Opus/OAuth or Codex Spark windows when returned.
- Local reset countdowns and inline recovery details for failed accounts.
- A narrow-terminal fallback when the full heat-panel layout does not fit.
- Per-provider lifetime **output-token** totals derived from local CLI state.

Lifetime totals are machine-wide local statistics, not totals for only the
accounts saved in sidekick:

- Claude reads `~/.claude/stats-cache.json`.
- Codex scans cumulative output totals in
  `~/.codex/sessions/**/rollout-*.jsonl` and caches file totals in
  `~/.config/sidekick-usages/codex-lifetime-cache.json`.

These local statistics are not uploaded. Missing or malformed lifetime files
produce a zero total without preventing provider usage checks.

## How provider access works

`sidekick-usages` uses the same provider backends and credential shapes used by
the installed CLIs. It does not scrape terminal output or use a headless
browser for usage checks.

### Claude Code

Credential discovery checks:

- macOS Keychain item `Claude Code-credentials`.
- Linux/WSL `~/.claude/.credentials.json`, then
  `~/.config/claude/.credentials.json`.
- Native Windows Claude credential files, then Windows Credential Manager.

The usage route depends on the saved OAuth scopes:

- Accounts with `user:profile` call
  `https://api.anthropic.com/api/oauth/usage` and can report 5-hour, 7-day,
  7-day Opus, and 7-day OAuth-app windows when returned.
- Setup-token and known inference-only accounts cannot use that endpoint. They
  POST a request with one input word and `max_tokens=1` to
  `https://api.anthropic.com/v1/messages`, discard the body, and read the
  unified 5-hour and 7-day rate-limit headers. This real request consumes a
  small amount of Claude quota.

Saved Claude OAuth logins with refresh tokens rotate automatically before a
known expiry or after HTTP 401. Refresh prefers `claude auth login --claudeai`
inside an isolated temporary `HOME`, then imports the rotated credentials. A
direct HTTPS OAuth exchange is available as a fallback. Neither path overwrites
the normal `~/.claude` login. Setup tokens cannot auto-refresh.

### Codex CLI

Credential discovery reads `$CODEX_HOME/auth.json` when `CODEX_HOME` is set,
otherwise `~/.codex/auth.json`. Sidekick copies each imported account into a
private file-backed Codex home under
`~/.config/sidekick-usages/codex/<filesystem-safe-label>/auth.json`.

`codex-login` follows the same rule: without `--codex-home` it logs in through
the normal `~/.codex` home; with an explicit home it configures file-backed auth
there and leaves the normal home untouched.

Usage calls `https://chatgpt.com/backend-api/codex/usage` with the saved bearer
token and `ChatGPT-Account-Id`. It reports the primary 5-hour window, secondary
7-day window, and provider-returned additional model limits. Expiring access
tokens rotate through `https://auth.openai.com/oauth/token`; rotated data is
written to the private sidekick cache, not the active `~/.codex` login.

## Commands

| Command | Purpose |
| --- | --- |
| `sidekick-usages` | Run `check` for every saved account. |
| `sidekick-usages check` | Explicit form of the default usage check. |
| `sidekick-usages --only <provider>` | Check only `claude` or `codex` accounts. |
| `sidekick-usages add <provider>` | Save auto-detected, piped, prompted, or `--token` credentials; supports `--label`, `--plan`, `--codex-home`, and `--force`. |
| `sidekick-usages list` | List labels, providers, plans, heartbeat state, and masked tokens. |
| `sidekick-usages remove <label>` | Delete one saved account. |
| `sidekick-usages rename <old> <new>` | Rename one saved account. |
| `sidekick-usages set-plan <label> <plan>` | Correct a display plan that the provider cannot introspect. |
| `sidekick-usages refresh <label>` | Import the current matching local login into one saved label. |
| `sidekick-usages refresh --all [--force] [--quiet]` | Rotate due saved refresh tokens without reading the current global login. |
| `sidekick-usages maintain [--quiet]` | Refresh due tokens, then heartbeat opted-in accounts; used by the daemon. |
| `sidekick-usages doctor [--provider ...] [--label ...] [--json]` | Report auth, refresh, usage-route, heartbeat, and manual-action diagnostics. |
| `sidekick-usages codex-login <label>` | Run Codex login and import a private account copy; supports `--device-auth`, `--codex-home`, and `--replace-identity`. |
| `sidekick-usages codex-export <label> --codex-home <path>` | Export complete saved Codex credentials to an isolated file-backed home. |
| `sidekick-usages setup-token claude` | Run Claude's long-lived token generator and save its output. |
| `sidekick-usages reset [--provider <id>] [-y]` | Delete all accounts or one provider's accounts. |
| `sidekick-usages check-update` | Query the latest GitHub release. |
| `sidekick-usages update [--dry-run]` | Run the detected `uv`, pipx, or Homebrew upgrade command. |
| `sidekick-usages daemon install\|status\|uninstall` | Manage user-level scheduled maintenance. |
| `sidekick-usages heartbeat ...` | Inspect, warm, enable, disable, or report usage-window heartbeat state. |
| `sidekick-usages --version` | Print the installed version. |

Run `sidekick-usages --help` and
`sidekick-usages <command> --help` for every option.

### Refresh identity safety

`refresh <label>` imports the provider's current local login. When both the
saved account and detected login expose provider account ids, sidekick refuses
a mismatch. Use `--replace-identity` only when intentionally reassigning the
label. `refresh --all` never performs local-login detection and never replaces
saved identities from the active Claude or Codex login.

### Updating a Git-tag installation

`check-update` accurately reports the latest GitHub release. The built-in
`update` command delegates to the detected package manager. A Git-tag uv install
is intentionally pinned, so update it by rerunning the tagged `uv tool install
--force` command from [Installation](#installation) with the new tag.

## Background maintenance and heartbeat

Token refresh and heartbeat are separate:

- `refresh --all` keeps saved access tokens valid using stored refresh tokens.
- Heartbeat intentionally sends a tiny model request to open an inactive usage
  window. It consumes provider quota and does not create free quota.
- `maintain --quiet` performs token refresh first, then heartbeat for accounts
  that were explicitly opted in.

Heartbeat is disabled by default and configured per account:

```bash
# One-shot check/warm; does not enable the daemon policy
sidekick-usages heartbeat <label>

# Opt into the default target, inspect state, then disable
sidekick-usages heartbeat enable <label>
sidekick-usages heartbeat status
sidekick-usages heartbeat disable <label>

# Codex only: explicitly target Spark or both independent windows
sidekick-usages heartbeat <label> --target spark
sidekick-usages heartbeat enable <label> --target all
```

Claude warms with `claude-haiku-4-5-20251001`. Codex's default `standard`
target warms with `gpt-5.4-mini`; the separate `spark` target uses
`gpt-5.3-codex-spark` and is opt-in. Cached future reset times prevent repeated
probes for the same target.

Install user-level scheduled maintenance with:

```bash
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

`--backend auto` selects:

| Platform | Backend |
| --- | --- |
| Native Windows | Windows Task Scheduler through a hidden `wscript.exe` wrapper |
| WSL | Windows Task Scheduler through a hidden wrapper that invokes `wsl.exe` |
| macOS | User LaunchAgent through launchd |
| Linux with `systemctl` | User-level systemd timer |
| Other Linux/Unix | Marked user crontab block |

The schedule runs `sidekick-usages maintain --quiet` every 30 minutes. It uses
saved sidekick credentials only and does not import the current global provider
login. See [token maintenance and daemon operations](./docs/token-maintenance.md)
and [heartbeat behavior and guardrails](./docs/heartbeat.md) for backend files,
logs, exit codes, and recovery procedures.

## Security and network access

- `accounts.json` contains raw OAuth credentials. On Unix it is written with
  mode `0600`; private Codex auth directories/files use `0700`/`0600`. On
  Windows, protection relies on the current user's filesystem ACLs.
- `list` masks token values, and `doctor --json` excludes access tokens,
  refresh tokens, id tokens, and raw provider credentials.
- Interactive token entry is hidden. Piped stdin is consumed only when local
  credential auto-detection finds no login. Passing `--token` can expose a
  secret in shell history or process listings, so prefer auto-detection, stdin,
  or the hidden prompt.
- Every built-in HTTP request rejects non-HTTPS URLs.
- There is no analytics or telemetry, and no automatic update check. The
  explicit `check-update` command contacts GitHub.

Runtime network destinations are:

| Purpose | Destination |
| --- | --- |
| Claude usage and tiny header probe | `api.anthropic.com` |
| Claude direct refresh fallback | `platform.claude.com` |
| Codex usage and heartbeat | `chatgpt.com` |
| Codex token refresh | `auth.openai.com` |
| Explicit release check | `api.github.com` |

Provider credentials are sent only to that provider's usage, model, or OAuth
hosts. GitHub release checks do not include provider credentials. Installation
and explicit updates additionally contact the selected package manager's normal
GitHub, Homebrew, or package-index destinations.

## Configuration

The account store is:

```text
~/.config/sidekick-usages/accounts.json
```

Labels are top-level JSON keys. Each serialized account currently contains all
of these fields:

```json
{
  "<label>": {
    "provider_id": "claude",
    "provider_account_id": null,
    "access_token": "<redacted>",
    "refresh_token": "<redacted-or-null>",
    "expires_at": 1781245745398,
    "plan": "max",
    "scopes": ["user:profile", "user:inference"],
    "codex_home": null,
    "codex_id_token": null,
    "codex_last_refresh": null,
    "last_refresh_at": null,
    "last_refresh_status": null,
    "last_refresh_error": null,
    "heartbeat_enabled": false,
    "heartbeat_5h_reset_at": null,
    "heartbeat_window_resets": null,
    "heartbeat_targets": null,
    "last_heartbeat_at": null,
    "last_heartbeat_status": null,
    "last_heartbeat_error": null
  }
}
```

Important field semantics:

- `expires_at` uses Unix milliseconds for Claude and Unix seconds for Codex.
- `provider_account_id` binds Codex requests and protects explicit imports from
  cross-account replacement when the id is known.
- `codex_home`, `codex_id_token`, and `codex_last_refresh` preserve enough
  private auth metadata to refresh or export a CLI-compatible Codex account.
- Refresh and heartbeat diagnostics are redacted user-facing state. Heartbeat
  targets and reset caches are optional and target-specific.
- If the new store is absent but
  `~/.config/cc-usage/accounts.json` exists, it is copied into the new schema;
  the legacy file is left in place.

Do not edit the store by hand. Use CLI commands so identity checks, file modes,
private Codex caches, and diagnostics remain consistent.

## Troubleshooting

### No accounts saved

Log in to the provider CLI, then import it:

```bash
sidekick-usages add claude --label claude-personal
sidekick-usages add codex --label codex-personal
```

### HTTP 401 or failed refresh

Saved OAuth accounts with refresh tokens rotate before known expiry and retry
once after HTTP 401. If the refresh token is missing, revoked, or rejected, log
in as that exact provider account and update the label explicitly:

```bash
claude auth login
sidekick-usages refresh <claude-label>

sidekick-usages codex-login <codex-label>
```

If a known provider account id differs, `refresh` refuses the replacement. Do
not bypass this with `--replace-identity` unless changing the label's account is
intentional.

For non-obvious Claude failures, use the
[Claude debugging log](./docs/debugging-claude.md). It covers isolated refresh,
direct token probes, scope routing, whitespace in stored tokens, and provider
response headers.

### Claude setup-token shows fewer windows

This is expected. An inference-only setup token cannot read
`/api/oauth/usage`, so sidekick reads only 5-hour and 7-day unified headers from
the tiny `/v1/messages` probe. It cannot auto-refresh because it has no refresh
token. Correct a cosmetic plan label with:

```bash
sidekick-usages set-plan <label> <plan>
```

### Missing Codex account id

Re-import a complete ChatGPT login:

```bash
sidekick-usages codex-login <label>
```

Use `--codex-home <path>` only when deliberately working with an isolated Codex
home. `codex-export` also requires complete id-token, account-id, and refresh
metadata; one successful `codex-login` normally supplies it.

### HTTP 429 or transient network errors

The HTTP client retries HTTP 429, server errors, and network failures with
backoff. After retries are exhausted, wait for the shown `Retry-After` interval
when available and run the check again.

### Daemon is installed but accounts do not rotate

Run the same command as the scheduler, then inspect diagnostics and backend
status:

```bash
sidekick-usages maintain --quiet
sidekick-usages doctor
sidekick-usages daemon status
```

Fresh accounts are skipped until they approach expiry. Heartbeat remains off
until enabled per account.

### Claude does not auto-detect on macOS

Verify the Keychain item directly:

```bash
security find-generic-password -s 'Claude Code-credentials' -w
```

If it is missing, run `claude auth login` and retry `add` or `refresh`.

## Development

### Repository layout

- `src/sidekick_usages/`: Typer CLI, provider adapters, storage, rendering,
  refresh, heartbeat, doctor, daemon, and update logic.
- `tests/`: 172 pytest cases covering CLI behavior, providers, HTTP errors,
  storage, rendering, maintenance, packaging, and cross-platform schedulers at
  this snapshot.
- `docs/`: operational guides plus the usage-TUI design and implementation
  record.
- `packaging/homebrew/`: formula generator and in-tree formula copy.
- `.github/workflows/`: CI, release, PyPI-gated publish, and Homebrew automation.

### Setup and validation

```bash
git clone https://github.com/Sawmonabo/sidekick-usages
cd sidekick-usages

uv python install 3.14
uv venv
uv sync --all-groups
uv pip install -e .

uv run sidekick-usages --help
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pytest --cov=sidekick_usages
SKIP=no-commit-to-branch uv run pre-commit run --all-files

npm ci
npx --no-install markdownlint-cli2 README.md
npm run lint:markdown

uv build
```

CI runs pre-commit, the full pytest suite on Linux, macOS, and Windows with
Python 3.14, then builds and smoke-tests the wheel. No minimum coverage
percentage is configured.

The direct README Markdown command passes at this snapshot. The repository-wide
`npm run lint:markdown` also scans the tracked historical TUI plan/spec under
`docs/superpowers/`; those two files currently carry a 95-error Markdown lint
baseline, primarily long lines, which is outside this README change.

Ruff targets Python 3.14, double quotes, LF endings, and a 79-column formatter
width; `E501` is intentionally ignored, so 79 columns is not a hard lint error.
Use PEP 604 unions such as `str | None` and Sphinx-style public docstrings. Ty
treats warnings as errors. Pytest discovers `tests/test_*.py` and `test_*`
functions under strict marker and configuration checks.

Use Conventional Commits such as `feat(render): ...`, `fix(cli): ...`,
`test: ...`, and `docs: ...`. Release Please builds release notes and tags from
that history. See [AGENTS.md](./AGENTS.md) for the concise contributor guide.

## License

[Apache-2.0](./LICENSE)

## Related

`sidekick-usages` is a satellite utility for
[ai-sidekicks](https://github.com/Sawmonabo/ai-sidekicks), a broader runtime for
human and multi-provider agent collaboration.

Project links:

- [Changelog](./CHANGELOG.md)
- [Issues](https://github.com/Sawmonabo/sidekick-usages/issues)
- [Latest release](https://github.com/Sawmonabo/sidekick-usages/releases/latest)
