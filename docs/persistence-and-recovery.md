# Persistence locations, migration, and recovery

This guide describes where Sidekick stores account state, how release 0.7.0
moves durable data to native application-data directories, and how to recover
or prepare a safe rollback to release 0.6.0.

## Location model

Sidekick owns three kinds of local data:

- `accounts.json`, the authoritative account document containing OAuth
  credentials and account status;
- private Codex auth bundles copied from a provider login; and
- the regenerable Codex lifetime-output cache.

Provider-native homes such as `~/.claude` and `~/.codex` are not Sidekick
application-data locations. Location migration never moves, deletes, or
overwrites the active provider login.

### Native locations

Release 0.7.0 uses the operating system's per-user application-data and cache
conventions:

| Platform | Account document | Private Codex root | Lifetime cache |
| --- | --- | --- | --- |
| Linux and WSL | `${XDG_DATA_HOME:-~/.local/share}/sidekick-usages/accounts.json` | `${XDG_DATA_HOME:-~/.local/share}/sidekick-usages/codex/` | `${XDG_CACHE_HOME:-~/.cache}/sidekick-usages/codex-lifetime-cache.json` |
| macOS | `~/Library/Application Support/sidekick-usages/accounts.json` | `~/Library/Application Support/sidekick-usages/codex/` | `~/Library/Caches/sidekick-usages/codex-lifetime-cache.json` |
| Windows | `%LOCALAPPDATA%\sidekick-usages\accounts.json` | `%LOCALAPPDATA%\sidekick-usages\codex\` | `%LOCALAPPDATA%\sidekick-usages\Cache\codex-lifetime-cache.json` |

Linux and WSL honor absolute `XDG_DATA_HOME` and `XDG_CACHE_HOME` values.
Relative XDG roots fail closed. WSL uses the Linux filesystem and Linux home;
it does not silently select a mounted Windows profile.

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

Installing release 0.7.0 does not silently relocate an existing store:

1. If only compatibility data exists, normal commands continue using it.
2. If all candidates are absent, the first authorized write uses the native
   location.
3. If native and compatibility authorities both exist, Sidekick proves they
   are equivalent or fails closed. It never chooses conflicting state.
4. The regenerable lifetime cache uses the native cache location and can be
   rebuilt from provider-local statistics.

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
`"schema_version": 1`. Released 0.6.0 documents remain readable. If `doctor`
reports a schema migration or prototype import requirement, run:

```bash
sidekick-usages migrate accounts
```

The command previews its exact operation and asks for confirmation. Use
`--yes` only after reviewing the preview. Immutable content-addressed backups
are published before the authority changes.

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
both locations and use the typed next command or report the redacted doctor
output.

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
the compatibility location, publishes the version-one lineage snapshot needed
for a future upgrade, commits exact generation-zero authority bytes, and runs
the pinned released 0.6.0 reader against the result. It preserves the native
authority and older compatibility evidence.

Only after that verification succeeds should the executable be downgraded:

```bash
uv tool install --force --python 3.14 \
  "git+https://github.com/Sawmonabo/sidekick-usages.git@v0.6.0"
```

If rollback preparation fails, do not downgrade. Keep the current release,
run `doctor`, and follow its reported recovery action. Upgrading again after a
prepared rollback is safe: release 0.7.0 recognizes the retained lineage and
selects the latest proven generation.

## Security boundaries

- Never attach `accounts.json`, private Codex auth files, transaction staging
  data, or full doctor output containing local paths to a public report.
- Redact labels and provider identifiers when sharing diagnostics.
- Do not run migration commands against a copied store on a network,
  removable, synthetic, or unsupported filesystem.
- Do not delete lock files, backups, receipts, or partial transaction data
  while `doctor` reports a recovery state.
