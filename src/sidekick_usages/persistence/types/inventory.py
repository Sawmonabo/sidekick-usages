"""Structural types for read-only persistence inventory."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence._platform import FilesystemQualification
from sidekick_usages.persistence.artifacts import FileSnapshot, ManagedArtifact

type FilesystemFactory = Callable[[Path], ReadOnlyPersistenceFilesystem]

__all__ = [
    "FilesystemFactory",
    "ReadOnlyPersistenceFilesystem",
]


class ReadOnlyPersistenceFilesystem(Protocol):
    """Qualified operations required by passive inventory."""

    def qualify(self) -> FilesystemQualification:
        """Require an approved local filesystem."""

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        """Return only siblings in the closed managed grammar."""

    def read_authority(self) -> FileSnapshot | None:
        """Read the bound path without following its final object."""

    def read_external_private_source(self) -> FileSnapshot | None:
        """Read a private import source from an owner-controlled parent."""

    def read_managed(
        self,
        artifact: ManagedArtifact,
    ) -> FileSnapshot | None:
        """Bounded-read one exact previously discovered artifact."""
