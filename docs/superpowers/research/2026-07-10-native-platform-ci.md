# Native Platform CI Findings

**Date:** 2026-07-10  
**Scope:** CS-14 native filesystem qualification and test collection  
**Evidence commit:** `56cc657052302e770547e25933205de216fc1893`  
**Evidence run:** GitHub Actions `29090469212`

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

## Verification requirement

The correction is not complete from Linux evidence. A new pushed matrix must
pass real macOS APFS qualification and then reach the Windows pywin32/DACL
tests. Any subsequent native failure remains a release blocker and must be
recorded here before the CS-14 native gate is closed.
