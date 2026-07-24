"""Provider-produced credential-stage hardening on POSIX."""

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.platform.contracts import (
    NativeFailureKind,
    NativePlatform,
)
from sidekick_usages.persistence.platform.macos.acl import has_extended_acl
from sidekick_usages.persistence.platform.posix.adapter import (
    _close_descriptor_stack,
    _no_follow_flag,
)
from sidekick_usages.persistence.platform.posix.private.tree import (
    _entry_metadata,
    _Identity,
    _list_names,
    _metadata,
    _native_error,
    _open_repair_child_directory,
    _open_repair_relative_directory,
    _open_repair_tree,
    _OpenedTree,
    _owned_descriptor,
    _RelativePath,
    _repair_directory_permissions,
    _RepairDirectory,
    _require_exact_entry,
    _require_root_identity,
    _scan_tree,
    _synchronize_namespace,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


@dataclass(frozen=True, slots=True)
class _StageEntry:
    relative: _RelativePath
    identity: _Identity
    directory: bool
    mode: int


def _validate_stage_mode(mode: int, private_mode: int) -> None:
    """Allow normalization only when no foreign principal can write."""
    if mode & 0o022 or (sys.platform == "darwin" and mode != private_mode):
        raise _native_error(NativeFailureKind.UNSAFE)


def _require_no_macos_acl(descriptor: int) -> None:
    """Reject provider output with a macOS extended ACL."""
    if sys.platform != "darwin":
        return
    try:
        if has_extended_acl(descriptor):
            raise _native_error(NativeFailureKind.UNSAFE)
    except OSError:
        raise _native_error(NativeFailureKind.UNSAFE) from None


def _open_stage_regular(
    parent_descriptor: int,
    basename: str,
    root_device: int,
    expected: _Identity,
) -> int:
    """Open one stable provider file whose mode may be read-broad."""
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | _no_follow_flag()
    try:
        descriptor = os.open(
            basename,
            flags,
            dir_fd=parent_descriptor,
        )
    except FileNotFoundError:
        raise _native_error(NativeFailureKind.CHANGED) from None
    except OSError:
        raise _native_error(NativeFailureKind.UNSAFE) from None
    try:
        metadata = _metadata(descriptor, NativeFailureKind.UNREADABLE)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != root_device
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        _require_no_macos_acl(descriptor)
        _validate_stage_mode(mode, _PRIVATE_FILE_MODE)
        if (
            metadata.st_dev,
            metadata.st_ino,
        ) != expected or _require_exact_entry(
            parent_descriptor, basename
        ) != expected:
            raise _native_error(NativeFailureKind.CHANGED)
    except BaseException as error:
        _close_descriptor_stack([descriptor], error)
    return descriptor


def _scan_stage_tree(
    opened: _OpenedTree,
) -> tuple[tuple[_StageEntry, ...], dict[_RelativePath, _Identity]]:
    """Preflight one isolated provider tree before changing any mode."""
    _require_root_identity(opened)
    root_metadata = _metadata(
        opened.root_descriptor,
        NativeFailureKind.UNREADABLE,
    )
    root_mode = stat.S_IMODE(root_metadata.st_mode)
    if (
        root_metadata.st_uid != os.geteuid()
        or root_metadata.st_dev != opened.root_device
    ):
        raise _native_error(NativeFailureKind.UNSAFE)
    _require_no_macos_acl(opened.root_descriptor)
    _validate_stage_mode(root_mode, _PRIVATE_DIRECTORY_MODE)
    identities: dict[_RelativePath, _Identity] = {(): opened.root_identity}
    entries = [_StageEntry((), opened.root_identity, True, root_mode)]
    pending: list[_RelativePath] = [()]
    while pending:
        relative = pending.pop()
        descriptor = _open_repair_relative_directory(
            opened,
            relative,
            identities,
        )
        with _owned_descriptor(
            descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            for basename in _list_names(descriptor):
                identity, metadata = _entry_metadata(descriptor, basename)
                child_relative = (*relative, basename)
                if stat.S_ISDIR(metadata.st_mode):
                    child = _open_repair_child_directory(
                        descriptor,
                        basename,
                    )
                    with _owned_descriptor(
                        child,
                        NativeFailureKind.UNREADABLE,
                    ):
                        child_metadata = _metadata(
                            child,
                            NativeFailureKind.UNREADABLE,
                        )
                        if (
                            child_metadata.st_dev,
                            child_metadata.st_ino,
                        ) != identity:
                            raise _native_error(NativeFailureKind.CHANGED)
                        _require_no_macos_acl(child)
                        mode = stat.S_IMODE(child_metadata.st_mode)
                        _validate_stage_mode(mode, _PRIVATE_DIRECTORY_MODE)
                    identities[child_relative] = identity
                    entries.append(
                        _StageEntry(child_relative, identity, True, mode)
                    )
                    pending.append(child_relative)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise _native_error(NativeFailureKind.UNSAFE)
                file_descriptor = _open_stage_regular(
                    descriptor,
                    basename,
                    opened.root_device,
                    identity,
                )
                with _owned_descriptor(
                    file_descriptor,
                    NativeFailureKind.UNREADABLE,
                ):
                    mode = stat.S_IMODE(
                        _metadata(
                            file_descriptor,
                            NativeFailureKind.UNREADABLE,
                        ).st_mode
                    )
                entries.append(
                    _StageEntry(child_relative, identity, False, mode)
                )
    _require_root_identity(opened)
    return tuple(entries), identities


def _repair_stage_file(
    opened: _OpenedTree,
    entry: _StageEntry,
    identities: dict[_RelativePath, _Identity],
) -> bool:
    """Set one preflight-proven provider file to owner-only access."""
    parent = _open_repair_relative_directory(
        opened,
        entry.relative[:-1],
        identities,
    )
    with _owned_descriptor(parent, NativeFailureKind.HARDEN):
        basename = entry.relative[-1]
        descriptor = _open_stage_regular(
            parent,
            basename,
            opened.root_device,
            entry.identity,
        )
        with _owned_descriptor(descriptor, NativeFailureKind.HARDEN):
            metadata = _metadata(descriptor, NativeFailureKind.CHANGED)
            if stat.S_IMODE(metadata.st_mode) != entry.mode:
                raise _native_error(NativeFailureKind.CHANGED)
            if entry.mode == _PRIVATE_FILE_MODE:
                return False
            try:
                os.fchmod(descriptor, _PRIVATE_FILE_MODE)
                os.fsync(descriptor)
            except OSError:
                raise _native_error(NativeFailureKind.HARDEN) from None
            hardened = _metadata(descriptor, NativeFailureKind.HARDEN)
            if (
                (hardened.st_dev, hardened.st_ino) != entry.identity
                or hardened.st_uid != os.geteuid()
                or hardened.st_nlink != 1
                or stat.S_IMODE(hardened.st_mode) != _PRIVATE_FILE_MODE
                or _require_exact_entry(parent, basename) != entry.identity
            ):
                raise _native_error(NativeFailureKind.HARDEN)
        _synchronize_namespace(parent)
        return True


def harden_provider_stage(
    root: Path,
    qualifier: NativePlatform,
) -> tuple[int, int]:
    """Normalize only a preflight-safe provider-produced subtree."""
    qualifier.qualify(root)
    with _open_repair_tree(root) as opened:
        if opened is None:
            return (0, 0)
        entries, identities = _scan_stage_tree(opened)
        repaired_directories = 0
        repaired_files = 0
        for entry in sorted(
            entries,
            key=lambda candidate: (
                len(candidate.relative),
                candidate.relative,
            ),
            reverse=True,
        ):
            if entry.directory:
                repaired_directories += _repair_directory_permissions(
                    opened,
                    _RepairDirectory(
                        entry.relative,
                        entry.identity,
                        entry.mode,
                    ),
                    identities,
                )
            else:
                repaired_files += _repair_stage_file(
                    opened,
                    entry,
                    identities,
                )
        _scan_tree(opened)
        return (repaired_directories, repaired_files)


__all__ = ["harden_provider_stage"]
