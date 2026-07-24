# Token maintenance, doctor, and daemon

This guide documents how `sidekick-usages` keeps saved Claude and
Codex accounts fresh, how optional usage-window heartbeat fits into
maintenance, how to diagnose auth problems, and how the cross-platform
resident supervisor is managed.

## Mental model

`sidekick-usages` has two different token update paths:

1. `sidekick-usages refresh <label>` imports the current local provider login
   into one explicit saved login label. For Claude, this is safe for
   subscription-login labels only unless the operator separately authorizes an
   authentication-method change.
2. `sidekick-usages refresh --all` uses only refresh tokens already
   saved in the sidekick config.

`sidekick-usages maintain --quiet` is the explicit foreground maintenance
command. It runs the second path above, then optional heartbeat/window warming
for accounts where heartbeat is explicitly enabled. Refresh and heartbeat stay
separate in code and behavior:

- Refresh keeps saved access tokens valid.
- Heartbeat intentionally sends a tiny provider request to open an
  inactive usage window.

The resident supervisor owns one durable per-account queue and delegates due
work to bounded worker processes. Saved-account maintenance never copies the
current global Claude or Codex login into arbitrary labels.

## Supported account types

| Account type | Auto-refresh | Notes |
| --- | --- | --- |
| Claude subscription-login credential | Yes, while login remains usable | On non-macOS systems with Claude Code installed, prefers the CLI in a private staged home. macOS or a missing executable uses bounded HTTPS refresh and immediately stages the result. Neither path changes the active Claude login. |
| Claude setup-token credential | No | Setup tokens do not contain refresh credentials. Replace explicitly when rejected; their issue date cannot be recovered from the token. |
| Codex ChatGPT login with `refresh_token` | Yes | Refreshes through the OpenAI OAuth token endpoint and transactionally updates Sidekick's private Codex credential bundle. |
| Account with rejected or revoked refresh token | No | Requires logging into the matching provider account again, then running an explicit single-label refresh. |

## Commands

### Diagnose accounts

```bash
sidekick-usages doctor
sidekick-usages doctor --json
sidekick-usages doctor --provider claude
sidekick-usages doctor --provider codex
sidekick-usages doctor --label <label>
```

`doctor` is read-only. It does not rotate tokens. It reports:

- CLI and supervisor versions
- platform, process, protocol, queue, journal, and broker health separately
- label
- provider
- provider-adapter availability
- plan
- usage route
- credential kind
- access-token expiry and login expiry as separate values
- secret-free identity availability
- five-day login-renewal state when a known login expiry is near
- whether the account can auto-refresh
- whether manual action is required
- latest refresh status and error, if sidekick has attempted a refresh
- heartbeat support, enablement, cached 5-hour reset, and last
  heartbeat result

Use `doctor --json` when scripting or collecting support data. The JSON
output does not include access tokens, refresh tokens, API keys, or raw
provider credentials.

### Refresh saved tokens

```bash
sidekick-usages refresh --all
sidekick-usages refresh --all --quiet
sidekick-usages refresh --all --force
```

`refresh --all` is the token-only saved-refresh command. It:

- refreshes accounts that are expired or near expiry
- skips fresh accounts unless `--force` is supplied
- persists each successful rotation immediately
- records failed refresh attempts on the affected account
- continues checking other accounts after one account fails
- never calls provider local-login detection
- never replaces saved identity from global Claude or Codex state

Every rotating refresh goes through one coordinator. Refreshes sharing one
provider refresh credential are serialized by the
credential-derived operation identity before provider traffic. The coordinator
resamples durable state
after acquiring the credential-derived lock, writes one private staged
replacement, commits only the targeted credential over unrelated account
changes, and cleans up after durability proof. A complete interrupted stage is
recovered locally without a second provider request.

Known Claude login expiry is independent from access-token expiry. At or
inside five days, maintenance emits the five-day login-renewal warning and one
manual action. It does not classify the warning as a failed refresh or persist
it over existing refresh diagnostics. An expired login fails closed before
provider traffic. Unknown login expiry and setup-token credentials do not
produce this proximity warning.

`--quiet` suppresses normal fresh/refreshed output and prints only
accounts that need manual action.

`--force` refreshes every account that has a saved refresh token,
regardless of expiry. It still does not import global provider logins.

### Warm inactive usage windows

Heartbeat is optional usage-window warming. It is not token freshness
and it is not free quota. A successful warm sends a real model request
and consumes a small amount of provider quota.

`maintain --quiet` refreshes saved credentials first, then processes
heartbeat-enabled accounts. See
[heartbeat behavior and guardrails](./heartbeat.md) for commands, supported
account types, provider targets, model requests, and persisted diagnostics.

### Run full maintenance manually

```bash
sidekick-usages maintain --quiet
```

`maintain --quiet` refreshes saved tokens first, then heartbeats enabled
accounts. If heartbeat is not enabled for any account, it behaves like token
maintenance only. It is an explicit foreground command, not the installed
service command.

### Import one current login explicitly

```bash
sidekick-usages refresh <label>
sidekick-usages refresh <label> --replace-identity
sidekick-usages refresh <label> --replace-auth-method
sidekick-usages refresh <label> --from-codex-home <path>
```

Use this only when you intentionally want to update one saved login label from
the provider's current local login. For Claude setup-token credentials, use
`sidekick-usages claude setup-token` instead.

If a saved provider account id exists and the current login belongs to
a different provider account, sidekick refuses the update. Use
`--replace-identity` only when you intentionally want the label to
become the newly logged-in provider account.

`--replace-auth-method` independently authorizes setup token to subscription
login. When method and identity both change, both flags are required.

## Resident supervisor lifecycle

```bash
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

The lifecycle is user-level only and requires no administrator privileges.
There is no backend flag, timer, periodic task, or cron fallback. Installation
enrolls saved accounts, starts the service, verifies the local handshake and
durable recovery state, completes one bounded readiness pass, restarts the
service, and verifies it again.

| Platform | Integration |
| --- | --- |
| Linux | One systemd user service |
| WSL | The same systemd user service plus one Windows logon rescue task |
| macOS | One user LaunchAgent |
| Native Windows | Resident supervision is explicitly disabled |

`daemon status` is read-only. `doctor` goes further by reporting platform,
process, protocol, queue, journal, and broker health independently. Neither
command installs, restarts, refreshes, or repairs anything.

### Linux and Ubuntu

The systemd integration writes one service:

```text
~/.config/systemd/user/sidekick-usages.service
```

It runs the exact installed `sidekick-usages-supervisor` executable and uses
`Restart=on-failure`. It does not install a timer.

Useful native commands:

```bash
systemctl --user status sidekick-usages.service
journalctl --user -u sidekick-usages.service
```

If user systemd is unavailable, installation reports that resident supervision
is unavailable. It does not silently select another scheduler.

### WSL

WSL uses the Linux systemd user service. It also installs one current-Windows-
user logon rescue task whose only action starts that service:

```powershell
wsl.exe --distribution <distro> --user <linux-user> --exec \
  systemctl --user start sidekick-usages.service
```

The rescue task never runs maintenance, refreshes tokens, or handles provider
credentials. Status validates its exact action, description, and trigger.

### Windows native

Native Windows returns a feature-disabled result. Linux/WSL and macOS are the
supported resident-service platforms.

### macOS

The launchd integration writes one LaunchAgent and two diagnostic log paths:

```text
~/Library/LaunchAgents/com.sidekick-usages.supervisor.plist
~/Library/Logs/sidekick-usages/supervisor.out.log
~/Library/Logs/sidekick-usages/supervisor.err.log
```

The LaunchAgent starts at login and restarts only after an unsuccessful exit.
It has no interval.

Useful native commands:

```bash
launchctl print gui/$(id -u)/com.sidekick-usages.supervisor
```

### Uninstall boundary

`sidekick-usages daemon uninstall` removes only the Sidekick-owned service,
WSL rescue task or LaunchAgent, local socket, service state, and sanitized
diagnostic logs. It preserves:

- the saved-account index
- protected private credentials
- usage and activity metrics
- durable account state
- the current native Claude and Codex logins
- provider executables and shell configuration

Sidekick creates no `claude` or `codex` wrapper or alias. Normal provider CLI
commands continue reading their native login locations.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All refreshable accounts are fresh or refreshed, and heartbeat accounts are active/warmed/skipped. |
| 1 | At least one account needs manual login or provider action. |
| 2 | Config or provider/system error during refresh, heartbeat, or doctor. |
| 3 | Resident-service install, status, or uninstall error. |

Maintenance automation should tolerate exit code 1 as an action-needed state. A
later run can still refresh other accounts after you fix the rejected
account.

## Config fields

`doctor` reports the current account authority and protected refresh state.
Sidekick uses one native per-user application-data layout. See
[persistence and recovery](./persistence-and-recovery.md) before repairing or
resetting a store.

Refresh diagnostics may be absent when no attempt has been recorded. When
present, the current schema uses these fields:

```json
{
  "last_refresh_at": "<UTC_TIMESTAMP>",
  "last_refresh_status": "ok",
  "last_refresh_error": null
}
```

`last_refresh_status` is one of:

- `ok`
- `failed`
- `skipped`
- `null` when no refresh attempt has been recorded

`last_refresh_error` is a redacted user-facing error string. It must
not contain raw tokens.

Heartbeat state and target defaults are documented in
[heartbeat behavior and guardrails](./heartbeat.md#persisted-diagnostics).

## Troubleshooting

### Doctor says auto-refresh is no

The account probably has no saved refresh token. Claude `setup-token`
accounts are the expected case. They can report usage, but they cannot
rotate themselves.

### Doctor says the refresh credential was rejected

For a Claude subscription-login label, sign into the matching account, then
update that one label:

```bash
claude auth login
sidekick-usages refresh <label>
```

For a Claude setup-token label, capture a replacement of the same method:

```bash
sidekick-usages claude setup-token --label <label> --force
```

For Codex, you can also use:

```bash
sidekick-usages codex login <label>
```

Use `--replace-identity` only if you intentionally want to replace the
saved provider account id behind that label.

### WSL install fails

Confirm PowerShell is reachable from WSL:

```bash
powershell.exe -NoProfile -Command '$PSVersionTable.PSVersion'
```

If PowerShell interop is unavailable, fix WSL interoperability before
installing. Sidekick does not install a reduced or alternate backend.

### The daemon installed but nothing rotates

Run the maintenance command directly:

```bash
sidekick-usages maintain --quiet
```

Then inspect:

```bash
sidekick-usages doctor
sidekick-usages daemon status
```

If accounts are fresh, no rotation is expected until they approach
expiry.

## Module architecture

The implementation keeps foreground maintenance, durable supervision, and
platform lifecycle ownership separate:

- `sidekick_usages.maintenance.TokenMaintenanceService` owns saved-token
  access-refresh policy, derived Claude login-renewal warnings, per-account
  outcomes, and diagnostic persistence.
- `sidekick_usages.credentials.CredentialRefreshCoordinator` owns the single
  provider-neutral saved-credential refresh entry point.
- `sidekick_usages.persistence.credential_refresh` and its focused schema,
  stage, merge, artifact, and private-stage modules own credential-derived
  locking, private staging, targeted commit, assessment, and recovery.
- `sidekick_usages.heartbeat.HeartbeatService` owns optional
  usage-window warming policy, opt-in checks, cached reset throttling,
  per-account outcomes, and diagnostic persistence.
- `sidekick_usages.heartbeat.HeartbeatProvider` is the narrow provider
  port. Concrete adapters such as `ClaudeHeartbeat` and
  `CodexHeartbeat` own provider endpoint details instead of adding
  heartbeat methods to the generic usage provider abstraction.
- `sidekick_usages.doctor.DoctorService` builds read-only provider and account
  results. Supervisor lifecycle inspection supplies independent platform,
  process, protocol, queue, journal, and broker health to the same presenters.
- `sidekick_usages.daemon.lifecycle.manager.DaemonManager` delegates
  install/status/uninstall and read-only health to the selected user-service
  integration.
- `SystemdBackend`, `WslBackend`, and `LaunchdBackend` own their exact
  platform artifacts and commands. Native Windows is feature-disabled.
- `SupervisorRuntime` owns one resident control socket and durable scheduler.
  Bounded worker processes own provider work.
- `SystemCommandRunner` is injected so tests can verify generated
  commands without touching the host service manager.

The CLI stays thin: commands parse Typer options, request one narrow lazily
composed context, render completed results, and map outcomes to exit codes.
