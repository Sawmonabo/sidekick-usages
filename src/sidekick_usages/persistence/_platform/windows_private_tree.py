"""Handle-qualified Windows private credential tree traversal."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
)

type Identity = tuple[int, int]
type RelativePath = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TreeEntry:
    """One identity-qualified private-tree descendant."""

    relative: RelativePath
    identity: Identity
    directory: bool


@dataclass(frozen=True, slots=True)
class OpenedTree:
    """Held descriptors and identity for one private-tree root."""

    root_path: Path
    parent_descriptor: int
    root_descriptor: int
    root_identity: Identity
    root_device: int
    root_basename: str


if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32con
    import win32file

    from sidekick_usages.persistence._platform.windows_files import (
        open_directory,
        open_existing,
    )
    from sidekick_usages.persistence._platform.windows_handles import (
        close_descriptor,
        close_handle,
        descriptor_from_handle,
        metadata,
        open_delete_target,
        owned_descriptor,
        validate_stat,
    )
    from sidekick_usages.persistence._platform.windows_namespace import (
        child_path,
        path_attributes,
        require_exact_entry,
        validate_membership,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        validate_security,
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

    def _open_child_directory(
        parent: Path,
        basename: str,
        parent_descriptor: int,
        *,
        delete: bool,
    ) -> int:
        if not require_exact_entry(parent, basename):
            raise _native_error(NativeFailureKind.CHANGED)
        access = win32file.GENERIC_READ
        if delete:
            access |= win32con.DELETE
        try:
            handle = win32file.CreateFile(
                str(child_path(parent, basename)),
                access,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_EXISTING,
                win32file.FILE_FLAG_BACKUP_SEMANTICS
                | win32file.FILE_FLAG_OPEN_REPARSE_POINT,
                None,
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.CHANGED) from None
        try:
            validate_security(int(handle), directory=True)
            validate_membership(parent_descriptor, int(handle), basename)
        except BaseException as error:
            close_handle(handle, error)
        descriptor = descriptor_from_handle(
            handle,
            os.O_RDONLY | os.O_BINARY,
        )
        try:
            child_metadata = metadata(
                descriptor,
                NativeFailureKind.UNSAFE,
            )
            parent_metadata = metadata(
                parent_descriptor,
                NativeFailureKind.UNSAFE,
            )
            validate_stat(child_metadata, directory=True)
            if child_metadata.st_dev != parent_metadata.st_dev:
                raise _native_error(NativeFailureKind.UNSAFE)
        except BaseException as error:
            close_descriptor(descriptor, error)
        return descriptor

    @contextmanager
    def open_tree(root: Path) -> Iterator[OpenedTree | None]:
        """Hold and validate one private-tree root when present."""
        if path_attributes(root.parent) is None:
            yield None
            return
        parent_descriptor = open_directory(root.parent, private=False)
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.UNREADABLE,
        ):
            if not require_exact_entry(root.parent, root.name):
                yield None
                return
            root_descriptor = _open_child_directory(
                root.parent,
                root.name,
                parent_descriptor,
                delete=False,
            )
            with owned_descriptor(
                root_descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                root_metadata = metadata(
                    root_descriptor,
                    NativeFailureKind.UNREADABLE,
                )
                identity = (root_metadata.st_dev, root_metadata.st_ino)
                yield OpenedTree(
                    root,
                    parent_descriptor,
                    root_descriptor,
                    identity,
                    root_metadata.st_dev,
                    root.name,
                )

    def require_root_identity(opened: OpenedTree) -> None:
        """Require the held root to retain its original namespace identity."""
        if not require_exact_entry(
            opened.root_path.parent,
            opened.root_basename,
        ):
            raise _native_error(NativeFailureKind.CHANGED)
        root_metadata = metadata(
            opened.root_descriptor,
            NativeFailureKind.CHANGED,
        )
        if (
            root_metadata.st_dev,
            root_metadata.st_ino,
        ) != opened.root_identity:
            raise _native_error(NativeFailureKind.CHANGED)

    def open_relative_directory(
        opened: OpenedTree,
        relative: RelativePath,
        identities: dict[RelativePath, Identity],
    ) -> tuple[Path, int]:
        """Reopen a private-tree directory chain by exact identity."""
        try:
            current_descriptor = os.dup(opened.root_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        descriptors = [current_descriptor]
        current_path = opened.root_path
        traversed: RelativePath = ()
        try:
            for component in relative:
                traversed = (*traversed, component)
                child = _open_child_directory(
                    current_path,
                    component,
                    current_descriptor,
                    delete=False,
                )
                descriptors.append(child)
                child_metadata = metadata(
                    child,
                    NativeFailureKind.UNREADABLE,
                )
                if (
                    child_metadata.st_dev,
                    child_metadata.st_ino,
                ) != identities[traversed]:
                    raise _native_error(NativeFailureKind.CHANGED)
                current_descriptor = child
                current_path /= component
        except BaseException as error:
            _close_descriptors(descriptors, error)
        final = descriptors.pop()
        _close_descriptors(descriptors)
        return current_path, final

    def list_names(path: Path) -> tuple[str, ...]:
        """List one directory deterministically or fail closed."""
        try:
            return tuple(sorted(entry.name for entry in path.iterdir()))
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None

    def scan_tree(
        opened: OpenedTree,
    ) -> tuple[tuple[TreeEntry, ...], dict[RelativePath, Identity]]:
        """Validate and inventory every private-tree descendant."""
        require_root_identity(opened)
        identities: dict[RelativePath, Identity] = {(): opened.root_identity}
        pending: list[RelativePath] = [()]
        entries: list[TreeEntry] = []
        while pending:
            relative = pending.pop()
            path, descriptor = open_relative_directory(
                opened,
                relative,
                identities,
            )
            with owned_descriptor(
                descriptor,
                NativeFailureKind.UNREADABLE,
            ):
                for basename in list_names(path):
                    if not require_exact_entry(path, basename):
                        raise _native_error(NativeFailureKind.CHANGED)
                    attributes = path_attributes(child_path(path, basename))
                    if attributes is None:
                        raise _native_error(NativeFailureKind.CHANGED)
                    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                        raise _native_error(NativeFailureKind.UNSAFE)
                    child_relative = (*relative, basename)
                    if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                        child = _open_child_directory(
                            path,
                            basename,
                            descriptor,
                            delete=False,
                        )
                        with owned_descriptor(
                            child,
                            NativeFailureKind.UNREADABLE,
                        ):
                            child_metadata = metadata(
                                child,
                                NativeFailureKind.UNREADABLE,
                            )
                            identity = (
                                child_metadata.st_dev,
                                child_metadata.st_ino,
                            )
                        identities[child_relative] = identity
                        pending.append(child_relative)
                        entries.append(
                            TreeEntry(child_relative, identity, True)
                        )
                        continue
                    child = open_existing(
                        path,
                        basename,
                        descriptor,
                        writable=False,
                    )
                    with owned_descriptor(
                        child,
                        NativeFailureKind.UNREADABLE,
                    ):
                        child_metadata = metadata(
                            child,
                            NativeFailureKind.UNREADABLE,
                        )
                        validate_stat(
                            child_metadata,
                            directory=False,
                            directory_device=opened.root_device,
                            allow_interrupted_link=False,
                        )
                        identity = (
                            child_metadata.st_dev,
                            child_metadata.st_ino,
                        )
                    entries.append(TreeEntry(child_relative, identity, False))
        require_root_identity(opened)
        return tuple(entries), identities

    def _mark_for_deletion(descriptor: int) -> None:
        try:
            handle = msvcrt.get_osfhandle(descriptor)
            win32file.SetFileInformationByHandle(
                handle,
                win32file.FileDispositionInfo,
                True,
            )
        except OSError, pywintypes.error:
            raise _native_error(NativeFailureKind.REMOVE) from None

    def _delete_file(
        parent: Path,
        parent_descriptor: int,
        entry: TreeEntry,
    ) -> None:
        basename = entry.relative[-1]
        descriptor = open_delete_target(
            parent,
            basename,
            parent_descriptor,
        )
        with owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            file_metadata = metadata(
                descriptor,
                NativeFailureKind.REMOVE,
            )
            if (
                file_metadata.st_dev,
                file_metadata.st_ino,
            ) != entry.identity:
                raise _native_error(NativeFailureKind.CHANGED)
            _mark_for_deletion(descriptor)

    def _delete_directory(
        parent: Path,
        parent_descriptor: int,
        entry: TreeEntry,
    ) -> None:
        basename = entry.relative[-1]
        descriptor = _open_child_directory(
            parent,
            basename,
            parent_descriptor,
            delete=True,
        )
        with owned_descriptor(descriptor, NativeFailureKind.REMOVE):
            directory_metadata = metadata(
                descriptor,
                NativeFailureKind.REMOVE,
            )
            if (
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            ) != entry.identity or list_names(child_path(parent, basename)):
                raise _native_error(NativeFailureKind.CHANGED)
            _mark_for_deletion(descriptor)

    def delete_entry(
        opened: OpenedTree,
        entry: TreeEntry,
        identities: dict[RelativePath, Identity],
    ) -> None:
        """Delete one exact prevalidated entry through its held identity."""
        require_root_identity(opened)
        parent, parent_descriptor = open_relative_directory(
            opened,
            entry.relative[:-1],
            identities,
        )
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.REMOVE,
        ):
            basename = entry.relative[-1]
            if not require_exact_entry(parent, basename):
                raise _native_error(NativeFailureKind.CHANGED)
            if entry.directory:
                _delete_directory(parent, parent_descriptor, entry)
            else:
                _delete_file(parent, parent_descriptor, entry)
            if require_exact_entry(parent, basename):
                raise _native_error(NativeFailureKind.CHANGED)


__all__ = [
    "Identity",
    "OpenedTree",
    "RelativePath",
    "TreeEntry",
    "delete_entry",
    "list_names",
    "open_relative_directory",
    "open_tree",
    "require_root_identity",
    "scan_tree",
]
