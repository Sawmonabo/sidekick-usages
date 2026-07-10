# Native Platform CI Findings

**Date:** 2026-07-10  
**Scope:** CS-14 native filesystem qualification and test collection  
**Evidence commit:** `56cc657052302e770547e25933205de216fc1893`  
**Evidence runs:** GitHub Actions `29090469212`, `29090699204`, and
`29090940639`

## Observed failures

The first full native matrix passed Linux, pre-commit, and the exact released
v0.6.0 harness, but failed at the macOS and Windows pytest boundaries.

The macOS 26 ARM runner rejected every real temporary directory as an
unsupported filesystem. The adapter asked `/usr/bin/stat` to classify
`/dev/fd/<descriptor>`. Apple documents `fdesc` as the filesystem that
implements `/dev/fd`, so that pseudo-path does not establish the filesystem
of the directory held by the descriptor. The resulting rejection also
prevented the adversarial deletion tests from reaching their injected races;
those secondary assertions were consequences of the qualification failure.

The Windows runner failed during collection because the platform-neutral test
module imported the Darwin adapter eagerly. That adapter imports the Unix-only
`fcntl` module. Windows-native tests therefore never reached the pywin32
boundary.

## First correction and second native finding

No dependency was added. Python 3.14 exposes macOS `fcntl.F_GETPATH`
specifically to obtain a path from an open descriptor. The first correction
used that operation to replace `/dev/fd` with an identity-checked actual path.
Run `29090699204` proved that this was still incorrect: unlike GNU `stat`, the
macOS `stat -f` option selects a file-metadata format. `%T` does not report the
mounted filesystem type. Real APFS qualification therefore still failed.

The final adapter calls Darwin `fstatfs64` on the already-held descriptor and
reads the bounded `f_fstypename` field from Apple's documented `statfs64`
layout. It:

1. holds the already security-validated directory descriptor;
2. invokes descriptor-relative `fstatfs64` without reconstructing a path;
3. bounds the native report through the fixed system structure;
4. decodes only the ASCII filesystem type name; and
5. accepts only the exact `apfs` result.

This makes the held descriptor the direct authority and fails closed on a
missing native symbol, native error, or invalid type name. The small stdlib
`ctypes` structure is copied directly from Apple's documented `statfs64`
contract and is isolated to the Darwin adapter. A general system-introspection
dependency would add a path/mount matching layer and substantially more
maintenance for one descriptor-native call.

The native test module now imports the Darwin adapter only on Unix and skips
the macOS-specific contract on Windows. A Darwin-only test exercises real APFS
qualification so future macOS runners validate the native path rather than
only a mocked classification result.

Runs `29090940639` and `29091775536` proved that real descriptor-relative APFS
qualification works. They also exposed a portable directory-disposition
assumption. Linux reports zero links for an unlinked directory that remains
open through a file descriptor; APFS can retain a link count of one before and
after successful removal. Apple documents directory link counts as
filesystem-dependent and documents `rmdir` as removing the named directory
entry.

The parent is an already validated private directory, so the portable proof
captures its complete stable child-name and device/inode set immediately
before removal. After `rmdir`, the implementation requires that exact set
minus only the intended basename and separately proves that the held victim
descriptor retains its prevalidated identity. An injected rename/replacement
adds or changes a parent entry and therefore fails closed without depending on
filesystem-specific link-count transitions.

## Windows-native qualification findings

The third matrix reached the Windows implementation and identified independent
native-contract defects rather than application failures.

First, Windows file-attribute constants belong to Python's `stat` module.
Using the pywin32 `win32file` namespace for
`FILE_ATTRIBUTE_REPARSE_POINT` and `FILE_ATTRIBUTE_DIRECTORY` failed because
that module does not export those constants. The adapter now uses the stdlib
constants. It also uses `win32api.GetFileAttributes`, whose pywin32 contract
raises a typed Windows error for a missing path. The previous
`GetFileAttributesW` wrapper returned the `INVALID_FILE_ATTRIBUTES` sentinel
as an integer in the exercised runtime, causing an absent child to be treated
as an unsafe existing object.

Second, a native Windows 3.14 and pywin32 312 reproduction proved that
`CopyFileW` produced a destination whose inherited security descriptor did not
meet Sidekick's exact protected-DACL contract. Immutable copies now read and
revalidate the held source descriptor, create the destination through the
existing private-file primitive, and then reprove source membership. This
reuses the security boundary instead of repairing a weaker copied object.

Third, the pywin32 `ReplaceFile` wrapper reverses its two native path
conversions in the current upstream source even though its generated Python
documentation presents the Win32 order. A native reproduction consequently
removed the intended authority and left the old bytes at the candidate name.
The adapter no longer depends on this wrapper. It uses `MoveFileExW` with
`MOVEFILE_REPLACE_EXISTING` for an existing destination and always uses
`MOVEFILE_WRITE_THROUGH`. Both files are already proven to reside in the same
qualified parent, and the candidate already has the exact private DACL. The
held candidate identity is then reproved under the final basename.

Run `29091775536` then exposed two Windows-only test and lock boundaries. A
pytest temporary directory has the runner's ordinary inherited DACL, not the
exact Sidekick protected DACL. Store fixtures now explicitly apply the same
released-layout permission repair that production exposes before claiming to
represent a valid Sidekick state. This keeps the test setup honest instead of
weakening production validation.

The persistent lock sidecar is empty, but its exclusive byte-range lock can
extend beyond the current end of file. Windows rejects an overlapping read
through a second handle even in the locking process. Passive inventory still
opens and validates the sidecar's exact handle, metadata, DACL, and stable
zero-byte size, but it no longer issues a data read when the first stable
metadata snapshot proves size zero. The second metadata snapshot remains
mandatory. Read handles also share existing write access so they can inspect
the already-open sidecar; byte-range locking remains the concurrency owner.

No new package is justified. pywin32 remains the maintained Windows binding
already required by the project, while the corrected operations reuse its
working APIs and the repository's qualified private-file primitive. An
additional atomic-write package would not strengthen handle identity, DACL
validation, or multi-artifact recovery.

## Primary sources

- [Apple `statfs(2)` and `fstatfs(2)` manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/statfs.2.html)
  documents descriptor-relative filesystem statistics and the filesystem type
  name field.
- [Python 3.14 `fcntl` documentation](https://docs.python.org/3.14/library/fcntl.html)
  documents macOS `F_GETPATH`, which informed and then bounded the rejected
  first correction.
- [Darwin `fstab(5)` manual](https://manp.gs/mac/5/fstab)
  identifies APFS as the default macOS filesystem and `fdesc` as the
  implementation behind `/dev/fd`.
- [Apple `rmdir(2)` manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/rmdir.2.html)
  defines successful removal of the named directory entry.
- [Apple `getattrlist(2)` manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/getattrlist.2.html)
  documents filesystem-dependent directory link-count behavior.
- [Python 3.14 `stat` documentation](https://docs.python.org/3.14/library/stat.html)
  defines the Windows file-attribute constants used with
  `st_file_attributes`.
- [Microsoft `MoveFileExW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-movefileexw)
  defines replacement and write-through flags and their same-volume behavior.
- [Microsoft `ReplaceFileW` documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)
  establishes the native replaced-file and replacement-file parameter order.
- [pywin32 `ReplaceFile` wrapper source](https://github.com/mhammond/pywin32/blob/main/win32/src/win32file.i#L3915-L3942)
  shows the binding's current argument conversions and native call.
- [Microsoft byte-range locking guidance](https://learn.microsoft.com/en-us/windows/win32/fileio/locking-and-unlocking-byte-ranges-in-files)
  documents that locks may extend beyond end of file and that overlapping
  access through a second handle fails.

## Verification requirement

The correction is not complete from one host's evidence. A new pushed matrix
must pass real macOS APFS qualification, the Windows pywin32/DACL suite, Linux,
the WSL harness, and both Homebrew source builds. Any subsequent native failure
remains a release blocker and must be recorded here before the CS-14 native
gate is closed.
