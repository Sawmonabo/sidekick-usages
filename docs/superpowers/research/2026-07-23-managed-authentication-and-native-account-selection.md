# Research: Managed Authentication and Native Account Selection

**Investigation date:** 2026-07-23

**Evidence cutoff:** 2026-07-25T23:59:59Z

**Decisions:** Replace Sidekick-owned Codex OAuth refresh with one
independently authenticated, Sidekick-owned `CODEX_HOME` per saved account and
use managed Codex app-server as the token authority for that home. Extend the
normal Sidekick dashboard with a TTY account cursor. An explicit `USE` action
changes the provider's shared native runtime: Claude's native credential
authority and Codex's native shared app-server. Do not create a command
wrapper, alias, shell function, or replacement vendor executable.

## Claude 2.1.220 Implementation Revalidation

The Claude implementation baseline was revalidated on 2026-07-24 before code
was changed. This section supersedes the report's earlier Claude 2.1.218
implementation assumptions; those passages remain as dated investigation
history.

The installed executable resolves to
`~/.local/share/claude/versions/2.1.220`. It is 275,012,592 bytes and has
SHA-256
`674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863`.
That digest matches Anthropic's official Linux x64 2.1.220 artifact. The
installed binary reports `2.1.220 (Claude Code)` and contains build ID
`788318c9115981678ca1a25f40cdb3b39df71403`. The public release tag resolves
to commit `7ef6eec9d9ba84ea6f233f26c45f1df5c5991843`.
[Claude Code v2.1.220 release](https://github.com/anthropics/claude-code/releases/tag/v2.1.220),
[official checksums](https://github.com/anthropics/claude-code/releases/download/v2.1.220/SHASUMS256.txt),
[tag commit](https://github.com/anthropics/claude-code/commit/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843).

The supported read-only capability probes are:

```bash
claude --version
claude auth status
claude auth login --help
```

Anthropic documents that `claude auth status` returns JSON by default and
offers `--text`; `--json` is accepted by the inspected artifact but is not a
documented contract, so Sidekick does not use it. An isolated empty-profile
probe returned exit code 1 with the exact logged-out object
`{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}`.
That proves profile isolation and response compatibility, not live provider
validity. The probe must therefore run with disposable `HOME` and
`CLAUDE_CONFIG_DIR` values and never against the user's native profile.
[Claude CLI commands](https://code.claude.com/docs/en/cli-usage#cli-commands).

The official multi-account boundary remains `CLAUDE_CONFIG_DIR`. Linux and WSL
use the profile's protected `.credentials.json`; WSL uses the Linux credential
backend. macOS keeps credentials in Keychain. Inspection of both exact 2.1.220
macOS artifacts reconfirmed the compatibility-sensitive namespace rule:
the default service is `Claude Code-credentials`, while a non-default
normalized config path uses `Claude Code-credentials-` plus the first eight
lowercase hexadecimal characters of its SHA-256 digest. Sidekick must
version-gate that binary-only observation and fail closed if Claude falls back
to plaintext storage.
[Claude environment variables](https://code.claude.com/docs/en/env-vars#environment-variables),
[Claude credential management](https://code.claude.com/docs/en/authentication#credential-management),
[Claude WSL installation](https://code.claude.com/docs/en/installation#set-up-on-windows).

Anthropic's supported refresh-token handoff is
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN` together with the account's exact
space-separated `CLAUDE_CODE_OAUTH_SCOPES`. It is an official login
provisioning input, not permission for Sidekick to implement OAuth rotation.
The value may exist only in the closed child environment for the target
profile; it must never enter argv, persistence, logs, errors, or the broker.

The exact installed Linux x64 binary strengthens the existing-session contract.
Every normal API-client construction runs Claude's credential-freshness check
before reading OAuth credentials. On Linux and WSL, that check stats the shared
profile's `.credentials.json`; a changed modification time clears the OAuth and
secure-storage caches before the request reads the new login. When that file is
absent, the same path clears cached readers and reads secure storage again,
which is the path used by the macOS Keychain backend. A release-matched macOS
runtime smoke remains required because the Linux artifact cannot execute that
backend.

The resulting product contract is exact: after official login completes, a
new bare `claude` uses the selected native authority immediately. An existing
ordinary subscription session sharing that native profile adopts it when the
session begins its next normal API attempt. An already-streaming request keeps
the client and credential with which it started. Environment-authenticated,
cloud-provider, helper, gateway, or alternate-profile sessions remain pinned
to their own higher-priority authority.

Claude exposes no supported attachable credential-reload channel for arbitrary
foreground terminals. `SIGHUP` terminates the inspected foreground process;
the daemon control socket has no auth-reload operation; and structured token
updates work only when a controller already owns the process's structured
standard input. Official login also preserves in-process tokens instead of
deliberately revoking sibling sessions. Sidekick must therefore rely on the
next-request freshness path and must never signal, inject input into, or
restart unrelated terminals.

Remote Control is likewise not externally observable with exact certainty.
Anthropic documents that it can be enabled after launch with
`/remote-control`, can be enabled automatically for every session, and uses
outbound HTTPS without opening an inbound local port. The documented CLI has
no external Remote Control status command. Sidekick may prove absence only
when no same-user Claude foreground exists; otherwise it must conservatively
require the one disruption approval because Remote Control cannot be ruled
out. [Remote Control](https://code.claude.com/docs/en/remote-control),
[Claude CLI reference](https://code.claude.com/docs/en/cli-usage).

## Table of Contents

- [Claude 2.1.220 Implementation Revalidation](#claude-21220-implementation-revalidation)
- [Executive Summary](#executive-summary)
- [Saved-Account Freshness and Metrics Gate](#saved-account-freshness-and-metrics-gate)
- [Required Operating Model: Private Authorities and Shared Runtimes](#required-operating-model-private-authorities-and-shared-runtimes)
- [Native Global Account Selection](#native-global-account-selection)
- [Research Scope and Method](#research-scope-and-method)
- [Key Findings](#key-findings)
  - [The apparent contradiction is real but scoped](#the-apparent-contradiction-is-real-but-scoped)
  - [The current design duplicates a rotating credential lineage](#the-current-design-duplicates-a-rotating-credential-lineage)
  - [Sidekick persistence is internally consistent](#sidekick-persistence-is-internally-consistent)
  - [Sidekick loses the exact authority failure](#sidekick-loses-the-exact-authority-failure)
  - [Codex already implements the correct refresh state machine](#codex-already-implements-the-correct-refresh-state-machine)
  - [A documented no-model-turn refresh method exists](#a-documented-no-model-turn-refresh-method-exists)
- [Comparative Analysis](#comparative-analysis)
- [Recommendations](#recommendations)
- [Implementation or Decision Implications](#implementation-or-decision-implications)
- [Risks and Open Questions](#risks-and-open-questions)
- [Source Matrix](#source-matrix)

## Executive Summary

The user's active Codex session is healthy. Sidekick's saved Codex credentials
are not. Both observations are true because they describe different auth homes
and different credential generations.

Live validation established:

- the default native Codex home is logged in with ChatGPT;
- its access token is fresh through 2026-08-01;
- `codex doctor --json` completed an authenticated Responses WebSocket
  handshake with HTTP 101;
- Sidekick's two saved Codex access tokens are expired;
- both Sidekick refresh attempts were rejected on 2026-07-23; and
- for the saved account matching the active native identity, the native and
  Sidekick access and refresh tokens are different.

Sidekick's canonical account rows exactly match their corresponding private
`auth.json` bundles. The failure is not a torn account/private transaction or
credential-file corruption. Sidekick consistently persisted credential
generations that are now stale or rejected.

The root design problem is authority duplication:

1. Sidekick runs or reads an official Codex login in a source `CODEX_HOME`.
2. It copies that managed token bundle into a separate private home and its
   account store.
3. The source home remains usable.
4. Sidekick later refreshes its copy by directly calling Codex's private OAuth
   endpoint.
5. Official Codex refreshes or replaces the source generation independently.
6. The two durable copies diverge, and an old refresh token eventually becomes
   unusable.

This is inconsistent with OpenAI's current guidance. The official CI/CD auth
guide says not to call the OAuth endpoint yourself, to let Codex update the
file, and to give each `auth.json` one serialized owner. An OpenAI maintainer
also states that independent `CODEX_HOME`s should authenticate separately
rather than share copied auth. [OpenAI CI/CD auth
guide](https://learn.chatgpt.com/docs/auth/ci-cd-auth),
[openai/codex issue #15410](https://github.com/openai/codex/issues/15410).

The recommended method is:

1. Allocate one final Sidekick-owned, file-backed `CODEX_HOME` for each saved
   account.
2. Authenticate that home independently through official Codex. Do not copy a
   managed bundle from the active native home.
3. Invoke the installed Codex app-server against that private home.
4. Use `account/read` with `refreshToken: false` for scoped account state and
   `refreshToken: true` when a forced refresh is actually needed.
5. Let Codex rotate and persist its own bundle.
6. Re-read the protected file and persist only sanitized identity, expiry,
   generation, and outcome metadata in Sidekick.
7. Keep the default native Codex credential file read-only; project the
   selected private account into Codex's shared runtime through a broker.

OpenAI's current app-server documentation explicitly says managed ChatGPT auth
is Codex-owned and `refreshToken: true` forces a token refresh. The exact
installed Codex 0.145.0 binary generated a version-matched schema containing
that same default method. [OpenAI app-server auth
documentation](https://learn.chatgpt.com/docs/app-server#auth-endpoints).

The requested native account selector is feasible on Linux, WSL, and macOS
without intercepting either provider command:

- the normal Sidekick dashboard can keep its current usage display and add a
  small up/down cursor before each account bullet;
- Enter can activate the highlighted account for that provider;
- ordinary `claude` continues resolving to Anthropic's binary and follows
  Claude's shared native credential authority;
- ordinary `codex` continues resolving to OpenAI's binary and, after one-time
  native daemon enrollment, follows Codex's shared app-server; and
- maintenance remains independent and continues for every saved account.

Anthropic now documents `CLAUDE_CONFIG_DIR` as useful for running multiple
accounts side by side, including the fact that macOS keeps credentials in the
system Keychain. Inspection of Anthropic's exact macOS arm64 and x64 Claude
Code 2.1.218 packages confirmed how those statements compose: a non-default
config directory selects a distinct Keychain service suffixed with the first
eight hexadecimal characters of that normalized directory's SHA-256 digest.
The default bare `claude` command continues using the unsuffixed native
Keychain entry. This removes the earlier macOS isolation blocker.

Claude's released changelog confirms parallel sessions share one credential
store and that current sessions recover when credentials are refreshed
outside them. Codex requires the shared-daemon design because its managed auth
manager deliberately ignores external `auth.json` changes until explicit
reload and refuses an unauthorized reload when the account ID changed.

The Codex daemon can broadcast `account/updated` to every connected TUI.
Sidekick can supply the selected account and answer refresh requests from the
account's independently managed private home. This is the richest no-wrapper
method, but the required `chatgptAuthTokens` method is marked unstable and
internal-only by OpenAI, so it must be exact-version gated.

It also requires a resident Sidekick broker. The existing Sidekick “daemon”
is a periodic one-shot `maintain --quiet` scheduler, not a continuously
running process, and cannot satisfy Codex's ten-second refresh callback. The
future selector must talk to a single-instance user service that holds the
broker connection after the dashboard exits.

The current live Codex sessions predate such setup: two processes are running,
but the native app-server control socket is absent. They need one restart
after daemon enrollment. Later daemon-connected ordinary TUIs can switch at
their next safe request. `codex exec`, native Windows Codex 0.145.0, and
launches that bypass daemon reuse remain outside this first capability.

The required first-release platform set is Linux, WSL, and macOS. Native
Windows is deliberately outside this design; it is not a substitute for any
of those three required environments.

No application source, credentials, login state, provider daemon, or token
authority state was changed during this investigation. This research report
is the only published repository artifact.

## Saved-Account Freshness and Metrics Gate

### Current result

The requirement is **not satisfied by the current live Codex state**, even
though current account enumeration is correctly selection-independent.

The current implementation already has the right population behavior:

- [`TokenMaintenanceService.refresh_all()`](../../../src/sidekick_usages/maintenance.py)
  snapshots the full saved-account store and evaluates every account unless
  the operator explicitly supplies a provider filter.
- [`UsageService.check()`](../../../src/sidekick_usages/usage/service.py)
  checks every saved account, refreshes an expired or near-expiry credential
  before fetching that account's usage, and returns per-account failures
  instead of hiding them.
- [`HeartbeatService.heartbeat_all()`](../../../src/sidekick_usages/heartbeat/service.py)
  iterates every enabled saved account.

A read-only doctor pass on 2026-07-23 found six saved accounts:

- four Claude setup-token accounts were warmed and required no manual action;
  and
- two Codex login accounts had expired access tokens, failed refresh status,
  failed heartbeat status, and required manual action.

Account labels and credential values were excluded from this check. Therefore
the dashboard can enumerate every account today, but it cannot display current
Codex metrics for the two failed saved authorities.

### Required resolution

Native account selection may ship only with these invariants:

1. Selection never filters maintenance, heartbeat, usage collection, or
   dashboard rows. It changes only the provider runtime used by normal
   `claude` or `codex` commands.
2. Every saved Codex account has one independently authenticated private
   `CODEX_HOME`; official managed Codex refreshes that home, and Sidekick does
   not copy tokens from the native home or call the private OAuth endpoint.
3. The selected Codex runtime is a projection from one private home. Scheduled
   maintenance still processes every private home.
4. For Claude, the selected subscription account uses the verified native
   authority while inactive subscription accounts use private authorities.
   Setup tokens remain non-refreshing credentials whose validity and lifetime
   are tracked honestly.
5. One account's refresh, login, heartbeat, broker, or metrics failure does not
   prevent later accounts from being attempted.
6. A metrics row is current only after that exact account completes a
   successful authenticated fetch. On failure, Sidekick shows an explicit
   unavailable or last-known result with its timestamp; it never presents
   stale data as current.
7. Active-account state comes from provider read-back and is independent of
   credential and metrics health. The approved dashboard communicates that
   state by initially anchoring the focused provider's cursor on the active
   account, not by adding an `IN USE` row label. An account can be active yet
   unhealthy, or inactive yet healthy and fully measured.

The managed private-home design described below resolves the failed Codex
authority problem while preserving the current all-account enumeration
behavior. These invariants are release gates for the account-selection
feature, not optional follow-up work.

## Required Operating Model: Private Authorities and Shared Runtimes

The original three-home finding remains part of the design, with one important
addition: Sidekick maintains each saved account privately, then explicitly
projects the selected account into the provider's shared native runtime.

```text
Codex account A -> Sidekick private managed CODEX_HOME A --+
                                                          |
Codex account B -> Sidekick private managed CODEX_HOME B --+
                                                          v
                                           native Codex app-server
                                                          |
                                       ordinary `codex` terminal TUIs
```

```text
Claude inactive account A -> Sidekick private authority A
Claude inactive account B -> Sidekick private authority B
                                           |
                                           v
                          selected native Claude credential authority
                                           |
                                 ordinary `claude` terminals
```

For each saved Codex account, Sidekick should periodically:

1. open only that account's private Codex home;
2. ask managed Codex to refresh it when needed;
3. verify the same account and an advanced credential generation;
4. save only sanitized refresh and health status; and
5. continue to the next account.

The Codex broker supplies only the selected account's current access token and
account ID to the native shared app-server. If Codex requests a refresh, the
broker routes it back to the matching private managed home. The default native
Codex auth file does not become a second rotating-token authority.

For Claude, the explicitly selected account becomes the native shared
credential authority. The prior active account's latest verified generation
must first be retained safely under its private saved-account authority. While
an account is active, Sidekick observes the native generation instead of
refreshing a stale private duplicate.

The saved accounts remain independent. A failure that requires account A to be
authenticated again must not stop account B from refreshing or operating.

Normal provider commands remain vendor-owned:

```text
`claude` -> Anthropic's installed binary
`codex`  -> OpenAI's installed binary
```

Sidekick adds no executable in front of either command. It does not change
`PATH`, install aliases, replace symlinks, or require a new shell. Only the
user's explicit `USE` action changes a provider runtime; background
maintenance must never silently switch the active account.

### Required platform contract

The first release must provide the same product contract on:

- Linux with a per-user systemd service;
- WSL with a per-user systemd service while the distribution is active and a
  Windows Task Scheduler rescue/start trigger because systemd does not keep a
  WSL instance alive; and
- macOS with a per-user LaunchAgent and the user's login Keychain.

All three use one resident Sidekick supervisor per OS user, not one daemon per
account or provider. That supervisor owns the selector control socket and the
Codex refresh-broker connection. Due maintenance runs in isolated short-lived
workers so provider imports, hangs, or crashes do not inflate or terminate the
resident control plane.

Provider homes and credential storage remain platform-specific:

| Boundary | Linux | WSL | macOS |
|---|---|---|---|
| Claude private account | Private config directory and credential file | Private config directory and credential file | Private config directory and config-derived Keychain item |
| Claude native selection | Default credential file | Default credential file | Default unsuffixed Keychain item |
| Codex private account | Private `CODEX_HOME` | Private `CODEX_HOME` | Private `CODEX_HOME` |
| Codex shared runtime | Official Unix control socket | Official Unix control socket | Official Unix control socket |
| Supervisor recovery | systemd user manager | systemd plus Task Scheduler rescue | LaunchAgent |

Native Windows is outside the first release. A platform adapter may share
code with WSL support, but native Windows must not weaken or delay the three
required environments.

## Superseded Launcher Analysis

> **Historical only:** The analysis below answered the earlier, narrower
> private-home launcher proposal. It is not the selected product design.
> The user requires native global switching without wrappers or aliases. The
> replacement design follows this collapsed section.

<!-- markdownlint-disable MD033 -->

<details>
<summary>Earlier private-home launcher investigation</summary>

### Previously requested context

This preserves the earlier requested note so its reasoning is not lost. Its
claim that normal Codex stays on a separate native account is superseded by
the later native-global selection requirement.

> Yes—with one important condition: separating the accounts is not enough by
> itself. Sidekick must also run maintenance for each account individually.
>
> ```text
> Normal terminal `codex`
>         -> ~/.codex
>         -> your normal/native Codex account
>
> Sidekick account A
>         -> Sidekick private Codex home A
>         -> account A credentials
>
> Sidekick account B
>         -> Sidekick private Codex home B
>         -> account B credentials
> ```
>
> For each saved account, Sidekick would periodically open that account's
> private Codex home, ask the official Codex process to refresh it when
> needed, confirm that expiry advanced, save only that account's refreshed
> status, and continue to the next account. Account A and account B remain
> independent; refreshing one must not overwrite or invalidate the other.
>
> Under that earlier design, running `codex` normally continued to use
> `~/.codex`. Sidekick set `CODEX_HOME` only for the private Codex child
> process it launched, so the setting did not modify the current terminal or
> later `codex` commands.

### Verdict

The design is feasible and safe on the current Linux/WSL installation if
“global” is defined accurately:

> The selected account is the default for new terminal launches routed
> through Sidekick.

It does not and should not change an already-running process, a desktop or
cloud session, or a provider process launched outside Sidekick integration.

The selector and maintenance loop are separate:

```text
selected Codex account  -> next Sidekick-routed `codex`
selected Claude account -> next Sidekick-routed `claude`

maintenance             -> every saved account, selected or not
```

Changing the selection must not copy, rotate, log out, or otherwise mutate
credentials. It writes only a non-secret provider-and-label pointer.

### Codex selection is fully supported

OpenAI documents `CODEX_HOME` as the root for Codex config, auth, logs,
sessions, skills, and package metadata. It defaults to `~/.codex`.
[OpenAI environment-variable
reference](https://learn.chatgpt.com/docs/config-file/environment-variables).

The Codex 0.145.0 release-matched source confirms:

- file credentials live at `<CODEX_HOME>/auth.json`;
- an explicit home is the config and auth boundary;
- file, keyring, auto, and ephemeral auth stores are supported; and
- keyring entries are namespaced from the canonical `CODEX_HOME` path.

Sources: [home
discovery](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/core/src/config/mod.rs#L4432-L4441),
[`auth.json`
location](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/storage.rs#L150-L152),
[keyring
namespace](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/storage.rs#L226-L245).

This means a private home remains account-specific even if Codex later uses
the OS keyring.

The installed CLI has no auth-account selector. Its `--profile` flag only
loads `$CODEX_HOME/<name>.config.toml`; it does not choose credentials. A
selected account must therefore select the entire home.

A local empty-home probe confirmed the installed binary honors the boundary:
with `CODEX_HOME` set to a new empty directory, `codex login status` returned
`Not logged in`. It did not use or change the native login.

One product consequence must be explicit: a private Codex home separates more
than tokens. User config, sessions, skills, plugins, logs, and other home state
also follow the selection. The first implementation should not symlink or
share auth or SQLite state between accounts.

### Claude selection is platform-dependent

Anthropic documents Claude subscription credentials at:

- macOS: the encrypted macOS Keychain;
- Linux: `~/.claude/.credentials.json`, mode `0600`;
- Windows: `%USERPROFILE%\.claude\.credentials.json`; and
- Linux or Windows with `CLAUDE_CONFIG_DIR`: under that directory.

[Claude Code credential
management](https://code.claude.com/docs/en/authentication#credential-management).

The installed Claude Code 2.1.218 binary honors this on the current machine.
With `CLAUDE_CONFIG_DIR` pointed at a new empty temporary directory,
`claude auth status --json` reported a safe `loggedIn: false` state and
initialized only that disposable directory. It did not adopt or change the
native credential.

A second installed-binary probe ran `claude daemon status` under two distinct
empty config directories. The current Linux binary selected a different
background-service socket namespace for each directory and reported no daemon
or workers in either. This is strong local evidence that background Claude
services are isolated by the selected config directory in 2.1.218.

That daemon behavior is not stated as a public cross-platform account
contract. Implementation still needs release-gated Linux and Windows tests,
and changing selection must not terminate an already-running service or
background session in the prior home.

Therefore:

- Linux/WSL: one durable private `CLAUDE_CONFIG_DIR` per subscription account
  is feasible;
- Windows: Anthropic documents the same credential redirect;
- macOS: a private config directory does not officially isolate the OAuth
  Keychain credential; and
- Sidekick must never swap or overwrite the macOS Keychain entry.

Claude also has strict credential precedence:

1. cloud-provider mode;
2. `ANTHROPIC_AUTH_TOKEN`;
3. `ANTHROPIC_API_KEY`;
4. `apiKeyHelper`;
5. `CLAUDE_CODE_OAUTH_TOKEN`; then
6. the subscription login.

[Claude Code authentication
precedence](https://code.claude.com/docs/en/authentication#authentication-precedence).

A selected subscription home could otherwise start under a higher-priority
credential. The launcher must detect that conflict and fail closed instead of
silently using the wrong account. It must not edit or clear the parent shell.

Claude setup-token accounts remain a separate, limited option. Anthropic
documents them as one-year tokens passed through
`CLAUDE_CODE_OAUTH_TOKEN`. They support model requests but cannot create
Remote Control sessions or fetch Claude.ai connectors, and bare mode ignores
them. They do not refresh; Sidekick must track expiry and request
regeneration. [Claude setup-token
documentation](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token).

### GitHub and installed-Claude findings

The inspected official `anthropics/claude-code` repository commit was
`2982f951552e94f38cd972764ae94c1d90c41da3`, matching the installed 2.1.218
release. The repository does not contain the proprietary native CLI's core
authentication source, so the investigation used its official changelog plus
the installed binary.

The changelog records fixes for:

- parallel sessions sharing a credential store
  ([2.1.211](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L214-L220));
- a refresh-token race between parallel sessions
  ([2.1.133](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L1684-L1693));
- Linux/Windows credential-file corruption and environment-token login
  behavior
  ([2.1.118](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L1998-L2015));
- background sessions no longer inheriting another session's provider
  environment from the daemon
  ([2.1.174](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L866-L880));
- credential removal from tool subprocess environments
  ([2.1.83](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L2701-L2708)).

This supports provider-owned homes and current concurrent-session behavior.
It does not support manually editing credentials or making undocumented
refresh variables part of the selector contract.

### Safe interactive CLI design

The existing account display uses Rich tables and already has a Rich
confirmation prompt. A first selector needs no new dependency:

```text
sidekick-usages use
sidekick-usages use codex
sidekick-usages use codex <label>
sidekick-usages use codex native
sidekick-usages use claude <label>
sidekick-usages current
sidekick-usages launch codex -- <arguments>
sidekick-usages launch claude -- <arguments>
```

The names are provisional; the required behavior is:

- with a TTY and no label, show eligible accounts plus `Native/default`;
- with a label, select without prompting;
- in non-TTY mode, never prompt;
- mark the current selection in the accounts display;
- label it “for new terminal launches”; and
- show whether transparent shell integration is active.

The default usage report should not unexpectedly block automation. The
interactive picker should be an explicit command or action from the account
display. A numbered Rich prompt is sufficient initially. If arrow-key
selection becomes required, `prompt_toolkit` should be evaluated and declared
as a direct dependency rather than used transitively.

### Persisted selection and launcher

The selection file should contain only routing state, such as:

```json
{
  "schema_version": 1,
  "providers": {
    "codex": {"mode": "saved", "label": "work"},
    "claude": {"mode": "native"}
  }
}
```

It needs strict decoding, private permissions, atomic write and recovery, a
cross-process lock, and transactional handling of account rename, removal,
reset, and migration. If a selected saved account disappears or is unsafe,
the launcher must stop clearly. It must never fall through to a different
saved account.

For a selected saved Codex account, the launcher sets:

```text
CODEX_HOME=<that account's private home>
```

For a selected Claude subscription account on Linux/Windows, it sets:

```text
CLAUDE_CONFIG_DIR=<that account's private home>
```

For a selected Claude setup-token account, it supplies the token only to the
Claude child and enables Claude's documented subprocess credential scrubbing.

For `Native/default`, the launcher changes nothing. It executes the real
provider binary with the inherited environment, preserving any native custom
home the user already configured.

The launcher must preserve arguments, signals, exit status, and vendor
auto-update behavior. It must resolve an absolute real binary outside the
Sidekick shim directory and reject recursion.

### How bare `codex` and `claude` can follow the selection

The portable base command is `sidekick-usages launch`. Transparent bare
provider commands require one opt-in shell-integration step.

Anthropic explicitly recommends putting a script named `claude` in a
directory earlier on `PATH` for terminal sessions and warns against replacing
the vendor-managed symlink. [Anthropic corporate-launcher
documentation](https://code.claude.com/docs/en/corporate-launcher#processes-that-start-outside-the-launcher).

Sidekick should use the same safe pattern for both providers:

```text
terminal `codex`
        -> Sidekick-owned shim
        -> read selected Codex pointer
        -> exec real Codex with selected child environment

terminal `claude`
        -> Sidekick-owned shim
        -> read selected Claude pointer
        -> exec real Claude with selected child environment
```

The one-time integration must be explicit, reversible, and verified in a new
ordinary terminal. It must not replace `~/.local/bin/codex` or
`~/.local/bin/claude`.

The current Codex session prepends its own release directory to `PATH`, so it
is not a valid place to verify a new terminal shim. This is expected: current
sessions are not switched.

### Keeping every account fresh

The selected account does not receive exclusive maintenance. Sidekick still
walks every saved account independently.

For Codex:

- app-server owns refresh in each private home;
- an actively used CLI owns normal refresh for its home;
- Sidekick should not start a competing forced refresh against a home used by
  a Sidekick-launched session; and
- a per-home activity lease lets maintenance skip only that active home and
  continue with all others.

When the session exits or a lease is safely proven stale, scheduled
maintenance resumes. This preserves the one-authority model while keeping
account A and B independent.

For Claude subscription homes:

- Claude owns credentials and in-use refresh;
- Sidekick may read structured health status;
- provider-login expiry requires official login again; and
- no documented no-model forced-refresh command was found.

The selector should not depend on Sidekick's current undocumented
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN` staging behavior. A short-lived access token
can be stale while the provider-owned login remains recoverable; Sidekick must
report that distinction accurately instead of demanding a refresh merely
because the access token expired.

For Claude setup-token accounts, there is no refresh loop. Sidekick tracks the
one-year lifetime and prompts for regeneration before expiry.

### Platform support matrix

| Provider/account type | Linux/WSL | Windows | macOS |
|---|---:|---:|---:|
| Codex managed ChatGPT login | Full | Full | Full |
| Claude subscription login | Full | Full | Native only |
| Claude setup-token | Limited | Limited | Limited |
| Native/default passthrough | Full | Full | Full |

“Native only” means Sidekick cannot safely isolate multiple full Claude
subscription logins within one macOS user using documented storage behavior.
“Limited” is the documented setup-token feature set, not a Sidekick
limitation.

> **Later correction:** This historical matrix is superseded. Current
> Anthropic documentation explicitly identifies `CLAUDE_CONFIG_DIR` as useful
> for multiple accounts, and the exact macOS 2.1.218 binaries prove distinct
> config-derived Keychain services. The current Linux/WSL/macOS matrix appears
> under [Required platform contract](#required-platform-contract).

### Rejected switching methods

Do not:

- copy a selected account over `~/.codex/auth.json`;
- replace Claude's native `.credentials.json`;
- swap the macOS Keychain credential;
- use Codex config profiles as auth profiles;
- export a fixed secret or home globally into shell startup files;
- replace vendor-managed executable symlinks; or
- silently launch Claude under a higher-priority credential.

Those methods either mutate the native login, affect active sessions, recreate
the rotating-token problem, or cannot guarantee which account is used.

The replacement native-global design and its implementation gates follow this
historical section.

</details>

<!-- markdownlint-enable MD033 -->

## Native Global Account Selection

> **Approved UI correction:** The tracked
> [interactive global account-selection design](../specs/2026-07-23-interactive-global-account-selection-design.md)
> supersedes this research mock's `IN USE` labels. The final design uses one
> cursor in the focused provider, starts it on the provider-verified active
> account, and reserves row text for actionable warnings.

### Product contract

The normal `sidekick-usages` command should retain its current Rich usage
dashboard and become interactive on a TTY:

```text
╭─ CLAUDE · 2 accounts ─────────────────────────────╮
│ › ● work       max       21%   5h                │
│   ● personal   pro       04%   5h                │
╰───────────────────────────────────────────────────╯

╭─ CODEX · 2 accounts ──────────────────────────────╮
│   ● work       team      18%   5h                 │
│   ● personal   plus     42%   5h                 │
╰───────────────────────────────────────────────────╯

↑↓ move   Tab provider   Enter use   r refresh   Esc cancel   q exit
```

The `›` cursor moves vertically across the focused provider's existing
account bullets. It begins on the provider-verified active account, and Tab
begins on the other provider's verified active account. Enter activates the
highlighted account. No persistent active-account label is added.

The default remains safe for automation:

- TTY stdin and stdout: render, then interact;
- redirected input or output: render once and exit;
- `sidekick-usages check`: render once and exit;
- `--no-interactive`: explicit terminal escape hatch; and
- `sidekick-usages use <provider> <label>`: scriptable equivalent.

`prompt_toolkit` is the preferred direct dependency to evaluate for portable
key input and terminal restoration. Rich remains the renderer. The selection
transaction is provider-neutral; provider adapters own activation.

### Normal provider commands remain normal

No command indirection is part of this design:

```text
terminal `claude` -> Anthropic's installed binary
terminal `codex`  -> OpenAI's installed binary
```

Live resolution on this machine still confirms the vendor-managed paths:

```text
~/.local/bin/claude -> ~/.local/share/claude/versions/2.1.218
~/.local/bin/codex  -> ~/.codex/packages/standalone/releases/0.145.0-.../codex
```

Sidekick must not:

- create a `claude` or `codex` wrapper;
- install an alias or shell function;
- place a shim directory before the vendor binaries;
- replace a vendor-managed symlink; or
- rewrite shell startup files on selection.

The shared provider runtimes, not command resolution, carry the selection.

### Claude native-global switch

Claude's native subscription credential is shared by ordinary local sessions.
Anthropic documents:

- subscription credentials in a protected file on Linux and Windows;
- subscription credentials in the system Keychain on macOS;
- `CLAUDE_CONFIG_DIR` as the whole-profile boundary; and
- that boundary as useful for running multiple accounts side by side.

[Claude environment variable
reference](https://code.claude.com/docs/en/env-vars#environment-variables),
[Claude credential
management](https://code.claude.com/docs/en/iam#credential-management).

The exact official macOS arm64 and x64 packages for Claude Code 2.1.218 were
downloaded from Anthropic's npm release and inspected without execution. Both
credential backends:

1. normalizes the selected config directory to NFC;
2. computes the first eight hexadecimal characters of its SHA-256 digest when
   `CLAUDE_CONFIG_DIR` is present;
3. reads, updates, and deletes
   `Claude Code-credentials-<directory-digest>` in Keychain; and
4. uses the unsuffixed `Claude Code-credentials` service when
   `CLAUDE_CONFIG_DIR` is absent.

The macOS binary uses Keychain as the primary protected backend and has a
plaintext-file fallback when a non-transient Keychain write fails. Sidekick
must fail closed if that fallback appears on macOS; it must never silently
accept downgraded credential storage. Every private account path must be one
stable, absolute, normalized path because a different path string selects a
different Keychain service.

The current Sidekick provider reads only the unsuffixed macOS service and
deliberately rejects its isolated refresh subprocess on Darwin. Those are
implementation gaps in Sidekick, not an upstream feasibility limit.

Claude's release-matched changelog also confirms:

- parallel sessions share one credential store;
- sessions recover when credentials were refreshed outside the session;
- refresh-token races are coordinated; and
- Remote Control disconnects when a different account signs in.

Sources:

- [shared credential
  store](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L214-L220)
- [external credential
  refresh](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L810-L829)
- [different-account Remote
  Control](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md#L837-L850)

Claude exposes no public saved-account selector or credential-export command.
It does, however, document `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` together with
`CLAUDE_CODE_OAUTH_SCOPES`: `claude auth login` exchanges that refresh token
directly instead of opening a browser. Sidekick's current Linux/WSL refresh
path already uses this official login process in a closed child environment.
The selector should use the same documented provider-owned transition, gated
by a capability probe and post-operation identity verification.

[Claude automated refresh-token
provisioning](https://code.claude.com/docs/en/env-vars#environment-variables).

The explicit Claude `USE` transaction should:

1. lock provider-wide credential and selection state;
2. identify, read back, and journal the current native account without
   modifying it;
3. retain its latest verified generation by running official
   `claude auth login` against that account's stable private
   `CLAUDE_CONFIG_DIR`;
4. verify the outgoing private authority before changing the native one;
5. refresh and verify the target through its isolated official Claude
   process;
6. invoke official `claude auth login` with the default config boundary to
   activate the target in the native credential authority;
7. verify target identity with `claude auth status --json`;
8. validate the resulting protected credential envelope and, on macOS, prove
   no plaintext fallback was created;
9. mark the target `native-active` and the prior account
   `private-inactive`; and
10. commit the non-secret selection.

Sidekick must not call `security add-generic-password`, splice credential
JSON, or copy a Keychain payload. Official Claude owns every credential write.
Rollback must run another verified official login transition rather than
restoring potentially revoked bytes.

New normal Claude terminals use the selected native account. Existing normal
subscription sessions use it at their next auth resolution/provider request.
An already-active HTTP request is not retargeted halfway through. Sessions
launched with higher-priority environment, API-key, gateway, or cloud
credentials remain intentionally outside the subscription switch.

### Codex native shared-daemon switch

Changing `~/.codex/auth.json` cannot hot-switch a running Codex 0.145.0
process. The release source explicitly says external modifications are not
observed until `reload()` and unauthorized recovery refuses reload when the
account ID differs.

Sources:

- [cached auth
  contract](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L1759-L1766)
- [account-matched
  reload](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L2109-L2141)

The no-wrapper solution is Codex's own local app-server daemon. On Unix, an
ordinary `codex` TUI probes the default control socket and automatically uses
the daemon when it is available and the launch configuration is replayable.

Sources:

- [default daemon
  probe](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/tui/src/lib.rs#L417-L443)
- [implicit daemon
  reuse](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/tui/src/lib.rs#L859-L980)
- [daemon lifecycle
  contract](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server-daemon/README.md)

Sidekick should keep each saved Codex account in its own managed private home.
For the selected account it:

1. asks managed Codex in that private home for a verified fresh token;
2. connects to the native shared daemon;
3. installs that runtime account through `chatgptAuthTokens`;
4. verifies the daemon reports the expected identity;
5. persists only the selected label and activation generation; and
6. keeps one broker connection alive to answer refresh requests.

App-server broadcasts `account/updated` to every connection, and the TUI
updates its account state when that event arrives.

Sources:

- [external account
  input](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L61-L104)
- [external login and account-update
  notification](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/request_processors/account_processor.rs#L688-L785)
- [notification
  broadcast](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/outgoing_message.rs#L590-L629)
- [TUI account-update
  handling](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/tui/src/app/app_server_events.rs#L97-L118)

When Codex receives a 401 in external mode, it broadcasts
`account/chatgptAuthTokens/refresh` and waits up to ten seconds. The Sidekick
broker validates the previous account ID, asks managed Codex in that account's
private home to refresh, verifies the new generation, and returns the new
access token. Sidekick does not call the private OAuth endpoint.

The server request is broadcast to every app-server connection. Release-matched
TUI code records that request but does not answer it; the resident Sidekick
broker must be the sole responder. Multiple broker responders would race on
one request ID and are not a supported topology.

Sources:

- [global server-request
  broadcast](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/outgoing_message.rs#L273-L350)
- [TUI leaves external refresh to its
  host](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/tui/src/app/app_server_requests.rs#L88-L150)

OpenAI labels `chatgptAuthTokens` unstable and for internal use only. The exact
installed 0.145.0 schema includes it, but Sidekick must capability-probe and
version-gate this integration. If the contract is missing, fail closed or
offer official browser login; never fall back to replacing `auth.json`.

### Current-machine transition and coverage

Live read-only inspection found two running Codex processes but no native
app-server control socket. The existing sessions are therefore embedded and
cannot receive a broker update. One-time setup must:

1. obtain consent to start the official Codex daemon;
2. start and verify the Sidekick broker;
3. let current work finish;
4. ask the user to restart only the pre-daemon Codex TUIs; and
5. prove later ordinary `codex` launches attach to the daemon.

After that, connected ordinary TUIs switch on their next safe request.

The current mechanism does not cover `codex exec`, Windows Codex 0.145.0, or
launches using configuration modes that bypass daemon reuse. Full parity for
those surfaces requires an upstream persistent daemon endpoint or
account-reload IPC. Sidekick must not claim otherwise.

### Both accounts remain fresh

Selection does not remove any account from maintenance.

For Codex, every account keeps its independent managed private home. The
selected shared runtime is a projection of one of those homes. A broker
refresh and scheduled refresh use the same per-home coordinator.

For Claude, the selected account's native credential is authoritative while
active; inactive accounts use their private authorities. Before switching
away, Sidekick retains the latest verified native generation for that account.
If the user runs `/login` outside Sidekick, Sidekick should reconcile the
provider's deliberate new identity rather than overwrite it.

One account's login or refresh failure must never prevent maintenance of the
other account.

The complete evidence, state machine, platform matrix, and implementation
gates are consolidated in this document.

## Research Scope and Method

### Scope

The investigation covered six evidence lanes:

1. current official OpenAI Codex authentication and app-server documentation;
2. the release-matched and current upstream `openai/codex` GitHub source;
3. the exact locally installed Codex executable and redacted live behavior;
4. current official Claude Code authentication and account-switching
   documentation, plus the `anthropics/claude-code` changelog;
5. the exact locally installed Claude Code executable and isolated-home
   behavior; and
6. Sidekick's current auth detection, login/import, refresh, usage recovery,
   private persistence, maintenance, and doctor implementation.

### Version baseline

| Component | Inspected version |
|---|---|
| Sidekick checkout | `develop` at `8c957b1e76dcdbdfbae61b5af2343fd30eb90f96` |
| Sidekick CLI | `sidekick-usages 0.7.0` |
| Installed Codex | `codex-cli 0.145.0` |
| Installed Codex binary SHA-256 | `a2a05dafaa1acb002a45eaec0a462de5b13694fcfcd7bc43305f14781ce7be14` |
| Release-matched Codex source | `25af12f7e61572b0bc18ddb1008be543b91519b0` |
| Upstream main comparison | `62ba648136c7e60b9380c40b60cb553a7d8eb1ab` |
| Installed Claude Code | `2.1.218 (Claude Code)` |
| Installed Claude binary SHA-256 | `e12071751a9336b8af1012c103358ff04ac18f9aaff4a738cff7ba5cdfaf63f2` |
| Official Claude Code repository | `2982f951552e94f38cd972764ae94c1d90c41da3` |

The core managed-refresh and app-server account semantics described below are
present at the release-matched commit. No material change to the selected
`account/read` refresh contract was found in the inspected main commit.

### Live method

The investigation used:

- `codex login status`;
- redacted `codex doctor --json`;
- installed CLI help and executable provenance;
- installed app-server schema generation;
- installed Claude help, auth command help, and executable provenance;
- an empty `CLAUDE_CONFIG_DIR` JSON auth-status probe;
- two distinct empty-config Claude daemon-namespace probes;
- an empty `CODEX_HOME` login-status probe;
- working-tree and PATH-installed Sidekick doctor output;
- metadata-only parsing of auth identity, expiry, and `last_refresh`; and
- equality-only comparisons between corresponding secret fields.

Token values were never emitted. Saved account labels are pseudonymized as A
and B in this report.

### Deliberately excluded actions

Because the request was purely investigative, the following were not run:

- direct OAuth token refresh or revocation;
- Sidekick credential refresh;
- Sidekick Codex login, import, or export;
- `codex login` or `codex logout`;
- `claude auth login`, `claude auth logout`, or `claude setup-token`;
- Claude daemon start, stop, install, or background-session mutation;
- app-server forced refresh against a real account;
- provider binary replacement or shell integration;
- selected-account state creation;
- token copies or manual auth edits; and
- any source implementation or test change.

The installed app-server schema was generated into the research scratch
directory without loading or changing credentials.

## Key Findings

### The apparent contradiction is real but scoped

The observed states are:

| Authority/surface | Redacted live state | What it proves |
|---|---|---|
| Default native Codex home | ChatGPT auth; `last_refresh` `2026-07-22T00:45:39.513698327Z`; access expires `2026-08-01T00:45:39Z` | The active native credential generation is fresh. |
| `codex login status` | `Logged in using ChatGPT` | The default home contains loadable ChatGPT auth. |
| `codex doctor --json` | Overall `ok`; authenticated Responses WebSocket handshake HTTP 101 | The default native credential worked live. |
| Sidekick saved A | Access expired `2026-07-20T16:21:19Z`; refresh rejected `2026-07-23T19:56:18.227029Z` | Saved/private A needs recovery. |
| Sidekick saved B | Access expired `2026-07-12T04:03:36Z`; refresh rejected `2026-07-23T19:56:18.403414Z` | Saved/private B needs recovery. |
| Default native vs saved B | Same provider identity; access tokens differ; refresh tokens differ | They are separate credential generations for the same account. |

`codex login status` only loads the selected home's stored auth and reports its
mode. Its implementation does not contact the authority or inspect another
home. [Installed-tag login status
source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/cli/src/login.rs#L424-L477).

Sidekick doctor evaluates the canonical saved accounts and their private
credential state. It does not claim to be evaluating the default native home,
but the current presentation does not make that boundary prominent.

The correct user-facing interpretation is:

> Your active native Codex login is healthy. Sidekick's independent saved copy
> for the same account is stale and its refresh token was rejected.

### The current design duplicates a rotating credential lineage

The current login/import path is structurally unsafe for managed Codex OAuth:

- `src/sidekick_usages/providers/codex/auth.py:128-194` runs `codex login`
  against a source home and prepares a private copy.
- `src/sidekick_usages/credentials/service.py:520-573` reads that source,
  builds the saved account, and commits the private bundle.
- `src/sidekick_usages/credentials/codex.py:136-189` places the copied auth in
  a Sidekick-owned home.
- `src/sidekick_usages/providers/codex/provider.py:117-202` later calls the
  OAuth token endpoint itself using the saved refresh token.

This is not merely a theoretical concern. OpenAI documents a one-file,
one-serialized-stream operational model. The CI/CD guide lists "another
machine or concurrent job rotated the token first" as a reason an old copy
must be reseeded. [OpenAI CI/CD auth
guide](https://learn.chatgpt.com/docs/auth/ci-cd-auth#operational-rules-that-matter).

The upstream maintainer response to a request for shared auth across separate
`CODEX_HOME`s is also direct: homes are independent; authenticate separately
in each. [Issue #15410](https://github.com/openai/codex/issues/15410).

Refresh-token reuse is nuanced. A Codex maintainer says the server allows
limited reuse for roughly an hour to absorb network flakiness; this mitigates
a short race but does not make days-old or weeks-old copies safe.
[Issue #10332](https://github.com/openai/codex/issues/10332). The observed
Sidekick copies are much older than that described window.

Replacement login adds another invalidation route. Codex changed its browser
and device login paths to revoke and clear the existing managed auth before
starting replacement login. A copied bundle can therefore remain complete on
disk after the authority has revoked it.
[PR #27674](https://github.com/openai/codex/pull/27674).

### Sidekick persistence is internally consistent

For both saved accounts, the canonical account record and corresponding
private auth bundle match on:

- provider account identity;
- access token;
- refresh token;
- ID token; and
- refresh timestamp.

The private auth files are protected and the canonical account store was not
in a dirty refresh transaction.

This is important because it narrows remediation:

- do not weaken the current identity and private persistence checks;
- do not treat the incident as an account-store corruption bug; and
- replace the credential authority model inside the existing transaction
  boundary.

The current coordinator's transaction shape remains useful for:

- pre/post file snapshots;
- identity proof;
- private permission enforcement;
- interrupted-operation recovery; and
- atomic persistence of sanitized account health.

It should stop making the account row a second secret authority.

### Sidekick loses the exact authority failure

The current provider posts a form-encoded refresh request to a hard-coded OAuth
endpoint and client ID. On HTTP 401, `src/sidekick_usages/http/client.py:224-232`
raises a generic `AuthError` without preserving a bounded error body. The
provider then records:

> Codex rejected the saved refresh token; log in again.

The installed Codex source recognizes at least:

- `refresh_token_expired`;
- `refresh_token_reused`;
- `refresh_token_invalidated`; and
- an unknown permanent or transient failure.

[Installed-tag classification
source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L1332-L1429).

Because Sidekick discarded the response body, the exact reason for the two
live rejections is unknowable from current evidence. Reused or invalidated is
the strongest architectural inference, but this report does not present it as
an observed backend code.

This loss also prevents precise recovery messaging:

- expired -> independently log in again;
- reused -> another writer advanced this credential lineage;
- invalidated -> login/logout or server action revoked it;
- account mismatch -> the selected authority changed identity;
- transient -> retry with backoff, no manual login yet.

### Codex already implements the correct refresh state machine

The installed release's `AuthManager` includes:

1. **Proactive timing.** Refresh within five minutes of a parseable access JWT
   expiry. Fall back to `last_refresh` older than eight days when expiry is not
   available. [Source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L2506-L2528).
2. **Guarded reload.** Under an in-process lock, reload the durable store for
   the same account before calling the token authority. If another official
   operation already changed it, use that generation instead.
   [Source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L2362-L2400).
3. **Fail-closed account boundary.** Do not load a different account into
   account-scoped state.
4. **Typed failure state.** Remember permanent refresh failures for the exact
   attempted auth generation.
5. **Rotation persistence.** Update returned access, refresh, and ID tokens,
   write `last_refresh`, save, and reload the cache.
   [Source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs#L2593-L2611).
6. **Unauthorized recovery.** Reload first, refresh second, then retry a
   failed authenticated request. PR #11802 fixed this behavior for
   app-server and account switches. [PR
   #11802](https://github.com/openai/codex/pull/11802).

Sidekick's current ten-minute expiry-only decision, direct endpoint request,
and one retry reproduce only a subset of that behavior.

Delegation is therefore a correctness choice, not just code reuse.

### A documented no-model-turn refresh method exists

OpenAI's app-server documentation exposes:

```json
{ "method": "account/read", "id": 1, "params": { "refreshToken": true } }
```

It states:

- managed ChatGPT auth is owned, persisted, and refreshed by Codex;
- `account/read` can optionally refresh tokens; and
- `refreshToken: true` forces refresh in managed mode.

[OpenAI app-server auth
documentation](https://learn.chatgpt.com/docs/app-server#auth-endpoints).

The release-matched protocol source says the same.
[GetAccountParams](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L481-L500).

The installed 0.145.0 binary generated a default schema containing:

```json
{
  "refreshToken": {
    "description": "When `true`, requests a proactive token refresh before returning...",
    "type": "boolean"
  }
}
```

A schema generated by the installed 0.145.0 binary contained this same
contract. The generated schema was investigation scratch and is intentionally
not published because the upstream source and version are cited directly.

The installed handler calls the same `AuthManager.refresh_token()` used by
Codex. It returns only redacted account metadata, not tokens.
[Handler](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/request_processors/account_processor.rs#L905-L1013).

This is a materially better integration than:

- a private OAuth HTTP call;
- a model-consuming `codex exec`;
- an incidental `codex doctor` side-effect; or
- experimental host-owned `chatgptAuthTokens` mode.

## Comparative Analysis

### Resident service topology

The two credible service designs are:

- **Option A, split control:** one resident Codex broker plus the existing
  independent periodic OS scheduler for maintenance.
- **Option B+, supervised workers:** one lean resident Sidekick supervisor
  owns broker callbacks and durable scheduling, but launches every
  credential-bearing maintenance operation in a bounded short-lived worker.

A plain monolith that imports providers and performs maintenance inline is not
equivalent to Option A: one hung refresh could miss Codex's ten-second callback
window, and provider memory would remain resident. It is rejected.

| Property | Option A: split control | Option B+: supervised workers |
|---|---|---|
| Codex callback latency | Excellent | Excellent through a dedicated priority lane |
| Maintenance failure isolation | Excellent; separate scheduled process | Excellent only if every risky operation stays in a worker |
| Crash recovery | Two independently supervised paths | One OS-supervised service plus a durable catch-up queue |
| Scheduling after account or network change | Waits for the next OS timer unless explicitly kicked | Immediate event-driven reschedule |
| Cross-path locking | Broker and scheduler must coordinate across processes | One coordinator still backed by durable cross-process locks |
| Steady memory | Lean broker; maintenance memory is transient | Lean supervisor; maintenance memory is transient |
| OS integration surface | Broker service plus timer/task definitions | One service definition plus WSL rescue trigger |
| Operational state | Split across broker and scheduler status | One health/readiness surface |
| Upgrade and rollback | Two lifecycle paths | One supervisor lifecycle; workers use the same installed version |
| Platform fit | Linux, WSL, macOS | Linux, WSL, macOS |

Single-point Linux measurements explain the worker boundary rather than set a
performance promise:

- a minimal Python control loop used about 19 MiB resident memory;
- importing Sidekick maintenance code raised that to about 44 MiB;
- an idle official Codex app-server used about 69 MiB immediately and about
  89 MiB after two seconds.

Therefore the preferred design is **Option B+**. It has the same meaningful
failure isolation as Option A only under these non-negotiable conditions:

1. the supervisor never imports provider-heavy maintenance code;
2. refresh, usage, migration, and repair execute in killable bounded workers;
3. Codex callbacks have a separate priority queue and cannot wait behind
   maintenance;
4. due work and retry state are durable and replayed after restart;
5. the existing qualified locks remain the final cross-process authority; and
6. systemd, Task Scheduler, or launchd restarts the supervisor.

Under those conditions B+ provides richer and faster coordination without
keeping more provider memory resident. If any condition is removed, Option A
is safer.

### Codex refresh method

| Method | Validates selected home | Can refresh | Model usage | Preserves Codex ownership | Main weakness | Decision |
|---|---:|---:|---:|---:|---|---|
| Current Sidekick direct OAuth POST | Partly | Yes | None | No | Private protocol drift, duplicate writer, generic 401 | Remove |
| Re-import default native auth | Reads a valid generation | Only after copying | None | No | Adopts active login and recreates split lineage | Reject |
| `codex login status` | No live authority validation | No | None | Yes | Only proves a loadable record exists | Status hint only |
| `codex doctor --json` | Yes, with live reachability | Incidental in current implementation | None | Yes | Contract is diagnostic/read-mostly, not a refresh API | Human diagnostics only |
| `codex exec --ephemeral` | Yes | Yes | Yes | Yes | Consumes quota and starts a model run | Compatibility fallback |
| App-server `account/read`, `refreshToken: true` | Account-scoped; post-state must be verified | Explicitly yes | None | Yes | Public result lacks precise refresh outcome | Recommended |
| App-server external-token mode | Depends on host | Host must refresh | None | No | Experimental and preserves Sidekick as OAuth owner | Reject |
| Independent login in each private home | Establishes correct authority | Enables official refresh | None after login | Yes | Requires migration and login UX | Required |

### Why not `codex doctor`

The live doctor probe is excellent evidence that the default native login
works. The installed source describes doctor as read-mostly and non-repairing.
Its WebSocket check currently creates an `AuthManager` and resolves auth, so a
due token may be refreshed incidentally. Depending on that behavior would
invert the command's stated contract and could change without notice.

### Why not `codex exec`

The official CI/CD guide uses `codex exec` and confirms that a normal run can
refresh the file. It is supported and can remain a fallback for older Codex
versions. A maintenance daemon should not spend a model turn merely to rotate
credentials when the app-server exposes an explicit account operation.

### Managed and external app-server modes serve different roles

Managed `chatgpt` mode remains the correct durable authority for each saved
private Codex home. Codex owns the refresh token, refresh protocol, and
persistence there.

The native shared daemon needs a runtime projection of the account selected in
Sidekick. The installed `chatgptAuthTokens` bridge is the only discovered
mechanism that can replace that runtime account and notify already-connected
TUIs without another browser login.

Sidekick therefore does not become the OAuth implementation:

```text
private home -> managed Codex refresh -> fresh access token
                                         |
                                         v
                            shared daemon external runtime
```

When the daemon asks for refresh, Sidekick delegates to managed Codex in the
matching private home and returns the resulting access token. The external
method is unstable/internal and must be version-gated; managed private-home
refresh is still the durable foundation.

## Recommendations

### 1. Establish a single-authority invariant

For each saved Codex account:

```text
Sidekick account index
        |
        | sanitized identity, expiry, outcome, authority path
        v
Sidekick-owned CODEX_HOME
        |
        | auth.json is the only secret authority
        v
one serialized official Codex app-server writer
```

Requirements:

- one independently authenticated private home per account;
- no managed token bundle copied from a home that remains in use;
- no token bundle duplicated in the account store;
- no active default-home mutation;
- no concurrent app-server or Codex process against the private home; and
- no silent fallback to Sidekick's direct refresh endpoint.

### 2. Seed private homes through official login in their final location

Future login must authenticate the final private home directly through either:

- app-server `account/login/start`; or
- a narrowly controlled official `codex login` subprocess with
  `CODEX_HOME` set to that final home.

App-server login is a better long-term fit because auth state, login
notifications, forced refresh, account read, rate limits, and usage are one
versioned protocol.

Do not seed by copying the active default `auth.json`. Do not seed by logging
in to a disposable source and leaving the source usable afterward.

Codex currently revokes and clears existing auth before replacement login. A
canceled login may leave the private home unauthenticated. Sidekick should
persist that explicit state and ask the user to finish login; restoring the
old bytes may restore a remotely revoked token.

### 3. Add a version-gated app-server bridge

The provider-specific Codex integration has two app-server roles.

For each private managed home, the adapter should:

1. resolve the exact installed executable;
2. record version and provenance;
3. require a compatible app-server account schema;
4. set the exact private `CODEX_HOME`;
5. enforce file-backed storage;
6. launch app-server over stdio;
7. send `initialize`;
8. send `initialized`;
9. issue `account/read`;
10. validate the bounded JSON-RPC result;
11. close or terminate the child predictably; and
12. redact all process errors before they cross the provider boundary.

For the native shared runtime, the adapter should:

1. inspect and manage the official daemon lifecycle;
2. connect over the default native control socket;
3. verify daemon and local CLI version compatibility;
4. capability-probe external account injection;
5. install the selected verified access token in ephemeral runtime auth;
6. verify the daemon account identity;
7. keep one singleton broker connection alive;
8. answer external refresh requests through the matching managed private
   home;
9. rehydrate the selected account after daemon restart; and
10. broadcast readiness only after daemon and broker checks pass.

The locally installed CLI labels the app-server command experimental, while
OpenAI documents it as the integration surface and includes `account/read` in
the default schema. Sidekick should treat the protocol as versioned and
capability-probed, not assume all installed versions match 0.145.0.

The external account input is even less stable: the installed schema marks it
internal-only. Support must be pinned to exact tested releases and stop
cleanly when the contract is absent.

### 4. Verify forced refresh by durable state, not RPC presence alone

In 0.145.0, app-server internally distinguishes:

- no attempt or success;
- transient failure; and
- permanent failure.

The public `account/read` response does not expose that enum. A permanent
failure can make the account field null; a transient failure can leave stale
account metadata visible.

Sidekick should therefore:

1. take a protected pre-operation snapshot;
2. call `account/read` with `refreshToken: true`;
3. take a protected post-operation snapshot;
4. parse only bounded metadata;
5. require the same provider account identity;
6. require an advanced `last_refresh` or credential generation for success;
7. reject malformed, missing, wrong-account, or regressed state; and
8. persist a safe outcome.

Do not parse Codex stderr for authority details. The current Codex source can
log a backend response body on refresh error; that is not a stable or
credential-safe interface.

### 5. Track authority and generation explicitly

Recommended non-secret fields:

- `auth_authority`: Sidekick private managed Codex home;
- `auth_home`;
- `auth_storage_mode`;
- provider account ID;
- access expiry;
- Codex `last_refresh`;
- last observed protected-file generation or keyed digest;
- last health-check time;
- last refresh-attempt time;
- refresh status: not due, success, transient failure, action required;
- safe failure reason when available;
- installed Codex version; and
- observation scope: native default or saved private.

Do not log or expose a stable raw hash of a token. If a generation comparison
needs content identity, use a domain-separated keyed digest held inside the
private persistence boundary, or filesystem/version metadata with collision
and rollback considerations.

### 6. Make doctor scope explicit

Doctor should distinguish at least:

- **native default health:** a read-only observation;
- **saved private health:** the authority Sidekick uses;
- **identity relation:** same account, different account, or unknown; and
- **generation relation:** current, older, newer, or not safely comparable.

Suggested diagnosis for this incident:

```text
Native Codex login: healthy.
Saved Sidekick login: expired; its private refresh token was rejected.
The two homes represent the same account but different credential generations.
Action: authenticate the Sidekick-owned private home independently.
The native login will not be changed.
```

After a successful official refresh or independent login, clear the prior
failed status for that exact saved authority.

### 7. Align scheduling with official behavior

Do not force rotation on every doctor or usage poll.

Use:

- access JWT expiry within five minutes when parseable; and
- Codex `last_refresh` older than eight days as the fallback.

These are current implementation values, not a permanent public constant.
Version-specific policy should live in the Codex adapter, not provider-neutral
core.

On a backend 401:

1. re-read the private home under its operation lock;
2. use official forced refresh through app-server;
3. verify the durable generation;
4. retry the original operation once; and
5. preserve transient vs action-required state.

### 8. Move compatible usage reads behind app-server

After refresh authority is stable, evaluate:

- `account/rateLimits/read`; and
- `account/usage/read`.

If they satisfy Sidekick's current usage and activity contracts, use them
instead of extracting the access token and calling private backend routes.
This would further reduce:

- secret duplication;
- private endpoint coupling;
- independent 401 recovery; and
- provider protocol drift.

This should be a separate, tested phase because current Sidekick aggregation
may require fields that differ from app-server's public response.

### 9. Migrate existing accounts through reauthentication

Both currently rejected private homes require independent official login.

For saved B, the healthy default native login is useful diagnostic evidence,
but it should not be copied into the private home. Doing so would recreate the
same shared lineage. Saved A's other inspected isolated native home is also
stale and is not a recovery authority.

A safe migration should:

1. preserve the current private home for recovery/audit until login begins;
2. start official login in the final private home;
3. accept that old remote auth may be revoked;
4. validate the returned identity against the saved account;
5. update sanitized account metadata;
6. remove duplicate token fields only after migration success; and
7. prove the default native auth file was not changed.

### 10. Request a stronger upstream outcome contract

The ideal app-server response would include redacted structured data such as:

```json
{
  "refreshAttempted": true,
  "refreshStatus": "failed",
  "failureReason": "reused",
  "account": null
}
```

Until then, Sidekick can prove success from durable post-state but cannot
always classify a failure exactly. An upstream feature request for a stable
one-shot `codex account refresh --json` would also benefit non-app-server
integrators.

## Implementation or Decision Implications

No implementation is authorized or included here. When implementation is
approved, ownership should follow the repository boundaries.

### `cli/`

Own:

- the TTY cursor embedded in the normal usage dashboard;
- cursor movement, provider jumps, Enter-to-use, refresh, and exit keys;
- provider-verified initial cursor placement and active-account restoration;
- the explicit non-interactive selection command;
- authenticated local communication with the resident selection broker;
- non-TTY render-and-exit behavior;
- first-use consent and one-time Codex transition guidance; and
- accurate coverage labels for ordinary, embedded, remote, and standalone
  provider sessions.

The registration-only Typer root should continue composing cohesive command
modules. `sidekick-usages check`, redirected I/O, and `--no-interactive` must
remain non-blocking.

### `daemon.py` and resident supervisor

The current scheduler backends periodically launch
`sidekick-usages maintain --quiet`. On systemd this is explicitly a
`Type=oneshot` service on a 30-minute timer; WSL currently selects the Windows
Task Scheduler backend. That is correct for maintenance but cannot own the
Codex refresh bridge.

The preferred B+ design replaces that split lifecycle with one lean
user-session supervisor. It must:

- stay resident after the dashboard exits;
- expose a private, peer-verified local control socket to the selector;
- enforce exactly one broker instance and one refresh responder;
- own the long-lived connection to Codex's native daemon;
- refresh and inject the selected account before reporting ready;
- reconnect and rehydrate selection after either process restarts;
- stop or report not-ready rather than silently using the wrong account; and
- persist due work and dispatch periodic all-account maintenance only through
  bounded short-lived worker processes.

The supervisor must keep its broker path operationally isolated from worker
execution: separate priority queues, hard deadlines, bounded worker
concurrency, durable retries, and no provider-heavy imports in the resident
process. The existing one-shot timer should be removed only after the new
service is installed and verified, so two schedulers never maintain the same
account concurrently.

On this WSL machine, the implementation must test real logon, WSL startup,
user-systemd, and Windows Task Scheduler behavior. The scheduled task becomes
a rescue/start trigger, not a second maintenance scheduler, because Microsoft
documents that systemd services do not keep a WSL instance alive. Merely
running the broker inside the interactive dashboard would lose refresh
capability as soon as the user exits.

Linux installs the equivalent systemd user service. macOS installs one
per-user LaunchAgent under the user's `Library/LaunchAgents`; it must run in
the logged-in user context so official Claude can use that user's Keychain.
None of these setup paths requires administrator rights.

### `providers/codex/`

Own:

- installed Codex discovery and compatibility;
- app-server subprocess and JSON-RPC schemas;
- account/login/read/refresh adapters;
- native app-server daemon lifecycle and socket health;
- version-gated external account injection;
- the persistent external-refresh broker protocol;
- app-server account-update and readiness handling;
- provider-specific metadata validation;
- app-server usage/rate-limit adapters, if adopted; and
- translation of safe Codex errors into provider failures.

Remove or deprecate:

- the direct OAuth endpoint and client-id refresh path;
- form-encoded Codex refresh transport; and
- any fallback that silently calls the private endpoint.

### `providers/claude/`

Own:

- official native-login transition and JSON auth-status adapters;
- stable per-account `CLAUDE_CONFIG_DIR` allocation;
- native credential-envelope validation on file-backed platforms;
- config-derived Keychain service discovery and read-only validation on
  macOS;
- rejection of macOS plaintext credential fallback;
- capability gating for documented refresh-token provisioning;
- higher-priority credential conflict detection;
- installed binary discovery and compatibility; and
- Linux, WSL, and macOS capability distinctions.

The current provider already stages CLI refresh in an isolated home. Native
activation needs a separately named, explicitly authorized adapter; background
maintenance must never call it. Sidekick may read protected provider state for
verification, but only official Claude may write credential files or Keychain
items.

### `credentials/`

Own:

- one-operation-per-private-home coordination;
- independent private-home login workflow;
- pre/post authority snapshots;
- same-account and generation invariants;
- provider-neutral outcome persistence; and
- migration away from duplicated account-store token material.

The existing coordinator is the right place to compose Codex's write with
Sidekick's account-state transaction.

It should also coordinate selected-account rename/remove/reset behavior and
the provider-wide activation transaction, Codex broker refresh delegation,
Claude native-active/private-inactive authority transitions, and crash
recovery.

### `persistence/`

Own:

- qualified private home creation;
- a qualified private Claude-home root;
- strict non-secret selected-account state;
- directory and file permissions;
- bounded reads;
- snapshot/generation checks;
- recovery from interrupted Codex writes; and
- schema migration for sanitized account metadata;
- selection-file locking and atomic recovery; and
- transactional selection updates on account rename, reset, or removal;
- provider activation journals; and
- Codex daemon/broker and Claude native-authority generations.

Do not manually rewrite an active Codex home.

### `maintenance.py`

Own:

- iteration over every account regardless of selection;
- due/not-due scheduling using provider-supplied policy;
- explicit force semantics;
- retry/backoff for transient subprocess failures; and
- suppression of repeated permanent refresh attempts until state changes;
- broker and scheduled refresh serialization for each Codex home;
- observation of the selected Claude native authority;
- private maintenance of inactive Claude accounts; and
- provider-specific distinction between refreshable login, setup-token
  lifetime, and reauthentication-required state.

### `doctor.py`

Own:

- authority-scoped presentation;
- native vs saved health separation;
- same-identity/different-generation explanation;
- precise manual action; and
- redaction.

### `usage/`

Initially:

- continue the current usage result contract;
- replace its refresh callback with official private-home refresh.

Later:

- evaluate app-server account rate-limit and usage methods;
- preserve existing aggregation and Rich presentation contracts.

### Test strategy

Before any live account test, add load-bearing tests for:

- installed binary missing or incompatible;
- app-server initialize timeout and malformed framing;
- `account/read` success without refresh;
- forced refresh with advanced same-account file generation;
- RPC account success but unchanged file;
- account-null permanent failure;
- transient child failure with unchanged auth;
- wrong-account post-state;
- malformed/truncated post-state;
- child exit before response;
- two Sidekick operations serialized for one home;
- different account homes operating independently;
- selecting one account while all saved accounts remain scheduled;
- default TTY cursor movement and Enter activation;
- redirected default invocation rendering once and exiting;
- `check` and `--no-interactive` never entering key-input mode;
- Claude native activation through the installed provider process;
- Claude existing-session use of externally refreshed native credentials;
- Claude in-flight requests finishing before the new account applies;
- Claude credential-precedence conflict failing closed;
- external Claude `/login` reconciliation;
- two stable macOS config paths selecting different Keychain services;
- macOS config-path spelling changes being rejected before they orphan a
  Keychain authority;
- a locked or unavailable Keychain failing closed;
- macOS plaintext credential fallback being detected and rejected;
- Claude activation and rollback using only official login writes;
- selected account rename, removal, reset, and malformed state;
- official Codex daemon start, readiness, restart, and version mismatch;
- two daemon-connected Codex TUIs receiving one account update;
- Codex switch applying to the next request, not an in-flight request;
- broker refresh routing by previous account ID;
- broadcast refresh requests being answered by exactly one broker;
- ordinary TUI connections ignoring, rather than racing, broker refresh;
- broker restart rehydrating selected runtime auth;
- the dashboard exiting while the resident broker remains healthy;
- a hung maintenance worker never delaying a Codex callback;
- worker termination leaving the supervisor healthy;
- durable due work replaying once after supervisor restart;
- the legacy periodic timer being removed only after supervisor readiness;
- Linux systemd-user and macOS LaunchAgent restart recovery;
- WSL logon, distribution restart, and Task Scheduler rescue;
- a missing internal-token capability failing closed;
- a pre-daemon embedded Codex session requiring one restart;
- `codex exec` and native Windows exclusions being reported accurately;
- non-TTY selection refusing to prompt;
- Linux, WSL, and macOS support-matrix behavior;
- canceled login after Codex cleared old auth;
- doctor wording for healthy native plus failed saved state;
- the default Codex auth path never being written;
- Claude native credentials being written only by official Claude; and
- token strings absent from logs, exceptions, reports, and representations.

All provider HTTP/auth tests should use synthetic identities and mock
authorities. A real-account test should be separately authorized, bounded to
the exact provider activation transaction, and include verified rollback or
reconciliation evidence.

## Risks and Open Questions

### App-server maturity and compatibility

The local help labels app-server experimental. OpenAI's current product docs
present it as the rich-client integration surface, and managed `account/read`
is in the default schema. The external `chatgptAuthTokens` input needed for
instant saved-account switching is more restrictive: its source and installed
schema explicitly label it unstable and internal-only. Capability probing,
exact release gates, and a fail-closed fallback are required.

### Incomplete structured refresh outcomes

The current public RPC does not expose the internal transient/permanent reason.
Durable before/after comparison can prove success, but failure messages may
remain less precise until upstream exposes a redacted reason.

### Cross-process concurrency

Codex's refresh lock is process-local. OpenAI's docs still require one
serialized stream per file. Sidekick must keep private homes unshared and
serialize its own subprocesses. It cannot make a shared file safe when an
unrelated program ignores the lock.

The shared runtime broker and scheduled maintenance can request refresh of the
same private home. They must use one Sidekick coordinator so managed Codex
remains the only durable writer. Other account homes must continue normally.

### Codex file-write atomicity

The inspected file backend truncates, writes, and flushes `auth.json`; it does
not use Sidekick's atomic rename transaction. Sidekick must not race or
overwrite the file, and its reader/recovery logic must distinguish a transient
partial write from permanently malformed state.

### Shared-runtime coverage

A persisted selection does nothing unless the provider's live shared runtime
matches it. Claude needs verified native credential identity. Codex needs both
the official app-server daemon and the Sidekick refresh broker.

An ordinary Codex TUI silently falls back to an embedded app-server when the
control socket is absent. Sidekick therefore needs separate daemon, broker,
and selected-identity readiness checks. Pre-daemon embedded sessions require
one restart. `codex exec`, native Windows 0.145.0, and non-replayable launch
configuration remain outside the shared daemon and must be labeled
accurately.

### Resident supervisor availability

Codex broadcasts external-token refresh requests to every connection and
waits only ten seconds. The current periodic Sidekick scheduler cannot meet
that contract. A single resident responder is required, with local peer
authentication, crash restart, duplicate-instance prevention, and explicit
not-ready behavior. The B+ supervisor must reserve callback capacity even
while maintenance workers are starting, running, timing out, or being
terminated.

There is also a narrow restart window before the broker reapplies the selected
external account. Because Sidekick does not wrap `codex`, it cannot intercept
a launch during that window. The systemd user service on Linux, combined
systemd/Task Scheduler recovery on WSL, and LaunchAgent on macOS should
supervise startup and minimize the window, but absolute startup atomicity
requires a stable upstream persistent external-auth or account-switch
contract.

### Claude macOS capability and Keychain availability

The earlier macOS feasibility blocker is resolved. Current Anthropic
documentation calls `CLAUDE_CONFIG_DIR` useful for multiple accounts, and the
exact macOS 2.1.218 arm64 and x64 binaries prove that each configured path
selects a distinct hashed Keychain service.

The remaining risk is compatibility, not architecture. The service-name
derivation is binary evidence rather than a separately versioned public API.
Sidekick must require a tested Claude release, authenticate each private path
through official Claude, and verify the resulting identity and protected
backend before accepting the account. It must not use the undocumented
`CLAUDE_SECURESTORAGE_CONFIG_DIR` variable found in the binary.

A locked, unavailable, or interaction-blocked login Keychain can prevent
unattended maintenance. The supervisor must preserve the last known metrics,
mark them stale with a timestamp, and request user repair. It must not unlock
the Keychain, ask for a macOS password, or accept Claude's plaintext fallback.

### Claude maintenance contract

Claude owns in-use subscription refresh. It also documents automated
refresh-token provisioning: with `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` and
`CLAUDE_CODE_OAUTH_SCOPES`, `claude auth login` exchanges the credential
without opening a browser. That is the no-model provider-owned operation for
inactive-account maintenance and explicit activation.

`claude auth status --json` remains a structured health and identity check,
not the refresh action. Every maintenance or activation success therefore
requires an official login-process success plus a separate protected-state
and identity read-back. Setup-token credentials remain non-refreshing and
must never enter this transition.

Claude setup tokens are explicitly non-refreshing one-year credentials.
Product status must say “regenerate” rather than “refresh.”

### Private-home user state

Private homes still contain more than authentication. They can contain Codex
config, skills, plugins, sessions, logs, and other mutable state used during
maintenance or independent login. The native shared Codex daemon must not
adopt a private home's unrelated user state merely to receive its selected
access token.

Claude's selected native account shares the user's existing native settings
and history by design. Inactive account authorities must remain credential
scoped so a switch does not replace unrelated native Claude configuration.

### Private-home login UX

Authenticating every private home separately is the upstream-supported model,
but it requires browser/device login per account. App-server exposes both
flows. Product decisions are needed for:

- browser vs device default;
- timeout and cancel behavior;
- identity replacement confirmation;
- how canceled login is reported after old auth was revoked; and
- whether a Sidekick background daemon may ever prompt.

### Account-store secret migration

The current account model stores access, refresh, and ID tokens in addition to
the private auth bundle. Moving to a sanitized index requires a versioned
migration, rollback strategy, and compatibility decision for older Sidekick
versions.

### Exact incident reason

The exact backend failure code for saved A and B is permanently unavailable in
current persisted evidence. The design conclusion does not depend on that
code: the two-authority model is unsupported and live generations have
diverged. Incident wording should still distinguish observed fact from
inference.

### Usage endpoint equivalence

App-server's public rate-limit and usage methods look promising, but their
field-level equivalence to Sidekick's current `/backend-api/codex/usage`
contract has not been proven in this investigation. Do not bundle that
transport migration into the first auth-authority change without a separate
contract analysis.

## Source Matrix

| Source | Type | As-of/version | Used for | Confidence |
|---|---|---|---|---:|
| [OpenAI Authentication](https://learn.chatgpt.com/docs/auth) | Official product docs | 2026-07-23 | Login caching, automatic refresh, file/keyring storage, credential secrecy | High |
| [OpenAI CI/CD Auth](https://learn.chatgpt.com/docs/auth/ci-cd-auth) | Official product docs | 2026-07-23 | Do not call OAuth directly; persist Codex-updated file; one serialized owner | High |
| [OpenAI App Server](https://learn.chatgpt.com/docs/app-server#auth-endpoints) | Official product docs | 2026-07-23 | Managed auth ownership and forced `account/read` refresh | High |
| [OpenAI environment variables](https://learn.chatgpt.com/docs/config-file/environment-variables) | Official product docs | 2026-07-23 | `CODEX_HOME` scope and default | High |
| [App-server protocol source](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L481-L500) | Release-matched upstream source | Codex 0.145.0 | Exact `GetAccountParams` contract | High |
| [App-server account handler](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/request_processors/account_processor.rs#L905-L1013) | Release-matched upstream source | Codex 0.145.0 | Refresh delegation and public outcome limitation | High |
| [Codex TUI daemon probe and reuse](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/tui/src/lib.rs#L417-L443) | Release-matched upstream source | Codex 0.145.0 | Bare Unix `codex` can discover and reuse the native shared daemon without a wrapper | High |
| [Codex daemon lifecycle](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server-daemon/README.md) | Release-matched upstream source | Codex 0.145.0 | Experimental native control socket, lifecycle, and Unix support boundary | High |
| [Codex external auth installation](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/request_processors/account_processor.rs#L688-L785) | Release-matched upstream source | Codex 0.145.0 | Process-wide external account installation and broadcast account-update notification | High |
| [Codex external refresh bridge](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/external_auth.rs#L18-L94) | Release-matched upstream source | Codex 0.145.0 | Refresh request broadcast and ten-second broker response window | High |
| [Codex account-update broadcast](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/app-server/src/outgoing_message.rs#L590-L629) | Release-matched upstream source | Codex 0.145.0 | Shared account updates reach connected app-server clients | High |
| [Codex auth manager](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/manager.rs) | Release-matched upstream source | Codex 0.145.0 | Timing, guarded reload, classification, persistence, recovery | High |
| [Codex auth storage](https://github.com/openai/codex/blob/25af12f7e61572b0bc18ddb1008be543b91519b0/codex-rs/login/src/auth/storage.rs) | Release-matched upstream source | Codex 0.145.0 | File semantics and permissions | High |
| [Issue #15410](https://github.com/openai/codex/issues/15410) | Upstream maintainer decision | 2026-03-21 | Separate homes require separate auth | High for maintainer statement |
| [Issue #10332](https://github.com/openai/codex/issues/10332) | Upstream issue plus maintainer response | 2026-02-04 | Limited refresh-token reuse window and concurrency nuance | Medium-high |
| [PR #11802](https://github.com/openai/codex/pull/11802) | Merged upstream change | 2026-02-18 | Guarded reload and app-server unauthorized recovery | High |
| [PR #27674](https://github.com/openai/codex/pull/27674) | Merged upstream change | 2026-06-12 | Revoke/clear existing auth before replacement login | High |
| [Claude Code Authentication](https://code.claude.com/docs/en/iam) | Official product docs | 2026-07-23 | Credential locations, precedence, login renewal, setup-token limits | High |
| [Claude Code environment variables](https://code.claude.com/docs/en/env-vars) | Official product docs | 2026-07-23 | Side-by-side accounts through `CLAUDE_CONFIG_DIR` and automated refresh-token provisioning | High |
| [Claude Code quickstart](https://code.claude.com/docs/en/quickstart#step-2-log-in-to-your-account) | Official product docs | 2026-07-23 | `/login` is the native account-switch action | High |
| [Claude Code Remote Control](https://code.claude.com/docs/en/remote-control) | Official product docs | 2026-07-25 | Runtime enablement, automatic enablement, outbound-only transport, and absence of an externally queryable local status boundary | High |
| [Claude issue #261](https://github.com/anthropics/claude-code/issues/261) | Official repository issue closed as completed | 2025-03-05 | `CLAUDE_CONFIG_DIR` accepted as the CLI multi-account boundary | Medium-high |
| [Claude Code changelog](https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md) | Official repository changelog | Claude 2.1.218 | Shared credential store, outside-session refresh reload, different-account Remote Control behavior, and concurrent refresh | High for released behavior |
| [`darwin-arm64`](https://www.npmjs.com/package/@anthropic-ai/claude-code-darwin-arm64/v/2.1.218) and [`darwin-x64`](https://www.npmjs.com/package/@anthropic-ai/claude-code-darwin-x64/v/2.1.218) Claude packages | Exact official platform packages and binaries | Claude 2.1.218 | Config-derived Keychain namespaces, official Keychain writes, and plaintext fallback behavior on both macOS architectures | High for this release |
| [Microsoft WSL systemd](https://learn.microsoft.com/en-us/windows/wsl/systemd) | Official platform docs | 2025-03-17 | User services can run in WSL, but do not keep the distribution alive | High |
| [Apple launchd guide](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html) | Official platform docs | Current archived platform guide | Per-user LaunchAgents, service restart, and user-session scope | High |
| Installed schema generated from the local binary | Local binary-generated primary evidence | Codex 0.145.0 | Confirms the local binary exposes external login and refresh-broker messages and marks them internal-only | High |
| Installed Codex empty-home, daemon-help, process, and socket probes | Local runtime evidence | Codex 0.145.0, 2026-07-23 | Confirms `CODEX_HOME` isolation, native daemon commands, and that current live sessions are embedded | High |
| Installed Linux Claude help, empty-home probes, read-only binary trace, and exact macOS package inspection | Local and release-binary primary evidence | Claude 2.1.220, 2026-07-25 | Confirms Linux file isolation, shared-profile next-request cache invalidation, macOS hashed Keychain isolation, auth-status surfaces, and provider-owned writes | High for Linux and static macOS contracts; macOS next-request runtime smoke remains |
| Live `codex doctor --json` | Local redacted runtime evidence | 2026-07-23 | Valid native session and authenticated WebSocket reachability | High |
| Live Sidekick doctor and protected state comparison | Local runtime/persistence evidence | Sidekick 0.7.0, 2026-07-23 | Saved failures, internal consistency, generation divergence | High |
| Sidekick source under `src/sidekick_usages/` | Local implementation | Commit `8c957b1e...` | Current import, refresh, tracking, persistence, doctor behavior, and periodic one-shot scheduler boundary | High |
