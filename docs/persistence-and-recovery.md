# Persistence locations, migration, and recovery

This guide describes where Sidekick stores account state, how the upcoming
0.7.0 release moves durable data to native application-data directories, and
how to recover or prepare a safe rollback to release 0.6.0.

## Location model

Sidekick owns three kinds of local data:

- `accounts.json`, the authoritative account document containing OAuth
  credentials and account status; and
- private Codex auth bundles copied from a provider login; and
- `token-activity.json`, strict last-successful Codex account activity
  snapshots containing no credentials, labels, or raw provider account IDs.

Credential refresh also uses private, short-lived transaction journals and a
single staged replacement envelope. These are recovery evidence, not account
configuration or fixtures.

Provider-native homes such as `~/.claude` and `~/.codex` are not Sidekick
application-data locations. Location migration never moves, deletes, or
overwrites the active provider login.

### Native locations

The upcoming 0.7.0 release uses the operating system's per-user
application-data conventions:

| Platform | Account document | Private Codex root | Activity snapshots |
| --- | --- | --- | --- |
| Linux and WSL | `${XDG_DATA_HOME:-~/.local/share}/sidekick-usages/accounts.json` | `${XDG_DATA_HOME:-~/.local/share}/sidekick-usages/codex/` | `${XDG_DATA_HOME:-~/.local/share}/sidekick-usages/token-activity.json` |
| macOS | `~/Library/Application Support/sidekick-usages/accounts.json` | `~/Library/Application Support/sidekick-usages/codex/` | `~/Library/Application Support/sidekick-usages/token-activity.json` |
| Windows | `%LOCALAPPDATA%\sidekick-usages\accounts.json` | `%LOCALAPPDATA%\sidekick-usages\codex\` | `%LOCALAPPDATA%\sidekick-usages\token-activity.json` |

Linux and WSL honor an absolute `XDG_DATA_HOME`. A relative data root fails
closed. WSL uses the Linux filesystem and Linux home; it does not silently
select a mounted Windows profile.

### Compatibility locations

Release 0.6.0 and earlier used these Sidekick-owned durable locations on every
platform:

```text
~/.config/sidekick-usages/accounts.json
~/.config/sidekick-usages/codex/
```

The prototype import source remains:

```text
~/.config/cc-usage/accounts.json
```

The prototype is import-only. Sidekick never merges it back after a successful
import and never deletes it automatically.

## Upgrade behavior

Upgrading to 0.7.0 will not silently relocate an existing store:

1. If only compatibility data exists, normal commands continue using it.
2. If all candidates are absent, the first authorized write uses the native
   location.
3. If native and compatibility authorities both exist, Sidekick proves they
   are equivalent or fails closed. It never chooses conflicting state.

### Obsolete derived Codex cache

Release 0.6.0 derived a Codex output-only total from local rollout files and
stored `codex-lifetime-cache.json` in the platform cache directory. The
upcoming 0.7.0 release does not read, trust, migrate, rewrite, or delete that
file. Codex token activity now comes from each saved account's authoritative
profile.

The inert file can be removed manually if it exists:

| Platform | Obsolete file |
| --- | --- |
| Linux and WSL | `${XDG_CACHE_HOME:-~/.cache}/sidekick-usages/codex-lifetime-cache.json` |
| macOS | `~/Library/Caches/sidekick-usages/codex-lifetime-cache.json` |
| Windows | `%LOCALAPPDATA%\sidekick-usages\Cache\codex-lifetime-cache.json` |

Normal usage checks remain read-only with respect to this obsolete artifact;
Sidekick does not perform hidden cleanup during a dashboard render.

### Authoritative Codex activity snapshots

`token-activity.json` is separate from credentials and from the obsolete
rollout cache. After a successful Codex account-profile request, Sidekick
stores only the authoritative lifetime total, a verified earliest activity
date when daily buckets reconcile exactly, and the fetch time. Records are
keyed by a SHA-256 digest of stable provider identity, not by account labels.

The snapshot document uses strict versioned JSON, bounded private reads,
qualified local filesystems, a cross-process hard lock, atomic replacement,
and post-write durability proof. A malformed or unsafe document fails closed
and is never silently replaced.

When a later profile request is rejected, the dashboard retains the last
successful snapshot and keeps the account authentication warning visible. An
account that has never produced a successful profile has no snapshot; Sidekick
does not replace that missing value with rollout or SQLite totals.

Inspect the current selection before making a change:

```bash
sidekick-usages doctor
sidekick-usages doctor --json
```

The JSON result includes the location code, source, destination, candidate
evidence, safe private-auth summary, and next command. It never includes raw
OAuth or private auth bytes.

## Migrate to native application data

First pause Sidekick's scheduled maintenance if it is installed:

```bash
sidekick-usages daemon status
sidekick-usages daemon uninstall
```

Then preview and confirm the migration:

```bash
sidekick-usages migrate locations
```

For reviewed non-interactive automation, pass `--yes`:

```bash
sidekick-usages migrate locations --yes
```

A conflict remains blocked by default. After independently verifying that the
compatibility location is the intended authority, explicitly replace the
conflicting canonical destination with:

```bash
sidekick-usages migrate locations --replace-conflicting-destination
```

Replacement remains blocked when a provably older credential from the
compatibility authority would replace the canonical credential for the same
provider and label. The proof requires different credentials plus timestamps
on both records, with the canonical `last_refresh_at` strictly newer. This
protects a recent rotating-token refresh from being rolled back by stale
compatibility state. Neither authority nor its recovery artifacts are changed
when this guard stops the migration.

The preview states that replacement intent before confirmation. The command
retains the displaced canonical account authority as an immutable snapshot,
keeps the compatibility source unchanged, and uses the same private-auth-first
transaction and recovery path as a normal location migration. Add `--yes` only
after reviewing that exact preview.

The command rechecks the source under both location locks, validates every
private Codex bundle through the provider-owned adapter, commits private auth
before account authority, and verifies the completed native state. A single
bounded rebase handles one source change; continuing changes fail explicitly.

Migration is idempotent. Re-running it after a completed migration reports the
already completed native selection. Compatibility account state, immutable
snapshots, and private auth are retained; the command does not automatically
delete rollback evidence.

Reinstall scheduled maintenance after a successful migration if desired:

```bash
sidekick-usages daemon install
sidekick-usages daemon status
```

## Schema migration and prototype import

The current account authority is a versioned document with
`"schema_version": 2`. Released 0.6.0 documents remain readable. If `doctor`
reports a schema migration or prototype import requirement, run:

```bash
sidekick-usages migrate accounts
```

The command previews its exact operation and asks for confirmation. Use
`--yes` only after reviewing the preview. Immutable content-addressed backups
are published before the authority changes.

## Credential refresh recovery

Every saved rotating refresh enters one provider-neutral coordinator. Work for
the same credential is serialized before provider traffic with a lock derived
from the provider and refresh credential. The secret itself is never written
to the lock name, journal, or diagnostics. After lock acquisition, Sidekick
reopens current authority and stabilizes again if the credential rotated while
a waiter was blocked.

The local transaction is:

1. write a bounded non-secret intent journal;
2. exchange with the provider exactly once;
3. strictly encode one private `replacement.json` stage containing the target
   account, an explicit plan update when returned, and a prepared Codex private
   bundle when required;
4. mark the stage complete;
5. merge only the target credential over unrelated account and heartbeat
   writes;
6. commit and prove any matching private Codex bundle; and
7. remove the private stage and journal after durability proof.

Refresh recovery runs before another refresh and during the relevant
persistence lifecycle operations. A complete stage can finish locally without
another provider request. An incomplete stage is discarded with the current
account unchanged. A newer, removed, or renamed target is never overwritten or
resurrected. Malformed, oversized, linked, permission-unsafe, or otherwise
untrusted evidence blocks mutation and remains available for diagnosis.

`doctor` reports refresh evidence as clean, recoverable, or blocked without
exposing a credential or identity. Account migration, location migration, and
rollback preparation resolve safe complete evidence first and stop on unsafe
evidence. Do not delete a journal, stage, or unknown refresh-root entry by
hand.

A full `sidekick-usages reset` acquires exclusive lifecycle ownership and
removes refresh transactions and staged credentials before deleting account
authority. It fails closed when the private namespace contains an unknown or
unsafe entry. `sidekick-usages daemon uninstall` removes scheduling only; it
does not remove accounts or refresh recovery evidence.

### Provider/local atomicity limit

No local transaction can make a provider's rotating OAuth exchange atomic
with Sidekick's filesystem. The unavoidable provider/local atomicity gap is
the interval after a provider returns replacement credentials and before the
complete private stage is durably written. A process loss in that interval may
require an explicit login recovery because another provider exchange could
invalidate the only returned credential. Sidekick minimizes this window and
recovers every state at or after a complete stage, but does not claim
distributed atomicity.

## Recovery states

Start every recovery with:

```bash
sidekick-usages doctor
sidekick-usages doctor --json
```

Follow the emitted `Next:` command. The stable location outcomes are:

- `empty`: no account authority exists;
- `prototype_only`: the import-only prototype is the only candidate;
- `compatibility_selected`: the released 0.6.0 location is authoritative;
- `canonical_selected`: native application data is authoritative;
- `equivalent_selected`: both locations are proven equivalent;
- `conflict`: authoritative locations differ without safe lineage;
- `partial`: a migration transaction needs recovery or resumption; and
- `candidate_blocked`: a candidate is malformed, unsafe, unreadable, or from
  an unsupported filesystem.

Do not repair a conflict by manually copying JSON or auth files. That bypasses
identity, permission, collision, generation, and durability checks. Preserve
both locations until you have established which authority is correct. Use
`--replace-conflicting-destination` only when compatibility is the intended
source; otherwise preserve the evidence and report the redacted doctor output.

If permissions are the only problem, preview and run:

```bash
sidekick-usages permissions repair
```

On Unix, Sidekick requires private directories and files to remain accessible
only to the current user. On Windows, it verifies the current-user DACL and
rejects unsafe namespaces, reparse points, and unsupported volumes.

## Prepare rollback to release 0.6.0

Do not install 0.6.0 over a native-only store. First run the current release's
rollback preparation while scheduled maintenance is quiescent:

```bash
sidekick-usages daemon uninstall
sidekick-usages migrate prepare-rollback --target v0.6.0
```

After reviewing the preview, confirm it or use the explicit automation form:

```bash
sidekick-usages migrate prepare-rollback --target v0.6.0 --yes
```

The command rewrites the latest canonical account and private-Codex state into
the compatibility location, publishes the current schema-version-two lineage
snapshot needed for a future upgrade, commits exact generation-zero authority
bytes, and runs the pinned released 0.6.0 reader against the result. It
preserves the native authority and older compatibility evidence.

Only after that verification succeeds should the executable be downgraded:

```bash
uv tool install --force --python 3.14 \
  "git+https://github.com/Sawmonabo/sidekick-usages.git@v0.6.0"
```

If rollback preparation fails, do not downgrade. Keep the current release,
run `doctor`, and follow its reported recovery action. After 0.7.0 is released,
upgrading again from a prepared rollback will be safe because it recognizes
the retained lineage and selects the latest proven generation.

## Security boundaries

- Never attach `accounts.json`, private Codex auth files, transaction staging
  data, or full doctor output containing local paths to a public report.
- Redact labels and provider identifiers when sharing diagnostics.
- Do not run migration commands against a copied store on a network,
  removable, synthetic, or unsupported filesystem.
- Do not delete lock files, backups, receipts, or partial transaction data
  while `doctor` reports a recovery state.
