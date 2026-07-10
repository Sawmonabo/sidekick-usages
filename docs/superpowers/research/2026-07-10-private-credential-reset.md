# Private Credential Reset Boundary

**Date:** 2026-07-10  
**Status:** implementation input for the approved persistence and credential
architecture

## Question

How should full reset remove Sidekick-owned private Codex credential bundles
without coupling persistence to Codex, following links, exposing arbitrary
callbacks inside the persistence transaction, or deleting account authority
before credential destruction is proven?

## Ground truth

Python 3.14 provides descriptor-relative traversal on Unix. `os.fwalk()`
yields a directory descriptor, does not follow symlinks by default, and its
descriptors are valid only until the next iteration unless duplicated.
Descriptor-relative operations are Unix-only, so this is not a portable
Windows implementation
([Python `os` documentation](https://docs.python.org/3.14/library/os.html#os.fwalk)).

The standard library's `shutil.rmtree()` uses a symlink-attack-resistant
implementation only where descriptor-based functions are available. Its
`rmtree.avoids_symlink_attacks` attribute exposes that capability. Python also
documents that Windows no longer traverses and deletes a directory junction's
contents, but that does not provide the handle identity, DACL, and exact
absence proof required by Sidekick
([Python `shutil` documentation](https://docs.python.org/3.14/library/shutil.html#shutil.rmtree)).

On Windows, a directory handle requires `FILE_FLAG_BACKUP_SEMANTICS`, and
`FILE_FLAG_OPEN_REPARSE_POINT` makes an open target the reparse point rather
than its destination. Without the latter, an open follows a symbolic-link
target
([Microsoft `CreateFileW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew)).
Microsoft separately documents that `DeleteFileW` removes a symbolic link
rather than its target, and that deletion is normally completed when the last
handle closes
([Microsoft `DeleteFileW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-deletefilew)).

The pinned pywin32 312 source exposes the native flags, handle-opening APIs,
`FileDispositionInfo`, and `SetFileInformationByHandle` needed for
handle-qualified Windows deletion. The accepted account-artifact adapter
already exercises that exact binding, so the private-tree adapter can reuse
the same handle-disposition protocol without adding local native signatures
([pywin32 312 `win32file` source](https://github.com/mhammond/pywin32/blob/b312/win32/src/win32file.i)).

POSIX defines `S_IRWXU`/`0700` as owner read, write, and execute/search with no
group or other mode bits. `fchmod()` applies the mode to an already-open file
descriptor, which lets Sidekick retain and revalidate object identity instead
of repairing a path after a separate pathname check
([Linux `fchmod(2)` documentation](https://man7.org/linux/man-pages/man2/fchmod.2.html)).

Microsoft documents that `icacls /inheritancelevel:r` removes only inherited
access-control entries and that `/grant:r` replaces only matching explicit
grants. Other explicit allow or deny entries can remain, so a recursive
`icacls` recipe cannot establish Sidekick's exact DACL contract
([Microsoft `icacls` documentation](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/icacls)).
The supported ACL APIs can instead protect a DACL from inheritance while
discarding inherited entries, and can create explicit file-system access rules
for exact identities and propagation flags
([Microsoft `SetAccessRuleProtection` documentation](https://learn.microsoft.com/en-us/dotnet/api/system.security.accesscontrol.objectsecurity.setaccessruleprotection?view=netframework-4.8.1),
[Microsoft `FileSystemAccessRule` documentation](https://learn.microsoft.com/en-us/dotnet/api/system.security.accesscontrol.filesystemaccessrule?view=net-10.0)).
Microsoft's well-known SID vocabulary identifies Local System and built-in
Administrators independently of localized account names
([Microsoft `WellKnownSidType` documentation](https://learn.microsoft.com/en-us/dotnet/api/system.security.principal.wellknownsidtype?view=net-10.0)).

## Adopt versus build

No new dependency is justified.

- Reuse the already adopted pywin32 boundary for Windows handle, reparse,
  identity, and DACL validation.
- Reuse Python's descriptor-relative primitives on POSIX, including macOS.
- Do not rely on an unconditional `shutil.rmtree()` because its documented
  attack resistance is platform-dependent and it does not produce Sidekick's
  typed identity or absence proof.
- Do not add a generic filesystem-cleanup library. A library that recursively
  deletes paths but does not implement Sidekick's closed ownership, security,
  and postcondition contract would move code without removing the risky work.
- Do not prescribe recursive `chmod`, `icacls`, or `Set-Acl` as recovery.
  Those tools do not retain Sidekick's open-handle identity, reparse, hard-link,
  volume, race, and exact postcondition proofs across the complete tree.

## Architectural decision

Define one narrow provider-neutral credential-artifact port at the persistence
coordinator boundary:

```python
class PrivateCredentialArtifacts(Protocol):
    def observe(self) -> OrphanedPrivateCredentials: ...
    def destroy_all(self) -> None: ...
```

The application injects one concrete, stateful adapter. It is not a per-call
callable, and persistence never imports Codex. The adapter is bound to the
Sidekick-owned private root and owns native traversal, security validation,
deletion, namespace hardening where the platform supports it, and an exact
post-delete rescan.

Full reset uses this order:

1. Perform scheduler and passive persistence assessment before prompting.
2. Acquire the persistence lock.
3. Repeat scheduler and complete passive assessment under the lock.
4. Invoke `destroy_all()` under the held lock.
5. Immediately require `observe()` to report `ABSENT`.
6. Delete managed secret backups and temporaries.
7. Delete the exact account authority last.
8. Reassess the complete state before reporting success.

Any private-artifact deletion or verification failure becomes the closed
`reset_incomplete` operation result. The account authority remains intact when
the private phase fails. A partial private deletion may be reported only as
incomplete; it is never success.

All Sidekick-owned private credential writes and deletes must participate in
the same persistence lock once the credential service owns them. Otherwise a
concurrent participating writer could recreate a bundle between absence proof
and authority deletion.

## Released-layout permission repair

The exact released v0.6 writer created the Sidekick application root and
private Codex root with the process default directory mode, while protecting
the account file, per-account directories, and credential files separately.
A normal `022` umask therefore leaves both roots at `0755`. Strict passive
assessment must not silently change that state, but refusing it without a
bounded recovery command would strand a valid released installation.

Sidekick therefore owns an explicit, confirmed
`sidekick-usages permissions repair` operation. `doctor` remains read-only and
reports this structured next command only for unsafe permission state. The
operation preserves every credential byte and uses the same native validation
vocabulary as ordinary persistence:

1. Preflight the application root as an exact current-user-owned,
   non-symlink/non-reparse directory on an approved local filesystem. Reject
   group/other-writable roots.
2. Hold the exact root descriptor/handle, apply only the approved `0700` mode
   or protected DACL, and verify the same identity and resulting security.
3. Acquire and prove the normal persistent account lock immediately after the
   bootstrap repair. Do not introduce a weaker second lock protocol.
4. Revalidate the account parent under the lock.
5. Prevalidate the complete private credential tree, repair only exact held
   directories/files, and strictly rescan it.
6. Reassess account and private state before reporting success.

On Windows the exact protected DACL contains only full-control allow entries
for the current user SID, Local System SID, and built-in Administrators SID;
directory rules carry container/object inheritance and file rules do not.
Repair uses the adopted pywin32 handle/security boundary, not a subprocess or
localized principal name.

Fresh private bundle writes use the same native boundary to create protected
directories and files. Both writes and repairs participate in the account
persistence lock so a full reset cannot prove absence and then race a bundle
recreation.

## Required adapter behavior

The concrete adapter must:

- bind to one absolute Sidekick-owned private root;
- treat an absent or safely empty root as `ABSENT`;
- reject a root or descendant that is a symlink, junction, reparse point,
  non-directory/non-regular object, cross-device object, unsafe owner or
  permission state, unsafe DACL, or unassessable object;
- require single-link regular credential files so reset cannot claim to erase
  a secret that remains through a hard link;
- traverse bottom-up without following descendants;
- validate identity again at deletion and surface races as typed failure;
- keep the exact victim descriptor/handle open across deletion and prove its
  link or disposition state changed as required, so a replacement name cannot
  be deleted while the original credential survives elsewhere;
- harden each successfully changed namespace where the native platform has a
  qualified primitive; and
- rescan the bound root before returning.

The adapter should remove all Sidekick-owned bundles in the bound private root,
not only paths still referenced by an authority. That lets explicit full reset
recover from an interrupted authority deletion or a previously orphaned
bundle while preserving the external provider login and prototype account
input.

## Tests that carry the decision

The few required tests are:

- successful credential-first, authority-last ordering;
- private deletion failure leaves authority untouched and reports
  `reset_incomplete`;
- a post-delete `PRESENT` observation blocks authority deletion;
- absent authority with orphaned private credentials can be explicitly reset;
- symlink/junction, hard-link, unsafe-permission/DACL, unsupported object, and
  identity-swap cases fail closed; and
- a successful reset retains the lock, prototype receipt, and external
  prototype while proving the private root empty.
