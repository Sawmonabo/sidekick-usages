"""Private credential-tree adapter for Windows."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.platform.contracts import (
    NativeFailureKind,
    NativeFilesystemError,
)
from sidekick_usages.persistence.platform.windows.private.tree import (
    Identity as _Identity,
)
from sidekick_usages.persistence.platform.windows.private.tree import (
    OpenedTree as _OpenedTree,
)
from sidekick_usages.persistence.platform.windows.private.tree import (
    RelativePath as _RelativePath,
)


@dataclass(frozen=True, slots=True)
class _RepairEntry:
    relative: _RelativePath
    identity: _Identity
    directory: bool
    security_valid: bool


if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32con
    import win32file

    from sidekick_usages.persistence.platform.windows.adapter import (
        WindowsPlatform,
        _open_directory,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        close_descriptor as _close_descriptor,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        close_handle as _close_handle,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        descriptor_from_handle as _descriptor_from_handle,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        metadata as _metadata,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        owned_descriptor as _owned_descriptor,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        validate_stat as _validate_stat,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        child_path as _child_path,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        path_attributes as _path_attributes,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        require_exact_entry as _require_exact_entry,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        validate_membership as _validate_membership,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        delete_empty_tree as _delete_empty_tree,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        delete_entry as _delete_entry,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        list_names as _list_names,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        open_tree as _open_tree,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        require_root_identity as _require_root_identity,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        scan_direct_tree as _scan_direct_tree,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        scan_tree as _scan_tree,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        repair_security as _repair_security,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        validate_external_private_source_file as _validate_external_file,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        validate_external_source_directory as _validate_external_directory,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        validate_repair_owner as _validate_repair_owner,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        validate_security as _validate_security,
    )

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

    def _close_descriptors(
        descriptors: list[int],
        primary: BaseException | None = None,
    ) -> None:
        failure = primary
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError:
                if failure is None:
                    failure = _native_error(NativeFailureKind.UNSAFE)
                else:
                    failure.add_note("Native descriptor cleanup also failed.")
        if failure is not None:
            raise failure from None

    def _duplicate_descriptor(
        descriptor: int,
        kind: NativeFailureKind,
    ) -> int:
        try:
            return os.dup(descriptor)
        except OSError:
            raise _native_error(kind) from None

    def _descriptor_handle(
        descriptor: int,
        kind: NativeFailureKind,
    ) -> int:
        try:
            return msvcrt.get_osfhandle(descriptor)
        except OSError:
            raise _native_error(kind) from None

    def _open_repair_child(
        parent: Path,
        basename: str,
        parent_descriptor: int,
        *,
        directory: bool,
    ) -> int:
        """Open an owner-owned child for exact DACL repair."""
        if not _require_exact_entry(parent, basename):
            raise _native_error(NativeFailureKind.CHANGED)
        attributes = _path_attributes(_child_path(parent, basename))
        if attributes is None or (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
            is not directory
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        flags = win32file.FILE_FLAG_OPEN_REPARSE_POINT
        os_flags = os.O_RDONLY | os.O_BINARY
        if directory:
            flags |= win32file.FILE_FLAG_BACKUP_SEMANTICS
        else:
            flags |= win32file.FILE_ATTRIBUTE_NORMAL
        try:
            handle = win32file.CreateFile(
                str(_child_path(parent, basename)),
                win32file.GENERIC_READ
                | win32con.READ_CONTROL
                | win32con.WRITE_DAC,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                flags,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.UNSAFE) from None
        try:
            _validate_repair_owner(int(handle))
            _validate_membership(parent_descriptor, int(handle), basename)
        except BaseException as error:
            _close_handle(handle, error)
        descriptor = _descriptor_from_handle(handle, os_flags)
        try:
            metadata = _metadata(descriptor, NativeFailureKind.UNSAFE)
            parent_metadata = _metadata(
                parent_descriptor,
                NativeFailureKind.UNSAFE,
            )
            _validate_stat(
                metadata,
                directory=directory,
                directory_device=(
                    None if directory else parent_metadata.st_dev
                ),
                allow_interrupted_link=False,
            )
            if metadata.st_dev != parent_metadata.st_dev:
                raise _native_error(NativeFailureKind.UNSAFE)
        except BaseException as error:
            _close_descriptor(descriptor, error)
        return descriptor

    @contextmanager
    def _open_repair_tree(root: Path) -> Iterator[_OpenedTree | None]:
        """Open a current-user-owned tree before exact DACL repair."""
        if _path_attributes(root.parent) is None:
            yield None
            return
        parent_descriptor = _open_directory(root.parent, private=True)
        with _owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            if not _require_exact_entry(root.parent, root.name):
                yield None
                return
            root_descriptor = _open_repair_child(
                root.parent,
                root.name,
                parent_descriptor,
                directory=True,
            )
            with _owned_descriptor(
                root_descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                metadata = _metadata(
                    root_descriptor,
                    NativeFailureKind.UNREADABLE,
                )
                yield _OpenedTree(
                    root,
                    parent_descriptor,
                    root_descriptor,
                    (metadata.st_dev, metadata.st_ino),
                    metadata.st_dev,
                    root.name,
                )

    def _open_repair_relative_directory(
        opened: _OpenedTree,
        relative: _RelativePath,
        identities: dict[_RelativePath, _Identity],
    ) -> tuple[Path, int]:
        """Reopen an owner-owned directory chain by exact identity."""
        try:
            current_descriptor = os.dup(opened.root_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        descriptors = [current_descriptor]
        current_path = opened.root_path
        traversed: _RelativePath = ()
        try:
            for component in relative:
                traversed = (*traversed, component)
                child = _open_repair_child(
                    current_path,
                    component,
                    current_descriptor,
                    directory=True,
                )
                descriptors.append(child)
                metadata = _metadata(child, NativeFailureKind.UNREADABLE)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != identities[traversed]:
                    raise _native_error(NativeFailureKind.CHANGED)
                current_descriptor = child
                current_path /= component
        except BaseException as error:
            _close_descriptors(descriptors, error)
        final = descriptors.pop()
        _close_descriptors(descriptors)
        return current_path, final

    def _security_is_valid(descriptor: int, *, directory: bool) -> bool:
        try:
            handle = msvcrt.get_osfhandle(descriptor)
            _validate_security(handle, directory=directory)
        except OSError, NativeFilesystemError:
            return False
        return True

    def _scan_repair_tree(
        opened: _OpenedTree,
    ) -> tuple[
        tuple[_RepairEntry, ...],
        dict[_RelativePath, _Identity],
    ]:
        """Preflight every owner-owned object before changing any DACL."""
        _require_root_identity(opened)
        identities: dict[_RelativePath, _Identity] = {(): opened.root_identity}
        root_security_valid = _security_is_valid(
            opened.root_descriptor,
            directory=True,
        )
        if not root_security_valid:
            _validate_external_directory(
                _descriptor_handle(
                    opened.root_descriptor,
                    NativeFailureKind.UNSAFE,
                )
            )
        entries = [
            _RepairEntry(
                (),
                opened.root_identity,
                True,
                root_security_valid,
            )
        ]
        pending: list[_RelativePath] = [()]
        while pending:
            relative = pending.pop()
            path, descriptor = _open_repair_relative_directory(
                opened,
                relative,
                identities,
            )
            with _owned_descriptor(
                descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                for basename in _list_names(path):
                    attributes = _path_attributes(_child_path(path, basename))
                    if attributes is None or (
                        attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        raise _native_error(NativeFailureKind.UNSAFE)
                    directory = bool(
                        attributes & stat.FILE_ATTRIBUTE_DIRECTORY
                    )
                    child = _open_repair_child(
                        path,
                        basename,
                        descriptor,
                        directory=directory,
                    )
                    with _owned_descriptor(
                        child,
                        NativeFailureKind.UNREADABLE,
                    ):
                        metadata = _metadata(
                            child,
                            NativeFailureKind.UNREADABLE,
                        )
                        identity = (metadata.st_dev, metadata.st_ino)
                        security_valid = _security_is_valid(
                            child,
                            directory=directory,
                        )
                        if not security_valid:
                            handle = _descriptor_handle(
                                child,
                                NativeFailureKind.UNSAFE,
                            )
                            if directory:
                                _validate_external_directory(handle)
                            else:
                                _validate_external_file(handle)
                    child_relative = (*relative, basename)
                    entries.append(
                        _RepairEntry(
                            child_relative,
                            identity,
                            directory,
                            security_valid,
                        )
                    )
                    if directory:
                        identities[child_relative] = identity
                        pending.append(child_relative)
        _require_root_identity(opened)
        return tuple(entries), identities

    def _repair_entry_permissions(
        opened: _OpenedTree,
        entry: _RepairEntry,
        identities: dict[_RelativePath, _Identity],
    ) -> bool:
        """Install one exact DACL while its preflight identity is held."""
        parent_to_close: int | None = None
        try:
            if not entry.relative:
                descriptor = _duplicate_descriptor(
                    opened.root_descriptor,
                    NativeFailureKind.UNREADABLE,
                )
                parent_descriptor = opened.parent_descriptor
                basename = opened.root_basename
            else:
                parent, parent_descriptor = _open_repair_relative_directory(
                    opened,
                    entry.relative[:-1],
                    identities,
                )
                parent_to_close = parent_descriptor
                descriptor = _open_repair_child(
                    parent,
                    entry.relative[-1],
                    parent_descriptor,
                    directory=entry.directory,
                )
                basename = entry.relative[-1]
            with _owned_descriptor(
                descriptor,
                NativeFailureKind.HARDEN,
            ):
                metadata = _metadata(
                    descriptor,
                    NativeFailureKind.CHANGED,
                )
                if (metadata.st_dev, metadata.st_ino) != entry.identity:
                    raise _native_error(NativeFailureKind.CHANGED)
                handle = _descriptor_handle(
                    descriptor,
                    NativeFailureKind.UNSAFE,
                )
                _validate_membership(
                    parent_descriptor,
                    handle,
                    basename,
                )
                _validate_repair_owner(handle)
                if not entry.security_valid:
                    _repair_security(handle, directory=entry.directory)
                else:
                    _validate_security(handle, directory=entry.directory)
                after = _metadata(
                    descriptor,
                    NativeFailureKind.HARDEN,
                )
                if (after.st_dev, after.st_ino) != entry.identity:
                    raise _native_error(NativeFailureKind.CHANGED)
                _validate_membership(
                    parent_descriptor,
                    handle,
                    basename,
                )
                repaired = not entry.security_valid
        except BaseException as error:
            if parent_to_close is not None:
                _close_descriptor(parent_to_close, error)
            raise
        if parent_to_close is not None:
            _close_descriptor(parent_to_close)
        return repaired

    class WindowsPrivateCredentialPlatform:
        """Secure recursive private-tree operations for fixed local NTFS."""

        def __init__(self) -> None:
            self._qualifier = WindowsPlatform()

        def ensure_directory(self, path: Path) -> None:
            """Create or validate one protected credential directory."""
            self._qualifier.qualify(path)
            self._qualifier.ensure_parent(path)
            self._qualifier.qualify(path)

        def repair_permissions(self, root: Path) -> tuple[int, int]:
            """Preflight and install exact DACLs without changing bytes."""
            self._qualifier.qualify(root)
            with _open_repair_tree(root) as opened:
                if opened is None:
                    return (0, 0)
                entries, identities = _scan_repair_tree(opened)
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
                    if not _repair_entry_permissions(
                        opened,
                        entry,
                        identities,
                    ):
                        continue
                    if entry.directory:
                        repaired_directories += 1
                    else:
                        repaired_files += 1
                _scan_tree(opened)
                return (repaired_directories, repaired_files)

        def harden_provider_stage(self, root: Path) -> tuple[int, int]:
            """Normalize only a preflight-safe provider-produced subtree."""
            return self.repair_permissions(root)

        def contains_artifacts(self, root: Path) -> bool:
            """Validate the complete tree and report any descendants."""
            self._qualifier.qualify(root)
            with _open_tree(root) as opened:
                if opened is None:
                    return False
                entries, _identities = _scan_tree(opened)
                return bool(entries)

        def list_directories(self, root: Path) -> tuple[str, ...]:
            """Validate the tree and list direct child directories."""
            self._qualifier.qualify(root)
            with _open_tree(root) as opened:
                if opened is None:
                    return ()
                entries, _identities = _scan_tree(opened)
                return tuple(
                    sorted(
                        entry.relative[0]
                        for entry in entries
                        if entry.directory and len(entry.relative) == 1
                    )
                )

        def list_directories_shallow(self, root: Path) -> tuple[str, ...]:
            """Validate only direct children and return directories."""
            self._qualifier.qualify(root)
            with _open_tree(root) as opened:
                if opened is None:
                    return ()
                return tuple(
                    sorted(
                        entry.relative[0]
                        for entry in _scan_direct_tree(opened)
                        if entry.directory
                    )
                )

        def list_files(self, root: Path) -> tuple[str, ...]:
            """Validate the tree and list direct child files."""
            self._qualifier.qualify(root)
            with _open_tree(root) as opened:
                if opened is None:
                    return ()
                entries, _identities = _scan_tree(opened)
                return tuple(
                    sorted(
                        entry.relative[0]
                        for entry in entries
                        if not entry.directory and len(entry.relative) == 1
                    )
                )

        def destroy_artifacts(self, root: Path) -> None:
            """Prevalidate then handle-delete every descendant bottom-up."""
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

        def destroy_tree(self, root: Path) -> None:
            """Delete one exact validated private tree including its root."""
            self.destroy_artifacts(root)
            _delete_empty_tree(root)


__all__ = ["WindowsPrivateCredentialPlatform"]
