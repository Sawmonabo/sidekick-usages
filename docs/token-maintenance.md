# Token maintenance, doctor, and daemon

This guide documents how `sidekick-usages` keeps saved Claude and
Codex accounts fresh, how to diagnose auth problems, and how the
cross-platform scheduler is installed.

## Mental model

`sidekick-usages` has two different token update paths:

1. `sidekick-usages refresh <label>` imports the current local provider
   login into one explicit saved label.
2. `sidekick-usages refresh --all` uses only refresh tokens already
   saved in the sidekick config.

The second path is what the daemon runs. It is intentionally safer for
multi-account stores because it never copies the current global Claude
or Codex login into arbitrary labels.

## Supported account types

| Account type | Auto-refresh | Notes |
| --- | --- | --- |
| Claude OAuth login with `refresh_token` | Yes | Uses the installed Claude Code CLI in a temporary `HOME`, imports rotated credentials, and leaves normal `~/.claude` untouched. |
| Claude `setup-token` account | No | Setup tokens do not contain refresh tokens. Replace manually when the token dies. |
| Codex ChatGPT login with `refresh_token` | Yes | Refreshes through the OpenAI OAuth token endpoint and writes the rotated auth bundle to sidekick's private Codex cache. |
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
- refresh-token presence
- access-token expiry when known
- provider account fingerprint when known
- whether the account can auto-refresh
- whether manual action is required
- latest refresh status and error, if sidekick has attempted a refresh

Use `doctor --json` when scripting or collecting support data. The JSON
output does not include access tokens, refresh tokens, API keys, or raw
provider credentials.

### Refresh saved tokens

```bash
sidekick-usages refresh --all
sidekick-usages refresh --all --quiet
sidekick-usages refresh --all --force
```

`refresh --all` is the scheduler-safe maintenance command. It:

- refreshes accounts that are expired or near expiry
- skips fresh accounts unless `--force` is supplied
- persists each successful rotation immediately
- records failed refresh attempts on the affected account
- continues checking other accounts after one account fails
- never calls provider local-login detection
- never replaces saved identity from global Claude or Codex state

`--quiet` suppresses normal fresh/refreshed output and prints only
accounts that need manual action. This is the mode used by the daemon.

`--force` refreshes every account that has a saved refresh token,
regardless of expiry. It still does not import global provider logins.

### Import one current login explicitly

```bash
sidekick-usages refresh <label>
sidekick-usages refresh <label> --replace-identity
sidekick-usages refresh <label> --from-codex-home <path>
```

Use this only when you intentionally want to update one saved label
from the provider's current local login.

If a saved provider account id exists and the current login belongs to
a different provider account, sidekick refuses the update. Use
`--replace-identity` only when you intentionally want the label to
become the newly logged-in provider account.

## Daemon install

```bash
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

The installed scheduler runs:

```bash
sidekick-usages refresh --all --quiet
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
wsl.exe -d <distro-name> -- bash -lc 'sidekick-usages refresh --all --quiet'
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
| 0 | All refreshable accounts are fresh or refreshed. |
| 1 | At least one account needs manual login. |
| 2 | Config or provider/system error during refresh or doctor. |
| 3 | Scheduler install, status, or uninstall error. |

Schedulers should tolerate exit code 1 as an action-needed state. A
later run can still refresh other accounts after you fix the rejected
account.

## Config fields

The account store lives at:

```text
~/.config/sidekick-usages/accounts.json
```

Refresh diagnostics are optional and backward-compatible:

```json
{
  "last_refresh_at": "2026-06-12T13:14:22.459000Z",
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

## Troubleshooting

### Doctor says auto-refresh is no

The account probably has no saved refresh token. Claude `setup-token`
accounts are the expected case. They can report usage, but they cannot
rotate themselves.

### Doctor says the refresh token was rejected

Log into the matching provider account again, then update that one
label:

```bash
sidekick-usages refresh <label>
```

For Codex, you can also use:

```bash
sidekick-usages codex-login <label>
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
sidekick-usages refresh --all
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
  refresh policy, near-expiry checks, per-account outcomes, and
  diagnostic persistence.
- `sidekick_usages.doctor.DoctorService` builds read-only account
  diagnostics and renders text or JSON output.
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

The CLI should stay thin: parse Typer options, instantiate these
services, render results, and map outcomes to exit codes.
