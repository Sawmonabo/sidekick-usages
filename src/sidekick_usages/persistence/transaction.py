"""Lock-scoped capability for account-persistence mutations."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
    Sha256Digest,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem


class _TransactionKey:
    """Module-private construction authority."""


_TRANSACTION_KEY = _TransactionKey()


class PersistenceTransaction:
    """Active mutation capability owned by one acquired persistence lock."""

    __slots__ = ("_active", "_filesystem", "_operation_lock")

    def __init__(
        self,
        filesystem: PersistenceFilesystem,
        key: _TransactionKey,
    ) -> None:
        if key is not _TRANSACTION_KEY:
            raise ValueError(
                "Persistence transactions require an active lock."
            )
        self._filesystem = filesystem
        self._active = True
        self._operation_lock = threading.Lock()

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish or exactly reuse a content-addressed source snapshot."""
        with self._operation():
            return self._filesystem._publish_immutable(generation, source)

    def publish_receipt(
        self,
        prototype_digest: Sha256Digest,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish or exactly reuse one canonical prototype receipt."""
        with self._operation():
            return self._filesystem._publish_receipt(
                prototype_digest,
                payload,
            )

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact authoritative bytes."""
        with self._operation():
            return self._filesystem._commit_authority(
                generation,
                payload,
                expected_source,
            )

    def recover_or_discard_temporary(
        self,
        temporary: ManagedArtifact,
    ) -> None:
        """Complete link-2 publication or discard one exact link-1 temp."""
        with self._operation():
            self._filesystem._recover_or_discard_temporary(temporary)

    def full_reset(self, expected_source: ExpectedAuthority) -> None:
        """Delete all credentials, then the exact authority last."""
        with self._operation():
            self._filesystem._full_reset(expected_source)

    @contextmanager
    def _operation(self) -> Iterator[None]:
        with self._operation_lock:
            if not self._active:
                raise RuntimeError(
                    "Persistence transaction is no longer active."
                )
            yield

    def _invalidate(self) -> None:
        with self._operation_lock:
            self._active = False


def _begin_transaction(
    filesystem: PersistenceFilesystem,
) -> PersistenceTransaction:
    return PersistenceTransaction(filesystem, _TRANSACTION_KEY)


__all__ = ["PersistenceTransaction"]
