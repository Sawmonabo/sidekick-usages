# Persistence and recovery

Sidekick has one current per-user data layout. It does not inspect, import, or
rewrite data from earlier layouts. Create and change account state through the
CLI so credential identity, permissions, and transaction checks remain intact.

## Current locations

The default data root follows the operating system through `platformdirs`:

| Platform | Default data root |
| --- | --- |
| Linux and WSL | `~/.local/share/sidekick-usages/` |
| macOS | `~/Library/Application Support/sidekick-usages/` |
| Windows | `%LOCALAPPDATA%\sidekick-usages\` |

An absolute `XDG_DATA_HOME` changes the Linux and WSL base directory. Relative
values fail closed. Runtime sockets and logs use the corresponding native
per-user runtime and log locations.

The data root contains:

| State | Purpose |
| --- | --- |
| `accounts.json` | Secret-free schema-version-three account index |
| `credentials/` | Protected provider credential authorities and private homes |
| `credential-refresh/` | Recoverable credential-refresh transactions |
| `usage-metrics.json` | Derived last-successful account usage snapshots |
| `token-activity.json` | Derived last-successful token-activity snapshots |
| `metrics-refresh.json` | Secret-free latest dashboard refresh diagnostic |
| `selected-accounts.json` | Selected account per provider |
| `activation-journals/` | Recoverable provider activation state |
| `operations/` | Durable supervisor work |
| `service-state.json` | Resident-service readiness |

Do not copy or edit these files manually. In particular, never copy a provider
login file into the Sidekick data root or copy a Sidekick authority into the
provider's native login location.

## Storage contract

`accounts.json` contains labels, plans, stable Sidekick account identifiers,
provider-safe identity metadata, health, and operational status. Access,
refresh, and identity tokens are stored only in the protected credential tree.
The index and credential authority are committed through qualified,
authority-last transactions.

Claude setup tokens and subscription logins are separate credential variants.
Codex accounts use independent private homes. A provider-qualified label is
unique, so the same display label may exist once for Claude and once for Codex
without sharing locks, journals, or credentials.

All persisted timestamps are canonical UTC. Unknown provider facts remain
unknown; malformed, unreadable, incomplete, or unsafe state fails closed.
Passive reads never change persisted state. A malformed derived
`usage-metrics.json` or `token-activity.json` remains a typed cache issue until
a nonempty, validated fresh-metrics write atomically rebuilds only that cache.
This recovery never applies to account or credential authority.

## Read-only diagnosis

Start with:

```bash
sidekick-usages doctor
sidekick-usages doctor --json
sidekick-usages daemon status
```

`doctor` reports current store, credential, refresh, heartbeat, and dashboard
metrics-refresh state without exposing credentials or raw provider identities.
Metrics diagnostics distinguish worker, cache, and account failures and retain
every bounded simultaneous cause. Provider and label filters retain global
causes plus only the requested accounts; the human view uses saved labels,
while JSON keeps stable account IDs. A malformed or unsafe authority is not
treated as an empty store.

## Account recovery

Use the Sidekick command that scopes the provider's official login to the
saved account:

```bash
# Claude subscription login
sidekick-usages refresh <claude-label>

# Claude setup token
sidekick-usages claude setup-token --label <claude-label> --force

# Codex ChatGPT login
sidekick-usages codex login <codex-label>
```

A setup-token-only Claude account requires `--replace-identity` once to approve
its first subscription association. Do not use it to bypass an established
identity mismatch.

Saved-account refresh recovery is local and transaction-aware. A complete safe
stage can finish without another provider request. Unsafe, incomplete, linked,
or concurrently changed evidence remains blocked so a newer authority is not
overwritten.

## Permission recovery

Preview the current roots with `doctor`, uninstall the resident supervisor if
it is active, then run:

```bash
sidekick-usages permissions repair
```

The command asks before changing permissions. `--yes` skips that confirmation.
It only repairs Sidekick-owned account and credential paths and verifies the
result; it does not alter provider-native login files.

## Reset and reinstall

To intentionally discard Sidekick state:

```bash
sidekick-usages reset --yes
```

Use `--provider claude` or `--provider codex` to remove only that provider's
accounts. Reset requires the resident supervisor to be absent. It verifies
official logout before retiring a managed Claude profile and deletes managed
Codex homes. Native Claude and Codex logins remain untouched.

Before replacing a release that owns a periodic Sidekick schedule, use that
still-installed release to run `sidekick-usages daemon uninstall` and verify
the old schedule is absent. Only then uninstall the old tool and install the
clean-break wheel. The current release does not inspect or retire scheduler
artifacts created by an earlier layout.

After reinstalling a clean-break release, recreate Claude accounts with `add`
or `claude setup-token`. `codex login` authenticates or repairs an existing
enrolled Codex label; it does not create one after reset. Clean Codex
enrollment belongs to the in-progress interactive account rollout. There is no
automatic or hidden conversion from an earlier Sidekick layout.

## Failure rules

- Never treat unreadable or malformed persistence as no accounts.
- Never delete journals or staging paths to force recovery.
- Never edit tokens, provider identities, timestamps, or authority references.
- Never run two manual state-changing commands while maintenance is active.
- Preserve blocked evidence and use the exact action emitted by `doctor`.
- If the provider rejects a saved login, authenticate that exact account
  through the official provider CLI before replacing the Sidekick authority.

See [token maintenance](./token-maintenance.md) for refresh behavior and
[heartbeat](./heartbeat.md) for model-request scheduling.
