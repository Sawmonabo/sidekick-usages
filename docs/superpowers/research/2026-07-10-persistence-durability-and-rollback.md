# Persistence Durability and Rollback Research

**Date:** 2026-07-10

**Status:** GO approved by the operator on 2026-07-10

**Scope:** Versioned account-schema migration, immutable backup, local
filesystem durability, cross-process coordination, Windows permissions,
interruption recovery, reset, and latest-state rollback to v0.6.0

## Table of contents

- [Decision question](#decision-question)
- [Recommendation](#recommendation)
- [Repository evidence](#repository-evidence)
- [Platform evidence](#platform-evidence)
- [Buy-versus-adopt analysis](#buy-versus-adopt-analysis)
- [Stored-generation contract](#stored-generation-contract)
- [Commit and recovery protocol](#commit-and-recovery-protocol)
- [Rollback and re-upgrade](#rollback-and-re-upgrade)
- [Security and filesystem boundary](#security-and-filesystem-boundary)
- [Verification matrix](#verification-matrix)
- [Risks and reversal conditions](#risks-and-reversal-conditions)
- [Sources](#sources)

## Decision question

Which exact persistence protocol and dependencies can let Sidekick Usages move
its credential-bearing unversioned JSON account file to a strict versioned
schema without destroying the only trustworthy copy, silently importing stale
credentials, weakening platform permissions, or losing post-upgrade changes
when a user rolls back to the released v0.6.0 binary?

The decision must separate four properties that are often conflated:

1. strict validation before data is authorized;
2. atomic visibility of old or new complete bytes;
3. durability across application/OS interruption within qualified platform
   guarantees; and
4. recoverability when the last commit point is uncertain.

## Recommendation

The operator approved **GO for the complete contract** on 2026-07-10.
Production versioned writing remains disabled until the later implementation
passes the native and actual-v0.6.0 acceptance harnesses.

The recommended contract is:

- integer `schema_version: 1` with exactly `schema_version` and `accounts` at
  the document root;
- one strict historical generation-zero schema covering v0.1.0 through v0.6.0;
- one distinct strict prototype schema accepted only from the prototype path;
- explicit `sidekick-usages migrate accounts`, never hidden load-time
  mutation;
- content-addressed immutable generation-zero and version-one backups;
- Portalocker 3.2.0 behind a Sidekick lock adapter;
- pywin32 312 as a direct Windows-only dependency for native file and DACL
  operations;
- a Sidekick-owned, schema-aware commit/recovery adapter because no reviewed
  generic atomic writer implements the required combined semantics;
- a non-secret content-addressed prototype import receipt retained across
  reset;
- full reset removal of all Sidekick-owned account credential documents and
  backups; and
- `sidekick-usages migrate prepare-rollback --target v0.6.0`, which converts
  the latest state rather than restoring a stale pre-upgrade snapshot.

Version one preserves explicit empty heartbeat target and reset collections,
but the pinned v0.6.0 reader collapses both to `None` through `_str_list()` and
`_str_dict()`. Those valid current states therefore fail rollback preflight
with `RollbackCompatibilityError` before a snapshot or authority mutation.
Every other version-one state remains losslessly reversible. Any later
persisted field or state requires schema version two and a newly approved
reverse policy.

## Repository evidence

### Current writer and failure modes

The current `store.py` writer emits an unversioned top-level label mapping and
uses direct `write_text()` truncation. It has no lock, temporary candidate,
backup, file synchronization, directory synchronization, or compare-before-
commit check. Permission adjustment occurs after writing on Unix.

Its permissive boundary currently permits or hides states the new contract
must reject:

- missing `access_token` can fall into prototype interpretation;
- `bool("false")` becomes true;
- mixed lists/maps silently filter elements;
- malformed prototype and some I/O failures are swallowed;
- a reset can delete the Sidekick file while leaving the prototype eligible
  for another import; and
- public `upsert()` plus `save()` allows in-memory and durable mutation to
  diverge.

### Historical released shapes

All released stores are unversioned, so generation zero cannot mean only the
latest 20-field record.

| Release | Fields emitted |
|---|---|
| v0.1.0 | Required `provider_id`, `access_token`, `refresh_token`, `expires_at`, and `plan` |
| v0.2.0-v0.3.0 | Base fields plus nullable `scopes` |
| v0.4.0-v0.4.1 | Adds `provider_account_id`, three Codex-auth fields, and three refresh-diagnostic fields |
| v0.5.0-v0.6.0 | Adds seven heartbeat fields, producing the current 20-field record |

The rollback oracle is the actual v0.6.0 tag at
`6a413b2772c3c11e9ef45a78a06ab79bfc0ca44c`. Its runtime store format matches
the current unversioned shape.

### Old-binary coordination

The installed “daemon” is a scheduler that periodically launches
`sidekick-usages maintain --quiet`; it is not a resident process. Explicit
migration can require pausing/uninstalling that schedule. The released binary
does not honor a new lock, so the migration also fingerprints and revalidates
the source immediately before commit. A later reappearance of generation zero
is a named `legacy_writer_detected` state, never silently treated as a new
first migration.

## Platform evidence

### Python and POSIX

Python 3.14.6 documents `os.replace()` as destination-overwriting replacement.
The CPython source maps it to `rename()`/`renameat()` on POSIX and
`MoveFileExW(..., MOVEFILE_REPLACE_EXISTING)` on Windows.[python-os]
[cpython-posixmodule]

POSIX specifies atomic namespace behavior for rename but separately states
that directory operations are not necessarily durable. Its rationale names
the common durable update pattern: synchronize a new file, rename it, and
synchronize the directory when the new name must survive a crash.[posix-rename]
[posix-rationale]

POSIX `link()` atomically creates a new directory entry and fails if the
destination exists. It is therefore suitable for POSIX no-replace publication
of an already written and synchronized immutable backup.[posix-link]

`tempfile.mkstemp(dir=parent)` provides secure exclusive creation in the same
directory, but it does not supply final publication or durability by itself.
[python-tempfile]

### macOS

Apple documents that ordinary `fsync()` can leave bytes in a drive cache and
provides `F_FULLFSYNC` to ask the drive to flush its buffered data to permanent
storage.[apple-fsync] The macOS writer should request `F_FULLFSYNC` for account
files and backups when available and treat an I/O failure as terminal. A
native APFS gate is required; Linux behavior is not macOS evidence.

### Windows

Windows separates the needed behavior across APIs:

- `CopyFileW(source, destination, TRUE)` fails if the destination exists and
  copies source security resource properties on supported Windows;
- `ReplaceFileW` replaces an existing file while preserving its DACL and
  selected metadata;
- `MoveFileExW` supports no-replace or replace publication and a write-through
  flag, but the documentation's explicit flush guarantee is for copy-and-
  delete moves;
- `FlushFileBuffers` flushes an opened file and reports failure; and
- `ReplaceFileW` documents `REPLACEFILE_WRITE_THROUGH` as unsupported.

[windows-copyfile] [windows-replacefile] [windows-movefile]
[windows-flush]

Consequently, Python `os.replace()` alone is insufficient for the stated ACL
and durability contract. Sidekick uses the maintained pywin32 bindings,
flushes and reopens the final file, reassesses its DACL, and makes the exact
Windows namespace-hardening behavior a native gate rather than an inferred
claim.

Windows hard links cannot be the universal backup publication primitive;
Microsoft documents `CreateHardLinkW` as NTFS-only and unsupported on ReFS.
[windows-hardlink]

### Production and research evidence

SQLite's atomic-commit documentation separates journal creation, journal
flush, locking, database write, database flush, and recovery. It also documents
filesystem/hardware limits and directory synchronization. This is evidence
against treating one rename as a transaction.[sqlite-atomic]

The OSDI ALICE study found that application crash-consistency protocols depend
on subtle persistence properties that vary across filesystems. CrashMonkey and
later bounded black-box testing provide systematic methods for exploring
recovered states.[alice] [crashmonkey] [bounded-crash]

The Sidekick protocol is small enough to inject every logical interruption
checkpoint and run fresh-process assessment after each one.

## Buy-versus-adopt analysis

Repository activity/popularity was checked on 2026-07-10. Counts are
time-sensitive context, not API guarantees.

| Candidate | Evidence and fit | Disposition |
|---|---|---|
| [Portalocker 3.2.0][portalocker] | Mature/production-stable cross-platform native hard locks; active source; 326 stars and 55 forks observed. POSIX uses `fcntl.flock`; Windows uses pywin32. | **GO for locking only** |
| [filelock 3.29.7][filelock] | Very active, Python 3.14, 963 stars and 135 forks observed. Its Unix implementation can change to `SoftFileLock` on `ENOSYS`. | NO-GO for this fail-closed lock boundary |
| [pywin32 312][pywin32] | Production/stable CPython 3.14 Windows wheels and maintained bindings for file/security APIs. | **GO on Windows** |
| [python-atomicwrites 1.4.1][atomicwrites] | Repository archived, last release in 2022, project documents limited Windows support. | NO-GO |
| Portalocker `open_atomic()` | Same-directory temp and file fsync for absent destinations, but asserts target absence, assumes rename atomicity, and omits replace, directory sync, ACL, source comparison, and recovery states. | NO-GO as writer |
| New Rust/young atomic helpers | Adds native build and supply-chain surface without the schema/recovery/rollback semantics. | NO-GO |
| SQLite as the store | Excellent transactional implementation, but incompatible with the approved JSON/v0.6.0 contract and disproportionate for this bounded document. | NO-GO; exemplar only |

Portalocker already declares pywin32 on Windows, but Sidekick must declare
pywin32 directly because it independently consumes native file and DACL APIs.
Depending on an incidental transitive dependency would make the security
contract fragile.

Recommended reviewed dependencies:

```toml
portalocker == 3.2.0
pywin32 == 312 ; sys_platform == "win32"
```

Both remain private to `persistence/`. No third-party type reaches core, CLI,
provider, path, doctor, or the persistence facade.

## Stored-generation contract

### Prototype

The prototype is a top-level label mapping whose records contain exactly
`token` and `plan`. Both are required non-empty strict strings. Extras, mixed
generations, and wrong types fail. It is accepted only from
`AccountLocations.prototype_cc_usage` when no authoritative Sidekick file
exists and only through explicit migration. It is never modified or deleted.

### Generation zero

One closed record schema requires the v0.1 five-field base and permits only the
known later fields with exact historical defaults. Scalars/containers are
strict, extras forbidden, and `provider_id` is exactly `claude` or `codex`.
This avoids an ambiguous union of nearly identical historical records.

### Version one

Version one has exactly:

```json
{
  "schema_version": 1,
  "accounts": {}
}
```

Every account record emits the current 20 semantic keys in deterministic
order. Pydantic's provider discriminator requires other-provider fields to be
null while retaining the uniform reverse-compatible record shape.

`expires_at`, refresh/heartbeat audit times, and heartbeat reset times are null
or canonical UTC strings:

```text
YYYY-MM-DDTHH:MM:SS.ffffffZ
```

Claude expiry must have exact millisecond precision and Codex expiry exact
second precision so the reverse integer conversion is lossless.
`codex_last_refresh` remains a bounded provider-native string.

Supported refresh status values are null, `ok`, `skipped`, or `failed`.
Supported persisted heartbeat values are the current closed heartbeat
vocabulary. Adding a persisted value later requires a schema/version review.

### JSON lexical rules

- UTF-8 without BOM;
- maximum 16 MiB document, checked before unbounded read;
- object root;
- duplicate keys rejected at every level;
- `NaN` and infinities rejected;
- maximum 512 accounts;
- exact, non-normalized labels: non-empty, no control/NUL characters, maximum
  512 encoded bytes;
- token values non-empty and maximum 256 KiB;
- diagnostic strings maximum 4 KiB;
- named finite bounds for lists/maps; and
- no raw input or validation-library error in public output.

Root dispatch treats a strict integer `schema_version` marker as an envelope;
unknown integers are future schemas and never fall back. An object-valued
generation-zero account label literally named `schema_version` remains
eligible for generation-zero validation. Prototype dispatch is never attempted
at the authoritative path.

Serialization uses UTF-8, `ensure_ascii=False`, two-space indentation, LF,
one trailing newline, fixed envelope/field order, and preserved account
insertion order.

## Commit and recovery protocol

For authoritative path `<accounts>`:

| Artifact | Name | Secret-bearing | Lifecycle |
|---|---|---:|---|
| Lock | `<accounts>.lock` | No | Persistent sidecar; not migration evidence |
| Generation-zero backup | `<accounts>.v0.<sha256>.bak` | Yes | Immutable until full reset |
| Version-one rollback snapshot | `<accounts>.v1.<sha256>.bak` | Yes | Immutable until full reset |
| Prototype receipt | `<accounts>.prototype.<sha256>.receipt` | No | Immutable; retained across reset |
| Temporary | `.<accounts>.<purpose>.<random>.tmp` | Possibly | Never authoritative; cleanup under lock only |

Digest is lowercase SHA-256 over exact bytes. Backup equivalence is exact byte
equality plus a matching digest-derived name, not semantic similarity.

### Immutable backup publication

1. acquire the persistence lock;
2. open final source no-follow and require a regular protected single-link
   object;
3. bounded-read, validate, and fingerprint exact source bytes;
4. if the digest-derived backup exists, require exact protected equality;
5. otherwise create a private same-directory temporary copy;
6. write/copy, flush, synchronize, reopen, and digest-verify it;
7. publish through an atomic no-replace operation;
8. synchronize the namespace through the qualified platform action; and
9. reopen and revalidate the final backup.

POSIX uses atomic `link()` publication. Windows uses a security-preserving
`CopyFileW` temporary and no-replace `MoveFileExW`. A conflict is never removed
or overwritten automatically.

### Authoritative commit

1. validate the complete target in memory;
2. serialize deterministic bytes;
3. create a private same-directory temporary file;
4. write, flush, synchronize, reopen, and validate it;
5. verify the source identity and digest are unchanged;
6. replace through the qualified platform primitive;
7. synchronize the namespace;
8. reopen final state and verify exact bytes, current schema, identity, and
   permissions; and
9. only then report success.

A failure after replacement but before confirmed hardening is
`durability_uncertain`. Sidekick reassesses observed artifacts and never
performs a blind reverse write.

### Artifact-derived restart states

- valid generation zero without a matching backup: migration required;
- valid generation zero with an exact matching backup: safe to resume;
- valid version one with required historical backup: migration complete;
- valid version one imported from an intact prototype with receipt: import
  complete;
- generation zero plus a retained v1 snapshot: explicit rollback-prepared
  state, with current generation zero authoritative;
- only owned temporaries plus a valid authority: clean/resume under lock;
- malformed, unreadable, future, unsafe, or conflicting authority: fail closed;
- matching receipt with no authoritative file: empty after reset; do not
  reimport; and
- later generation-zero reappearance after migration: legacy writer detected.

No malformed state becomes empty and no historical backup silently becomes
current authority.

### Explicit surfaces

Schema migration is owned only by `persistence/migrations.py` and never hidden
inside `AccountStore.load()`.

```text
sidekick-usages migrate accounts [--yes] [--reimport-prototype]
sidekick-usages migrate prepare-rollback --target v0.6.0 [--yes]
```

Normal commands encountering generation zero or an eligible prototype return a
stable migration-required action. Help and version remain composition-free.
Doctor performs read-only assessment without constructing the account store.

Both mutating commands acquire the lock, require the Sidekick scheduler to be
stopped, show only safe generation/count/path/backup information, and require
interactive confirmation unless `--yes` is explicit. Reimport is never
automatic.

## Rollback and re-upgrade

Rollback preparation first preserves exact current version-one bytes, then
converts every field to the strict v0.6.0 shape:

- labels and insertion order remain;
- canonical Claude/Codex expiry becomes exact milliseconds/seconds;
- canonical timestamps become accepted UTC `Z` strings;
- enums become current strings; and
- all credential, scope, path, plan, map, and diagnostic values remain.

Before publishing the snapshot, the pure reverse transform rejects an
explicit empty `heartbeat_targets` or `heartbeat_window_resets`. Commit
`6a413b2772c3c11e9ef45a78a06ab79bfc0ca44c` proves that the released reader
returns `result or None` for both helpers, so no generation-zero spelling can
preserve those distinctions. The command reports manual action and leaves
version one untouched; it never coerces valid current state while claiming a
lossless downgrade.

The command commits generation zero and then runs the actual isolated v0.6.0
reader against it. The original pre-upgrade backup is never substituted for
newer post-upgrade state.

If v0.6.0 later changes that generation-zero file, re-upgrade treats the
current file as authority, creates a new digest-derived v0 backup, and produces
a new version one. Retained v1 snapshots are historical and never silently
preferred.

After conditional native relocation, rollback preparation also materializes
the latest reverse document at the compatibility location and invokes the
injected `PrivateAuthMigrator` to validate/copy Sidekick-owned Codex bundles
before account paths commit. External/provider-native homes are never
rewritten.

## Security and filesystem boundary

### POSIX/macOS/WSL

- new Sidekick state directory: `0o700`;
- credential file, backup, temporary, and lock: `0o600` from creation;
- final object: regular, no-follow, link count one;
- unsafe existing permissions are assessed before secrets are parsed;
- explicit migration may narrow permissions, ordinary read does not silently
  mutate; and
- directory metadata is synchronized after publish, replace, cleanup, and
  credential deletion.

### Windows

Use pywin32 to evaluate owner/SID/DACL state and native file operations. The
approved DACL permits the current user, LocalSystem, and Administrators and
rejects a null DACL or broad credential read/write for Everyone, Anonymous,
Guests, Authenticated Users, or Builtin Users. Reparse-point final objects fail
closed. The native test runs as a non-administrator.

Enterprise inheritance that cannot be safely classified produces repair
guidance; Sidekick does not strip it silently.

### Supported storage

Initial support is limited to qualified local filesystems on native Linux,
macOS, Windows, and WSL's Linux filesystem. Remote/network shares,
cross-device paths, WSL Windows-mounted paths, unsupported lock primitives,
unassessable ACLs, and unavailable synchronization behavior fail with a typed
`unsupported_filesystem`/permission state.

“Atomic where supported” is not sufficient: support must be explicitly
classified and observable in doctor output.

### Reset

Full reset under the lock removes the authoritative document, every valid
Sidekick-managed v0/v1 account backup, secret-bearing owned temporaries, and
eventually all referenced Sidekick-owned private Codex bundles. It retains the
non-secret lock and import receipts and never deletes the external prototype.

If any credential artifact cannot be removed, reset returns
`reset_incomplete` and does not claim that all accounts were deleted.
Provider-scoped reset cannot delete shared historical backups safely; only
full reset or an explicit future prune workflow destroys those copies.

## Verification matrix

The smallest load-bearing coverage is:

1. one table-driven lexical/schema suite for prototype, all historical shapes,
   exact version one, duplicates, constants, extras, bounds, and future schema;
2. one pure forward/reverse suite with exact deterministic bytes;
3. one parameterized interruption suite over every backup/temp/replace/sync
   checkpoint, followed by fresh-process assessment and resume;
4. one real two-process lock/source-change suite, including a simulated
   non-participating old writer;
5. one native permission/link/replace/flush suite per supported platform;
6. one reset/prototype-receipt suite proving deleted credentials do not
   reappear and partial deletion is not success;
7. one actual-v0.6.0 repeated upgrade/downgrade harness with changes made on
   both sides; and
8. one human/JSON doctor secrecy table for every assessment state.

Named interruption checkpoints include source validation; backup temporary
creation/write/sync/publication/namespace sync; output temporary
creation/write/sync; source revalidation; replacement return; namespace sync;
and final reopen/revalidation.

Native evidence uses the exact built wheel and final lock and records OS,
filesystem, Python, dependency versions, and wheel hash. Required environments
are local Linux, macOS/APFS, Windows/NTFS as a normal user, and WSL2 on its
Linux filesystem. A Linux hosted runner is not evidence for the other systems.

## Risks and reversal conditions

- No application can prove arbitrary drive firmware honors flush requests;
  claims remain scoped to the qualified OS/filesystem interfaces.
- Microsoft does not provide a simple documented Python-equivalent parent-
  directory synchronization sequence. The exact Windows namespace-hardening
  action remains a native implementation gate.
- Portalocker documentation renders an older release label in places, so the
  decision is grounded in the tagged 3.2.0 source and metadata. Python 3.14
  behavior must be proven in the matrix.
- pywin32 is a broad binary Windows dependency. Reverse the decision if exact
  CPython 3.14 wheels, normal-user behavior, or packaging cannot pass.
- Old binaries ignore the new advisory lock. Scheduler quiescence,
  source-change detection, immutable backups, and `legacy_writer_detected`
  bound the risk; they cannot prevent an arbitrary external editor.
- CS-13 must preserve the exact persisted and reverse invariants. A material
  runtime-model mismatch reopens CS-10 rather than silently changing version
  one.
- Reverse Portalocker/pywin32 adoption and keep the versioned writer disabled
  if maintenance, security, packaging, platform behavior, or artifact size
  becomes unacceptable.

## Sources

[python-os]: https://docs.python.org/3.14/library/os.html#os.replace
[python-tempfile]: https://docs.python.org/3.14/library/tempfile.html#tempfile.mkstemp
[cpython-posixmodule]: https://github.com/python/cpython/blob/v3.14.6/Modules/posixmodule.c#L5898-L5969
[posix-rename]: https://pubs.opengroup.org/onlinepubs/9799919799/functions/rename.html
[posix-rationale]: https://pubs.opengroup.org/onlinepubs/9799919799/xrat/V4_xbd_chap01.html
[posix-link]: https://pubs.opengroup.org/onlinepubs/009696699/functions/link.html
[apple-fsync]: https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html
[windows-copyfile]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-copyfilew
[windows-replacefile]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew
[windows-movefile]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
[windows-flush]: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
[windows-hardlink]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-createhardlinkw
[sqlite-atomic]: https://www.sqlite.org/atomiccommit.html
[alice]: https://www.usenix.org/conference/osdi14/technical-sessions/presentation/pillai
[crashmonkey]: https://www.usenix.org/conference/hotstorage17/program/presentation/martinez
[bounded-crash]: https://www.usenix.org/conference/osdi18/presentation/mohan
[portalocker]: https://github.com/wolph/portalocker/tree/v3.2.0
[filelock]: https://github.com/tox-dev/filelock/tree/3.29.7
[pywin32]: https://github.com/mhammond/pywin32/tree/b312
[atomicwrites]: https://github.com/untitaker/python-atomicwrites
