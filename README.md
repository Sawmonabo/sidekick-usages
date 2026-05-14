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

# Add a Codex account
sidekick-usages add codex

# Generate a long-lived token for Claude (one-year)
sidekick-usages setup-token claude

# Check usage for everything
sidekick-usages

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

**Claude Code**

- Endpoint: `https://api.anthropic.com/api/oauth/usage`
- Credentials: macOS Keychain item `Claude Code-credentials`,
  Linux `~/.claude/.credentials.json`, or Windows Credential Manager.
- Token shape: `sk-ant-oat01-…` (OAuth access token)
- Buckets reported: 5-hour, 7-day, 7-day Opus, 7-day OAuth apps

**Codex CLI**

- Endpoint: `https://chatgpt.com/backend-api/codex/usage`
- Credentials: `~/.codex/auth.json`
- Token shape: JWT (`eyJ…`)
- Buckets reported: primary 5-hour window, secondary 7-day window,
  plus per-model additional rate limits.
- Codex tokens expire roughly hourly; `sidekick-usages` automatically
  refreshes them via the stored refresh token when a request returns
  401, and writes the new access token back to the local config.

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
| `sidekick-usages list`                   | Show saved accounts with masked tokens.                                            |
| `sidekick-usages remove <label>`         | Delete one account.                                                                |
| `sidekick-usages rename <old> <new>`     | Rename an account.                                                                 |
| `sidekick-usages refresh <label>`        | Pull the current local token into a saved account.                                 |
| `sidekick-usages setup-token <provider>` | Run the provider's long-lived token generator (Claude only).                       |
| `sidekick-usages reset`                  | Wipe all saved accounts.                                                           |
| `sidekick-usages reset --provider <id>`  | Wipe one provider's accounts.                                                      |

Run `sidekick-usages --help` or `sidekick-usages <cmd> --help` for the
full option list on each command.

## Troubleshooting

**"No accounts saved"**
You haven't run `add` yet. Run `sidekick-usages add claude` (or `codex`)
after logging into the CLI normally.

**"Token expired or invalid (HTTP 401)"**
Your token was revoked or rolled. Log into the provider's CLI again,
then run `sidekick-usages refresh <label>` to pull the new token. For
Codex, this usually happens automatically — but if the refresh token
has also been revoked, you'll need to `codex login` again.

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
    "refresh_token": null,
    "expires_at": null,
    "plan": "max"
  },
  "codex-plus": {
    "provider_id": "codex",
    "access_token": "eyJ...",
    "refresh_token": "...",
    "expires_at": 1715750400,
    "plan": "plus"
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
uv run ruff check src/
uv run mypy src/sidekick_usages
uv run pytest

# Build wheel
uv build
```

The codebase follows these conventions:

- Line length ≤ 79 columns (hard limit, enforced by ruff)
- Sphinx-style docstrings (`:param:` / `:return:` / `:raises:` / `:ivar:`)
- PEP 604 union syntax everywhere (`str | None`)
- `from __future__ import annotations` at the top of every module
- mypy strict mode

## License

[Apache-2.0](./LICENSE)

## Related

`sidekick-usages` is a satellite utility for the
[ai-sidekicks](https://github.com/Sawmonabo/ai-sidekicks) project,
which provides the broader multi-agent collaboration runtime.
