# Token maintenance, doctor, and daemon

This guide documents how `sidekick-usages` keeps saved Claude and
Codex accounts fresh, how optional usage-window heartbeat fits into
maintenance, how to diagnose auth problems, and how the cross-platform
resident supervisor is managed.

## Mental model

`sidekick-usages` has two different account-maintenance paths:

1. `sidekick-usages refresh <label>` repairs that exact Claude or Codex
   account through official login in its independent private profile. When
   that Claude account is selected, it also maintains its verified native
   authority.
2. `sidekick-usages refresh --all` maintains every saved or managed authority
   independently of selection.

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

## Selection and private authorities

Each saved account owns an independent private provider authority regardless
of which account is selected:

- every Claude subscription account keeps its official private profile fresh;
- the selected Claude account additionally has separately verified native
  state, with private and native credential generations kept distinct;
- every Codex account keeps its official managed home fresh; and
- the selected Codex runtime state is separate from those private homes.

Changing Claude selection does not change Codex selection, and changing Codex
selection does not change Claude selection. Unselected accounts remain
eligible for maintenance, usage collection, and opted-in heartbeat.

Sidekick does not install a `claude` or `codex` wrapper, alias, shell function,
PATH shim, or replacement symlink. Normal provider commands keep using their
vendor executables and native login locations. New bare commands see the
provider-verified selection. Supported existing sessions update on their next
safe request; an in-flight request is never retargeted.

The hardened feature-branch dashboard contract is cached-first and interactive
only when both input and output are TTYs on Linux, WSL, or macOS. It is the
canonical selection surface. Its single cursor reflects provider read-back,
not a separate `IN USE` label. Repeated Enter and Esc are ignored while
activation is in flight; `q` and Ctrl-C remain safe and responsive. See the
[complete key map](../README.md#check-select-and-manage-accounts).

Explicit `check`, redirected I/O, and `--no-interactive` render once without
reading keys. Scripts use:

```bash
sidekick-usages use <provider> <label>
```

`use` never prompts or installs the service. Native Windows keeps one-shot
reporting, but interactive selection and resident supervision are
feature-disabled. Codex session switching supports ordinary TUIs using the
shared daemon; `codex exec`, pre-daemon embedded TUIs, native Windows, and
launch modes that bypass daemon reuse remain unsupported.

Claude setup-only selection requires at least one exact-build-qualified,
Sidekick-owned structured participant. Mixed refreshable selection proves the
native target first, then installs its exact committed lease into integrated
participants. Admission waits for active and background provider work to end
naturally; it never retargets an in-flight operation. Shell forwarding is
installed only during the qualified installed-release cutover and enrolls
future launches from newly loaded shells. It never retrofits, restarts, or
kills an existing unmanaged process.

## Supported account types

| Account type | Auto-refresh | Notes |
| --- | --- | --- |
| Claude subscription-login credential | Yes, while login remains usable | Official Claude refresh runs in the account's stable private profile. A legacy stored authority is migration input only. The native Claude login is unchanged. |
| Claude setup-token credential | No | Setup tokens do not contain refresh credentials. Replace explicitly when rejected. Sidekick tracks lifetime only when it has trusted capture evidence. |
| Codex ChatGPT managed login | Yes | The official Codex process refreshes the exact account's independent managed home. Sidekick performs no private OAuth exchange. |
| Account with rejected or revoked refresh authority | No | Requires logging into the matching provider account again, then running an explicit single-label refresh. |

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
- latest dashboard metrics-refresh result, retry history, and bounded
  account/cache failures
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

`refresh --all` is the provider-owned maintenance command. It:

- enrolls every saved account independently of the selected account
- refreshes accounts that are expired or near expiry
- skips fresh accounts unless `--force` is supplied
- persists each successful rotation immediately
- records failed refresh attempts on the affected account
- continues checking other accounts after one account fails
- reads native Claude only for the exact provider-verified selected account
- never replaces saved identity from global Claude or Codex state

Every refresh goes through its owning coordinator and exact account-operation
lock. Claude keeps the stable private profile fresh for every subscription
account. For the selected account it separately verifies and refreshes the
same identity in native Claude, while keeping private and native generations
distinct. Codex runs its official process in that account's protected managed
home. Selection never filters enrollment, due checks, or later attempts. One
account's failure does not stop the next account, and background maintenance
never switches a native identity. Each coordinator resamples durable state
before commit and cannot overwrite an unrelated account.

Usage and heartbeat use a separate authority-aware lease. The selected Claude
account opens its provider-verified native authority; inactive Claude accounts
open their private managed authorities. A dual-authority Claude account still
opens one credential mode for one logical lookup, so its preserved setup token
does not duplicate subscription metrics.

Known Claude login expiry is independent from access-token expiry. At or
inside five days, maintenance emits the five-day login-renewal warning and one
manual action. It does not classify the warning as a failed refresh or persist
it over existing refresh diagnostics. An expired login fails closed before
provider traffic. Unknown login expiry and setup-token credentials do not
produce this proximity warning.

`--quiet` suppresses normal fresh/refreshed output and prints only
accounts that need manual action.

`--force` requests maintenance for every refreshable account regardless of
expiry. It still does not import global provider logins.

### Warm inactive usage windows

Heartbeat is optional usage-window warming. It is not token freshness
and it is not free quota. A successful warm sends a real model request
and consumes a small amount of provider quota.

`maintain --quiet` refreshes owned account authorities first, then processes
heartbeat-enabled accounts. See
[heartbeat behavior and guardrails](./heartbeat.md) for commands, supported
account types, provider targets, model requests, and persisted diagnostics.

### Run full maintenance manually

```bash
sidekick-usages maintain --quiet
```

`maintain --quiet` maintains owned account authorities first, then heartbeats
enabled accounts. If heartbeat is not enabled for any account, it behaves like
token maintenance only. It is an explicit foreground command, not the
installed service command.

### Repair one saved login

```bash
# Claude
sidekick-usages refresh <label>
sidekick-usages refresh <label> --replace-identity

# Codex
sidekick-usages refresh <label>
sidekick-usages codex login <label> [--device-auth]
```

For Claude, `refresh` operates only on the saved label's stable private
profile. A setup-token-only label retains that authority and uses
`--replace-identity` once to approve its first subscription association.
Renew a rejected setup token with `sidekick-usages claude setup-token`.

For Codex, either command starts the official sign-in inside that account's
final Sidekick-managed Codex home. It does not read or replace the active native
Codex login. `refresh` uses browser login; `codex login --device-auth` selects
the official device flow.

### Migrate existing saved authorities

```bash
sidekick-usages migrate managed-auth
```

The migration prints a secret-safe preview, makes the user service ready,
migrates Codex accounts and then Claude accounts independently, and proves the
final saved-account set. It continues after account-scoped failures and can be
rerun to resume. Claude setup-token authority is preserved when a subscription
authority is added. The command accepts no token argument; provider browser,
device, MFA, password, or consent remains inside the official login flow.

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

Under the next-release interactive contract, the first action that needs an
absent service keeps the cached dashboard visible and asks once for `y` or
`n`. Approval calls the same user-level lifecycle directly, verifies readiness,
and resumes the original action. Refusal or bounded failure preserves the
dashboard. Non-interactive `use` never prompts or starts setup.

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

For Claude, the account probably has no refreshable authority. Claude
`setup-token` accounts are the expected case: they can report usage but cannot
rotate themselves. For Codex, inspect managed-authority and supervisor health,
then use the official managed-login repair if action is required.

### Doctor says the refresh credential was rejected

For a Claude subscription-login label, repair that exact private profile:

```bash
sidekick-usages refresh <label>
```

For a Claude setup-token label, capture a replacement of the same method:

```bash
sidekick-usages claude setup-token --label <label> --force
```

For Codex, use either managed-home repair command:

```bash
sidekick-usages refresh <label>
sidekick-usages codex login <label>
```

Sidekick verifies that the completed login belongs to the saved Codex identity.
It never repairs a Codex label by importing the active native login.

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
- `sidekick_usages.credentials.claude.managed.maintenance.`
  `ClaudeManagedAuthorityCoordinator` owns exact-account Claude verification
  plus independent official private-profile and selected-native refresh.
- `sidekick_usages.credentials.claude.authority.`
  `ClaudeManagedCredentialResolver` opens the selected verified native
  authority or an inactive private authority under one held account lock.
- `sidekick_usages.credentials.CredentialRefreshCoordinator` remains only for
  legacy stored-authority migration input.
- `sidekick_usages.credentials.codex.managed.service.`
  `CodexManagedAuthorityCoordinator` owns exact-account Codex verification and
  official managed-home refresh.
- `sidekick_usages.persistence.credentials.refresh.service` and its focused schema,
  stage, merge, artifact, and private-stage modules own stored-credential
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
