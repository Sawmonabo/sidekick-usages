"""Windows handle-qualified private bundle mutations."""

import sys
from pathlib import Path

from sidekick_usages.persistence.artifacts import (
    portable_basename_key,
    require_portable_unique_basenames,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.types import (
    NativeFailureKind,
    RelativePath,
)
from sidekick_usages.persistence.platform.windows.private.models import (
    OpenedChain,
    OpenedTree,
)
from sidekick_usages.persistence.private.bundles.paths import (
    private_bundle_relative_components,
)

if sys.platform == "win32":
    import pywintypes
    import win32file

    from sidekick_usages.persistence.platform.windows.adapter import (
        WindowsPlatform,
    )
    from sidekick_usages.persistence.platform.windows.files import (
        read_file,
        remove_validated,
    )
    from sidekick_usages.persistence.platform.windows.handles import (
        descriptor_handle,
        open_mutation_source,
        owned_descriptor,
    )
    from sidekick_usages.persistence.platform.windows.namespace import (
        child_path,
        validate_membership,
    )
    from sidekick_usages.persistence.platform.windows.private.tree import (
        delete_empty_tree,
        delete_entry,
        list_names,
        open_component_chain,
        open_tree,
        require_chain_identity,
        scan_tree,
    )


if sys.platform == "win32":

    def _install_staged_file(
        opened: OpenedTree,
        transaction: OpenedChain,
        stage_basename: str,
        target: OpenedChain,
        target_basename: str,
        expected: NativeFile | None,
        limit: int,
    ) -> NativeFile:
        source_parent = transaction.paths[-1]
        target_parent = target.paths[-1]
        stage = read_file(source_parent, stage_basename, limit)
        if stage is None or (
            read_file(target_parent, target_basename, limit) != expected
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        source_descriptor = open_mutation_source(
            source_parent,
            stage_basename,
            transaction.descriptors[-1],
            stage.device,
            stage.inode,
        )
        with owned_descriptor(
            source_descriptor,
            NativeFailureKind.REPLACE,
        ):
            require_chain_identity(opened, transaction)
            require_chain_identity(opened, target)
            if read_file(target_parent, target_basename, limit) != expected:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            require_chain_identity(opened, transaction)
            require_chain_identity(opened, target)
            flags = win32file.MOVEFILE_WRITE_THROUGH
            if expected is not None:
                flags |= win32file.MOVEFILE_REPLACE_EXISTING
            try:
                win32file.MoveFileExW(
                    str(child_path(source_parent, stage_basename)),
                    str(child_path(target_parent, target_basename)),
                    flags,
                )
            except pywintypes.error:
                raise NativeFilesystemError(
                    NativeFailureKind.REPLACE
                ) from None
            validate_membership(
                target.descriptors[-1],
                descriptor_handle(
                    source_descriptor,
                    NativeFailureKind.CHANGED,
                ),
                target_basename,
            )
        final = read_file(target_parent, target_basename, limit)
        if final is None or (
            (final.device, final.inode) != (stage.device, stage.inode)
            or final.data != stage.data
        ):
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        require_chain_identity(opened, transaction)
        require_chain_identity(opened, target)
        return final

    def _read_bundle_files(
        opened: OpenedTree,
        chain: OpenedChain,
        max_files: int,
        file_limit: int,
        total_limit: int,
    ) -> tuple[tuple[str, NativeFile], ...]:
        names = list_names(chain.paths[-1])
        try:
            for basename in names:
                private_bundle_relative_components(basename)
            require_portable_unique_basenames(names)
        except ValueError:
            raise NativeFilesystemError(NativeFailureKind.UNSAFE) from None
        if len(names) > max_files:
            raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)
        files: list[tuple[str, NativeFile]] = []
        total = 0
        for basename in names:
            snapshot = read_file(chain.paths[-1], basename, file_limit)
            if snapshot is None:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            total += len(snapshot.data)
            if total > total_limit:
                raise NativeFilesystemError(NativeFailureKind.TOO_LARGE)
            files.append((basename, snapshot))
        require_chain_identity(opened, chain)
        for basename, snapshot in files:
            if read_file(chain.paths[-1], basename, file_limit) != snapshot:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
        if list_names(chain.paths[-1]) != names:
            raise NativeFilesystemError(NativeFailureKind.CHANGED)
        return tuple(files)

    class WindowsPrivateBundlePlatform:
        """Qualified component-relative bundle mutation for local NTFS."""

        def __init__(self) -> None:
            self._qualifier = WindowsPlatform()

        def _ensure_root(self, root: Path) -> None:
            self._qualifier.qualify(root)
            self._qualifier.ensure_parent(root)
            self._qualifier.qualify(root)

        def relative_entry_present(
            self,
            root: Path,
            relative: RelativePath,
            basename: str,
        ) -> bool:
            """Report one exact child entry without reading its contents."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    return False
                with open_component_chain(
                    opened,
                    relative,
                    create=False,
                ) as chain:
                    if chain is None:
                        return False
                    require_chain_identity(opened, chain)
                    names = list_names(chain.paths[-1])
                    try:
                        require_portable_unique_basenames(names)
                    except ValueError:
                        raise NativeFilesystemError(
                            NativeFailureKind.UNSAFE
                        ) from None
                    target = portable_basename_key(basename)
                    present = any(
                        portable_basename_key(name) == target for name in names
                    )
                    require_chain_identity(opened, chain)
                    if list_names(chain.paths[-1]) != names:
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    return present

        def ensure_relative_directory(
            self,
            root: Path,
            relative: RelativePath,
        ) -> None:
            """Create one handle-qualified private directory chain."""
            self._ensure_root(root)
            with open_tree(root) as opened:
                if opened is None:
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                with open_component_chain(
                    opened,
                    relative,
                    create=True,
                ) as chain:
                    if chain is None:
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)

        def read_relative_file(
            self,
            root: Path,
            relative: RelativePath,
            basename: str,
            limit: int,
        ) -> NativeFile | None:
            """Read one file through a held handle-qualified chain."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    return None
                with open_component_chain(
                    opened,
                    relative,
                    create=False,
                ) as chain:
                    if chain is None:
                        return None
                    require_chain_identity(opened, chain)
                    result = read_file(chain.paths[-1], basename, limit)
                    require_chain_identity(opened, chain)
                    return result

        def read_relative_bundle(
            self,
            root: Path,
            relative: RelativePath,
            max_files: int,
            file_limit: int,
            total_limit: int,
        ) -> tuple[tuple[str, NativeFile], ...] | None:
            """Read one complete direct-file bundle through held handles."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    return None
                with open_component_chain(
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
            transaction_relative: RelativePath,
            stage_basename: str,
            target_relative: RelativePath,
            target_basename: str,
            expected: NativeFile | None,
            limit: int,
        ) -> NativeFile:
            """Move one journal stage through held component chains."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                with open_component_chain(
                    opened,
                    transaction_relative,
                    create=False,
                ) as transaction:
                    if transaction is None:
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    with open_component_chain(
                        opened,
                        target_relative,
                        create=True,
                    ) as target:
                        if target is None:
                            raise NativeFilesystemError(
                                NativeFailureKind.CHANGED
                            )
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
            relative: RelativePath,
            basename: str,
            expected: NativeFile,
            limit: int,
        ) -> None:
            """Delete one exact file through a held component chain."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    raise NativeFilesystemError(NativeFailureKind.CHANGED)
                with open_component_chain(
                    opened,
                    relative,
                    create=False,
                ) as chain:
                    if chain is None or (
                        read_file(chain.paths[-1], basename, limit) != expected
                    ):
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    if not remove_validated(
                        chain.paths[-1],
                        basename,
                        expected.device,
                        expected.inode,
                    ):
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    require_chain_identity(opened, chain)

        def contains_relative_artifacts(
            self,
            root: Path,
            relative: RelativePath,
        ) -> bool:
            """Validate and report one relative bundle's descendants."""
            self._qualifier.qualify(root)
            with open_tree(root) as opened:
                if opened is None:
                    return False
                with open_component_chain(
                    opened,
                    relative,
                    create=False,
                ) as chain:
                    if chain is None:
                        return False
                    nested = OpenedTree(
                        chain.paths[-1],
                        chain.descriptors[-2],
                        chain.descriptors[-1],
                        chain.identities[-1],
                        opened.root_device,
                        relative[-1],
                    )
                    entries, _identities = scan_tree(nested)
                    return bool(entries)

        def destroy_relative_tree(
            self,
            root: Path,
            relative: RelativePath,
        ) -> None:
            """Delete one exact relative tree through held components."""
            self._qualifier.qualify(root)
            target_path: Path | None = None
            with open_tree(root) as opened:
                if opened is None:
                    return
                with open_component_chain(
                    opened,
                    relative,
                    create=False,
                ) as chain:
                    if chain is None:
                        return
                    target_path = chain.paths[-1]
                    nested = OpenedTree(
                        target_path,
                        chain.descriptors[-2],
                        chain.descriptors[-1],
                        chain.identities[-1],
                        opened.root_device,
                        relative[-1],
                    )
                    entries, identities = scan_tree(nested)
                    for entry in sorted(
                        entries,
                        key=lambda candidate: (
                            len(candidate.relative),
                            candidate.relative,
                        ),
                        reverse=True,
                    ):
                        delete_entry(nested, entry, identities)
                    remaining, _remaining = scan_tree(nested)
                    if remaining:
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                with open_component_chain(
                    opened,
                    relative[:-1],
                    create=False,
                ) as parent_chain:
                    if parent_chain is None or target_path is None:
                        raise NativeFilesystemError(NativeFailureKind.CHANGED)
                    delete_empty_tree(target_path)
                    require_chain_identity(opened, parent_chain)
