# Design Spec — Interactive Global Claude and Codex Account Selection

- **Status:** Approved 2026-07-23; not implemented
- **Date:** 2026-07-23
- **Repository:** `sidekick-usages`
- **Branch:** `develop`
- **Evidence commit:** `25135bbe03e51d3a3232a5171dd5c893822f4e14`
- **Research:** [Managed Authentication and Native Account
  Selection][research]
- **Required platforms:** Linux, WSL, and macOS
- **Production impact:** None. This document authorizes no source,
  credential, provider-login, service, or scheduler change.

---

This specification is the durable product and architecture authority for
adding interactive account selection to the existing Sidekick Usages
dashboard. It consolidates the approved brainstorming decisions and the
release-matched provider research.

The research report remains the evidence record. Where its earlier dashboard
mock showed an `IN USE` label, this specification supersedes that treatment:
the focused provider's cursor starts on its provider-verified active account,
and account rows carry text only when action is required.

## Table of Contents

1. [Executive Decision](#1-executive-decision)
2. [Goals and Non-Goals](#2-goals-and-non-goals)
3. [Terms and Product Invariants](#3-terms-and-product-invariants)
4. [Chosen Architecture](#4-chosen-architecture)
5. [Account Authority, Freshness, and Metrics](#5-account-authority-freshness-and-metrics)
6. [Interactive Dashboard Contract](#6-interactive-dashboard-contract)
7. [Supervisor Lifecycle and Platform Integration](#7-supervisor-lifecycle-and-platform-integration)
8. [Persisted State and Recovery](#8-persisted-state-and-recovery)
9. [Claude Activation](#9-claude-activation)
10. [Codex Activation](#10-codex-activation)
11. [External Login Reconciliation](#11-external-login-reconciliation)
12. [Security and Secret Handling](#12-security-and-secret-handling)
13. [Errors, Diagnostics, and Uninstallation](#13-errors-diagnostics-and-uninstallation)
14. [Current-Machine Migration](#14-current-machine-migration)
15. [Testing and Performance](#15-testing-and-performance)
16. [Release Acceptance Gates](#16-release-acceptance-gates)
17. [Repository Ownership](#17-repository-ownership)
18. [Rejected Designs](#18-rejected-designs)
19. [Risks and Revalidation Triggers](#19-risks-and-revalidation-triggers)
20. [Source Matrix](#20-source-matrix)

## 1. Executive Decision

The normal `sidekick-usages` TTY dashboard becomes an interactive,
provider-scoped account selector while preserving its current usage, reset,
warning, activity-total, branding, and responsive-layout contracts.

For each provider:

1. Sidekick privately maintains every saved account.
2. Sidekick reads the provider's actual native account.
3. The dashboard initially places its cursor on that verified account.
4. Moving the cursor previews another account.
5. Enter validates, repairs if necessary, and activates that account.
6. Sidekick verifies the provider's resulting identity before committing the
   selection.
7. New ordinary `claude` or `codex` terminals use the selected account.
8. Supported existing sessions adopt it on their next safe authenticated
   request.

No row-level `IN USE`, `ACTIVATING`, or `MIGRATION REQUIRED` badge is added.
The cursor communicates the normal current state. Temporary progress belongs
in the dashboard footer, and an account row receives explanatory text only
when the user needs to act.

The vendor commands remain vendor commands:

```text
terminal `claude` -> Anthropic's installed executable
terminal `codex`  -> OpenAI's installed executable
```

Sidekick does not create wrappers, aliases, shell functions, PATH shims, or
replacement symlinks. It does not edit shell startup files. Global selection
is carried by the providers' shared native runtimes.

One lean resident supervisor runs per operating-system user. It owns:

- the user-only local control socket;
- durable, event-driven scheduling;
- the high-priority Codex refresh-broker connection; and
- recovery coordination.

Provider refresh, usage, login, migration, and repair work runs in bounded
short-lived workers. Provider-heavy modules never remain imported in the
supervisor. This is the approved **B+ supervised-worker architecture**.

The first release must satisfy one product contract on:

- native Linux through a systemd user service;
- WSL through a systemd user service plus a Windows rescue/start task; and
- macOS through a per-user LaunchAgent in the login user context.

Native Windows is outside this feature's first release.

## 2. Goals and Non-Goals

### 2.1 Goals

The feature must:

- make the existing default dashboard interactive on a TTY;
- select one active account independently for Claude and Codex;
- preserve ordinary vendor command resolution;
- affect new ordinary provider terminals without a new launch command;
- update supported ongoing sessions at the provider's next safe request;
- keep every saved account maintained regardless of selection;
- keep failed or stale accounts visible with truthful status;
- use only official provider processes for durable credential writes;
- recover safely from interruption without guessing the active account;
- install and operate without administrator rights;
- remain responsive while maintenance is running or failing;
- use one small supervisor rather than one daemon per account;
- support Linux, WSL, Apple Silicon macOS, and Intel macOS; and
- allow the one-machine migration to be completed and verified by Codex
  during implementation rollout.

### 2.2 Non-goals

The first release does not:

- wrap or replace `claude` or `codex`;
- synchronize the selection across different machines or OS users;
- support native Windows account switching;
- retarget an already in-flight provider request;
- override API keys, gateways, cloud-provider credentials, or other
  higher-priority provider environments;
- make Claude setup tokens refreshable;
- call Codex's private OAuth endpoint;
- copy a native Codex `auth.json` into a private home;
- manually edit Claude credential files or Keychain entries;
- auto-import an unknown external provider login;
- promise shared-daemon switching for `codex exec`;
- promise switching for Codex launch configurations that cannot reuse the
  official daemon; or
- hide provider compatibility limitations behind a plausible fallback.

## 3. Terms and Product Invariants

### 3.1 Terms

**Saved account**
: One logical Sidekick account with a stable internal identifier, provider
  identity, user-facing label, plan metadata, credential authorities, health,
  and metrics history.

**Private authority**
: The provider-owned credential state used to maintain one saved account
  without adopting or overwriting another account.

**Native authority**
: The provider's default local credential state used by ordinary vendor
  commands.

**Shared runtime**
: The provider process or credential boundary through which ordinary local
  sessions observe the selected account.

**Active account**
: The account most recently proven by provider read-back to be active in that
  provider's shared runtime. A persisted Sidekick pointer alone cannot prove
  this state.

**Navigation cursor**
: The temporary account-row focus shown as `›`. It begins on the active
  account, moves for preview, and returns to the active account when preview
  is canceled.

**Maintenance worker**
: A bounded, short-lived child process that performs provider-heavy work and
  exits afterward.

### 3.2 Non-negotiable invariants

1. Selection changes runtime use, not the set of maintained accounts.
2. Every saved account is evaluated independently for maintenance and
   metrics.
3. One account failure does not stop later accounts from being attempted.
4. Durable credential writes belong only to the official Claude or Codex
   process.
5. Sidekick never copies provider credential files to perform a switch.
6. A successful switch requires provider identity read-back.
7. A failed switch does not silently select a different account.
8. An external official login wins over a stale Sidekick selection.
9. Persisted selection contains no access, refresh, ID, or setup token.
10. Credential health, metrics freshness, and active-account state remain
    independent.
11. Stale metrics are timestamped and never presented as current.
12. The dashboard, supervisor, and workers never log credential values.
13. Unsupported provider capabilities disable switching before native
    authentication is touched.
14. Existing qualified locks remain the final cross-process authority.
15. The normal vendor executable path is unchanged before and after setup.

## 4. Chosen Architecture

### 4.1 Runtime shape

```text
normal sidekick-usages TTY dashboard
                  |
                  | user-only local socket
                  v
       one lean Sidekick supervisor
          |                       |
          | high-priority         | durable due/retry queue
          v                       v
   Codex refresh broker       bounded short-lived workers
          |                       |
          v                       +-> Claude private/native operations
   official shared daemon         +-> Codex private-home operations
          |                       +-> usage and health collection
          v
 ordinary daemon-connected
       Codex terminals
```

The dashboard renders cached state immediately and sends actions to the
supervisor. It never reads a secret merely to move the cursor or render an
account row.

The supervisor owns coordination, not provider business logic. It:

- accepts authenticated local requests;
- provides readiness and progress events;
- serializes provider activation;
- tracks durable due work and retries;
- maintains one Codex refresh responder;
- starts isolated workers with hard deadlines;
- reconciles unfinished activation journals; and
- reports a safe degraded state when proof is unavailable.

Workers own credential-bearing operations. A worker receives an opaque
operation record, opens only the qualified account authority required for
that operation, returns a strict sanitized result, and exits.

### 4.2 Why B+ is selected

The two credible architectures were:

| Property | Split broker and scheduler | B+ supervised workers |
|---|---|---|
| Codex callback responsiveness | Strong | Strong through a reserved lane |
| Maintenance isolation | Separate process | Short-lived bounded workers |
| Event-driven maintenance | Additional coordination | Native to supervisor |
| Durable recovery | Two lifecycle paths | One queue and one journal owner |
| Service definitions | Service plus timer | One service plus WSL rescue |
| Resident provider imports | None in broker | None in supervisor |
| Health surface | Split | Unified |

A plain resident monolith is not B+. The approved design depends on all of
these conditions:

1. provider-heavy maintenance code is never imported by the supervisor;
2. refresh, usage, login, migration, and repair stay in killable workers;
3. Codex callbacks have reserved capacity and never queue behind maintenance;
4. due and retry records survive process and machine restart;
5. cross-process filesystem locks remain authoritative; and
6. the operating-system user manager restarts the supervisor.

Removing any condition requires reconsidering the split-service alternative.

### 4.3 Process and dependency boundaries

Rich remains the dashboard renderer. `prompt_toolkit` is the selected direct
dependency for portable key input, raw-mode lifecycle, signal handling, and
terminal restoration.

It must be imported lazily only after:

- stdin and stdout are confirmed as TTYs;
- the one-shot dashboard has rendered; and
- interactive mode has not been disabled.

The supervisor and non-interactive CLI paths must not import
`prompt_toolkit`.

The official Codex shared daemon is a provider process, not part of the
Sidekick supervisor's memory target. Private-home Codex app-server processes
are worker-scoped and terminate after their bounded operation.

## 5. Account Authority, Freshness, and Metrics

### 5.1 Claude

Each selectable Claude subscription account has one stable, absolute private
`CLAUDE_CONFIG_DIR`.

- On Linux and WSL, the private profile contains Claude's protected credential
  file.
- On macOS, the stable profile path selects Claude's config-derived Keychain
  service.
- The active subscription account uses Claude's default native credential
  authority.
- Inactive subscription accounts use their private authorities.
- Before Sidekick switches away from an active account, it uses official
  Claude to retain the latest verified generation in that account's private
  authority.
- Background maintenance never activates an inactive account globally.

Claude setup tokens are separate non-refreshing credentials:

- they remain saved and monitored through their fixed lifetime;
- heartbeat or usage activity may confirm that they still work;
- maintenance must say `regenerate`, never `refresh`, near expiry;
- they cannot become the native subscription authority for bare `claude`; and
- Enter starts official subscription-login migration before global use.

After successful migration, one logical Claude account may contain both:

- a private refreshable subscription-login authority used for switching; and
- the preserved setup token, monitored until the user removes it or it
  expires.

The account appears once in the dashboard and its metrics are not
double-counted.

### 5.2 Codex

Each saved Codex account has one independently authenticated, Sidekick-owned
private `CODEX_HOME`.

- Managed Codex inside that home is the sole durable credential writer.
- Sidekick does not duplicate its token bundle in the account index.
- Sidekick does not import the native default `auth.json`.
- Sidekick does not call Codex's private OAuth endpoint.
- A forced refresh uses app-server `account/read` with
  `refreshToken: true`.
- Success requires protected pre/post state showing the same identity and an
  advanced credential generation.
- A transient or permanent failure is stored only as a safe typed outcome.

The selected Codex account is projected ephemerally into the official shared
daemon. The private home remains the durable authority. A broker refresh and
scheduled maintenance both use the same per-home coordinator.

### 5.3 Scheduling

Every saved account receives its own due state. Selection is not a scheduling
filter.

Work becomes due from:

- provider-specific expiry policy;
- a backend 401;
- supervisor startup;
- network recovery;
- credential or account migration;
- explicit user refresh;
- provider-runtime restart; or
- a persisted retry deadline.

The scheduler:

- uses jittered deadlines to avoid refresh bursts;
- suppresses repeated permanent failures until relevant state changes;
- serializes work for the same private authority;
- permits independent accounts to continue;
- reserves the Codex callback lane;
- catches up missed work once after restart; and
- never runs duplicate legacy and supervisor schedules.

### 5.4 Metrics

Usage and token-activity collection continues for all saved accounts.

Each account result independently records:

- the last successful authenticated fetch time;
- the account identity used for that fetch;
- current usage windows when available;
- the last-known snapshot when a current fetch fails; and
- a typed, redacted failure.

A failed fetch remains visible. Last-known values render dimmed with their
age. They are not silently converted to zero and are not represented as
current.

Credential health, metrics health, and active-account state may differ. For
example, the active account can require repair while an inactive account has
fresh metrics.

## 6. Interactive Dashboard Contract

### 6.1 Current layout is preserved

The existing masthead, provider panels, account groups, usage columns, reset
countdowns, panel totals, warning rows, legend, wide layout, and narrow layout
remain product contracts.

The only persistent account-row addition is the `›` cursor immediately before
the existing account bullet in the focused provider.

```text
╭─ CLAUDE · 3 accounts ─────────────────────────────────────────────╮
│                                                                  │
│  › ●  work@example.test              max      0%     51%         │
│                                             2h 28m  4d 13h        │
│                                                                  │
│    ●  personal@example.test          max      0%     96%         │
│                                             1h 58m  15h 8m        │
│                                                                  │
│    ●  automation@example.test        max      0%     99%         │
│         Complete the official Claude login before using it.      │
│                                                                  │
╰──────────────────  1,106,429,559 tokens · since Dec 28, 2025 ───╯

 ↑/↓ or j/k move   Tab provider   Enter use   r refresh   ? help   q exit
```

The mock uses synthetic identities. Exact spacing remains responsive and is
owned by the existing renderer.

### 6.2 Cursor semantics

Only one cursor is visible because only one provider is focused.

On entry:

1. Sidekick reads the provider's actual active identity.
2. Claude is focused first when it has rows; otherwise the first non-empty
   provider is focused.
3. The cursor appears on that provider's matching saved account.
4. If the provider has an unknown external login, a temporary external row is
   inserted and receives the cursor.
5. If no account is active, the first account receives navigation focus and
   the footer states that no verified native login is active.

Input behavior:

- Up/Down and `j`/`k` move within the focused provider.
- Tab changes provider and begins on that provider's active account.
- Moving away from the active account is a preview only.
- Enter activates or repairs the previewed account.
- Esc cancels the preview and returns to the active account.
- `r` refreshes the previewed account.
- `R` refreshes all due accounts without changing selection.
- `?` opens concise keyboard help.
- `q` exits.
- Ctrl-C exits and restores the terminal.

Switching providers cancels an uncommitted preview. After a verified switch,
the cursor remains on the newly active account.

### 6.3 Row and footer messages

Healthy rows receive no extra status text and no active-account badge.

Rows show explanatory text only for actionable or degraded state, such as:

```text
Complete the official Claude login before using this account.
Codex rejected this saved login. Press Enter to repair and use it.
Metrics last updated 2h 14m ago; retry scheduled.
This external login is not saved in Sidekick.
```

Transient switch progress belongs in the footer:

```text
Switching to personal@example.test… verifying with Claude Code
```

The progress line disappears after completion. The footer then returns to
keyboard help.

The active account can itself display a warning. Cursor position communicates
which account is active; warning text communicates its independent health.

### 6.4 Confirmation policy

A routine healthy switch requires one Enter press.

An additional confirmation is allowed only when provider evidence proves the
switch is disruptive, including a Claude Remote Control session that the
provider will disconnect.

Migration, browser login, MFA, and provider consent remain official provider
steps rather than Sidekick confirmation dialogs.

### 6.5 First-use setup

The dashboard renders before service setup is needed.

On the first switch requiring the supervisor, Sidekick:

1. explains the per-user service in plain language;
2. asks once for confirmation;
3. installs and starts it without administrator access;
4. verifies socket, service, provider, and broker readiness; and
5. continues the original switch without making the user repeat it.

A failed setup preserves the dashboard and displays one actionable error.

### 6.6 Automation

Interactive mode is entered only when both stdin and stdout are TTYs.

These paths always render once and exit:

- redirected stdin or stdout;
- `sidekick-usages check`; and
- `sidekick-usages --no-interactive`.

The scriptable equivalent is:

```text
sidekick-usages use <provider> <label>
```

It never prompts. If setup, migration, login, or confirmation is required, it
fails with an exact interactive remediation command.

## 7. Supervisor Lifecycle and Platform Integration

### 7.1 Instance scope

There is one Sidekick supervisor per operating-system user:

- one per numeric Linux user in a native Linux installation;
- one per Linux user within each WSL distribution; and
- one per macOS login user.

It is not one service per account, provider, private home, shell, or terminal.
A different WSL distribution is a different OS instance and therefore has its
own user service and protected state.

### 7.2 Linux

Linux uses one systemd user service.

The service:

- starts with the user manager;
- restarts after unexpected exit;
- uses only user-owned paths;
- exposes a Unix-domain socket in a qualified private runtime directory;
- runs the exact installed Sidekick version; and
- requires no administrator access.

The prior periodic timer is removed only after the new supervisor is ready and
its durable schedule has adopted all due work.

### 7.3 WSL

WSL uses:

1. the same systemd user service while the distribution is running; and
2. one Windows Task Scheduler rescue/start trigger.

Microsoft documents that systemd does not keep a WSL distribution alive. The
Windows task therefore starts the correct distribution and user service after
logon or when recovery is needed.

The Windows task:

- is not a second maintenance scheduler;
- does not refresh accounts;
- receives no credential or token;
- does not own the Codex broker;
- uses explicit distribution and Linux-user identity; and
- reports start failure separately from account health.

### 7.4 macOS

macOS uses one per-user LaunchAgent.

It:

- runs only in the logged-in user's context;
- uses that user's login Keychain;
- restarts after unexpected exit;
- stores no credential in the property list;
- uses stable Sidekick application paths; and
- requires no administrator access.

Apple Silicon and Intel installations have the same product contract. Exact
Claude binary capability is checked independently for each architecture.

### 7.5 Readiness and upgrade

The dashboard and supervisor perform a versioned protocol handshake.

Readiness requires:

- matching compatible Sidekick protocol versions;
- a peer-verified socket;
- a healthy durable-state store;
- provider capability results;
- activation-journal reconciliation; and
- for Codex switching, a ready official daemon and singleton broker.

If the installed CLI and supervisor are incompatible, guided setup restarts
or upgrades the user service and rechecks readiness. It does not mutate
provider authentication while versions disagree.

### 7.6 Sleep, shutdown, and network loss

Due work is persisted before dispatch. After sleep, reboot, WSL shutdown, or
network recovery, the supervisor:

1. reloads the queue;
2. reconciles any running-operation record;
3. applies bounded jitter;
4. starts each due operation once; and
5. continues later accounts even when one fails.

No busy polling is permitted. Provider and native-authority observation uses
OS notifications where reliable and bounded scheduled read-back elsewhere.

## 8. Persisted State and Recovery

### 8.1 State classes

The feature adds strict provider-neutral records for:

1. **selected account state** — non-secret last verified identity and
   generation for each provider;
2. **activation journal** — the current or last provider switch transaction;
3. **due/retry queue** — account operations and safe retry metadata;
4. **service state** — protocol version, readiness, and sanitized lifecycle
   observations; and
5. **account authority metadata** — qualified private authority, provider
   identity, generation, and health.

Existing metrics snapshots remain independently owned by usage persistence.

These records use existing qualified path discovery, owner-only permissions,
strict schema decoding, bounded reads, cross-process locks, atomic writes, and
recovery behavior.

### 8.2 Selected state

Selected state contains only:

- provider;
- stable Sidekick account identifier;
- verified provider identity;
- verified provider-runtime generation;
- verification time; and
- sanitized activation outcome.

It contains no friendly label as authority and no credential value.

On supervisor startup, provider read-back is compared with this record:

- a matching identity restores ready state;
- a different saved identity reconciles to the external provider choice;
- an unknown identity becomes an external-active state;
- no identity becomes logged-out state; and
- an unreadable or ambiguous identity blocks switching for that provider.

### 8.3 Activation journal

Before a switch, Sidekick records:

- provider and operation identifier;
- source and target stable account identifiers;
- source provider identity and generation;
- expected target provider identity;
- current transaction phase;
- start and last-update times; and
- sanitized failure or recovery state.

Valid phases are conceptually:

```text
prepared
outgoing retained
target activated
read-back verified
committed
rolled back
reconciliation required
```

Exact enum names belong to implementation, but illegal phase transitions must
be unrepresentable.

### 8.4 Locking

A provider activation lock prevents simultaneous switches for the same
provider. Account authority locks serialize maintenance, broker refresh,
migration, and activation against the same private home.

When more than one account lock is required, locks are acquired in stable
internal-account order. Provider-wide activation is acquired before account
locks. The implementation plan must prove that the high-priority Codex
callback cannot deadlock behind an unrelated provider operation.

### 8.5 Recovery decision

Recovery reads actual provider state before taking action.

- If no native mutation occurred, the previous active account remains and the
  operation is closed as failed.
- If the target is already proven active, Sidekick completes the commit.
- If the previous account is proven active, Sidekick records rollback.
- If a different deliberate external identity is active, external choice
  wins and Sidekick reconciles.
- If an incomplete Sidekick transition changed native state to an unverified
  identity, Sidekick attempts official-provider rollback.
- If rollback cannot be proven, the provider is marked
  `reconciliation required` and no further switch is allowed.

Recovery never restores stale provider credential bytes.

## 9. Claude Activation

### 9.1 Capability preflight

Before activation, the Claude adapter verifies:

- the exact installed Claude executable and version;
- structured `claude auth status --json` behavior;
- private `CLAUDE_CONFIG_DIR` isolation;
- documented refresh-token provisioning support;
- the expected protected storage backend;
- absence of conflicting higher-priority credentials in Sidekick's execution
  environment; and
- on macOS, the config-derived Keychain namespace and absence of plaintext
  fallback.

An unsupported or ambiguous result disables Claude switching before native
credentials are changed.

### 9.2 Private profile stability

Each account receives one stable absolute normalized private profile path.
The path cannot be renamed, respelled, or replaced after authentication
without an explicit provider migration.

This is especially important on macOS, where Claude 2.1.218 derives the
Keychain service from the normalized path. Sidekick does not use the
undocumented `CLAUDE_SECURESTORAGE_CONFIG_DIR` variable.

### 9.3 Healthy subscription switch

The explicit switch transaction is:

1. acquire the Claude activation lock;
2. read and journal the native account identity and generation;
3. detect whether Remote Control or another known disruptive state requires
   confirmation;
4. use official Claude to retain the outgoing account's latest credential
   generation in its private profile;
5. verify the outgoing private identity and protected backend;
6. refresh and verify the target private profile through official Claude;
7. ask official `claude auth login` to activate the target at the default
   native boundary;
8. verify native target identity through structured auth status;
9. verify the protected credential envelope;
10. on macOS, prove no plaintext fallback appeared;
11. commit the sanitized selected-account record; and
12. notify the dashboard.

The credential-bearing worker may read the protected source credential only
for the documented official transition. It passes that credential to the
official child through a narrowly constructed child environment, never
through command arguments, persistence, logs, or the supervisor socket.

Sidekick does not call `security add-generic-password`, splice credential
JSON, or copy a Keychain payload. Official Claude performs every durable
write.

### 9.4 Setup-token migration

Enter on a setup-token-only account does not pretend to switch it globally.
Sidekick:

1. explains that the token remains tracked but cannot power bare `claude`;
2. starts official Claude subscription login in the account's final private
   profile;
3. waits for provider completion or cancellation;
4. verifies the provider identity against the saved account;
5. records the new private subscription authority;
6. preserves the setup token as a separate fixed-lifetime credential; and
7. continues the originally requested switch.

A mismatch requires explicit identity-replacement handling. It is never
accepted under the account's friendly label.

### 9.5 Existing sessions

New ordinary Claude terminals read the default native credential and use the
new account.

Existing ordinary subscription sessions observe the provider's shared
credential update on their next authentication resolution or provider
request. An already in-flight request completes under its original account.

Sessions using an API key, gateway, cloud-provider mode, higher-priority
environment token, or another isolated profile remain outside this switch.

### 9.6 Failure and rollback

Before native activation, failure leaves the previous account active.

After native mutation:

- Sidekick first reads the actual native identity;
- a verified target completes the switch;
- otherwise Sidekick uses official Claude to reactivate the verified outgoing
  private authority;
- rollback is verified through structured identity and protected-state
  read-back; and
- unprovable rollback blocks later switching and requests reconciliation.

Potentially revoked credential bytes are never restored manually.

## 10. Codex Activation

### 10.1 Durable private authority

Each account is authenticated independently in its final private
`CODEX_HOME`, using:

- app-server `account/login/start`; or
- a bounded official `codex login` child with that final `CODEX_HOME`.

The native default home is not copied. The account index does not become a
second token store.

Private-home maintenance:

1. launches managed Codex against the exact private home;
2. initializes the version-matched app-server protocol;
3. reads the account identity;
4. requests forced refresh only when due or required;
5. compares protected pre/post generations;
6. requires the same account identity;
7. stores only sanitized metadata; and
8. closes the private app-server.

### 10.2 Shared-daemon activation

The Codex switch transaction is:

1. acquire the Codex activation lock;
2. verify the official native daemon and broker are ready;
3. read and journal the daemon's current runtime identity;
4. obtain a verified fresh target access token from managed Codex in the
   target private home;
5. install that account ephemerally through version-gated
   `chatgptAuthTokens`;
6. verify that the daemon reports the expected identity;
7. commit the sanitized selected-account record; and
8. allow the daemon's account-update event to reach connected clients.

The access token exists only in the credential worker, broker, and official
daemon memory required for the operation. It is not written to selected
state.

### 10.3 Refresh broker

The supervisor keeps exactly one broker connection to the shared daemon.

When Codex requests external-token refresh, the broker:

1. validates the request and previous account identity;
2. resolves the matching selected private home;
3. dispatches a high-priority managed-Codex refresh;
4. verifies the same identity and advanced generation;
5. returns the new access token within Codex's deadline; and
6. stores only a sanitized outcome.

The callback does not wait behind scheduled maintenance. Other connected TUIs
must not answer the request. A stale, wrong-account, or late result is
rejected.

### 10.4 Session coverage

After one-time daemon enrollment:

- new ordinary Unix `codex` TUIs discover the official daemon and use the
  selected account;
- connected daemon-backed TUIs receive the account update;
- their next safe request uses the selected account; and
- in-flight requests are not retargeted.

Codex sessions started before daemon enrollment require one restart. The
first migration handles that restart guidance.

The first release does not claim switching for:

- `codex exec`;
- native Windows Codex;
- an ordinary TUI with a non-replayable launch configuration; or
- any launch that bypasses official daemon reuse.

### 10.5 Compatibility failure

`chatgptAuthTokens` is internal and unstable in the researched Codex release.
The integration is therefore exact-version and capability gated.

If the method or refresh-broker contract is absent:

- Sidekick disables Codex global switching;
- existing saved accounts remain visible;
- private-home maintenance may continue when its public managed methods still
  pass their independent capability gate;
- the native default auth file is not overwritten; and
- the dashboard instructs the user to use official Codex login until support
  is revalidated.

## 11. External Login Reconciliation

An explicit official provider login outside Sidekick is authoritative.

The supervisor observes native identity:

- at dashboard startup;
- before and after every switch;
- after provider runtime restart;
- after credential-generation notification where available; and
- on a bounded scheduled read-back.

When an external identity matches a saved account, Sidekick:

1. updates the verified active-account record;
2. moves that provider's initial cursor position;
3. cancels stale pending activation for another account; and
4. continues maintaining every saved account.

When it is unknown, Sidekick adds a temporary external row:

```text
› ●  External Claude login
     This external login is not saved in Sidekick.
```

The row is not silently imported, labeled, or assigned another account's
metrics. The user may explicitly start an import or select a saved account.

If an external login races a Sidekick switch, provider read-back decides the
result. Sidekick never overwrites the deliberate external identity merely to
make its journal match.

## 12. Security and Secret Handling

### 12.1 Threat model

The design protects against:

- another unprivileged OS user;
- accidental credential exposure through CLI output or diagnostics;
- label and provider-identity mixups;
- corrupted or partial persisted state;
- duplicated refresh authority;
- process interruption;
- stale selection pointers; and
- unintended provider fallback.

It cannot protect credentials from root, the user's own fully compromised
account, or a malicious process already holding the same Keychain and file
authority as the user.

### 12.2 Local control socket

The supervisor listens only on a local Unix-domain socket. It opens no TCP or
HTTP port.

The socket:

- lives under a qualified owner-only runtime directory;
- uses owner-only permissions;
- verifies peer operating-system credentials;
- accepts a bounded versioned protocol;
- rejects unknown fields and oversized messages;
- rate-limits abusive local requests;
- carries only opaque account identifiers, actions, progress, and sanitized
  results; and
- never transports access, refresh, ID, or setup tokens to the dashboard.

The WSL Windows rescue task never connects to the credential protocol.

### 12.3 Secret lifecycle

Credential values:

- remain in provider-owned protected storage;
- are opened only inside the provider adapter or credential worker that needs
  them;
- are never placed in command arguments;
- are never included in inherited general-purpose environments;
- are never stored in journals, queue records, service state, or metrics;
- are excluded from representations and exceptions;
- are redacted before crossing provider boundaries; and
- are released when the bounded operation exits.

When an official provider command requires a credential environment variable,
the worker creates a minimal child environment, launches the exact provider
executable directly, and removes its own reference after the child exits.

### 12.4 Files and Keychain

Sidekick private directories use owner-only traversal permissions. Secret
files retain provider-required owner-only modes. State writes remain atomic
and recoverable.

On macOS:

- official Claude remains the only Keychain writer;
- Sidekick runs in the login user context;
- a locked or unavailable Keychain fails closed;
- Sidekick never asks for or stores the macOS password;
- a plaintext credential fallback is rejected; and
- last-known metrics remain visible with a stale timestamp.

### 12.5 Logging

Local diagnostic history may contain only:

- opaque stable account identifier;
- provider;
- operation identifier;
- timestamps and duration;
- transaction phase;
- provider version;
- sanitized typed result; and
- retry or action-required state.

Friendly labels, email addresses, provider account IDs, token fingerprints,
credential payloads, raw provider bodies, and stable raw token hashes are
excluded from logs.

## 13. Errors, Diagnostics, and Uninstallation

### 13.1 Error classes

The product preserves separate user-visible outcomes for:

- service not installed;
- service unavailable;
- supervisor version mismatch;
- provider executable missing;
- provider version unsupported;
- account missing;
- account malformed;
- credential unreadable;
- credential expired but refreshable;
- credential rejected or revoked;
- official login required;
- setup-token regeneration required;
- migration required;
- transient provider or network failure;
- metrics stale;
- external account active;
- switch rolled back; and
- reconciliation required.

The dashboard renders plain-language actions. Provider-specific details remain
inside adapters and sanitized typed errors.

### 13.2 Dashboard failure behavior

If the service is unavailable, the dashboard still renders cached metrics and
attempts a guided restart when the user requests an action. Switching remains
disabled until readiness is proven.

If one account fails, later accounts remain available and scheduled.

If recovery cannot prove the active identity, the affected provider shows one
reconciliation warning. The other provider remains usable.

### 13.3 Doctor

`sidekick-usages doctor` reports, without secrets:

- Sidekick CLI and supervisor versions;
- service installation, startup, and readiness;
- OS backend and WSL rescue health;
- local socket ownership and peer verification;
- Claude and Codex executable provenance;
- provider capability results;
- native active identity relation;
- each private authority's health;
- selected-state generation relation;
- metrics freshness;
- due and retry status;
- unfinished activation state; and
- exact manual action.

Doctor distinguishes a healthy native login from an unhealthy saved private
authority. It never treats a warm account as proof that the service or WSL
rescue path is healthy.

### 13.4 Existing daemon commands

The existing public command group remains the lifecycle owner:

```text
sidekick-usages daemon install
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

Its implementation evolves from periodic one-shot maintenance to the
supervisor design while preserving supported compatibility and migration
behavior.

Uninstall:

- stops and removes the Sidekick user service;
- removes the WSL rescue task or macOS LaunchAgent it owns;
- removes only Sidekick-owned runtime files;
- leaves provider executables untouched;
- leaves the currently active native provider login untouched;
- leaves saved accounts and metrics untouched;
- does not log out Claude or Codex; and
- does not edit shell configuration.

Deleting saved accounts or credentials requires a separate explicit command.

## 14. Current-Machine Migration

### 14.1 Responsibility

Codex will perform the manual migration on this machine during implementation
rollout. The user is required only for an unavoidable provider-controlled
browser confirmation, MFA, password, or consent screen.

No migration occurs while this design or its implementation plan is being
written.

### 14.2 Preconditions

Before touching provider state, the rollout records:

- exact Sidekick, Claude, and Codex versions and executable paths;
- current vendor symlink targets;
- the current native identity for each provider;
- all saved logical accounts and credential kinds;
- each private authority's health;
- current metrics timestamps;
- current scheduler and service state; and
- a secret-safe recovery inventory.

All live mutations use the Sidekick CLI and official provider processes.
Credential files and Keychain records are never manually copied or edited.

### 14.3 Service transition

The current one-shot scheduler remains installed until:

1. the new supervisor is installed;
2. its user service is ready;
3. the queue contains every saved account;
4. the Codex daemon and broker are ready when supported;
5. one bounded maintenance pass succeeds or records truthful account errors;
   and
6. the new service proves restart recovery.

Only then is the legacy periodic schedule removed. Two maintenance schedulers
must never remain active.

### 14.4 Claude accounts

The current machine's setup-token-only Claude accounts are handled one at a
time:

1. allocate the final stable private profile;
2. launch official Claude subscription login there;
3. let the user complete only unavoidable provider confirmation;
4. verify the returned provider identity against the saved logical account;
5. verify protected storage and, on macOS, Keychain isolation;
6. record the refreshable subscription authority;
7. preserve the setup token and its fixed expiry;
8. collect a current account-scoped result;
9. switch into and away from the account through official transitions; and
10. prove inactive maintenance continues.

A canceled or mismatched login leaves the original saved account and active
native login unchanged.

### 14.5 Codex accounts

Each rejected or expired Codex account is repaired independently:

1. allocate or validate its final private `CODEX_HOME`;
2. start official Codex login inside that home;
3. verify the returned provider identity;
4. verify official managed refresh and durable generation;
5. migrate the account index to sanitized authority metadata;
6. retire the unusable duplicated credential only after replacement success;
7. preserve metrics history;
8. test broker projection and refresh; and
9. continue to the next account even if one requires later action.

A rejected or revoked token cannot be kept fresh. It is replaced through
official login rather than retained as false authority.

### 14.6 Native selection and session checks

After every private authority is ready:

1. identify and preserve the deliberate current native selections;
2. enroll the official Codex daemon;
3. restart only Codex sessions that predate daemon enrollment;
4. select every Claude account and verify a new bare `claude`;
5. select every Codex account and verify a new bare `codex`;
6. verify supported ongoing sessions on their next request;
7. verify in-flight requests are not retargeted;
8. verify all unselected accounts still receive maintenance and metrics;
9. verify external official login reconciliation;
10. verify vendor executable resolution and symlink targets are unchanged; and
11. capture before/after terminal output for the TUI change.

## 15. Testing and Performance

### 15.1 Automated test boundary

The normal test suite uses typed fake provider processes, filesystems, clocks,
Keychain adapters, subprocesses, sockets, schedulers, and network boundaries.
It never requires real credentials, public network access, or native login
mutation.

Real-account testing is a separately authorized rollout activity bounded to
the exact current-machine migration in Section 14.

### 15.2 Dashboard tests

Tests must prove:

- the current wide and narrow dashboard contracts remain intact;
- exactly one cursor appears in interactive mode;
- initial focus matches provider read-back;
- Tab focuses the other provider's active account;
- movement previews without switching;
- Esc returns to the active account;
- Enter performs one activation;
- the cursor stays on the verified target afterward;
- no `IN USE`, `ACTIVATING`, or `MIGRATION REQUIRED` badge appears;
- healthy rows have no extra status text;
- actionable warnings use the correct account state;
- service and provider progress appears only in the footer;
- `r`, `R`, `?`, `q`, and Ctrl-C behave as specified;
- terminal modes are restored after success, error, signal, and worker crash;
- redirected I/O, `check`, and `--no-interactive` never read keys; and
- the explicit `use` command never prompts.

Pseudoterminal tests cover arrow sequences, resize, narrow fallback, no-color
mode, and interrupted rendering.

### 15.3 Supervisor and recovery tests

Tests must prove:

- only one supervisor and Codex responder can run per user;
- socket peer credentials and permissions are enforced;
- malformed, oversized, unauthorized, and incompatible requests fail closed;
- a hung worker never delays a Codex callback;
- worker timeout or termination leaves the supervisor healthy;
- due work survives restart and runs once;
- account failures do not stop later accounts;
- no duplicate legacy schedule remains after transition;
- every activation phase recovers from forced process death;
- provider read-back, not the journal, decides recovery;
- wrong-account, malformed, and partial provider state fails closed;
- external login wins a race with Sidekick activation;
- rollback uses an official provider transition;
- failed rollback enters reconciliation-required state; and
- secrets are absent from sockets, logs, journals, errors, and process
  arguments.

### 15.4 Claude tests

Tests must cover:

- stable private profiles;
- independent Linux and WSL credential files;
- distinct macOS Keychain services for two stable config paths;
- Apple Silicon and Intel binary capability results;
- locked or unavailable Keychain;
- plaintext fallback detection;
- official login-only native writes;
- setup-token-only migration;
- preservation of setup-token lifetime tracking;
- provider identity mismatch;
- higher-priority credential conflict;
- Remote Control disruption confirmation;
- external `/login` reconciliation;
- existing session next-request behavior; and
- in-flight request stability.

### 15.5 Codex tests

Tests must cover:

- independently authenticated private homes;
- managed `account/read` without refresh;
- forced refresh with an advanced same-account generation;
- unchanged, regressed, null, malformed, and wrong-account post-state;
- private app-server timeout and framing failure;
- official daemon start, readiness, restart, and version mismatch;
- version-gated external account installation;
- two connected TUIs receiving one account update;
- exactly one refresh responder;
- broker routing by previous account identity;
- broker restart and selection rehydration;
- missing internal capability failing closed;
- default native `auth.json` never being written;
- pre-daemon sessions requiring one restart; and
- unsupported `codex exec` and launch modes being reported accurately.

### 15.6 Platform tests

The release matrix includes:

| Platform | Automated coverage | Required live coverage |
|---|---|---|
| Linux | systemd user service and TTY integration | install, restart, switch |
| WSL | Linux service plus task-generation tests | Windows logon, WSL stop/start, rescue |
| macOS arm64 | LaunchAgent, Keychain adapter, TTY | install, lock/unlock, switch |
| macOS x64 | LaunchAgent and exact binary capability | install and switch |
| Native Windows | feature-disabled behavior only | Not supported in v1 |

Existing project CI may continue testing unrelated native Windows behavior.
It must not falsely advertise this feature as supported there.

### 15.7 Performance gates

Measured on the documented reference machine:

- cached dashboard first paint completes within 250 ms;
- cursor input and visible feedback target a 50 ms local p95;
- the idle Sidekick supervisor uses no more than 30 MiB resident memory after
  steady state;
- the supervisor has no provider-heavy modules imported;
- maintenance and private-home provider workers exit after their task;
- the official shared Codex daemon is measured separately;
- no idle busy loop produces sustained CPU use;
- one hung worker does not affect dashboard input or broker latency; and
- catch-up work is bounded and does not create a refresh storm.

The research measurements were approximately 19 MiB for a minimal Python
control loop, 44 MiB after importing maintenance, and 69–89 MiB for the
official Codex app-server. They justify the process boundaries but are not
substitutes for release measurements.

## 16. Release Acceptance Gates

The feature is releasable only when all of these statements are proven:

1. Normal `claude` and `codex` resolve to the same vendor executables before
   and after Sidekick setup.
2. No wrapper, alias, shell function, PATH shim, vendor symlink replacement,
   or shell-startup edit exists.
3. The normal TTY dashboard supports the approved cursor interaction.
4. Non-TTY and explicit one-shot paths remain non-blocking.
5. Healthy rows carry no persistent selection labels.
6. Actionable account warnings remain clear and account-specific.
7. One healthy Enter press switches the selected provider.
8. Claude and Codex selections remain independent.
9. New ordinary supported provider terminals use the selected account.
10. Supported ongoing sessions change on their next safe request.
11. In-flight requests are not retargeted.
12. Every saved account remains maintained and measured when unselected.
13. Setup tokens remain tracked honestly through fixed expiry.
14. Invalid Codex credentials are repaired through independent official login.
15. One account failure does not stop another account.
16. External official login wins and reconciles without silent import.
17. Every interrupted switch either commits a verified identity, rolls back
    through an official provider transition, or blocks for reconciliation.
18. The supervisor meets memory, responsiveness, and callback-isolation gates.
19. Linux, WSL, Apple Silicon macOS, and Intel macOS pass their required
    platform tests.
20. Provider compatibility failure occurs before native auth mutation.
21. Secret-leak tests pass for output, arguments, sockets, logs, journals,
    errors, and representations.
22. Guided installation and complete uninstallation require no administrator
    rights and leave provider logins untouched.
23. The current-machine migration and cross-account live verification are
    completed.

## 17. Repository Ownership

Implementation follows the existing repository boundaries.

### 17.1 `cli/`

Owns:

- TTY detection and lazy interactive composition;
- key handling and terminal restoration;
- the `use` command;
- first-use service consent;
- progress and error presentation;
- non-interactive refusal to prompt; and
- registration through the existing typed lazy Typer composition.

The registration-only root remains registration-only.

### 17.2 `usage/`

Owns:

- cursor-aware Rich rendering;
- stable wide and narrow layouts;
- provider focus and warning-row presentation;
- transient footer rendering; and
- stale metrics presentation.

It does not own provider activation or credentials.

### 17.3 `core/`

Owns infrastructure-free identifiers, enums, legal state transitions, and UTC
invariants. It does not import CLI, Rich, provider, persistence, filesystem,
HTTP, subprocess, or OS path discovery.

### 17.4 `providers/claude/`

Owns:

- installed Claude discovery and capability checks;
- structured auth status;
- official private and native login adapters;
- protected credential-envelope validation;
- stable profile and macOS Keychain namespace behavior;
- setup-token capability distinctions;
- higher-priority credential detection; and
- provider-specific sanitized failures.

### 17.5 `providers/codex/`

Owns:

- installed Codex discovery and capability checks;
- private-home managed app-server protocol;
- account login, read, refresh, and generation validation;
- official shared-daemon lifecycle and socket health;
- version-gated external account installation;
- broker request and response protocol;
- daemon account-update handling; and
- provider-specific sanitized failures.

The current direct private OAuth refresh path is removed after migration and
compatibility closure; it is not a fallback.

### 17.6 `credentials/`

Owns:

- provider-neutral activation orchestration;
- one-operation-per-authority coordination;
- pre/post snapshots;
- same-account and generation invariants;
- official rollback policy;
- external-login reconciliation;
- setup-token-to-subscription migration policy; and
- broker and scheduled refresh serialization.

### 17.7 `persistence/`

Owns:

- strict selected-state, journal, queue, and authority schemas;
- qualified private Claude and Codex paths;
- owner-only permissions;
- atomic writes and recovery;
- cross-process locking;
- account rename, reset, removal, and migration transactions;
- sanitized credential-index migration; and
- no-secret validation.

### 17.8 `daemon.py`, `maintenance.py`, and `heartbeat/`

`daemon.py` owns OS service installation, status, uninstall, readiness, and
the lean supervisor boundary.

`maintenance.py` owns selection-independent due work, dispatch policy,
backoff, and result aggregation.

`heartbeat/` retains provider activity behavior and must not become a second
selection or scheduling authority.

### 17.9 Other owners

- `paths.py` remains the only Sidekick application-path owner.
- `clock.py` remains the wall-clock acquisition owner.
- `http/` retains pooled HTTPS and retry policy where provider adapters still
  need direct HTTPS.
- `serialization/` retains strict JSON decoding.
- `doctor.py` owns secret-safe diagnostic presentation.
- `packaging/` and workflows own exact artifact and platform verification.
- Tests mirror the public service, transaction, adapter, persistence, CLI,
  and rendering boundaries.

Architecture checks must reject provider imports in the supervisor module and
interactive/rendering imports outside the CLI or usage boundaries.

## 18. Rejected Designs

The following are explicitly rejected:

- a `claude` or `codex` wrapper, alias, shim, or shell function;
- replacing a vendor-managed executable or symlink;
- editing shell startup files or exporting a selected home globally;
- copying or swapping `auth.json` files;
- manually writing Claude credential JSON or Keychain entries;
- using Codex config profiles as credential profiles;
- calling Codex's private OAuth endpoint;
- duplicating one rotating Codex credential family across homes;
- using the active native login as every saved account's maintenance source;
- filtering maintenance or metrics to the selected account;
- one daemon per account or private home;
- a provider-heavy resident monolith;
- an independent scheduler plus broker when B+ invariants are satisfied;
- performing maintenance inline in the dashboard;
- a periodic one-shot scheduler as the Codex refresh responder;
- storing credentials in the selection record or control protocol;
- silently accepting macOS plaintext credential fallback;
- silently importing an unknown external login;
- falling back to a different saved account after failure;
- using a friendly label as provider identity;
- row-level `IN USE`, `ACTIVATING`, or `MIGRATION REQUIRED` badges;
- prompting from redirected or explicitly non-interactive commands; and
- claiming unsupported native Windows, `codex exec`, or non-daemon coverage.

## 19. Risks and Revalidation Triggers

### 19.1 Codex internal daemon contract

The researched `chatgptAuthTokens` bridge is internal and unstable. Any Codex
upgrade, schema change, daemon lifecycle change, refresh-request deadline
change, or account-update behavior change requires:

- source and schema reinspection;
- exact installed-binary capability tests;
- synthetic broker integration tests; and
- live promotion only after the fail-closed gate passes.

There is a narrow daemon-start window before the selected account is
rehydrated. Without a wrapper, Sidekick cannot intercept a bare `codex`
launched while the official control socket is absent; Codex may use its
embedded native path. OS supervision, readiness checks, and rapid rehydration
minimize but cannot mathematically remove this window. Complete atomicity
requires a stable upstream persistent-auth or account-switch contract.

### 19.2 Claude credential storage

The config-derived macOS Keychain service is proven in the exact official
2.1.218 arm64 and x64 binaries, but it is not a separately versioned public
storage API. Every Claude upgrade requires:

- documentation review;
- exact platform package inspection or capability proof;
- two-profile isolation testing;
- protected-backend verification; and
- plaintext-fallback rejection testing.

### 19.3 Provider session semantics

Existing-session adoption is provider behavior, not Sidekick process
injection. A provider release that changes credential reload or daemon event
handling requires renewed tests. The product must continue saying
`next safe request`, not `mid-request`.

### 19.4 Setup-token lifetime

Claude setup tokens remain fixed-lifetime credentials. No amount of Sidekick
maintenance can rotate them. Expiry and regeneration language must remain
truthful even after the same account gains a subscription authority.

### 19.5 Keychain and unattended work

A locked login Keychain may pause macOS maintenance. Sidekick preserves
last-known metrics, marks them stale, and asks for user repair. It never
downgrades storage or attempts to unlock Keychain.

### 19.6 WSL lifecycle

WSL systemd cannot keep a distribution alive. Windows updates, WSL changes,
Task Scheduler policy, distribution rename, or Linux-user rename require
revalidation of the rescue/start path.

### 19.7 Dependency and performance

`prompt_toolkit`, Rich, Python, and provider import changes can affect
dashboard first paint and supervisor memory. Release measurement must use the
exact built wheel and supported Python version. If the approved performance
gates fail, optimize lazy boundaries before weakening resilience.

## 20. Source Matrix

The exhaustive evidence, source excerpts, local binary findings, and
comparative analysis are in the tracked [research report][research].

| Source | Type | Used for |
|---|---|---|
| [OpenAI Authentication][openai-auth] | Official docs | Codex-managed login and automatic refresh |
| [OpenAI CI/CD Auth][openai-ci] | Official docs | One serialized owner; no direct OAuth calls |
| [OpenAI App Server][openai-app-server] | Official docs | Managed account read and forced refresh |
| [OpenAI Codex source][codex-source] | Release source | Daemon reuse, external auth, broker, session events |
| [Claude IAM][claude-iam] | Official docs | Credential storage and native login behavior |
| [Claude environment variables][claude-env] | Official docs | Private profiles and official refresh-token provisioning |
| [Claude changelog][claude-changelog] | Release record | Shared credentials and existing-session reload |
| Exact Claude 2.1.218 macOS packages | Official binaries | Config-derived Keychain services on arm64 and x64 |
| [Microsoft WSL systemd][wsl-systemd] | Official docs | WSL user-service and lifetime boundary |
| [Apple launchd guide][apple-launchd] | Official docs | Per-user LaunchAgent lifecycle |
| Local installed binaries and state | Local evidence | Exact compatibility baseline and current migration need |

Implementation must revalidate current provider versions rather than treating
the 2026-07-23 evidence baseline as permanent.

[research]: ../research/2026-07-23-managed-authentication-and-native-account-selection.md
[openai-auth]: https://learn.chatgpt.com/docs/auth
[openai-ci]: https://learn.chatgpt.com/docs/auth/ci-cd-auth
[openai-app-server]: https://learn.chatgpt.com/docs/app-server#auth-endpoints
[codex-source]: https://github.com/openai/codex/tree/25af12f7e61572b0bc18ddb1008be543b91519b0
[claude-iam]: https://code.claude.com/docs/en/iam
[claude-env]: https://code.claude.com/docs/en/env-vars
[claude-changelog]: https://github.com/anthropics/claude-code/blob/2982f951552e94f38cd972764ae94c1d90c41da3/CHANGELOG.md
[wsl-systemd]: https://learn.microsoft.com/en-us/windows/wsl/systemd
[apple-launchd]: https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
