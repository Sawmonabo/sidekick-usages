"""POSIX descriptor-relative private bundle mutations."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.persistence.artifacts import (
    require_portable_unique_basenames,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.ports import NativePlatform
from sidekick_usages.persistence.platform.posix import files, namespace
from sidekick_usages.persistence.platform.posix.adapter import (
    _remove_exact_entry,
)
from sidekick_usages.persistence.platform.posix.namespace import (
    PRIVATE_DIRECTORY_MODE,
)
from sidekick_usages.persistence.platform.posix.private.tree import (
    _delete_directory,
    _delete_entry,
    _metadata,
    _open_tree,
    _OpenedTree,
    _require_root_identity,
    _scan_tree,
    _TreeEntry,
)
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.private.bundles.paths import (
    private_bundle_relative_components,
)

type _Identity = tuple[int, int]
type _RelativePath = tuple[str, ...]


@dataclass(slots=True)
class _OpenedChain:
    descriptors: list[int]
    identities: tuple[_Identity, ...]
    components: _RelativePath


def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
    return NativeFilesystemError(kind)


def _require_chain_identity(
    opened: _OpenedTree,
    chain: _OpenedChain,
    *,
    final_may_be_absent: bool = False,
) -> None:
    _require_root_identity(opened)
    if not chain.descriptors or len(chain.descriptors) != len(
        chain.identities
    ):
        raise _native_error(NativeFailureKind.CHANGED)
    for index, descriptor in enumerate(chain.descriptors):
        metadata = _metadata(descriptor, NativeFailureKind.CHANGED)
        if (
            metadata.st_dev,
            metadata.st_ino,
        ) != chain.identities[index] or metadata.st_dev != opened.root_device:
            raise _native_error(NativeFailureKind.CHANGED)
        if index == 0:
            continue
        parent = chain.descriptors[index - 1]
        basename = chain.components[index - 1]
        observed = namespace.require_exact_entry(parent, basename)
        if final_may_be_absent and index == len(chain.descriptors) - 1:
            if observed not in {None, chain.identities[index]}:
                raise _native_error(NativeFailureKind.CHANGED)
        elif observed != chain.identities[index]:
            raise _native_error(NativeFailureKind.CHANGED)


def _open_chain_child(
    opened: _OpenedTree,
    parent: int,
    component: str,
    *,
    create: bool,
) -> tuple[int, _Identity] | None:
    identity = namespace.require_exact_entry(parent, component)
    if identity is None:
        if not create:
            return None
        try:
            os.mkdir(
                component,
                PRIVATE_DIRECTORY_MODE,
                dir_fd=parent,
            )
            os.fsync(parent)
        except FileExistsError:
            raise _native_error(NativeFailureKind.CHANGED) from None
        except OSError:
            raise _native_error(NativeFailureKind.CREATE) from None
        identity = namespace.require_exact_entry(parent, component)
        if identity is None:
            raise _native_error(NativeFailureKind.CHANGED)
    child = namespace.open_child_directory(parent, component, private=True)
    metadata = _metadata(child, NativeFailureKind.UNREADABLE)
    if (
        metadata.st_dev,
        metadata.st_ino,
    ) != identity or metadata.st_dev != opened.root_device:
        namespace.close_descriptor_stack(
            [child],
            _native_error(NativeFailureKind.CHANGED),
        )
    return child, identity


@contextmanager
def _open_component_chain(
    opened: _OpenedTree,
    relative: _RelativePath,
    *,
    create: bool,
    final_may_be_absent: bool = False,
) -> Iterator[_OpenedChain | None]:
    try:
        root_descriptor = os.dup(opened.root_descriptor)
    except OSError:
        raise _native_error(NativeFailureKind.UNREADABLE) from None
    descriptors = [root_descriptor]
    identities = [opened.root_identity]
    primary: BaseException | None = None
    missing = False
    try:
        for component in relative:
            opened_child = _open_chain_child(
                opened,
                descriptors[-1],
                component,
                create=create,
            )
            if opened_child is None:
                missing = True
                break
            child, identity = opened_child
            descriptors.append(child)
            identities.append(identity)
        if missing:
            yield None
        else:
            chain = _OpenedChain(
                descriptors,
                tuple(identities),
                relative,
            )
            _require_chain_identity(opened, chain)
            yield chain
            _require_chain_identity(
                opened,
                chain,
                final_may_be_absent=final_may_be_absent,
            )
    except BaseException as error:
        primary = error
    namespace.close_descriptor_stack(descriptors, primary)


def _read_relative_file(
    opened: _OpenedTree,
    chain: _OpenedChain,
    basename: str,
    limit: int,
) -> NativeFile | None:
    _require_chain_identity(opened, chain)
    parent = chain.descriptors[-1]
    identity = namespace.require_exact_entry(parent, basename)
    if identity is None:
        return None
    flags = (
        os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | namespace.no_follow_flag()
    )
    try:
        descriptor = os.open(basename, flags, dir_fd=parent)
    except FileNotFoundError:
        raise _native_error(NativeFailureKind.CHANGED) from None
    except OSError:
        raise _native_error(NativeFailureKind.UNSAFE) from None
    with namespace.owned_descriptor(descriptor, NativeFailureKind.UNREADABLE):
        metadata = _metadata(descriptor, NativeFailureKind.UNREADABLE)
        if (metadata.st_dev, metadata.st_ino) != identity:
            raise _native_error(NativeFailureKind.CHANGED)
        result = files.read_descriptor(descriptor, opened.root_device, limit)
        if namespace.require_exact_entry(parent, basename) != identity:
            raise _native_error(NativeFailureKind.CHANGED)
    _require_chain_identity(opened, chain)
    return result


def _install_staged_file(
    opened: _OpenedTree,
    transaction: _OpenedChain,
    stage_basename: str,
    target: _OpenedChain,
    target_basename: str,
    expected: NativeFile | None,
    limit: int,
) -> NativeFile:
    stage = _read_relative_file(
        opened,
        transaction,
        stage_basename,
        limit,
    )
    if stage is None:
        raise _native_error(NativeFailureKind.CHANGED)
    current = _read_relative_file(
        opened,
        target,
        target_basename,
        limit,
    )
    if current != expected:
        raise _native_error(NativeFailureKind.CHANGED)
    source_descriptor = transaction.descriptors[-1]
    target_descriptor = target.descriptors[-1]
    _require_chain_identity(opened, transaction)
    _require_chain_identity(opened, target)
    if (
        _read_relative_file(
            opened,
            target,
            target_basename,
            limit,
        )
        != expected
    ):
        raise _native_error(NativeFailureKind.CHANGED)
    _require_chain_identity(opened, transaction)
    _require_chain_identity(opened, target)
    try:
        if expected is None:
            os.link(
                stage_basename,
                target_basename,
                src_dir_fd=source_descriptor,
                dst_dir_fd=target_descriptor,
                follow_symlinks=False,
            )
        else:
            os.replace(
                stage_basename,
                target_basename,
                src_dir_fd=source_descriptor,
                dst_dir_fd=target_descriptor,
            )
        os.fsync(source_descriptor)
        os.fsync(target_descriptor)
    except FileExistsError:
        raise _native_error(NativeFailureKind.CHANGED) from None
    except OSError:
        raise _native_error(NativeFailureKind.REPLACE) from None
    final = _read_relative_file(
        opened,
        target,
        target_basename,
        limit,
    )
    if (
        final is None
        or (final.device, final.inode) != (stage.device, stage.inode)
        or final.data != stage.data
    ):
        raise _native_error(NativeFailureKind.CHANGED)
    _require_chain_identity(opened, transaction)
    _require_chain_identity(opened, target)
    return final


def _delete_relative_file(
    opened: _OpenedTree,
    chain: _OpenedChain,
    basename: str,
    expected: NativeFile,
    limit: int,
) -> None:
    if _read_relative_file(opened, chain, basename, limit) != expected:
        raise _native_error(NativeFailureKind.CHANGED)
    parent = chain.descriptors[-1]
    _remove_exact_entry(
        parent,
        basename,
        (expected.device, expected.inode),
        allow_interrupted_link=True,
    )
    try:
        os.fsync(parent)
    except OSError:
        raise _native_error(NativeFailureKind.SYNCHRONIZE) from None
    if namespace.require_exact_entry(parent, basename) is not None:
        raise _native_error(NativeFailureKind.CHANGED)
    _require_chain_identity(opened, chain)


def _bundle_names(
    chain: _OpenedChain,
    max_files: int,
) -> tuple[str, ...]:
    try:
        names = tuple(sorted(os.listdir(chain.descriptors[-1])))
        for basename in names:
            private_bundle_relative_components(basename)
        require_portable_unique_basenames(names)
    except ValueError:
        raise _native_error(NativeFailureKind.UNSAFE) from None
    except OSError:
        raise _native_error(NativeFailureKind.UNREADABLE) from None
    if len(names) > max_files:
        raise _native_error(NativeFailureKind.TOO_LARGE)
    return names


def _read_bundle_pass(
    opened: _OpenedTree,
    chain: _OpenedChain,
    names: tuple[str, ...],
    file_limit: int,
    total_limit: int,
) -> tuple[tuple[str, NativeFile], ...]:
    files: list[tuple[str, NativeFile]] = []
    total = 0
    for basename in names:
        snapshot = _read_relative_file(
            opened,
            chain,
            basename,
            file_limit,
        )
        if snapshot is None:
            raise _native_error(NativeFailureKind.CHANGED)
        total += len(snapshot.data)
        if total > total_limit:
            raise _native_error(NativeFailureKind.TOO_LARGE)
        files.append((basename, snapshot))
    return tuple(files)


def _read_bundle_files(
    opened: _OpenedTree,
    chain: _OpenedChain,
    max_files: int,
    file_limit: int,
    total_limit: int,
) -> tuple[tuple[str, NativeFile], ...]:
    names = _bundle_names(chain, max_files)
    files = _read_bundle_pass(
        opened,
        chain,
        names,
        file_limit,
        total_limit,
    )
    _require_chain_identity(opened, chain)
    for basename, snapshot in files:
        if (
            _read_relative_file(
                opened,
                chain,
                basename,
                file_limit,
            )
            != snapshot
        ):
            raise _native_error(NativeFailureKind.CHANGED)
    if _bundle_names(chain, max_files) != names:
        raise _native_error(NativeFailureKind.CHANGED)
    return files


class PosixPrivateBundlePlatform:
    """Qualified component-relative bundle mutation for Linux and macOS."""

    def __init__(self, qualifier: NativePlatform) -> None:
        self._qualifier = qualifier

    def _ensure_root(self, root: Path) -> None:
        self._qualifier.qualify(root)
        self._qualifier.ensure_parent(root)
        self._qualifier.qualify(root)

    def relative_entry_present(
        self,
        root: Path,
        relative: _RelativePath,
        basename: str,
    ) -> bool:
        """Report one exact child entry without opening its contents."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return False
            with _open_component_chain(
                opened,
                relative,
                create=False,
            ) as chain:
                if chain is None:
                    return False
                _require_chain_identity(opened, chain)
                parent = chain.descriptors[-1]
                identity = namespace.require_exact_entry(parent, basename)
                _require_chain_identity(opened, chain)
                if namespace.require_exact_entry(parent, basename) != identity:
                    raise _native_error(NativeFailureKind.CHANGED)
                return identity is not None

    def ensure_relative_directory(
        self,
        root: Path,
        relative: _RelativePath,
    ) -> None:
        """Create one descriptor-relative private directory chain."""
        self._ensure_root(root)
        with _open_tree(root) as opened:
            if opened is None:
                raise _native_error(NativeFailureKind.CHANGED)
            with _open_component_chain(
                opened,
                relative,
                create=True,
            ) as chain:
                if chain is None:
                    raise _native_error(NativeFailureKind.CHANGED)

    def read_relative_file(
        self,
        root: Path,
        relative: _RelativePath,
        basename: str,
        limit: int,
    ) -> NativeFile | None:
        """Read one file through a retained component-descriptor chain."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return None
            with _open_component_chain(
                opened,
                relative,
                create=False,
            ) as chain:
                if chain is None:
                    return None
                return _read_relative_file(
                    opened,
                    chain,
                    basename,
                    limit,
                )

    def read_relative_bundle(
        self,
        root: Path,
        relative: _RelativePath,
        max_files: int,
        file_limit: int,
        total_limit: int,
    ) -> tuple[tuple[str, NativeFile], ...] | None:
        """Read one complete direct-file bundle through a retained chain."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return None
            with _open_component_chain(
                opened,
                relative,
                create=False,
            ) as chain:
                if chain is None:
                    return None
                return _read_bundle_files(
                    opened,
                    chain,
                    max_files,
                    file_limit,
                    total_limit,
                )

    def install_staged_file(
        self,
        root: Path,
        transaction_relative: _RelativePath,
        stage_basename: str,
        target_relative: _RelativePath,
        target_basename: str,
        expected: NativeFile | None,
        limit: int,
    ) -> NativeFile:
        """Install one journal stage through held source and target chains."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                raise _native_error(NativeFailureKind.CHANGED)
            with _open_component_chain(
                opened,
                transaction_relative,
                create=False,
            ) as transaction:
                if transaction is None:
                    raise _native_error(NativeFailureKind.CHANGED)
                with _open_component_chain(
                    opened,
                    target_relative,
                    create=True,
                ) as target:
                    if target is None:
                        raise _native_error(NativeFailureKind.CHANGED)
                    return _install_staged_file(
                        opened,
                        transaction,
                        stage_basename,
                        target,
                        target_basename,
                        expected,
                        limit,
                    )

    def delete_relative_file(
        self,
        root: Path,
        relative: _RelativePath,
        basename: str,
        expected: NativeFile,
        limit: int,
    ) -> None:
        """Delete one exact file through its retained component chain."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                raise _native_error(NativeFailureKind.CHANGED)
            with _open_component_chain(
                opened,
                relative,
                create=False,
            ) as chain:
                if chain is None:
                    raise _native_error(NativeFailureKind.CHANGED)
                _delete_relative_file(
                    opened,
                    chain,
                    basename,
                    expected,
                    limit,
                )

    def contains_relative_artifacts(
        self,
        root: Path,
        relative: _RelativePath,
    ) -> bool:
        """Validate and report descendants of one relative bundle."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return False
            with _open_component_chain(
                opened,
                relative,
                create=False,
            ) as chain:
                if chain is None:
                    return False
                nested = _OpenedTree(
                    chain.descriptors[-2],
                    chain.descriptors[-1],
                    chain.identities[-1],
                    opened.root_device,
                    relative[-1],
                )
                entries, _identities = _scan_tree(nested)
                return bool(entries)

    def destroy_relative_tree(
        self,
        root: Path,
        relative: _RelativePath,
    ) -> None:
        """Delete one exact relative tree through retained descriptors."""
        self._qualifier.qualify(root)
        with _open_tree(root) as opened:
            if opened is None:
                return
            with _open_component_chain(
                opened,
                relative,
                create=False,
                final_may_be_absent=True,
            ) as chain:
                if chain is None:
                    return
                nested = _OpenedTree(
                    chain.descriptors[-2],
                    chain.descriptors[-1],
                    chain.identities[-1],
                    opened.root_device,
                    relative[-1],
                )
                entries, identities = _scan_tree(nested)
                for entry in sorted(
                    entries,
                    key=lambda candidate: (
                        len(candidate.relative),
                        candidate.relative,
                    ),
                    reverse=True,
                ):
                    _delete_entry(nested, entry, identities)
                remaining, _remaining_identities = _scan_tree(nested)
                if remaining:
                    raise _native_error(NativeFailureKind.CHANGED)
                _delete_directory(
                    nested,
                    nested.parent_descriptor,
                    _TreeEntry(
                        (nested.root_basename,),
                        nested.root_identity,
                        True,
                    ),
                )
