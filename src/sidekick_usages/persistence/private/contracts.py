"""Contracts for private credential persistence adapters."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.models.credential import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.platform.contracts import NativeFile
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialState,
)


class PrivateCredentialArtifacts(Protocol):
    """Sidekick-owned credential artifacts used by reset coordination."""

    def observe(self) -> PrivateCredentialState:
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

    def list_directories(self, root: Path) -> tuple[str, ...]:
        """Return validated direct child directory basenames."""

    def list_directories_shallow(self, root: Path) -> tuple[str, ...]:
        """Return direct directories without scanning their descendants."""

    def list_files(self, root: Path) -> tuple[str, ...]:
        """Return validated direct child file basenames."""

    def ensure_directory(self, path: Path) -> None:
        """Create or validate one protected private directory."""

    def repair_permissions(self, root: Path) -> tuple[int, int]:
        """Preflight and repair a private tree without changing bytes."""

    def harden_provider_stage(self, root: Path) -> tuple[int, int]:
        """Normalize one isolated provider-produced private subtree."""

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
]
