"""Windows handle-qualified private tree traversal."""

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.types import NativeFailureKind

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32con
    import win32file
    import winerror

    from sidekick_usages.persistence.platform.windows.files import (
        open_directory,
        open_existing,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        close_descriptor,
        close_descriptors,
        close_handle,
        descriptor_from_handle,
        metadata,
        open_delete_target,
        owned_descriptor,
        validate_stat,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        child_path,
        path_attributes,
        require_exact_entry,
        validate_membership,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        private_security_attributes,
        validate_security,
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


@dataclass(slots=True)
class OpenedChain:
    """Held descriptors and identities for one root-relative chain."""

    paths: tuple[Path, ...]
    descriptors: list[int]
    identities: tuple[Identity, ...]
    components: RelativePath


if sys.platform == "win32":

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

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
        try:
            root_handle = msvcrt.get_osfhandle(opened.root_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.CHANGED) from None
        validate_membership(
            opened.parent_descriptor,
            root_handle,
            opened.root_basename,
        )

    def require_chain_identity(
        opened: OpenedTree,
        chain: OpenedChain,
        *,
        final_may_be_absent: bool = False,
    ) -> None:
        """Revalidate every held component, volume, and parent membership."""
        require_root_identity(opened)
        if not chain.descriptors or len(chain.descriptors) != len(
            chain.identities
        ):
            raise _native_error(NativeFailureKind.CHANGED)
        for index, descriptor in enumerate(chain.descriptors):
            value = metadata(descriptor, NativeFailureKind.CHANGED)
            identity = (value.st_dev, value.st_ino)
            if identity != chain.identities[index] or (
                value.st_dev != opened.root_device
            ):
                raise _native_error(NativeFailureKind.CHANGED)
            if index == 0:
                continue
            parent_descriptor = chain.descriptors[index - 1]
            parent = chain.paths[index - 1]
            basename = chain.components[index - 1]
            present = require_exact_entry(parent, basename)
            if final_may_be_absent and index == len(chain.descriptors) - 1:
                if present:
                    validate_membership(
                        parent_descriptor,
                        msvcrt.get_osfhandle(descriptor),
                        basename,
                    )
                continue
            if not present:
                raise _native_error(NativeFailureKind.CHANGED)
            validate_membership(
                parent_descriptor,
                msvcrt.get_osfhandle(descriptor),
                basename,
            )

    def _open_or_create_chain_child(
        opened: OpenedTree,
        parent: Path,
        parent_descriptor: int,
        component: str,
        *,
        create: bool,
    ) -> int | None:
        present = require_exact_entry(parent, component)
        if not present:
            if not create:
                return None
            try:
                win32file.CreateDirectoryW(
                    str(child_path(parent, component)),
                    private_security_attributes(directory=True),
                )
            except pywintypes.error as error:
                if error.winerror in {
                    winerror.ERROR_ALREADY_EXISTS,
                    winerror.ERROR_FILE_EXISTS,
                }:
                    raise _native_error(NativeFailureKind.CHANGED) from None
                raise _native_error(NativeFailureKind.CREATE) from None
        child = _open_child_directory(
            parent,
            component,
            parent_descriptor,
            delete=False,
        )
        value = metadata(child, NativeFailureKind.UNREADABLE)
        if value.st_dev != opened.root_device:
            close_descriptor(
                child,
                _native_error(NativeFailureKind.CHANGED),
            )
        return child

    @contextmanager
    def open_component_chain(
        opened: OpenedTree,
        relative: RelativePath,
        *,
        create: bool,
        final_may_be_absent: bool = False,
    ) -> Iterator[OpenedChain | None]:
        """Hold a handle-qualified root-relative component chain."""
        try:
            root_descriptor = os.dup(opened.root_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        descriptors = [root_descriptor]
        paths = [opened.root_path]
        identities = [opened.root_identity]
        primary: BaseException | None = None
        missing = False
        try:
            for component in relative:
                child = _open_or_create_chain_child(
                    opened,
                    paths[-1],
                    descriptors[-1],
                    component,
                    create=create,
                )
                if child is None:
                    missing = True
                    break
                value = metadata(child, NativeFailureKind.UNREADABLE)
                descriptors.append(child)
                paths.append(child_path(paths[-1], component))
                identities.append((value.st_dev, value.st_ino))
            if missing:
                yield None
            else:
                chain = OpenedChain(
                    tuple(paths),
                    descriptors,
                    tuple(identities),
                    relative,
                )
                require_chain_identity(opened, chain)
                yield chain
                require_chain_identity(
                    opened,
                    chain,
                    final_may_be_absent=final_may_be_absent,
                )
        except BaseException as error:
            primary = error
        close_descriptors(descriptors, primary)

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
            close_descriptors(descriptors, error)
        final = descriptors.pop()
        close_descriptors(descriptors)
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

    def scan_direct_tree(opened: OpenedTree) -> tuple[TreeEntry, ...]:
        """Validate the root namespace without traversing child directories."""
        require_root_identity(opened)
        entries: list[TreeEntry] = []
        for basename in list_names(opened.root_path):
            if not require_exact_entry(opened.root_path, basename):
                raise _native_error(NativeFailureKind.CHANGED)
            attributes = path_attributes(
                child_path(opened.root_path, basename)
            )
            if attributes is None:
                raise _native_error(NativeFailureKind.CHANGED)
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise _native_error(NativeFailureKind.UNSAFE)
            directory = bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)
            if directory:
                child = _open_child_directory(
                    opened.root_path,
                    basename,
                    opened.root_descriptor,
                    delete=False,
                )
            else:
                child = open_existing(
                    opened.root_path,
                    basename,
                    opened.root_descriptor,
                    writable=False,
                )
            with owned_descriptor(child, NativeFailureKind.UNREADABLE):
                child_metadata = metadata(
                    child,
                    NativeFailureKind.UNREADABLE,
                )
                validate_stat(
                    child_metadata,
                    directory=directory,
                    directory_device=(
                        None if directory else opened.root_device
                    ),
                    allow_interrupted_link=False,
                )
                if child_metadata.st_dev != opened.root_device:
                    raise _native_error(NativeFailureKind.UNSAFE)
                identity = (child_metadata.st_dev, child_metadata.st_ino)
            entries.append(TreeEntry((basename,), identity, directory))
        require_root_identity(opened)
        return tuple(entries)

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

    def delete_empty_tree(root: Path) -> None:
        """Handle-delete one exact validated empty private-tree root."""
        with open_tree(root) as opened:
            if opened is None:
                return
            entries, _identities = scan_tree(opened)
            if entries:
                raise _native_error(NativeFailureKind.CHANGED)
            identity = opened.root_identity
        parent_descriptor = open_directory(root.parent, private=False)
        with owned_descriptor(
            parent_descriptor,
            NativeFailureKind.REMOVE,
        ):
            if not require_exact_entry(root.parent, root.name):
                raise _native_error(NativeFailureKind.CHANGED)
            _delete_directory(
                root.parent,
                parent_descriptor,
                TreeEntry((root.name,), identity, True),
            )
            if require_exact_entry(root.parent, root.name):
                raise _native_error(NativeFailureKind.CHANGED)
