# Persistence Contract Closure Research

- **Status:** Normative CS-14 implementation authority
- **Retrieved:** 2026-07-10
- **Scope:** Remaining schema, artifact, locking, filesystem, and native
  durability decisions required before the versioned account writer is
  enabled
- **Integrated by:**
  [architecture design](../specs/2026-07-09-maintainable-application-architecture-design.md)
  and
  [implementation plan](../plans/2026-07-09-maintainable-application-architecture.md)

## 1. Decision summary

The approved persistence direction remains sound, but the writer must not be
enabled from broad terms such as "bounded field," "qualified filesystem," or
"atomic replacement." This document closes those terms with exact values and
platform protocols.

The resulting decisions are:

- use strict, numerically bounded Pydantic persistence schemas;
- use one deterministic receipt format and one closed temporary grammar;
- use a five-second hard-lock budget over a securely opened sidecar;
- use descriptor-relative POSIX operations and a small native platform
  package;
- initially allow only proven local filesystem families;
- preserve `ReplaceFileW` for Windows replacement and DACL semantics, but do
  not claim or request its unsupported write-through flag;
- validate every native result by reopening the final authority;
- treat a current authority without a historical backup as a legitimate first
  write rather than inventing provenance;
- reject provider-incompatible generation-zero records without data loss; and
- keep the writer fail-closed until Linux, macOS, Windows, WSL, built-wheel,
  and actual-v0.6.0 gates pass.

## 2. Primary-source findings

### 2.1 POSIX and Python filesystem behavior

Python 3.14 exposes low-level descriptor-relative operations, `O_NOFOLLOW`,
macOS `O_NOFOLLOW_ANY`, `os.link()`, atomic `os.replace()`, and `os.fsync()`.
Its documentation says a successful `os.replace()` is atomic, subject to the
same-filesystem requirement. [Python 3.14 `os` documentation][python-os]

Linux documents a separate durability requirement: synchronizing a file does
not necessarily synchronize the directory entry that names it. The containing
directory descriptor must also be synchronized. [Linux `fsync(2)`][linux-fsync]
POSIX defines rename visibility atomically and describes descriptor-relative
`renameat()` as the protection against pathname replacement races.
[POSIX `rename(3p)`][posix-rename]

Apple documents that ordinary `fsync()` may stop at the drive buffer and
provides `F_FULLFSYNC` for applications that require a stronger request to
permanent storage. It is a required native call for account authorities and
credential-bearing backups on supported macOS filesystems.
[Apple `fsync(2)`][apple-fsync]

These sources support the approved sequence:

1. securely open the parent namespace;
2. create a private same-directory temporary;
3. write and synchronize the temporary;
4. reopen and validate it;
5. atomically publish or replace it;
6. synchronize the parent namespace; and
7. reopen and validate the final object.

### 2.2 Windows replacement and synchronization

Microsoft documents `ReplaceFileW` as one replacement operation that carries
selected metadata and the existing DACL to the replacement. It also states
that `REPLACEFILE_WRITE_THROUGH` is not supported.
[Microsoft `ReplaceFileW`][replace-file]

Microsoft separately documents `MoveFileExW(MOVEFILE_WRITE_THROUGH)` as not
returning until the file is moved on disk and documents
`FlushFileBuffers()` as forcing buffered file data to the device.
[Microsoft `MoveFileExW`][move-file]
[Microsoft `FlushFileBuffers`][flush-file]
Its file-caching documentation also says filesystem metadata is cached and
must be flushed or written through. [Microsoft file caching][file-caching]

The implementation therefore must not set `REPLACEFILE_WRITE_THROUGH` or
claim that `ReplaceFileW` alone proves power-loss durability. It will:

1. create the temporary with a private DACL and write-through intent;
2. write it completely and call `FlushFileBuffers`;
3. reopen and verify the temporary;
4. use `ReplaceFileW` with flags `0` for an existing authority;
5. use no-replace `MoveFileExW` for immutable publication;
6. reopen the resulting authority with no-reparse semantics;
7. call `FlushFileBuffers` on the final handle;
8. verify exact bytes, identity, regular-file state, and effective DACL; and
9. return `durability_uncertain` after any post-replacement hardening failure.

This is a documented best-effort protocol, not a fabricated Windows directory
`fsync` equivalent. Native NTFS tests remain a release gate. If the gate does
not establish the required behavior, Windows writing remains disabled and the
design decision is reopened.

### 2.3 Stable file identity

Python documents that Windows `st_ino` contains the file index when available
and may be up to 128 bits. The standard `stat_result` vocabulary also exposes
`st_dev`, `st_nlink`, file attributes, and the reparse tag.
[Python 3.14 `os` documentation][python-os]

Microsoft likewise documents a volume serial plus file index as the identity
of an open file on one computer. [Microsoft file identity][windows-identity]
The implementation uses:

- POSIX: `(st_dev, st_ino)`;
- Windows: `(st_dev, st_ino)` from the open final handle; and
- every platform: an exact SHA-256 digest over bounded bytes.

Identity detects replacement. The digest detects content change. Neither
substitutes for the other.

### 2.4 Lock behavior

Portalocker 3.2.0 defines a five-second default timeout, a 250 ms default
check interval, and a monotonic timeout generator. Its high-level `Lock`
opens the pathname before attempting the lock. [Portalocker 3.2.0 source][portalocker]

Sidekick retains the five-second budget but uses a 100 ms check interval for
responsive CLI feedback. It does not use the high-level pathname opener for
the credential lock. The filesystem adapter first creates or opens the
permanent sidecar without following the final object, verifies its security
and identity, and gives the open file object to Portalocker's low-level
exclusive non-blocking operation. A timeout becomes `store_locked`.

The lock is cooperative. Source identity and digest checks remain mandatory
because v0.6.0 and external editors do not participate.

### 2.5 Windows library and typing fit

pywin32 312 declares Python 3.14 support and publishes Windows wheels. Its own
release guidance recommends pinning because any incremental release may
contain an interface change. [pywin32 312 on PyPI][pywin32]

The typeshed project publishes the matching
`types-pywin32==312.0.0.20260609` stub package.
[types-pywin32 on PyPI][types-pywin32]
The stubs cover the required modules and APIs, but some native results remain
incomplete. Sidekick therefore uses the stubs as a development dependency and
still narrows native return objects at its adapter boundary. It never adds
`Any`, a blanket suppression, or a cast to pretend an unchecked native value
is safe.

## 3. Exact schema limits

All limits are measured after UTF-8 encoding unless explicitly described as a
count. Null does not consume a member slot, but a present value must satisfy
its bound.

| Field or collection | Exact limit |
|---|---:|
| Complete document | 16 MiB |
| Accounts | 512 entries |
| Account label | 1-512 bytes; no Unicode control characters |
| Access, refresh, or ID token | 1-262,144 bytes |
| Provider account ID | 1-4,096 bytes |
| Plan | 1-256 bytes |
| Scopes | 128 entries |
| One scope | 1-4,096 bytes |
| Codex auth-home string | 1-32,768 bytes |
| Opaque Codex last-refresh string | 1-4,096 bytes |
| Diagnostic error | 1-4,096 bytes |
| Heartbeat target list | 32 entries |
| One heartbeat target ID | 1-256 bytes |
| Heartbeat reset map | 32 entries |
| One reset-map key | 1-256 bytes |
| Historical timestamp input | 20-32 ASCII bytes |

Containers reject duplicates where the representation can express them,
mixed element types, empty present strings, and extras. `scopes=None` remains
different from a known-empty scope tuple. `heartbeat_targets=None` remains
different from an explicit empty target tuple.

The bounds are deliberately above current provider values while preventing a
single field from consuming the full document budget. A future need for a
larger value is a schema amendment, not permissive coercion.

## 4. Historical and current timestamp grammar

Generation-zero Sidekick-owned timestamps accept exactly:

```text
YYYY-MM-DDTHH:MM:SS[.f{1,6}](Z|+00:00)
```

The parser performs semantic calendar validation, rejects leap seconds,
requires UTC, and uses integer arithmetic. It accepts years 0001 through
9999. Version-one output always emits six fractional digits and `Z`.

Provider-native expiry integers use these closed ranges:

- Claude milliseconds: `0..253402300799999`;
- Codex seconds: `0..253402300799`.

Boolean is never an integer. Claude values must reverse from an aware UTC
datetime at exact millisecond precision. Codex values must reverse at exact
second precision. A value that cannot round-trip exactly is invalid rather
than truncated.

`codex_last_refresh` remains an opaque bounded provider-native string and is
not parsed by persistence.

## 5. Provider discrimination

Generation zero and version one enforce the same provider compatibility:

- Claude rejects non-null provider-account ID, Codex auth home, Codex ID
  token, and Codex last-refresh fields; and
- Codex rejects non-null Claude scopes.

Migration never silently nulls an incompatible value. That would discard
information while claiming success.

Refresh status is exactly `null`, `ok`, `skipped`, or `failed`.
Heartbeat status is exactly `null`, `warmed`, `active`, `disabled`,
`unsupported`, `failed`, or `enabled` while the released schema retains that
value. Any new persisted status requires a new stored-schema decision.

## 6. Artifact grammar

For authority basename `<accounts>`, persistence owns only:

```text
<accounts>.lock
<accounts>.v0.<64-lowercase-hex>.bak
<accounts>.v1.<64-lowercase-hex>.bak
<accounts>.prototype.<64-lowercase-hex>.receipt
.<accounts>.<purpose>.<32-lowercase-hex>.tmp
```

`purpose` is one of:

- `authority`;
- `backup`;
- `snapshot`; or
- `receipt`.

The random component contains 128 bits from `secrets.token_hex(16)`. Unknown,
partially matching, uppercase, or malformed basenames are foreign and are
never opened, cleaned, or deleted by Sidekick.

The non-secret prototype receipt is deterministic UTF-8 JSON:

```json
{
  "receipt_version": 1,
  "prototype_sha256": "<64-lowercase-hex>",
  "target_schema_version": 1
}
```

It uses two-space indentation, LF, and one trailing newline. Its filename
digest is the exact prototype digest, not a digest of the receipt bytes.

## 7. Filesystem qualification

The initial allowlist is deliberately smaller than the universe of local
filesystems:

| Host | Allowed authority filesystem |
|---|---|
| Native Linux | ext4, XFS, or Btrfs |
| macOS | APFS |
| Native Windows | NTFS |
| WSL | ext4 inside the Linux distribution |

The implementation rejects:

- NFS, SMB/CIFS, FUSE network mounts, and UNC shares;
- WSL 9p/DrvFS Windows mounts;
- tmpfs and other volatile filesystems;
- overlayfs;
- FAT, exFAT, ReFS, and Windows cluster/shared volumes;
- cross-device temporary or backup paths; and
- unknown filesystem names.

Linux and WSL resolve the longest matching entry from
`/proc/self/mountinfo`, decode its specified escapes, and compare the open
directory identity with the selected mount. macOS uses the native filesystem
report for the securely opened directory. Windows requires a local drive and
NTFS volume report with persistent ACL support.

The current WSL evidence is:

```text
/      ext4
/mnt/c 9p (aname=drvfs)
```

The implementation must accept the first and reject the second. Package
metadata or mocked platform names are not proof.

## 8. Module ownership

Three genuinely different native implementations satisfy the rule of three.
They must not be compressed into one sprawling conditional module.

```text
persistence/
├── __init__.py
├── account_store.py
├── errors.py
├── filesystem.py
├── locking.py
├── migrations.py
├── schemas.py
└── _platform/
    ├── __init__.py
    ├── macos.py
    ├── posix.py
    └── windows.py
```

`filesystem.py` is a persistence-specific facade bound to one account
location. `_platform/` owns native calls only. It does not become a general
filesystem toolkit.

`locking.py` owns the one cooperative hard-lock protocol. `migrations.py`
owns state assessment and mutation coordination. `schemas.py` owns strict
boundary DTOs and deterministic codecs. `account_store.py` accepts only
current version-one or true empty state and never migrates.

## 9. First-write provenance

A valid version-one authority without a generation-zero backup is current.
This is the expected result of the first authorized persist from an empty
installation. A matching v0 backup proves migration history; its absence does
not prove deletion or corruption.

This avoids adding a speculative provenance marker and preserves the approved
two-field version-one envelope. Recovery remains lossless because rollback
preparation snapshots current version one and performs the pure reverse
transformation even when no historical v0 backup exists.

Doctor may report whether a matching historical backup exists, but it must not
invent how a backup-less current authority was created.

## 10. Lock, permission, and scheduler policy

The persistence lock waits at most five seconds and checks every 100 ms. It
fails closed as `store_locked`; it never waits indefinitely or inherits an
unreviewed library default.

Unsafe POSIX mode bits or Windows DACLs are assessment failures. Sidekick does
not silently `chmod` or rewrite an enterprise DACL. Doctor emits bounded,
platform-specific manual guidance. Any future permission-repair command needs
its own explicit confirmation contract.

Migration checks every scheduler backend that can coexist on the host:

- native Linux: systemd user timer and cron marker;
- macOS: launchd agent and cron marker;
- Windows: Task Scheduler task;
- WSL: systemd user timer, cron marker, and Windows Task Scheduler task.

"Stopped" means no Sidekick-owned schedule is installed. A backend that
cannot be assessed blocks mutation; an idle interval is not quiescence.
Preview and confirmation occur without holding the persistence lock. The
scheduler check and complete persistence assessment are repeated under the
lock immediately before mutation.

## 11. Native acceptance gates

The writer remains disabled until all of these are green:

1. Linux runs descriptor-relative security, synchronization, replacement,
   interruption, and reopen-verification tests on an allowlisted filesystem.
2. macOS reports APFS and proves the `F_FULLFSYNC` path.
3. Windows reports NTFS and proves pywin32 installation, no-reparse opens,
   effective DACL classification, temporary creation, replacement, sharing
   failures, final flush, and reopen-verification as a normal user.
4. WSL accepts its ext4 root and rejects `/mnt/c` 9p/DrvFS.
5. The exact wheel contains every persistence module, installs the Windows
   dependency only on Windows, and includes no retired `store.py`.
6. The exact v0.6.0 commit reads the reverse document after a current-schema
   mutation, writes another mutation, and the current binary migrates that
   latest generation-zero state again.
7. The upgrade/downgrade cycle succeeds twice with deterministic bytes and
   digest-derived artifact names.

No mocked operating-system name, source-tree import, or successful unit test
substitutes for a native gate.

[apple-fsync]: https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html
[file-caching]: https://learn.microsoft.com/en-us/windows/win32/fileio/file-caching
[flush-file]: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-flushfilebuffers
[linux-fsync]: https://www.man7.org/linux/man-pages/man2/fsync.2.html
[move-file]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw
[portalocker]: https://github.com/wolph/portalocker/blob/v3.2.0/portalocker/utils.py
[posix-rename]: https://www.man7.org/linux/man-pages/man3/rename.3p.html
[python-os]: https://docs.python.org/3.14/library/os.html
[pywin32]: https://pypi.org/project/pywin32/312/
[replace-file]: https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew
[types-pywin32]: https://pypi.org/project/types-pywin32/312.0.0.20260609/
[windows-identity]: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/ns-fileapi-by_handle_file_information
