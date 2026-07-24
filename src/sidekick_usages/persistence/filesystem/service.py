"""Qualified commits for the current account authority."""

from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.schema.account import decode_version_three
from sidekick_usages.persistence.types.artifact import ManagedArtifactKind

__all__ = ["PersistenceFilesystem"]


class PersistenceFilesystem(PrivateFilesystem):
    """Filesystem facade bound to one current account index."""

    def _validate_recovery_artifact(
        self,
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        if artifact.kind is not ManagedArtifactKind.AUTHORITY:
            raise ValueError("Recovery target must be the account authority.")
        decode_version_three(payload)

    def _commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Atomically commit and prove current account-index bytes."""
        decode_version_three(payload)
        return self._commit_payload(
            payload,
            expected_source,
            decode_version_three,
        )
