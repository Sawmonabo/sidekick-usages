"""Structural types shared across account transaction boundaries."""

from typing import Protocol, runtime_checkable

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
    Sha256Digest,
)

__all__ = ["AccountTransactionFilesystem"]


@runtime_checkable
class AccountTransactionFilesystem(Protocol):
    """Account-only filesystem mutations exposed under a held lock."""

    def _publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish one content-addressed account snapshot."""

    def _publish_migration_snapshot(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish one validated account migration snapshot."""

    def _publish_receipt(
        self,
        prototype_digest: Sha256Digest,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish one validated prototype receipt."""

    def _commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit one exact account authority."""

    def _full_reset(self, expected_source: ExpectedAuthority) -> None:
        """Delete one account authority and its owned credentials."""
