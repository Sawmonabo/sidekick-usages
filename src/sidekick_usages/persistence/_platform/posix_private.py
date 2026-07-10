"""Descriptor-relative POSIX private credential tree operations."""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
    NativePlatform,
)
from sidekick_usages.persistence._platform.posix import (
    _close_descriptor_stack,
    _no_follow_flag,
    _open_child_directory,
    _open_directory,
    _owned_descriptor,
    _path_metadata,
    _require_exact_entry,
    _validate_file,
)

type _Identity = tuple[int, int]
type _RelativePath = tuple[str, ...]

_PRIVATE_DIRECTORY_MODE = 0o700


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    relative: _RelativePath
    identity: _Identity
    directory: bool


@dataclass(frozen=True, slots=True)
class _OpenedTree:
    parent_descriptor: int
    root_descriptor: int
    root_identity: _Identity
    root_device: int
    root_basename: str


@dataclass(frozen=True, slots=True)
class _RepairDirectory:
    relative: _RelativePath
    identity: _Identity
    mode: int


def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
    return NativeFilesystemError(kind)


def _metadata(descriptor: int, kind: NativeFailureKind) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError:
        raise _native_error(kind) from None


def _validate_directory(
    metadata: os.stat_result,
    root_device: int,
) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != root_device
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise _native_error(NativeFailureKind.UNSAFE)


def _open_repair_child_directory(
    parent_descriptor: int,
    basename: str,
) -> int:
    """Open one owner-owned directory without accepting a path race."""
    expected = _require_exact_entry(parent_descriptor, basename)
    if expected is None:
        raise _native_error(NativeFailureKind.CHANGED)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | _no_follow_flag()
    try:
        descriptor = os.open(basename, flags, dir_fd=parent_descriptor)
    except OSError:
        raise _native_error(NativeFailureKind.UNSAFE) from None
    try:
        metadata = _metadata(descriptor, NativeFailureKind.UNREADABLE)
        parent_metadata = _metadata(
            parent_descriptor,
            NativeFailureKind.UNREADABLE,
        )
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_dev != parent_metadata.st_dev
            or (metadata.st_dev, metadata.st_ino) != expected
            or _require_exact_entry(parent_descriptor, basename) != expected
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
    except BaseException as error:
        _close_descriptor_stack([descriptor], error)
    return descriptor


@contextmanager
def _open_tree(root: Path) -> Iterator[_OpenedTree | None]:
    parent_metadata = _path_metadata(root.parent)
    if parent_metadata is None:
        yield None
        return
    parent_descriptor = _open_directory(root.parent, private=False)
    with _owned_descriptor(
        parent_descriptor,
        NativeFailureKind.UNREADABLE,
    ):
        identity = _require_exact_entry(parent_descriptor, root.name)
        if identity is None:
            yield None
            return
        root_descriptor = _open_child_directory(
            parent_descriptor,
            root.name,
            private=True,
        )
        with _owned_descriptor(
            root_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            metadata = _metadata(
                root_descriptor,
                NativeFailureKind.UNREADABLE,
            )
            if identity != (metadata.st_dev, metadata.st_ino):
                raise _native_error(NativeFailureKind.CHANGED)
            yield _OpenedTree(
                parent_descriptor,
                root_descriptor,
                identity,
                metadata.st_dev,
                root.name,
            )


@contextmanager
def _open_repair_tree(root: Path) -> Iterator[_OpenedTree | None]:
    """Open a tree whose directories may have the released broad mode."""
    parent_metadata = _path_metadata(root.parent)
    if parent_metadata is None:
        yield None
        return
    parent_descriptor = _open_directory(root.parent, private=True)
    with _owned_descriptor(
        parent_descriptor,
        NativeFailureKind.UNREADABLE,
    ):
        identity = _require_exact_entry(parent_descriptor, root.name)
        if identity is None:
            yield None
            return
        root_descriptor = _open_repair_child_directory(
            parent_descriptor,
            root.name,
        )
        with _owned_descriptor(
            root_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            metadata = _metadata(
                root_descriptor,
                NativeFailureKind.UNREADABLE,
            )
            if identity != (metadata.st_dev, metadata.st_ino):
                raise _native_error(NativeFailureKind.CHANGED)
            yield _OpenedTree(
                parent_descriptor,
                root_descriptor,
                identity,
                metadata.st_dev,
                root.name,
            )


def _require_root_identity(opened: _OpenedTree) -> None:
    if (
        _require_exact_entry(
            opened.parent_descriptor,
            opened.root_basename,
        )
        != opened.root_identity
    ):
        raise _native_error(NativeFailureKind.CHANGED)
    metadata = _metadata(
        opened.root_descriptor,
        NativeFailureKind.CHANGED,
    )
    if (metadata.st_dev, metadata.st_ino) != opened.root_identity:
        raise _native_error(NativeFailureKind.CHANGED)


def _open_relative_directory(
    opened: _OpenedTree,
    relative: _RelativePath,
    identities: dict[_RelativePath, _Identity],
) -> int:
    try:
        current = os.dup(opened.root_descriptor)
    except OSError:
        raise _native_error(NativeFailureKind.UNREADABLE) from None
    descriptors = [current]
    traversed: _RelativePath = ()
    try:
        for component in relative:
            traversed = (*traversed, component)
            child = _open_child_directory(
                current,
                component,
                private=True,
            )
            descriptors.append(child)
            metadata = _metadata(child, NativeFailureKind.UNREADABLE)
            if (
                metadata.st_dev,
                metadata.st_ino,
            ) != identities[traversed]:
                raise _native_error(NativeFailureKind.CHANGED)
            current = child
    except BaseException as error:
        _close_descriptor_stack(descriptors, error)
    final = descriptors.pop()
    _close_descriptor_stack(descriptors)
    return final


def _open_repair_relative_directory(
    opened: _OpenedTree,
    relative: _RelativePath,
    identities: dict[_RelativePath, _Identity],
) -> int:
    """Reopen an owner-owned directory chain by preflight identity."""
    try:
        current = os.dup(opened.root_descriptor)
    except OSError:
        raise _native_error(NativeFailureKind.UNREADABLE) from None
    descriptors = [current]
    traversed: _RelativePath = ()
    try:
        for component in relative:
            traversed = (*traversed, component)
            child = _open_repair_child_directory(current, component)
            descriptors.append(child)
            metadata = _metadata(child, NativeFailureKind.UNREADABLE)
            if (metadata.st_dev, metadata.st_ino) != identities[traversed]:
                raise _native_error(NativeFailureKind.CHANGED)
            current = child
    except BaseException as error:
        _close_descriptor_stack(descriptors, error)
    final = descriptors.pop()
    _close_descriptor_stack(descriptors)
    return final


def _list_names(descriptor: int) -> tuple[str, ...]:
    try:
        return tuple(sorted(os.listdir(descriptor)))
    except OSError:
        raise _native_error(NativeFailureKind.UNREADABLE) from None


def _entry_metadata(
    descriptor: int,
    basename: str,
) -> tuple[_Identity, os.stat_result]:
    """Return stable no-follow metadata for one exact child name."""
    identity = _require_exact_entry(descriptor, basename)
    if identity is None:
        raise _native_error(NativeFailureKind.CHANGED)
    try:
        metadata = os.stat(
            basename,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        raise _native_error(NativeFailureKind.CHANGED) from None
    except OSError:
        raise _native_error(NativeFailureKind.UNSAFE) from None
    if identity != (metadata.st_dev, metadata.st_ino):
        raise _native_error(NativeFailureKind.CHANGED)
    return identity, metadata


def _open_regular(
    parent_descriptor: int,
    basename: str,
    root_device: int,
    expected: _Identity,
) -> int:
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
        _validate_file(
            metadata,
            root_device,
            allow_interrupted_link=False,
        )
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


def _scan_tree(
    opened: _OpenedTree,
) -> tuple[tuple[_TreeEntry, ...], dict[_RelativePath, _Identity]]:
    _require_root_identity(opened)
    identities: dict[_RelativePath, _Identity] = {(): opened.root_identity}
    pending: list[_RelativePath] = [()]
    entries: list[_TreeEntry] = []
    while pending:
        relative = pending.pop()
        descriptor = _open_relative_directory(opened, relative, identities)
        with _owned_descriptor(
            descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            for basename in _list_names(descriptor):
                identity, metadata = _entry_metadata(descriptor, basename)
                child_relative = (*relative, basename)
                if stat.S_ISDIR(metadata.st_mode):
                    _validate_directory(metadata, opened.root_device)
                    child = _open_child_directory(
                        descriptor,
                        basename,
                        private=True,
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
                    identities[child_relative] = identity
                    pending.append(child_relative)
                    entries.append(_TreeEntry(child_relative, identity, True))
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise _native_error(NativeFailureKind.UNSAFE)
                file_descriptor = _open_regular(
                    descriptor,
                    basename,
                    opened.root_device,
                    identity,
                )
                with _owned_descriptor(
                    file_descriptor,
                    NativeFailureKind.UNREADABLE,
                ):
                    pass
                entries.append(_TreeEntry(child_relative, identity, False))
    _require_root_identity(opened)
    return tuple(entries), identities


def _scan_repair_tree(
    opened: _OpenedTree,
) -> tuple[
    tuple[_RepairDirectory, ...],
    dict[_RelativePath, _Identity],
]:
    """Preflight every object before any security metadata changes."""
    _require_root_identity(opened)
    root_metadata = _metadata(
        opened.root_descriptor,
        NativeFailureKind.UNREADABLE,
    )
    if (
        root_metadata.st_uid != os.geteuid()
        or root_metadata.st_dev != opened.root_device
    ):
        raise _native_error(NativeFailureKind.UNSAFE)
    identities: dict[_RelativePath, _Identity] = {(): opened.root_identity}
    directories = [
        _RepairDirectory(
            (),
            opened.root_identity,
            stat.S_IMODE(root_metadata.st_mode),
        )
    ]
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
                        mode = stat.S_IMODE(child_metadata.st_mode)
                    identities[child_relative] = identity
                    directories.append(
                        _RepairDirectory(child_relative, identity, mode)
                    )
                    pending.append(child_relative)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise _native_error(NativeFailureKind.UNSAFE)
                file_descriptor = _open_regular(
                    descriptor,
                    basename,
                    opened.root_device,
                    identity,
                )
                with _owned_descriptor(
                    file_descriptor,
                    NativeFailureKind.UNREADABLE,
                ):
                    pass
    _require_root_identity(opened)
    return tuple(directories), identities


def _require_repair_directory_identity(
    opened: _OpenedTree,
    directory: _RepairDirectory,
    identities: dict[_RelativePath, _Identity],
) -> None:
    """Require a repaired descriptor to remain at its preflight name."""
    if not directory.relative:
        _require_root_identity(opened)
        return
    parent = _open_repair_relative_directory(
        opened,
        directory.relative[:-1],
        identities,
    )
    with _owned_descriptor(parent, NativeFailureKind.CHANGED):
        if (
            _require_exact_entry(parent, directory.relative[-1])
            != directory.identity
        ):
            raise _native_error(NativeFailureKind.CHANGED)


def _repair_directory_permissions(
    opened: _OpenedTree,
    directory: _RepairDirectory,
    identities: dict[_RelativePath, _Identity],
) -> bool:
    """Set one proven directory to the exact owner-only mode."""
    descriptor = _open_repair_relative_directory(
        opened,
        directory.relative,
        identities,
    )
    with _owned_descriptor(descriptor, NativeFailureKind.HARDEN):
        metadata = _metadata(descriptor, NativeFailureKind.CHANGED)
        if (
            (metadata.st_dev, metadata.st_ino) != directory.identity
            or metadata.st_uid != os.geteuid()
            or metadata.st_dev != opened.root_device
            or stat.S_IMODE(metadata.st_mode) != directory.mode
        ):
            raise _native_error(NativeFailureKind.CHANGED)
        _require_repair_directory_identity(opened, directory, identities)
        if stat.S_IMODE(metadata.st_mode) == _PRIVATE_DIRECTORY_MODE:
            return False
        try:
            os.fchmod(descriptor, _PRIVATE_DIRECTORY_MODE)
        except OSError:
            raise _native_error(NativeFailureKind.HARDEN) from None
        hardened = _metadata(descriptor, NativeFailureKind.HARDEN)
        if (
            (hardened.st_dev, hardened.st_ino) != directory.identity
            or hardened.st_uid != os.geteuid()
            or stat.S_IMODE(hardened.st_mode) != _PRIVATE_DIRECTORY_MODE
        ):
            raise _native_error(NativeFailureKind.HARDEN)
        _require_repair_directory_identity(opened, directory, identities)
        _synchronize_namespace(descriptor)
        return True


def _synchronize_namespace(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        raise _native_error(NativeFailureKind.SYNCHRONIZE) from None


def _delete_file(
    opened: _OpenedTree,
    parent_descriptor: int,
    entry: _TreeEntry,
) -> None:
    basename = entry.relative[-1]
    descriptor = _open_regular(
        parent_descriptor,
        basename,
        opened.root_device,
        entry.identity,
    )
    with _owned_descriptor(descriptor, NativeFailureKind.REMOVE):
        try:
            os.unlink(basename, dir_fd=parent_descriptor)
        except FileNotFoundError:
            raise _native_error(NativeFailureKind.CHANGED) from None
        except OSError:
            raise _native_error(NativeFailureKind.REMOVE) from None
        if _metadata(descriptor, NativeFailureKind.REMOVE).st_nlink != 0:
            raise _native_error(NativeFailureKind.CHANGED)
    _synchronize_namespace(parent_descriptor)


def _delete_directory(
    opened: _OpenedTree,
    parent_descriptor: int,
    entry: _TreeEntry,
) -> None:
    basename = entry.relative[-1]
    descriptor = _open_child_directory(
        parent_descriptor,
        basename,
        private=True,
    )
    with _owned_descriptor(descriptor, NativeFailureKind.REMOVE):
        metadata = _metadata(descriptor, NativeFailureKind.REMOVE)
        if (metadata.st_dev, metadata.st_ino) != entry.identity or _list_names(
            descriptor
        ):
            raise _native_error(NativeFailureKind.CHANGED)
        before_links = metadata.st_nlink
        try:
            os.rmdir(basename, dir_fd=parent_descriptor)
        except FileNotFoundError:
            raise _native_error(NativeFailureKind.CHANGED) from None
        except OSError:
            raise _native_error(NativeFailureKind.REMOVE) from None
        if (
            _metadata(descriptor, NativeFailureKind.REMOVE).st_nlink
            >= before_links
        ):
            raise _native_error(NativeFailureKind.CHANGED)
    _synchronize_namespace(parent_descriptor)


def _delete_entry(
    opened: _OpenedTree,
    entry: _TreeEntry,
    identities: dict[_RelativePath, _Identity],
) -> None:
    _require_root_identity(opened)
    parent_relative = entry.relative[:-1]
    parent_descriptor = _open_relative_directory(
        opened,
        parent_relative,
        identities,
    )
    with _owned_descriptor(parent_descriptor, NativeFailureKind.REMOVE):
        observed = _require_exact_entry(
            parent_descriptor,
            entry.relative[-1],
        )
        if observed != entry.identity:
            raise _native_error(NativeFailureKind.CHANGED)
        if entry.directory:
            _delete_directory(opened, parent_descriptor, entry)
        else:
            _delete_file(opened, parent_descriptor, entry)
        if (
            _require_exact_entry(
                parent_descriptor,
                entry.relative[-1],
            )
            is not None
        ):
            raise _native_error(NativeFailureKind.CHANGED)


class PosixPrivateCredentialPlatform:
    """Secure recursive private-tree operations for Linux and macOS."""

    def __init__(self, qualifier: NativePlatform) -> None:
        self._qualifier = qualifier

    def ensure_directory(self, path: Path) -> None:
        """Create or validate one owner-only credential directory."""
        self._qualifier.qualify(path)
        self._qualifier.ensure_parent(path)
        self._qualifier.qualify(path)

    def repair_permissions(self, root: Path) -> tuple[int, int]:
        """Repair only preflight-safe owner-owned directory modes."""
        self._qualifier.qualify(root)
        with _open_repair_tree(root) as opened:
            if opened is None:
                return (0, 0)
            directories, identities = _scan_repair_tree(opened)
            repaired = sum(
                _repair_directory_permissions(opened, directory, identities)
                for directory in sorted(
                    directories,
                    key=lambda candidate: (
                        len(candidate.relative),
                        candidate.relative,
                    ),
                    reverse=True,
                )
            )
            _scan_tree(opened)
            return (repaired, 0)

    def contains_artifacts(self, root: Path) -> bool:
        """Validate the complete tree and report whether it has children."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return False
            entries, _identities = _scan_tree(opened)
            return bool(entries)

    def destroy_artifacts(self, root: Path) -> None:
        """Prevalidate then identity-delete every descendant bottom-up."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return
            entries, identities = _scan_tree(opened)
            for entry in sorted(
                entries,
                key=lambda candidate: (
                    len(candidate.relative),
                    candidate.relative,
                ),
                reverse=True,
            ):
                _delete_entry(opened, entry, identities)
            remaining, _remaining_identities = _scan_tree(opened)
            if remaining:
                raise _native_error(NativeFailureKind.CHANGED)


__all__ = ["PosixPrivateCredentialPlatform"]
