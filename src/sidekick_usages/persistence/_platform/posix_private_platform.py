"""Public POSIX adapter for private credential-tree operations."""

from pathlib import Path

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativePlatform,
)
from sidekick_usages.persistence._platform import posix_private as tree
from sidekick_usages.persistence._platform.posix_provider_stage import (
    harden_provider_stage,
)


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
        with tree._open_repair_tree(root) as opened:
            if opened is None:
                return (0, 0)
            directories, identities = tree._scan_repair_tree(opened)
            repaired = sum(
                tree._repair_directory_permissions(
                    opened,
                    directory,
                    identities,
                )
                for directory in sorted(
                    directories,
                    key=lambda candidate: (
                        len(candidate.relative),
                        candidate.relative,
                    ),
                    reverse=True,
                )
            )
            tree._scan_tree(opened)
            return (repaired, 0)

    def harden_provider_stage(self, root: Path) -> tuple[int, int]:
        """Normalize only a preflight-safe provider-produced subtree."""
        return harden_provider_stage(root, self._qualifier)

    def contains_artifacts(self, root: Path) -> bool:
        """Validate the complete tree and report whether it has children."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return False
            entries, _identities = tree._scan_tree(opened)
            return bool(entries)

    def list_directories(self, root: Path) -> tuple[str, ...]:
        """Validate the complete tree and list direct child directories."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return ()
            entries, _identities = tree._scan_tree(opened)
            return tuple(
                sorted(
                    entry.relative[0]
                    for entry in entries
                    if entry.directory and len(entry.relative) == 1
                )
            )

    def list_directories_shallow(self, root: Path) -> tuple[str, ...]:
        """Validate only direct children and return directory basenames."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return ()
            return tuple(
                sorted(
                    entry.relative[0]
                    for entry in tree._scan_direct_tree(opened)
                    if entry.directory
                )
            )

    def list_files(self, root: Path) -> tuple[str, ...]:
        """Validate the complete tree and list direct child files."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return ()
            entries, _identities = tree._scan_tree(opened)
            return tuple(
                sorted(
                    entry.relative[0]
                    for entry in entries
                    if not entry.directory and len(entry.relative) == 1
                )
            )

    def destroy_artifacts(self, root: Path) -> None:
        """Prevalidate then identity-delete every descendant bottom-up."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return
            entries, identities = tree._scan_tree(opened)
            for entry in sorted(
                entries,
                key=lambda candidate: (
                    len(candidate.relative),
                    candidate.relative,
                ),
                reverse=True,
            ):
                tree._delete_entry(opened, entry, identities)
            remaining, _remaining_identities = tree._scan_tree(opened)
            if remaining:
                raise tree._native_error(NativeFailureKind.CHANGED)

    def destroy_tree(self, root: Path) -> None:
        """Delete one exact validated private tree including its root."""
        self._qualifier.qualify(root)
        with tree._open_tree(root) as opened:
            if opened is None:
                return
            entries, identities = tree._scan_tree(opened)
            for entry in sorted(
                entries,
                key=lambda candidate: (
                    len(candidate.relative),
                    candidate.relative,
                ),
                reverse=True,
            ):
                tree._delete_entry(opened, entry, identities)
            remaining, _remaining_identities = tree._scan_tree(opened)
            if remaining:
                raise tree._native_error(NativeFailureKind.CHANGED)
            tree._require_root_identity(opened)
            root_entry = tree._TreeEntry(
                (opened.root_basename,),
                opened.root_identity,
                True,
            )
            tree._delete_directory(
                opened,
                opened.parent_descriptor,
                root_entry,
            )


__all__ = ["PosixPrivateCredentialPlatform"]
