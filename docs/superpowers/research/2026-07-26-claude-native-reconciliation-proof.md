# Claude Native Reconciliation and Association Proof

- **Status:** Proven and implementation-ready
- **Date:** 2026-07-26
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Source baseline:** `0ba4f8087c8492db9ffb6b9d904839b9767a3581`
- **Required platforms:** Linux, WSL, macOS Arm64, macOS x64
- **Live-change boundary:** Read-only; no Claude login, selection, credential
  write, process signal, or service mutation was performed

## Verdict

The repeated Claude reconciliation warnings are caused by two Sidekick
defects, not by a broken Claude login:

1. Sidekick reads Anthropic's provider-owned
   `~/.claude/.credentials.json` with Sidekick's stricter private-storage
   rule. The provider directory is safely `0755`, but that rule requires
   `0700`, so the read fails before official status reconciliation can
   complete.
2. The dashboard copies that one provider-level read failure onto every
   saved Claude row.

The correction is feasible without wrapping `claude`, guessing an account,
pinning a provider release, changing the active native login, or weakening
Sidekick-owned storage:

- add one read-only provider-owned filesystem operation;
- derive one strictly validated status association key from official,
  exact-profile `claude auth status`;
- pair that key with the complete protected credential semantics and verify
  the complete snapshot twice before committing selected state;
- represent an unassociated native login as one external row;
- associate a saved account only through official login in that account's
  private profile after the user explicitly chooses it;
- activate Claude through one journaled official-login transition that
  verifies both secure request credentials and Claude's separate account
  profile;
- remove CLI version from durable credential equality; and
- render provider degradation once, while preserving account-specific
  warnings on their own rows.

There are no unresolved design alternatives in this report.

## What was proven

### Live, read-only baseline

The installed command resolves as follows:

```text
~/.local/bin/claude
  -> ~/.local/share/claude/versions/2.1.220
```

The stable launcher reported `2.1.220 (Claude Code)`. Two read-only
`claude auth status` calls returned the same healthy first-party Team
subscription state. Only field presence and classification were retained;
personal values were redacted.

That release number identifies the reproducible evidence snapshot. It is not
a runtime target, equality requirement, upper bound, or release allowlist.

Before and after those calls:

- the active Claude process retained the same PID and command;
- `~/.claude` retained the same owner, device, inode, and `0755` mode;
- `.credentials.json` retained the same owner, device, inode, size,
  timestamps, and `0600` mode; and
- the Sidekick supervisor retained the same PID and stable launcher argument.

Anthropic officially documents `claude auth status` as JSON with exit code
`0` when logged in and `1` when logged out. It also documents Linux
credentials at `~/.claude/.credentials.json` with mode `0600`.
[Claude CLI reference](https://code.claude.com/docs/en/cli-usage),
[authentication documentation](https://code.claude.com/docs/en/authentication).

### Exact local failure chain

The current source takes this path:

```text
native Claude credential
  -> PersistenceFilesystem.read_opaque_private()
  -> PosixPlatform.read(private_parent=True)
  -> reject every group/other parent bit
  -> safe 0755 provider directory becomes UNREADABLE
  -> selected Claude runtime becomes UNREADABLE
  -> every saved Claude row receives RECONCILIATION_REQUIRED
```

The provider-owned credential enters the private reader in
[`credentials/claude/native/authority/service.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/credentials/claude/native/authority/service.py#L45-L83).
The POSIX adapter selects the strict parent rule in
[`persistence/platform/posix/adapter.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/persistence/platform/posix/adapter.py#L297-L365),
and that rule rejects `0755` in
[`persistence/platform/posix/namespace.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/persistence/platform/posix/namespace.py#L110-L140).
The dashboard then copies provider unreadability into each row in
[`usage/dashboard/service.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/usage/dashboard/service.py#L210-L244).

A direct invocation of the current native reader rejected the live file with
`UnsafeManagedFileError`, while official status reported the login as healthy.
This reproduces the defect without exposing credential contents.

### Filesystem feasibility and security

A disposable synthetic proof exercised the existing descriptor reader:

| State | Result |
| --- | --- |
| Owner `0755` parent and owner regular `0600` file | Accepted |
| Owner `0700` parent and owner regular `0600` file | Accepted |
| Group-writable `0775` parent | Rejected |
| World-readable `0644` file | Rejected |
| Credential symlink | Rejected |
| Two-link credential | Rejected |
| Oversized credential | Rejected |
| Final provider-directory symlink | Rejected |

POSIX directory write permission controls whether another principal can
create, remove, or rename entries. A `0755` directory permits search and
listing but not entry mutation by group or other users.
[Linux `path_resolution(7)`](https://man7.org/linux/man-pages/man7/path_resolution.7.html),
[`unlink(2)`](https://man7.org/linux/man-pages/man2/unlink.2.html),
[`rename(2)`](https://man7.org/linux/man-pages/man2/rename.2.html).

The existing reader already checks regular-file type, current-user ownership,
private file mode, same-device identity, bounded size, link count, and
device/inode/size/mtime/ctime stability before and after the read.
[`persistence/platform/posix/files.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/persistence/platform/posix/files.py#L13-L89).

The production operation must add the protections that a simple
`private_parent=False` change would miss:

- filesystem qualification and file access use the same held parent
  descriptor;
- only the direct `process_home / ".claude"` directory beneath the
  operating-system-discovered current-user home is accepted;
- the direct provider directory is revalidated after the read;
- the final provider directory and credential entry are never followed;
- exact-name scanning retains constant-size match/alias state, examines every
  accepted entry, and fails `TOO_LARGE` if entry 4,097 exists;
- the provider file must have exactly one link;
- the credential limit is 1 MiB, matching the Keychain boundary; and
- macOS rejects any extended ACL on the direct provider directory or file.

Sidekick-owned private state keeps its existing stricter `0700` directory,
private-file, atomic-publication, and recovery rules.

### Why official status is the association surface

Anthropic documents `CLAUDE_CONFIG_DIR` as the configuration, credential,
history, and plugin root and explicitly presents it as the mechanism for
running multiple accounts side by side.
[Claude environment variables](https://code.claude.com/docs/en/env-vars).

Read-only A/B probes against the installed binary showed:

- the native profile returned logged-in status;
- a fresh isolated `CLAUDE_CONFIG_DIR` returned logged-out status and exit
  code `1`; and
- two distinct isolated directories remained independent.

The current credential envelope can contain valid access and refresh
credentials without `tokenAccount` identity. The installed status command
still supplied bounded `email`, `orgId`, `orgName`, and
`subscriptionType`. Therefore exact-profile status is the only available
runtime association surface, while the protected envelope supplies
generation, expiry, scope, and refresh health.

The installed Linux binary and matching official macOS artifacts show why
both observations are required. Claude keeps request credentials in the
protected `claudeAiOauth` record and keeps displayed account/profile state in
a separate global `oauthAccount` record. The official login flow updates both.
Protected storage alone proves request authority; structured status proves
the matching account-profile state. Neither is sufficient by itself.

The opaque status association key is:

```text
sha256(
  length(email) || exact_provider_email ||
  length(orgId) || exact_provider_org_id
)
```

Both nonempty `email` and nonempty `orgId` are required. The digest is
domain-prefixed before being stored in the existing opaque
`ProviderIdentity` value type. Raw email and organization ID are not
persisted in selected state.

Anthropic documents JSON status but does not document those exact fields as
immutable or globally unique identifiers. The key therefore becomes trusted
association evidence only after the user explicitly connects one exact
private profile through official login. Every later use revalidates it.
Duplicate keys across saved private profiles fail closed, and the key never
authorizes a label-based merge, import, overwrite, or identity replacement.
It does not upgrade a setup-token record to the remote vault design's
`provider_verified` assurance; it is a local, explicitly established profile
association.

The following are never identity evidence:

- a Sidekick label;
- row order;
- cursor position;
- plan name;
- organization display name;
- subscription display name; or
- the provider executable version.

If official status omits or changes a required field, Sidekick preserves
saved accounts and reports one provider verification failure. It does not
fall back to a label or mutable organization name.

### Race proof

Metadata checks alone are insufficient on the current WSL filesystem. A
same-size, same-tick synthetic overwrite did not advance `mtime` or `ctime`,
so a metadata-only observation accepted it. When the timestamp was advanced,
the reader correctly returned `CHANGED`.

The existing reconciler already performs two complete native observations,
retries once when they disagree, performs a final current-state check after
the retry, and compare-and-swaps selected state.
[`credentials/claude/activation/reconciliation.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/credentials/claude/activation/reconciliation.py#L62-L110).

That semantic defense remains mandatory:

1. read the exact profile credential;
2. obtain the official status association key from the same profile;
3. reread and require the same `(generation, plan, scopes,
   access_expires_at, refresh_expires_at, health, action)` tuple;
4. perform the reconciler's second complete observation; and
5. commit only through the existing compare-and-swap.

The outer observations compare the association key plus that complete
protected tuple. Executable version remains diagnostic and outside this
equality. A mixed observation never becomes selected state.

### Saved-account association

A healthy native login is not automatically one of Sidekick's saved rows.
Until one explicitly associated private profile proves the same current
status association key, the native login is represented as one
`EXTERNAL_ACTIVE` row. A duplicate private key also remains external and
disables switching.

Association is an explicit, account-scoped operation:

1. the user focuses one setup-only Claude row and presses Enter;
2. the TUI exits and restores the terminal;
3. Sidekick reloads the stable account ID and prompts:

   ```text
   Connect '<label>' for future Claude switching?
   This keeps its setup token and does not change the active Claude account. [y/N]
   ```

4. official `claude auth login --claudeai` runs with only that account's
   private `CLAUDE_CONFIG_DIR`;
5. two complete official-status and protected-semantic observations must
   agree;
6. only that saved account gains a managed subscription authority; and
7. its existing setup-token authority and metrics remain intact.

Association does not modify or select the native Claude login. If the native
status key already equals the newly associated private key, read-only
reconciliation may select that row without a credential write. Otherwise the
external row remains and a later Enter is a separate, explicit switch.

Claude remains the only credential writer. Sidekick never copies, edits,
chmods, replaces, or removes a provider credential file or Keychain item.

### Official native activation

A healthy saved-account switch is one journaled official transition:

1. journal the outgoing native status association key and complete protected
   semantic tuple and, on Linux/WSL, the provider-visible credential
   `mtimeMs`;
2. retain the outgoing authority in its private profile through official
   Claude;
3. prove the target private profile through the same complete two-part proof;
4. invoke official `claude auth login --claudeai` at the native default
   profile using the target's short-lived leased refresh credential;
5. require two stable post-login observations with the target status
   association key and target protected semantics; and
6. on Linux/WSL, require official login to advance the credential
   `mtimeMs`; and
7. only then commit the selected target.

The official login process owns both provider writes. Sidekick neither
constructs nor patches `claudeAiOauth` or `oauthAccount`. If official login
updates only one store, either store changes again, the two observations do
not describe the same target, or Linux/WSL `mtimeMs` does not advance, the
selection is not committed. Recovery re-observes both stores and may use only
the official login path to complete or roll back the journaled transition.
Every Linux/WSL recovery or rollback login has the same pre/post `mtimeMs`
advance requirement before its result can be treated as propagated.

## Cross-platform contract

| Platform | Association surface | Protected generation and health | Required safety |
| --- | --- | --- | --- |
| Linux | Exact-profile official status | Provider-owned `0600` file | Current-user, non-writable direct parent; no-follow; one link; same device; bounded stable read |
| WSL | Same Linux contract | Same Linux file contract | Same checks plus live WSL Gate A |
| macOS Arm64 | Exact-profile official status | macOS Keychain | APFS; `O_NOFOLLOW_ANY`; no extended ACL; exact `/usr/bin/security`; bounded typed failures; plaintext fallback rejected before and after |
| macOS x64 | Same as Arm64 | Same as Arm64 | Same release and CI gates |

Anthropic documents macOS Keychain storage and Linux file storage.
[Authentication documentation](https://code.claude.com/docs/en/authentication).
Apple documents descriptor-based ACL inspection and Keychain as the
platform secret store.
[Apple `acl_get_fd_np(3)`](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/acl_get_fd.3.html),
[Keychain Services](https://developer.apple.com/documentation/security/keychain-services).

The exact current macOS Keychain item-name derivation was independently
confirmed in the official Arm64 and x64 `2.1.220` packages. That algorithm is
not a published identity API. It remains isolated inside the
secret-maintenance adapter and is validated on both macOS CI architectures.
Selection and association use the capability-probed status key, so an
upstream Keychain namespace change cannot cause Sidekick to guess another
account.

Native Windows remains feature-disabled and is outside the required product
contract.

## Provider update resilience

### The rule

```text
provider readiness =
  stable vendor launcher
  + current target qualified for this operation
  + required behavior/schema probes pass
  + target unchanged until the operation ends

provider readiness != saved executable version equals installed version
```

No provider release target, saved-release equality, upper bound, or release
allowlist may determine account association or readiness. Explicit one-sided
lower safety floors remain valid and accept every newer release whose
behavioral probes pass.

Concretely, no Sidekick account, service artifact, worker, or launcher stores
"run Claude/Codex release X." The only resolved release path is frozen in
memory for the current operation to prevent a mid-operation executable swap.
The next operation resolves the vendor launcher again. A lower floor can
exclude only an older build known to lack a required official surface; it
cannot reject a newer build. Current behavior and schema probes, not a saved
release string, decide whether the operation can proceed.

### Claude

Current Sidekick already:

- stores the stable launcher path;
- resolves its current target for each worker operation;
- verifies the target before and after provider commands;
- probes `auth status` and `auth login --help`; and
- accepts newer versions above its supported safety floor.

The remaining update defect is the exact comparison in
`managed_authority_matches()`:

```text
saved executable_version == observed executable_version
```

That comparison is unrelated to credential identity. It is reused by
activation, resolution, migration verification, and maintenance, so one
compatible Claude update can falsely mark every valid private authority as
requiring reconciliation.
[`credentials/claude/managed/authority/service.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/credentials/claude/managed/authority/service.py#L192-L205).

The equality must be removed. `executable_version` remains diagnostic
"last verified with" metadata and is refreshed only after a successful
authority commit. The lower safety floor blocks only known-inadequate old
releases; it has no upper bound and cannot reject a normal update merely
because its release number changed. Behavioral probes remain authoritative.

The existing focused launcher test already proves that a target change during
an operation fails closed and that rediscovering the same launcher at a newer
compatible target succeeds.
[`tests/providers/claude/test_managed_boundaries.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/tests/providers/claude/test_managed_boundaries.py#L138-L264).

### Codex

The same no-pin audit was applied to Codex:

- the service stores the stable `codex` launcher, not a release target;
- a worker resolves and qualifies the launcher's current target;
- the current CLI generates and probes its own app-server schema;
- a compatible newer CLI is accepted;
- if that update leaves an older official managed daemon running, Sidekick
  asks the current official CLI to restart it once and verifies the new
  daemon; and
- version metadata is not part of saved Codex account identity.

The running daemon must match the current operation's CLI because the
app-server schema is generated per Codex release. Automatic current-CLI
restart is a protocol-safety check, not a version pin. OpenAI documents that
generated app-server bindings are specific to the CLI version that produced
them.
[Codex app-server documentation](https://learn.chatgpt.com/docs/app-server).

The existing focused test proves a newer `0.146.0` target is accepted and an
older managed daemon is restarted exactly once.
[`tests/providers/codex/test_app_server.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/tests/providers/codex/test_app_server.py#L80-L130),
[`same file`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/tests/providers/codex/test_app_server.py#L145-L296).

## Existing-session behavior after Claude selection

The installed Linux binary and release-matched official Darwin Arm64 and x64
artifacts close the ordinary-session contract. This is a release-observed
provider behavior, not a Sidekick version gate:

| Surface | Behavior after selecting account B |
| --- | --- |
| New normal `claude` process | Uses account B from the native default profile |
| Existing ordinary first-party subscription session on Linux/WSL | Uses B on the next request after Sidekick observes official login advance the provider-owned credential file's `mtimeMs` |
| Existing ordinary first-party subscription session on macOS | Uses B on the first request after Claude's healthy-Keychain cache expires, bounded by 30 seconds, or earlier if the old token is rejected |
| Request already in flight | Finishes or fails with the credential captured when it began; Sidekick never migrates or replays it |
| Explicit `CLAUDE_CONFIG_DIR` | Remains on that explicit profile |
| API key, auth token, helper, gateway, Bedrock, Vertex, or Foundry session | Remains on the higher-precedence explicit credential |
| Different-account Remote Control session | Claude disconnects it by design; Sidekick requires explicit approval before the switch |

Each ordinary model query creates a new client. Before constructing it, Claude
checks the native credential source, reads the complete current OAuth record,
and supplies that record's access token. Linux invalidates that state when
the credential file's `mtimeMs` changes. Sidekick therefore requires an
observed official-login advance before committing the Linux/WSL propagation
guarantee; a semantic credential change without that advance fails closed.
The Darwin builds use the same request-time path, but their Keychain reader
caches the decoded envelope for 30 seconds. Claude independently watches its
global account-profile file every second, so the official login transition
converges both request identity and displayed profile state.

Anthropic's changelog corroborates, but does not substitute for, that binary
control-flow proof. It records removal of stale request configuration after
credentials change outside a session and expressly documents
different-account Remote Control disconnection.
[External credential refresh](https://github.com/anthropics/claude-code/blob/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843/CHANGELOG.md#L853-L859),
[Remote Control](https://github.com/anthropics/claude-code/blob/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843/CHANGELOG.md#L877-L880).

A Keychain read failure is not reported as successful propagation: Claude may
continue using its stale in-process cache, so Sidekick preserves a provider
failure until native status and protected read-back are healthy. It never
signals, restarts, injects input into, or replays work for an existing
session.

Normal `claude` remains Anthropic's command. Sidekick adds no wrapper, alias,
shell function, PATH shim, symlink replacement, or shell-startup edit.

## Dashboard result

After the correction:

- a uniquely associated saved account owns the cursor;
- an unassociated but healthy native login appears as one external row;
- no saved row is implied active when association is unverified;
- provider unreadability appears once as
  `<provider> login could not be verified; account switching is paused.`;
- unavailable verification appears once as
  `<provider> account verification is unavailable; saved metrics remain visible.`;
- that provider advisory is muted amber below the provider column headings
  and before the first row;
- expired/rejected/setup/repair/metrics states remain on the affected account;
- only a focused setup-only row shows
  `Enter to connect this account for Claude switching.`; and
- cached first paint and the fixed keyboard footer remain unchanged.

The existing muted advisory role is reused. No bright yellow, new theme,
duplicate warning state, or Rich dependency is introduced.

## Performance and memory

The fix does not add provider work to first paint:

- cached dashboard projection remains the first frame;
- native reconciliation remains outside the cached renderer;
- all account usage and activity lookups continue to be submitted before
  results are consumed;
- the existing worker cap remains in force;
- exact-directory scanning uses constant memory and stops at 4,096 entries;
- credential reads are capped at 1 MiB;
- status output remains capped at 4 KiB with a five-second timeout; and
- association starts one official login only for the chosen account.

The current concurrent wave is visible in
[`usage/lookup/wave.py`](https://github.com/Sawmonabo/sidekick-usages/blob/0ba4f8087c8492db9ffb6b9d904839b9767a3581/src/sidekick_usages/usage/lookup/wave.py#L90-L155).
There is no serial provider loop and no daemon or thread per account.

## Acceptance proof

### Automated, load-bearing checks

Only existing test owners are extended:

1. provider filesystem, macOS Keychain fallback, and compatible launcher
   update;
2. status-derived association, unknown external login, and race rejection;
3. native activation commits only after the official process leaves matching
   secure-credential and account-profile truth;
4. one-account association preserving setup authority and native state;
5. provider-level dashboard state without row fan-out;
6. wide and narrow single-advisory rendering; and
7. typed TUI association handoff to the existing migration coordinator.

No new test file, broad permutation matrix, live-provider test, network test,
or duplicate PTY suite is required.

Local iteration uses only named focused tests plus Ruff, `ty`, the architecture
gate, and `git diff --check`. The full suite and exact-wheel smoke run remain
on the existing CI matrix to avoid another high-memory WSL run:

- Linux x64;
- macOS Arm64;
- macOS x64; and
- Windows regression coverage with Claude selection still disabled.

### Live Gate A: safe reconciliation

Gate A is mandatory before claiming the defect fixed. It does not change the
Claude account:

1. record redacted official status, stable launcher, active Claude PIDs/start
   times, and native credential metadata;
2. install the candidate through the Sidekick CLI;
3. allow read-only reconciliation and one natural scheduled cycle;
4. require the same launcher, official status association key, PIDs, and
   credential metadata across each stable observation;
5. require the production path and focused boundary proof to expose no native
   credential writer, chmod, repair, login, logout, or activation call;
6. require `EXTERNAL_ACTIVE` if no private saved account proves the
   association;
7. require one external row and no repeated saved-row reconciliation warning;
   and
8. require the compatible installed version not to invalidate any authority.

Gate A must not press Enter on a Claude account, run login, select an account,
restart Claude, signal a Claude process, or read, hash, copy, or claim equality
of credential bytes. If an independent Claude process naturally refreshes
credentials during an observation, the run is concurrent and must be repeated
after state stabilizes.

### Live Gate B: separately approved association

Gate B runs only after the user names one exact saved account:

1. preserve the target's setup authority and metrics and every other account;
2. confirm the exact displayed target;
3. run official login only in that target's private profile;
4. require two complete stable association and protected-semantic proofs;
5. require the native login, active Claude processes, and other accounts to
   remain unchanged;
6. require only the target to gain managed subscription authority; and
7. require a separate later Enter before any different-account selection.

Association is not selection. Gate B does not authorize switching the live
Claude account.

### Live Gate C: separately approved selection

Gate C runs only after the user separately names an associated source and
target and approves any detected Remote Control disruption. It selects that
target once, proves native official status and the dashboard cursor agree,
proves a new ordinary process uses it, and proves an already-running ordinary
session adopts it on the platform-specific request boundary established
above. Active process identities must remain unchanged, in-flight work is not
retargeted, and the approved target remains selected unless the user
separately authorizes another switch.

On Linux/WSL, Gate C records the provider-visible credential `mtimeMs` before
selection and requires the official login to advance it before the existing
session request. A missing advance fails the gate without a manual file touch.

Gate C excludes explicit environment authentication, another
`CLAUDE_CONFIG_DIR`, and unapproved Remote Control. It never restarts,
signals, or injects input into provider processes.

## Closed decision ledger

| Question | Final decision |
| --- | --- |
| Is the current native login broken? | No. Official status is healthy; Sidekick rejects the safe parent mode. |
| May Sidekick loosen its private storage? | No. Add a distinct provider-owned read-only operation. |
| What relates an explicitly associated Claude profile? | A capability-probed exact-profile status key from required email and org ID, stored only as an opaque digest and rejected on duplicate or schema drift. |
| May labels or organization names be used? | No. |
| What proves token continuity? | The complete protected semantic tuple across two complete observations. |
| What if status association evidence is incomplete? | Preserve state, disable switching, and show one provider advisory. |
| How is a saved row associated? | User-chosen, official login in that row's stable private profile. |
| Does association switch native Claude? | No. |
| What must a native switch prove? | The official login left both the protected request authority and separate status/profile association on the exact target. |
| Are provider CLI versions pinned? | No release target, saved-release equality, upper bound, or release allowlist. One-sided lower safety floors allow every newer release whose behavior probes pass. |
| What happens after an update? | Workers follow the stable launcher, reprobe behavior, and accept compatible new targets; Codex restarts an older managed daemon with the current CLI. |
| Does a normal command change? | No wrapper, alias, PATH shim, or shell edit. |
| What platforms ship? | Linux, WSL, macOS Arm64, and macOS x64. |
| When does an existing ordinary Claude session adopt a switch? | Linux/WSL on the next request after an observed official-login `mtimeMs` advance; macOS after Claude's healthy-Keychain cache expires within 30 seconds, or earlier after rejection. |
| What happens to an in-flight Claude request? | It is never migrated or replayed. |
| Can Remote Control survive a different-account switch? | No guarantee; require approval because Claude disconnects it by design. |
| Are account lookups serialized? | No. Cached first paint and bounded concurrent lookup remain. |
| What live work is currently authorized? | Gate A only; no association or account change. |

## Source matrix

| Source | Evidence class | Used for |
| --- | --- | --- |
| [Claude CLI reference](https://code.claude.com/docs/en/cli-usage) | Official documentation | Status JSON and exit behavior; login commands |
| [Claude environment variables](https://code.claude.com/docs/en/env-vars) | Official documentation | Exact-profile isolation and multiple accounts |
| [Claude authentication](https://code.claude.com/docs/en/authentication) | Official documentation | Credential location, mode, Keychain, precedence, provider-owned writes |
| [Claude installation](https://code.claude.com/docs/en/installation) | Official documentation | Native updates, supported Linux/WSL/macOS architectures |
| [Commit-fixed Anthropic changelog evidence](https://github.com/anthropics/claude-code/blob/7ef6eec9d9ba84ea6f233f26c45f1df5c5991843/CHANGELOG.md) | Official upstream history | Reproducible credential-refresh and Remote Control statements; not a runtime release constraint |
| [Claude `2.1.220` Linux x64 artifact](https://github.com/anthropics/claude-code/releases/download/v2.1.220/claude-linux-x64.tar.gz) | Installed/release binary | Reproducible status schema, two-store official login, and request-time credential adoption |
| [Claude `2.1.220` Darwin Arm64 package](https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-arm64/-/claude-code-darwin-arm64-2.1.220.tgz) | Release binary | Reproducible Arm64 request-time adoption, Keychain cache, and namespace observation |
| [Claude `2.1.220` Darwin x64 package](https://registry.npmjs.org/@anthropic-ai/claude-code-darwin-x64/-/claude-code-darwin-x64-2.1.220.tgz) | Release binary | Reproducible Intel request-time adoption, Keychain cache, and namespace observation |
| [Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services) | Primary platform documentation | macOS secret-storage contract |
| [Apple XNU `fcntl.h`](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/fcntl.h) | Primary OS source | `O_NOFOLLOW_ANY` |
| [Linux `open(2)`](https://man7.org/linux/man-pages/man2/open.2.html) | Primary OS documentation | Descriptor-relative and no-follow behavior |
| [Codex app-server](https://learn.chatgpt.com/docs/app-server) | Official OpenAI documentation | Per-release generated schema and current-target protocol proof |
| Installed Codex `0.145.0` and current local source | Installed binary/source | Stable launcher, generated capability, managed-daemon restart |
| Sidekick source at `0ba4f808...` | Local primary evidence | Failure chain and exact change owners |
| Focused synthetic and existing tests | Behavioral evidence | Filesystem rejection, race behavior, no-guess projection |
