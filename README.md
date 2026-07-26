# sidekick-usages

```text
      o
     .-.
  .--┴-┴--.    sidekick usages
  | O   O |   >> A multi-account usage dashboard for Claude Code and Codex CLI.
  | ||||| |   >> Limits + resets + account status, one terminal.
  '--___--'
```

[![Version](https://img.shields.io/github/v/release/Sawmonabo/sidekick-usages?label=version&color=ff1447)](https://github.com/Sawmonabo/sidekick-usages/releases/latest)
![Python](https://img.shields.io/badge/python-%3E%3D3.14-3776AB?logo=python&logoColor=white)
![Package Manager](https://img.shields.io/badge/package%20manager-uv-2f2a45)
![TUI](https://img.shields.io/badge/TUI-Typer%20%2B%20Rich-009485)
![Tests](https://img.shields.io/badge/tests-pytest%209%2B-0f172a?logo=pytest&logoColor=white)

Inspect Claude Code and Codex CLI usage across multiple saved accounts without
switching the active provider login for every check. The CLI groups rate-limit
windows, reset times, scope-aware token activity, and per-account failures in
one terminal view.

Routine checks do not open a browser. Initial account setup, an explicit
`sidekick-usages codex login`, or recovery from expired credentials can still
require the provider's normal login flow.

> [!NOTE]
> This README tracks `develop` and its next-release command surface. The stable
> installation instructions use the latest published release shown by the
> version badge. Install `develop` below when you need every documented command.

## Table of contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Quick start](#quick-start)
- [What it reports](#what-it-reports)
- [How provider access works](#how-provider-access-works)
- [Commands](#commands)
- [Background maintenance and heartbeat](#background-maintenance-and-heartbeat)
- [Security and network access](#security-and-network-access)
- [Persistence and recovery](#persistence-and-recovery)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)
- [Related](#related)

## Requirements

- macOS, Linux, or WSL for interactive account selection and resident
  supervision. Native Windows keeps one-shot reporting, but interactive
  selection and resident supervision are explicitly feature-disabled.
- Python 3.14 or newer. Homebrew and `uv --python 3.14` provision an
  appropriate interpreter when needed.
- A normal Claude Code login for Claude credential auto-detection.
- A supported Codex CLI for official login and refresh inside each saved
  account's managed home.
- `git` and [`uv`](https://docs.astral.sh/uv/) for the Git-tag installation
  path below.

Codex API-key mode is not supported. Managed Codex credentials are read only
inside the exact protected managed home during worker-scoped operations. To
detect an external native login, the supervisor may read native authentication
long enough to derive a non-secret identity and generation, then immediately
discard it. Native authentication is never imported, copied, persisted,
written, or used as managed credentials. Claude supports Claude Code OAuth
logins and Claude `setup-token` credentials, not Anthropic API keys.

## Installation

### Homebrew on macOS or Linux

The public tap currently packages release `v0.7.0` and its Python 3.14 runtime:

```bash
brew tap Sawmonabo/tap
brew install sidekick-usages
sidekick-usages --version
```

### uv from the GitHub release tag

The project is not currently published on public PyPI, and release `v0.7.0`
does not attach wheel or source-distribution assets. Install the tagged source
directly instead:

```bash
uv tool install --python 3.14 "git+https://github.com/Sawmonabo/sidekick-usages.git@v0.7.0"
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

### uv from the current development branch

To use the next-release command surface documented in this README before it is
published, install directly from `develop`:

```bash
uv tool install --force --python 3.14 "git+https://github.com/Sawmonabo/sidekick-usages.git@develop"
uv tool update-shell
```

The development branch can change before release. Use the tagged installation
above when you need a stable, reproducible version.

## Quick start

### Save provider accounts

Save an initial Claude login under a stable label, then move that saved
authority into its stable Sidekick-managed profile:

```bash
# Claude Code subscription login
claude auth login
sidekick-usages add claude --label <claude-label>
sidekick-usages refresh <claude-label>
```

Authenticate an existing Codex label in its independent managed home:

```bash
sidekick-usages codex login <codex-label>
```

The official sign-in runs directly inside the label's stable Sidekick-managed
Codex home. It does not read, copy, or change the native `~/.codex` login.

Claude's long-lived setup token is also supported:

```bash
sidekick-usages claude setup-token --label <setup-label>
```

A setup token has no refresh token and must be replaced manually when rejected.
Its usage check sends a tiny Claude model request to obtain rate-limit headers;
see [How provider access works](#how-provider-access-works).

### Migrate existing saved accounts

Move existing saved labels to independent provider-managed authorities:

```bash
sidekick-usages migrate managed-auth
```

The command starts with a secret-safe preview, uses official provider login
when an account needs it, continues past account-scoped failures, and can be
rerun to resume. It never accepts a token argument.

### Check, select, and manage accounts

```bash
# Cached account dashboard, for all accounts or one provider
sidekick-usages
sidekick-usages --only claude

# Detailed health diagnostics
sidekick-usages doctor

# Account management
sidekick-usages rename <old-label> <new-label>
sidekick-usages set-plan <label> max
sidekick-usages remove <label>
```

The next-release dashboard contract paints secret-free cached metrics first on
a supported TTY, then updates accounts from one bounded concurrent lookup.
Exactly one `›` cursor appears in the focused provider. It begins on that
provider's verified active account, so healthy rows need no `IN USE` label.
When no native account is verified, the first row receives navigation focus
without being presented as active.

| Key | Behavior |
| --- | --- |
| Up/Down or `k`/`j` | Preview another account in the focused provider. |
| Tab | Focus the other provider at its verified active account. |
| Enter | Use or repair the previewed account. |
| Esc | Return to the provider's verified active account. |
| `r` / `R` | Refresh the previewed account / every due account. |
| `?` | Toggle concise keyboard help. |
| `y` / `n` | Approve or decline guided per-user service setup when asked. |
| `q` / Ctrl-C | Exit normally / restore the terminal and exit with code 130. |

While activation is in flight, another Enter or Esc is ignored. `q` and
Ctrl-C remain responsive; an already-started provider transaction completes or
recovers through the supervisor instead of being abandoned mid-change.

Scripts can select one exact saved account without opening the dashboard:

```bash
sidekick-usages use claude <claude-label>
sidekick-usages use codex <codex-label>
```

`use` never prompts or installs the service. If preparation is required, it
returns the exact interactive command to run. Neither selection path creates a
wrapper, alias, shell function, or PATH shim: normal `claude` and `codex`
commands keep resolving to the vendor executables and their native login
locations.

Selection is provider-specific. Every saved account keeps an independent
private authority, while the selected Claude native state and selected Codex
runtime state remain separate. Unselected accounts continue to participate in
maintenance, usage, and opted-in heartbeat.

Claude `add` is idempotent by access token. It auto-detects Claude credentials
first, then falls back to piped stdin or a hidden prompt when no local login is
found. Use `--force` only to replace an existing label intentionally.

## What it reports

The default `check` view provides:

- Provider-grouped panels with account labels and known plan tags.
- Utilization for primary 5-hour and 7-day windows plus named model limits,
  such as Claude Opus/OAuth or Codex Spark windows when returned.
- Local reset countdowns and inline recovery details for failed accounts.
- A narrow-terminal fallback when the full heat-panel layout does not fit.
- Exact token activity at normal widths and precision-preserving compact totals
  in the narrow fallback.

The activity subtitle uses one provider-neutral presentation contract:

- Claude matches Claude Code's local `/stats` accounting. It adds historical
  input and output tokens from `stats-cache.json` to live UTC-day input and
  output tokens from project transcripts, while excluding cache-read and
  cache-creation tokens. Its verified `firstSessionDate` supplies the footer's
  `since` date.
- Codex reads each eligible saved ChatGPT account's authoritative token profile
  and sums `lifetime_tokens`. When valid unique daily buckets sum exactly to
  the lifetime value, their earliest date supplies `since`. Each successful
  account profile is retained as a strict Sidekick-owned snapshot, so a later
  authentication failure does not erase the last authoritative value.

Both panels render the same footer shape. These values are illustrative, not
live account data:

```text
915,947,703 tokens  ·  since Dec 28, 2025
7,455,971,162 tokens  ·  since Apr 7, 2026
```

The supported narrow layout keeps the full year on a deliberate second line:

```text
CLAUDE · 915.95M tokens
         since Dec 28, 2025

CODEX · 7.456B tokens
        since Apr 7, 2026
```

Sidekick never substitutes Codex rollout files, SQLite state, or the obsolete
output-only cache. An account without a successful profile snapshot has no
recoverable lifetime value. Malformed, unreadable, authentication, transport,
and snapshot-persistence failures remain explicit while valid usage and
retained activity stay visible; the command renders the result before returning
its typed non-zero status.

## How provider access works

`sidekick-usages` uses the same provider backends and credential shapes used by
the installed CLIs. It does not scrape terminal output or use a headless
browser for usage checks.

### Claude Code

[Claude-specific documentation](./docs/claude/README.md) records provider
schema authority, retrieval guidance, and symptom-first debugging. Public
schemas described there are documentation and authoring contracts; they are
not additional Sidekick runtime dependencies.

Credential discovery checks:

- macOS Keychain item `Claude Code-credentials`.
- Linux/WSL `~/.claude/.credentials.json`, then
  `~/.config/claude/.credentials.json`.
- Native Windows Claude credential files, then Windows Credential Manager.

The usage route depends on the saved credential variant:

- Subscription-login credentials call
  `https://api.anthropic.com/api/oauth/usage` and can report 5-hour, 7-day,
  7-day Opus, and 7-day OAuth-app windows when returned.
- Setup-token credentials cannot use that endpoint. They
  POST a request with one input word and `max_tokens=1` to
  `https://api.anthropic.com/v1/messages`, discard the body, and read the
  unified 5-hour and 7-day rate-limit headers. This real request consumes a
  small amount of Claude quota.

Saved Claude subscription logins rotate only through official Claude login in
the account's stable private profile. Sidekick validates the protected
identity and credential generation afterward; it never calls Anthropic's
OAuth refresh endpoint itself. Usage and heartbeat read the verified native
authority for the selected account and the private authority for each inactive
account. Background maintenance keeps every private copy fresh and separately
refreshes the selected native authority without changing its identity or
selecting another account. Setup-token credentials cannot auto-refresh.

Access-token expiry and login expiry are independent. When a known Claude
login expiry is at or inside five days, maintenance emits a manual renewal
warning without recording a failed refresh. An expired login fails closed
before provider traffic. A setup token's issue date is not encoded in its
value, so Sidekick cannot infer its one-year deadline.

Claude token activity is read-only local state. Sidekick reads the documented
statistics cache and the live transcript suffix needed to match Claude Code's
current `/stats` total. It never refreshes, rewrites, locks, renames, or deletes
Claude-owned activity files.

### Codex CLI

[Codex-specific documentation](./docs/codex/README.md) records provider
research, app-server schema guidance, and architecture status. Proposed
behavior there is not part of the current runtime unless an implementation
record says otherwise.

`sidekick-usages codex login <label>` and `sidekick-usages refresh <label>`
authenticate a saved Codex label directly in its stable managed home. Sidekick
verifies the expected provider identity and an advanced provider-owned
generation before committing the managed authority. Protected authentication
state is verified only inside that managed home. The native Codex home remains
untouched.

Ordinary Codex TUIs connected to the supported shared daemon receive later
account updates. A TUI launched before daemon enrollment must be restarted
once. `codex exec`, native Windows, and launch modes that bypass daemon reuse
are not switchable; in-flight requests are never retargeted.

Usage calls `https://chatgpt.com/backend-api/codex/usage` with a short-lived
projection from the exact account's protected managed home. It reports the
primary 5-hour window, secondary 7-day window, and provider-returned additional
model limits. The official Codex process owns refresh inside each managed home;
Sidekick does not perform a private OAuth exchange or touch the active
`~/.codex` login.

Token activity independently calls
`https://chatgpt.com/backend-api/wham/profiles/me` for each eligible saved
account and validates the returned `stats.lifetime_tokens`. A rate-limit
endpoint failure does not suppress this independent safe read. An activity
authentication failure does not start a second refresh loop, and no local
rollout total is used as a fallback.

## Commands

| Command | Purpose |
| --- | --- |
| `sidekick-usages` | Open the cached-first interactive dashboard on a supported TTY; redirected I/O remains one-shot. |
| `sidekick-usages check` | Run a one-shot usage check. |
| `sidekick-usages --only <provider>` | Show only `claude` or `codex` accounts. |
| `sidekick-usages --no-interactive` | Render once without reading terminal input. |
| `sidekick-usages use <provider> <label>` | Select one exact saved account without prompting or installing the service. |
| `sidekick-usages migrate managed-auth` | Migrate saved accounts to verified provider-managed authorities. |
| `sidekick-usages add claude` | Save auto-detected, piped, prompted, or `--token` Claude credentials; supports `--label`, `--plan`, and `--force`. |
| `sidekick-usages remove <label>` | Delete one saved account. |
| `sidekick-usages rename <old> <new>` | Rename one saved account. |
| `sidekick-usages set-plan <label> <plan>` | Correct a display plan that the provider cannot introspect. |
| `sidekick-usages refresh <label>` | Repair one Claude or Codex label through official login in its stable private profile. |
| `sidekick-usages refresh --all [--force] [--quiet]` | Maintain every due saved authority without reading the current global login. |
| `sidekick-usages maintain [--quiet]` | Manually refresh due tokens, then heartbeat opted-in accounts. |
| `sidekick-usages doctor [--provider ...] [--label ...] [--json]` | Report independent supervisor, persistence, provider, auth, refresh, and heartbeat health. |
| `sidekick-usages codex login <label>` | Run official Codex login in the label's final managed home; supports `--device-auth`. |
| `sidekick-usages claude setup-token` | Run Claude's long-lived token generator and save its output. |
| `sidekick-usages permissions repair` | Preview and repair Sidekick-owned credential permissions. |
| `sidekick-usages reset [--provider <id>] [-y]` | Delete all accounts or one provider's accounts. |
| `sidekick-usages check-update` | Query the latest GitHub release. |
| `sidekick-usages update [--dry-run]` | Run the detected `uv`, pipx, or Homebrew upgrade command. |
| `sidekick-usages daemon install\|status\|uninstall` | Manage the current user's resident account supervisor. |
| `sidekick-usages heartbeat ...` | Inspect, warm, enable, disable, or report usage-window heartbeat state. |
| `sidekick-usages --version` | Print the installed version. |

Append `--help` or `-h` to the root command, any command group, or any command
to see its options.

### Refresh identity safety

For Claude, `refresh <label>` operates only on that label's stable private
profile. A legacy saved subscription authority may seed official login there;
the active native Claude login is never read or changed. A setup-token-only
label retains its setup token and requires `--replace-identity` once to approve
the first subscription identity association. Later identity mismatches fail
closed.

For Codex, `refresh <label>` starts official browser login in the label's final
managed home. It never imports the active native login. `refresh --all` uses
only saved or managed authorities.

### Updating a Git-tag installation

`check-update` accurately reports the latest GitHub release. The built-in
`update` command delegates to the detected package manager. A Git-tag uv install
is intentionally pinned, so update it by rerunning the tagged `uv tool install
--force` command from [Installation](#installation) with the new tag.

## Background maintenance and heartbeat

Token refresh and heartbeat are separate:

- `refresh --all` maintains Claude stored authorities and each independent
  Codex managed home.
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

Manage the user-level resident supervisor with:

```bash
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

Under the next-release interactive contract, the first action that needs an
absent service keeps the cached view visible and asks once for approval.
Approval installs and verifies the current user's service without
administrator rights, then resumes the original action. Declining or a bounded
setup failure leaves the dashboard usable.

| Platform | Backend |
| --- | --- |
| Linux | One systemd user service |
| WSL | The same systemd user service plus a Windows logon rescue task |
| macOS | One user LaunchAgent |
| Native Windows | Resident supervision is explicitly disabled |

There is no backend override, timer, or cron fallback. The service owns one
durable queue and launches bounded workers when account work is due. The WSL
rescue task only starts the Linux service; it never refreshes credentials
itself.

`daemon uninstall` removes only Sidekick's service definition, WSL rescue task
or LaunchAgent, socket, service state, and diagnostic logs. It preserves saved
accounts, private credentials, usage metrics, and the active native Claude and
Codex logins. It creates no shell alias or wrapper, so normal `claude` and
`codex` commands continue to use their native login locations.

See [token maintenance and daemon operations](./docs/token-maintenance.md) and
[heartbeat behavior and guardrails](./docs/heartbeat.md) for lifecycle, health,
exit-code, and recovery details.

## Security and network access

- `accounts.json` is a secret-free account index. Raw provider credentials live
  only in the protected private authority tree. Unix directories/files use
  `0700`/`0600`; Windows protection uses the current user's filesystem ACLs.
- `list` masks token values, and `doctor --json` excludes access tokens,
  refresh tokens, id tokens, and raw provider credentials.
- Claude token entry is hidden. `sidekick-usages add claude` consumes piped
  stdin only when Claude credential auto-detection finds no login. Its
  `--token` option can expose a secret in shell history or process listings, so
  prefer Claude auto-detection, stdin, or the hidden prompt.
- Every built-in HTTP request rejects non-HTTPS URLs.
- Requests use bounded pooled connections, verified TLS, closed operation
  classes, and retry behavior that distinguishes safe reads from mutations.
- There is no analytics or telemetry, and no automatic update check. The
  explicit `check-update` command contacts GitHub.

Runtime network destinations are:

| Purpose | Destination |
| --- | --- |
| Claude usage and tiny header probe | `api.anthropic.com` |
| Official Claude login and refresh | Provider destinations owned by Claude CLI |
| Codex usage, token activity, and heartbeat | `chatgpt.com` |
| Official Codex login and refresh | Provider destinations owned by Codex CLI |
| Explicit release check | `api.github.com` |

Provider credentials are sent only through that provider's owned usage, model,
or authentication client. The official managed Codex process owns Codex
authentication traffic. GitHub release checks do not include provider
credentials. Installation and explicit updates additionally contact the
selected package manager's normal GitHub, Homebrew, or package-index
destinations.

See [HTTP transport and retry behavior](./docs/networking.md) for proxy, TLS,
pool, timeout, payload-bound, retry-safety, and error contracts.

## Persistence and recovery

Sidekick uses one native per-user application-data layout on Linux, WSL,
macOS, and Windows. The account index is secret-free; provider credentials and
private homes live under a separate protected authority. `doctor` reports
current store and recovery state without exposing either credentials or
provider identities.

Do not manually copy or edit account, credential, journal, or private-home
files. See [persistence and recovery](./docs/persistence-and-recovery.md) for
the platform path matrix, permission repair, reset behavior, and supported
account recovery flows.

## Configuration

On Linux and WSL, the current store defaults to
`~/.local/share/sidekick-usages/accounts.json`. macOS and Windows use their
native per-user application-data locations.

The current account index is a strict schema-version-three envelope keyed by
stable Sidekick account identifiers. Each record carries a provider-qualified
label and references a protected credential authority:

- Claude setup-token and subscription authorities are independent.
- Claude stored-login and Codex managed authorities record only safe health,
  expiry, identity, and generation metadata in the account index.
- Provider credential values remain exclusively in the protected credential
  tree.

Important field semantics:

- Access, refresh, audit, and heartbeat times use canonical UTC text.
- `provider_account_id` binds Codex requests and managed authority updates to
  the expected account when the id is known.
- Refresh and heartbeat diagnostics are redacted user-facing state. Heartbeat
  targets and reset caches may be absent and remain target-specific.

Do not edit the store by hand. Use CLI commands so identity checks, file modes,
managed Codex homes, and diagnostics remain consistent.

## Troubleshooting

### No accounts saved

Import a Claude login:

```bash
sidekick-usages add claude --label <claude-label>
```

Official Codex login repairs an existing enrolled label:

```bash
sidekick-usages codex login <codex-label>
```

It repairs only an existing saved Codex label; it does not create a new saved
identity after a clean reset.

### HTTP 401 or failed refresh

Saved Claude subscription logins rotate access credentials before known access
expiry and retry once after HTTP 401. Codex authentication failures recover
through the managed authority, never through inline private OAuth.

For a Claude subscription login, repair that exact saved label:

```bash
sidekick-usages refresh <claude-label>
```

Sidekick opens provider-controlled login only when the label's private
authority requires it. The native Claude login remains unchanged.

For a rejected setup token, capture a new setup token instead of importing the
active subscription login:

```bash
sidekick-usages claude setup-token --label <setup-label> --force
```

For Codex, use its explicit login workflow:

```bash
sidekick-usages codex login <codex-label>
```

For Claude, a known provider-account mismatch requires the explicit
`--replace-identity` authorization. Codex instead verifies its managed-home
login against the already saved identity.

For non-obvious Claude failures, use the
[Claude debugging guide](./docs/claude/debugging.md). It covers separate
access/login lifetimes, the five-day login-renewal warning, explicit
credential transitions, cause-only diagnostics, and recovery evidence.

### Claude setup-token shows fewer windows

This is expected. An inference-only setup token cannot read
`/api/oauth/usage`, so sidekick reads only 5-hour and 7-day unified headers from
the tiny `/v1/messages` probe. It cannot auto-refresh because it has no refresh
token. Correct a cosmetic plan label with:

```bash
sidekick-usages set-plan <label> <plan>
```

### Missing Codex account id

Repair the existing label with official managed login:

```bash
sidekick-usages codex login <label>
```

The login runs only in the account's stable Sidekick-managed home. It does not
adopt or overwrite the native Codex login.

### HTTP 429 or transient network errors

The HTTP client retries only when the closed operation-safety policy permits
it. Safe reads may retry selected 429, 5xx, and ambiguous transport failures;
Credential refreshes and heartbeat model requests fail closed when repetition
could duplicate a mutation. After attempts stop, wait for the shown
`Retry-After` interval when available and run the command again. See
[HTTP transport and retry behavior](./docs/networking.md).

### Daemon is installed but accounts do not rotate

Run maintenance explicitly, then inspect independent supervisor and account
health:

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
security find-generic-password -s 'Claude Code-credentials' >/dev/null
```

If it is missing, run `claude auth login` and retry `add`. Repair an already
saved label with `sidekick-usages refresh <label>`, which uses only that
label's private managed profile.

## Development

### Repository layout

- `src/sidekick_usages/`: Typer CLI, provider adapters, storage, rendering,
  refresh, heartbeat, doctor, daemon, and update logic.
- `src/sidekick_usages/core/`: provider-neutral models, identifiers, expiry,
  and aware-UTC invariants with no infrastructure dependencies.
- `src/sidekick_usages/cli/`: registration-only application root, typed lazy
  composition, help adapter, token input, and cohesive command owners.
- `src/sidekick_usages/providers/`: Claude and Codex boundary schemas and
  provider-specific adapters.
- `src/sidekick_usages/http/`: provider-neutral pooled HTTPS transport and
  retry policy.
- `src/sidekick_usages/persistence/`: strict schemas, qualified filesystem
  operations, account/private transactions, credential-refresh recovery, and
  selected-account state.
- `src/sidekick_usages/credentials/`: provider-neutral credential workflows,
  Claude transition/lifetime policy, serialized refresh coordination,
  and managed Codex home and authority coordination.
- `src/sidekick_usages/usage/`: usage results, application service, and Rich
  presentation; `branding.py` is the one robot and product-copy source.
- `tests/`: focused pytest coverage for CLI behavior, providers, HTTP errors,
  storage, rendering, maintenance, packaging, and cross-platform supervision.
- `docs/`: shared operational guides, provider research and schemas,
  architecture specifications, and implementation records.
- `packaging/homebrew/`: formula generator and in-tree formula copy.
- `.github/workflows/`: CI, release, PyPI-gated publish, and Homebrew automation.

### Setup and validation

```bash
git clone https://github.com/Sawmonabo/sidekick-usages
cd sidekick-usages

uv python install 3.14
uv sync --all-groups

uv run sidekick-usages -h
uv run python packaging/check_architecture.py
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files

npm ci
npm audit --audit-level=moderate
npm run lint:markdown

uv build
uv run python packaging/smoke_wheel.py --build
```

The Markdown toolchain requires Node.js 22 or newer; `package.json` records
that development-only runtime floor.

CI runs pre-commit, the full pytest suite on Linux, macOS, and Windows with
Python 3.14, then builds, benchmarks, and smoke-tests the exact installed
wheel. No minimum coverage percentage is configured.

Ruff targets Python 3.14, double quotes, LF endings, a 79-column source and
docstring limit, and explicit annotation enforcement. Use PEP 604 unions such
as `str | None`, Python 3.14 native type parameters and aliases, and
Sphinx-style public docstrings. Ty treats warnings as errors. Pytest discovers
`tests/test_*.py` and `test_*` functions under strict marker and configuration
checks.

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
