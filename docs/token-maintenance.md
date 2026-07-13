# Token maintenance, doctor, and daemon

This guide documents how `sidekick-usages` keeps saved Claude and
Codex accounts fresh, how optional usage-window heartbeat fits into
scheduled maintenance, how to diagnose auth problems, and how the
cross-platform scheduler is installed.

## Mental model

`sidekick-usages` has two different token update paths:

1. `sidekick-usages refresh <label>` imports the current local provider login
   into one explicit saved login label. For Claude, this is safe for
   subscription-login labels only unless the operator separately authorizes an
   authentication-method change.
2. `sidekick-usages refresh --all` uses only refresh tokens already
   saved in the sidekick config.

The daemon runs `sidekick-usages maintain --quiet`. That command first
runs the second path above, then runs optional heartbeat/window warming
for accounts where heartbeat is explicitly enabled. Refresh and
heartbeat stay separate in code and behavior:

- Refresh keeps saved access tokens valid.
- Heartbeat intentionally sends a tiny provider request to open an
  inactive usage window.

The scheduled path is intentionally safer for multi-account stores
because it never copies the current global Claude or Codex login into
arbitrary labels.

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

- label
- provider
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

### Run full scheduled maintenance

```bash
sidekick-usages maintain --quiet
```

`maintain --quiet` is what the daemon installs. It refreshes saved
tokens first, then heartbeats enabled accounts. If heartbeat is not
enabled for any account, `maintain --quiet` behaves like token
maintenance only.

### Import one current login explicitly

```bash
sidekick-usages refresh <label>
sidekick-usages refresh <label> --replace-identity
sidekick-usages refresh <label> --replace-auth-method
sidekick-usages refresh <label> --from-codex-home <path>
```

Use this only when you intentionally want to update one saved login label from
the provider's current local login. For Claude setup-token credentials, use
`sidekick-usages claude setup-token` or the exact-label
`sidekick-usages claude restore-setup-token` recovery instead.

If a saved provider account id exists and the current login belongs to
a different provider account, sidekick refuses the update. Use
`--replace-identity` only when you intentionally want the label to
become the newly logged-in provider account.

`--replace-auth-method` independently authorizes setup token to subscription
login. When method and identity both change, both flags are required.

## Daemon install

```bash
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

The installed scheduler runs:

```bash
sidekick-usages maintain --quiet
```

It runs every 30 minutes. The scheduler is user-level only and does not
require root or administrator privileges.

### Backend selection

`sidekick-usages daemon install --backend auto` chooses the backend from
the current platform:

| Platform | Default backend |
| --- | --- |
| Windows native | Windows Task Scheduler via a silent `wscript.exe` wrapper |
| WSL | Windows Task Scheduler via a silent `wscript.exe` wrapper |
| macOS | launchd LaunchAgent |
| Native Linux or Ubuntu with user systemd | systemd user timer |
| Linux without user systemd | cron |

You can override detection:

```bash
sidekick-usages daemon install --backend systemd
sidekick-usages daemon install --backend cron
sidekick-usages daemon install --backend launchd
sidekick-usages daemon install --backend task-scheduler
```

For WSL, the default is Windows Task Scheduler because it can wake the
distro. An in-WSL systemd timer only runs while the distro is already
running, so use `--backend systemd` in WSL only if that tradeoff is
intentional.

### Linux and Ubuntu

The systemd backend writes:

```text
~/.config/systemd/user/sidekick-usages-refresh.service
~/.config/systemd/user/sidekick-usages-refresh.timer
```

The timer uses:

```text
OnBootSec=5m
OnUnitActiveSec=30m
RandomizedDelaySec=5m
Persistent=true
```

Useful native commands:

```bash
systemctl --user status sidekick-usages-refresh.timer
systemctl --user list-timers sidekick-usages-refresh.timer
journalctl --user -u sidekick-usages-refresh.service
```

If user systemd is unavailable, `--backend auto` falls back to a marked
crontab block. Uninstall removes only the sidekick-marked block.

### WSL

The WSL default installs a Windows scheduled task that runs a
sidekick-owned VBScript wrapper with `wscript.exe`:

```powershell
wscript.exe //B //Nologo %LOCALAPPDATA%\sidekick-usages\daemon\refresh.vbs
```

The wrapper runs PowerShell hidden, and that PowerShell script runs:

```powershell
wsl.exe -d <distro-name> -- bash -lc 'sidekick-usages maintain --quiet'
```

This keeps refreshes working even when the distro is not already
running, while avoiding the visible terminal flash that direct
`wsl.exe` scheduled tasks can create. `daemon status` and
`daemon uninstall` use the same Task Scheduler backend.

Generated Windows-side files live under:

```text
%LOCALAPPDATA%\sidekick-usages\daemon\
```

The wrapper appends output to:

```text
refresh.out.log
refresh.err.log
```

### Windows native

The Windows backend uses PowerShell and Task Scheduler, but the
scheduled task action points at `wscript.exe`, not the console
executable directly. This prevents periodic refreshes from flashing a
terminal window.

```powershell
Register-ScheduledTask
Get-ScheduledTask
Get-ScheduledTaskInfo
Unregister-ScheduledTask
```

The task name is:

```text
sidekick-usages-refresh
```

Generated launcher and log files live under:

```text
%LOCALAPPDATA%\sidekick-usages\daemon\
```

### macOS

The launchd backend writes:

```text
~/Library/LaunchAgents/com.sidekick-usages.refresh.plist
~/Library/Logs/sidekick-usages/refresh.out.log
~/Library/Logs/sidekick-usages/refresh.err.log
```

Useful native commands:

```bash
launchctl print gui/$(id -u)/com.sidekick-usages.refresh
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.sidekick-usages.refresh.plist
```

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | All refreshable accounts are fresh or refreshed, and heartbeat accounts are active/warmed/skipped. |
| 1 | At least one account needs manual login or provider action. |
| 2 | Config or provider/system error during refresh, heartbeat, or doctor. |
| 3 | Scheduler install, status, or uninstall error. |

Schedulers should tolerate exit code 1 as an action-needed state. A
later run can still refresh other accounts after you fix the rejected
account.

## Config fields

`doctor` reports the selected account source and destination. Existing 0.6.0
installations can remain at `~/.config/sidekick-usages/accounts.json`; fresh
installations of the upcoming 0.7.0 release use the operating system's native
application-data directory. See
[persistence locations, migration, and recovery](./persistence-and-recovery.md)
before inspecting or moving a store.

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

If PowerShell is unavailable, either fix Windows interop or explicitly
install an in-WSL backend:

```bash
sidekick-usages daemon install --backend systemd
```

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

The implementation is split so scheduler behavior is reusable and
testable:

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
- `sidekick_usages.doctor.DoctorService` builds one read-only result.
  Pure human and JSON presenters consume that same completed result.
- `sidekick_usages.daemon.DaemonManager` selects a scheduler backend
  and delegates install/status/uninstall.
- `sidekick_usages.daemon.SchedulerBackend` is the reusable backend
  base class.
- `SystemdBackend`, `CronBackend`, `LaunchdBackend`, and
  `TaskSchedulerBackend` implement OS-specific scheduling.
- `HiddenWindowsLauncher` generates the Windows/WSL no-console
  launcher artifacts and preserves scheduler exit codes through the
  wrapper process.
- `SystemCommandRunner` is injected so tests can verify generated
  commands without touching the host scheduler.

The CLI stays thin: commands parse Typer options, request one narrow lazily
composed context, render completed results, and map outcomes to exit codes.
