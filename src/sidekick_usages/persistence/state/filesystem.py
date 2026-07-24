"""Qualified filesystem for strict non-secret state."""

from collections.abc import Callable
from pathlib import Path

from sidekick_usages.persistence.errors import InvalidManagedArtifactError
from sidekick_usages.persistence.models.artifact import ManagedArtifact
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.types.artifact import (
    ManagedArtifactKind,
)


class ManagedStateFilesystem(PrivateFilesystem):
    """Bind common private-file transactions to one state decoder."""

    def __init__(
        self,
        path: Path,
        decoder: Callable[[bytes], object],
    ) -> None:
        super().__init__(path)
        self._decoder = decoder

    def _validate_recovery_artifact(
        self,
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        if (
            artifact.kind is not ManagedArtifactKind.AUTHORITY
            or self.grammar.parse(artifact.basename) != artifact
        ):
            raise InvalidManagedArtifactError(artifact.basename)
        self._decoder(payload)
