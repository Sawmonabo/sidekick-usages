"""Structural type for current account transaction commits."""

from typing import Protocol, runtime_checkable

from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)


@runtime_checkable
class AccountTransactionFilesystem(Protocol):
    """Current account mutation exposed only under a held lock."""

    def _commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit one exact current account authority."""
