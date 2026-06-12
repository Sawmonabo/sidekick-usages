# sidekick-usages

Check Claude Code and Codex CLI usage across multiple accounts in
one command. No browser logins, no copy-pasting from `/status`,
no juggling shells.

## Install

### Option 1: One-line bootstrap (recommended)

```bash
curl -LsSf https://raw.githubusercontent.com/Sawmonabo/sidekick-usages/main/install.sh | bash
```

The installer checks for [`uv`](https://docs.astral.sh/uv/), installs
it if missing, then installs `sidekick-usages` as a global tool.

### Option 2: Direct via `uv`

If you already have `uv` (or want to manage it yourself):

```bash
uv tool install sidekick-usages
```

To upgrade later:

```bash
uv tool upgrade sidekick-usages
```

### Option 3: Homebrew (macOS / Linux)

```bash
brew tap Sawmonabo/tap
brew install sidekick-usages
```

### Alternative: pipx

If you prefer `pipx`:

```bash
pipx install sidekick-usages
```

## Quick start

```bash
# Add an account (auto-detects your local Claude Code login)
sidekick-usages add claude

# Add one Codex account from the default Codex CLI login.
# This copies the current ~/.codex auth into sidekick's private cache.
sidekick-usages add codex

# Add/update another Codex account:
# run normal Codex login, then import the current ~/.codex auth.
sidekick-usages codex-login sabossedgh@fortressinfosec.com
sidekick-usages codex-login a.sawmon@ymail.com

# Generate a long-lived token for Claude (one-year)
sidekick-usages setup-token claude

# Check usage for everything
sidekick-usages

# Check auth/token health without rotating anything
sidekick-usages doctor

# Keep saved refresh-token accounts fresh in the background
sidekick-usages daemon install

# Just one provider
sidekick-usages --only claude

# Manage accounts
sidekick-usages list
sidekick-usages rename claude-max-1 personal
sidekick-usages remove personal
sidekick-usages reset --provider codex
```

## How it works

`sidekick-usages` calls each provider's official OAuth usage endpoint
directly — the same one the provider's CLI hits when you run
`/status`. No scraping, no headless browsers, no API keys.

### Claude Code

- Endpoint: `https://api.anthropic.com/api/oauth/usage`
- Credentials: macOS Keychain item `Claude Code-credentials`,
  Linux `~/.claude/.credentials.json`, or Windows Credential Manager.
- Token shape: `sk-ant-oat01-…` (OAuth access token)
- Buckets reported: 5-hour, 7-day, 7-day Opus, 7-day OAuth apps
- Claude login tokens with a stored refresh token are refreshed
  automatically when they expire or return 401. Sidekick asks the
  installed `claude auth login --claudeai` flow to refresh inside a
  temporary `HOME`, imports the rotated credentials, and does not
  overwrite your normal `~/.claude` login. `setup-token` outputs do
  not include refresh tokens and must be replaced manually if
  rejected.

### Codex CLI

- Endpoint: `https://chatgpt.com/backend-api/codex/usage`
- Credentials: Codex itself keeps the active/default login at
  `~/.codex/auth.json`. `sidekick-usages` imports that login into
  its own per-account cache under
  `~/.config/sidekick-usages/codex/<label>/auth.json` so other apps
  that expect `~/.codex` keep working.
- Token shape: JWT (`eyJ…`)
- Buckets reported: primary 5-hour window, secondary 7-day window,
  plus per-model additional rate limits.
- Codex tokens expire roughly hourly and refresh automatically via the
  stored refresh token before expiry or when a request returns 401.
  Rotated tokens are written back to sidekick's private per-account
  cache, not to global `~/.codex`.

## Security

- **Tokens never leave your machine** except in HTTPS requests to
  the provider's own API.
- **Config lives at `~/.config/sidekick-usages/accounts.json`** with
  `chmod 600` on Unix.
- **Interactive token prompts are hidden** (Rich's password prompt)
  so tokens don't appear in your terminal scrollback or shell history.
- **Tokens piped via stdin are read raw** so you can pass them from
  a secrets manager:
  `op read 'op://Personal/Claude/token' | sidekick-usages add claude`
- **Zero telemetry**: this tool calls no third-party services.

## Commands

| Command                                 | What it does                                                                       |
| --------------------------------------- | ---------------------------------------------------------------------------------- |
| `sidekick-usages`                        | Show usage for every saved account (default).                                      |
| `sidekick-usages check`                  | Same as above, named explicitly.                                                   |
| `sidekick-usages --only <provider>`      | Filter to one provider.                                                            |
| `sidekick-usages add <provider>`         | Save a new account. Auto-detects from local CLI, or accepts `--token`. Idempotent. |
| `sidekick-usages add codex --codex-home <path>` | Advanced: import from a specific source `CODEX_HOME`, then copy into sidekick's private cache. |
| `sidekick-usages codex-login <label>` | Run normal `codex login`, then save/update the label from `~/.codex`. |
| `sidekick-usages codex-export <label> --codex-home <path>` | Advanced: write a saved Codex account into another file-backed Codex home when complete auth metadata is available. |
| `sidekick-usages list`                   | Show saved accounts with masked tokens.                                            |
| `sidekick-usages remove <label>`         | Delete one account.                                                                |
| `sidekick-usages rename <old> <new>`     | Rename an account.                                                                 |
| `sidekick-usages refresh <label>`        | Pull the saved/default local token into a saved account.                           |
| `sidekick-usages refresh <label> --from-codex-home <path>` | Pull a Codex token from a specific `CODEX_HOME`.                     |
| `sidekick-usages refresh --all`           | Refresh due accounts from saved refresh tokens only; never imports current global CLI login. |
| `sidekick-usages refresh --all --quiet`   | Scheduler-safe maintenance mode: only prints accounts that need manual action.     |
| `sidekick-usages refresh --all --force`   | Refresh every account with a saved refresh token, even if still fresh.             |
| `sidekick-usages doctor`                  | Show auth/token health, usage route, auto-refreshability, and manual action items. |
| `sidekick-usages doctor --json`           | Emit machine-readable doctor output with secrets redacted.                         |
| `sidekick-usages daemon install`          | Install a user-level scheduler that runs `refresh --all --quiet` every 30 minutes. |
| `sidekick-usages daemon status`           | Inspect the installed scheduler.                                                   |
| `sidekick-usages daemon uninstall`        | Remove the installed scheduler.                                                    |
| `sidekick-usages setup-token <provider>` | Run the provider's long-lived token generator (Claude only).                       |
| `sidekick-usages reset`                  | Wipe all saved accounts.                                                           |
| `sidekick-usages reset --provider <id>`  | Wipe one provider's accounts.                                                      |

Run `sidekick-usages --help` or `sidekick-usages <cmd> --help` for the
full option list on each command.

`daemon install --backend auto` picks a user-level scheduler for the
host: Windows Task Scheduler on Windows, Windows Task Scheduler via
`wsl.exe` inside WSL, launchd on macOS, user-level systemd on native
Linux/Ubuntu, and cron if systemd is unavailable. The daemon never
copies the current global Claude or Codex login into saved labels; it
only uses refresh tokens already stored in sidekick-usages.

For the complete token-maintenance model, scheduler backend details,
and operational troubleshooting, see
[docs/token-maintenance.md](./docs/token-maintenance.md).

## Troubleshooting

**"No accounts saved"**
You haven't run `add` yet. Run `sidekick-usages add claude` (or `codex`)
after logging into the CLI normally.

**"Token expired or invalid (HTTP 401)"**
For saved Claude/Codex accounts with a refresh token, `sidekick-usages`
tries to renew the access token automatically and writes the new token
back to its config. Claude refresh is delegated to the installed
Claude Code binary in an isolated temporary home because Claude's
platform token endpoint is sensitive to the official client flow. If
the refresh token is missing, expired, revoked, or the CLI reports
`Claude CLI refresh failed`, log into the provider's CLI again as that
same account, then run
`sidekick-usages refresh <label>` to pull the current local token.

For multiple Codex accounts, `~/.codex` remains the normal active
Codex login used by other apps. To update a sidekick label, sign into
the desired account with normal Codex login and run
`sidekick-usages refresh <label>`, or let
`sidekick-usages codex-login <label>` run the login flow for you.
Sidekick copies the resulting auth bundle into its private
per-account cache. Use `--from-codex-home` only for advanced imports
from a non-default source.

If a Claude account still 401s after a clean refresh — or for any
other non-obvious Claude-provider behavior — see
[docs/debugging-claude.md](./docs/debugging-claude.md), a running log
of debugging techniques and root causes for the Claude provider. The
first entry covers the 401 case: direct-`/v1/messages`-probe
technique, how to read `anthropic-organization-id` /
`overage-disabled-reason` headers to verify a token's account, and
the two non-expiry causes (whitespace in stored bytes, stale
`account.scopes`) that masquerade as expired tokens.

**"Rate limited (HTTP 429)"**
The provider's API is throttling you. The tool retries automatically
with backoff; if it still fails after a few attempts, wait the duration
shown in the error message.

**Token doesn't auto-detect on macOS**
The Keychain item `Claude Code-credentials` lives under your login
keychain. If
`security find-generic-password -s 'Claude Code-credentials' -w`
fails on the command line, sidekick-usages can't read it either.
Re-running `claude login` usually re-creates the item correctly.

## Configuration

Config file location: `~/.config/sidekick-usages/accounts.json`

Schema:

```json
{
  "personal-max": {
    "provider_id": "claude",
    "access_token": "sk-ant-oat01-...",
    "refresh_token": "...",
    "expires_at": 1781245745398,
    "plan": "max",
    "scopes": ["user:profile", "user:inference"],
    "last_refresh_at": "2026-06-12T13:14:22.459000Z",
    "last_refresh_status": "ok",
    "last_refresh_error": null
  },
  "codex-plus": {
    "provider_id": "codex",
    "access_token": "eyJ...",
    "provider_account_id": "...",
    "refresh_token": "...",
    "expires_at": 1715750400,
    "plan": "plus",
    "scopes": null,
    "codex_home": "/home/me/.config/sidekick-usages/codex/codex-plus",
    "codex_id_token": "...",
    "codex_last_refresh": "2026-06-12T00:00:00Z",
    "last_refresh_at": null,
    "last_refresh_status": null,
    "last_refresh_error": null
  }
}
```

You shouldn't normally edit this by hand — use the CLI commands instead.

## Development

```bash
git clone https://github.com/Sawmonabo/sidekick-usages
cd sidekick-usages

# Install dev tooling + package in editable mode
uv venv
uv sync --group dev
uv pip install -e .

# Run lint / type-check / tests
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pytest

# Build wheel
uv build
```

The codebase follows these conventions:

- Line length ≤ 79 columns (hard limit, enforced by ruff)
- Sphinx-style docstrings (`:param:` / `:return:` / `:raises:` / `:ivar:`)
- PEP 604 union syntax everywhere (`str | None`)
- `from __future__ import annotations` at the top of every module
- ty with warnings treated as errors

## License

[Apache-2.0](./LICENSE)

## Related

`sidekick-usages` is a satellite utility for the
[ai-sidekicks](https://github.com/Sawmonabo/ai-sidekicks) project,
which provides the broader multi-agent collaboration runtime.
