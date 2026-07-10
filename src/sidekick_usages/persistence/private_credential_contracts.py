"""Typed contracts for private credential persistence adapters."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence._platform import NativeFile
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials


@dataclass(frozen=True, slots=True)
class PrivateCredentialRepairResult:
    """Verified outcome of one explicit private-permission repair."""

    root: Path
    account_parent_repaired: bool
    directories_repaired: int
    files_repaired: int
    artifacts_present: bool

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError(
                "Private credential repair root must be absolute."
            )
        if type(self.account_parent_repaired) is not bool:
            raise TypeError("account_parent_repaired must be Boolean.")
        if self.directories_repaired < 0 or self.files_repaired < 0:
            raise ValueError(
                "Private credential repair counts cannot be negative."
            )
        if type(self.artifacts_present) is not bool:
            raise TypeError("artifacts_present must be Boolean.")


class PrivateCredentialArtifacts(Protocol):
    """Sidekick-owned credential artifacts used by reset coordination."""

    def observe(self) -> OrphanedPrivateCredentials:
        """Return closed presence evidence or fail without guessing."""

    def destroy_all(self) -> None:
        """Delete every private credential artifact and verify removal."""

    def repair_permissions(
        self,
        *,
        locked_precondition: Callable[[], None],
    ) -> PrivateCredentialRepairResult:
        """Repair a released tree under the shared account lock."""


class PrivateCredentialNative(Protocol):
    """Native recursive private-tree operations."""

    def contains_artifacts(self, root: Path) -> bool:
        """Return whether a fully validated private tree has descendants."""

    def ensure_directory(self, path: Path) -> None:
        """Create or validate one protected private directory."""

    def repair_permissions(self, root: Path) -> tuple[int, int]:
        """Preflight and repair a private tree without changing bytes."""

    def destroy_artifacts(self, root: Path) -> None:
        """Delete a fully validated private tree bottom-up."""

    def destroy_tree(self, root: Path) -> None:
        """Delete a fully validated private tree and exact root."""


class PrivateBundleNative(Protocol):
    """Native component-qualified private-bundle operations."""

    def ensure_relative_directory(
        self,
        root: Path,
        relative: tuple[str, ...],
    ) -> None:
        """Create one qualified private directory component chain."""

    def read_relative_file(
        self,
        root: Path,
        relative: tuple[str, ...],
        basename: str,
        limit: int,
    ) -> NativeFile | None:
        """Read one file through a qualified component chain."""

    def read_relative_bundle(
        self,
        root: Path,
        relative: tuple[str, ...],
        max_files: int,
        file_limit: int,
        total_limit: int,
    ) -> tuple[tuple[str, NativeFile], ...] | None:
        """Read one complete direct-file bundle through qualified handles."""

    def install_staged_file(
        self,
        root: Path,
        transaction_relative: tuple[str, ...],
        stage_basename: str,
        target_relative: tuple[str, ...],
        target_basename: str,
        expected: NativeFile | None,
        limit: int,
    ) -> NativeFile:
        """Install one journal stage through qualified component chains."""

    def delete_relative_file(
        self,
        root: Path,
        relative: tuple[str, ...],
        basename: str,
        expected: NativeFile,
        limit: int,
    ) -> None:
        """Delete one exact file through a qualified component chain."""

    def contains_relative_artifacts(
        self,
        root: Path,
        relative: tuple[str, ...],
    ) -> bool:
        """Validate and report descendants of one relative bundle."""

    def destroy_relative_tree(
        self,
        root: Path,
        relative: tuple[str, ...],
    ) -> None:
        """Delete one exact relative bundle through qualified components."""


__all__ = [
    "PrivateBundleNative",
    "PrivateCredentialArtifacts",
    "PrivateCredentialNative",
    "PrivateCredentialRepairResult",
]
