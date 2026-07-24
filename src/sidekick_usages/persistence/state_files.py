"""Shared recovery policy for strict non-secret state files."""

from enum import StrEnum

from sidekick_usages.persistence.artifacts import (
    ArtifactPurpose,
    ManagedArtifactKind,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    PersistenceCode,
    PersistenceError,
)
from sidekick_usages.persistence.private_filesystem import PrivateFilesystem
from sidekick_usages.persistence.transaction import PersistenceTransaction

__all__ = [
    "ManagedStateConflictError",
    "ManagedStateConflictKind",
    "recover_state_file",
]

_MAX_RECOVERABLE_TEMPORARIES = 8


class ManagedStateConflictKind(StrEnum):
    """Closed unsafe state mutations rejected before persistence."""

    SELECTED_ACCOUNT = "selected_account"
    ACTIVE_ACTIVATION = "active_activation"
    RUNNING_OPERATION = "running_operation"
    CONCURRENT_CHANGE = "concurrent_change"


class ManagedStateConflictError(PersistenceError):
    """A state mutation conflicts with active durable work."""

    def __init__(self, kind: ManagedStateConflictKind) -> None:
        self.code = PersistenceCode.SOURCE_CHANGED
        self.kind = kind
        message = {
            ManagedStateConflictKind.SELECTED_ACCOUNT: (
                "The selected account cannot be removed."
            ),
            ManagedStateConflictKind.ACTIVE_ACTIVATION: (
                "An active provider switch already owns this state."
            ),
            ManagedStateConflictKind.RUNNING_OPERATION: (
                "A running account operation cannot be removed."
            ),
            ManagedStateConflictKind.CONCURRENT_CHANGE: (
                "Managed service state changed concurrently."
            ),
        }[kind]
        super().__init__(message)


def recover_state_file(
    filesystem: PrivateFilesystem,
    transaction: PersistenceTransaction,
) -> None:
    """Discard bounded interrupted candidates under the exact file lock."""
    managed = filesystem.discover_managed()
    temporaries = tuple(
        artifact
        for artifact in managed
        if artifact.kind is ManagedArtifactKind.TEMPORARY
    )
    if len(temporaries) > _MAX_RECOVERABLE_TEMPORARIES:
        raise InterruptedArtifactError(temporaries[0].basename)
    for artifact in managed:
        if artifact.kind in {
            ManagedArtifactKind.AUTHORITY,
            ManagedArtifactKind.LOCK,
        }:
            continue
        if (
            artifact.kind is ManagedArtifactKind.TEMPORARY
            and artifact.purpose is ArtifactPurpose.AUTHORITY
        ):
            transaction.recover_or_discard_temporary(artifact)
            continue
        raise InvalidManagedArtifactError(artifact.basename)
