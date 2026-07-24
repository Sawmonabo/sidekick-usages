"""Lock-scoped capability for persistence mutations."""

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.types.transaction import (
    AccountTransactionFilesystem,
)

TRANSACTION_KEY = object()


class PersistenceTransaction:
    """Active mutation capability owned by one acquired persistence lock."""

    __slots__ = ("_active", "_filesystem", "_operation_lock")

    def __init__(
        self,
        filesystem: PrivateFilesystem,
        key: object,
    ) -> None:
        if key is not TRANSACTION_KEY:
            raise ValueError(
                "Persistence transactions require an active lock."
            )
        self._filesystem = filesystem
        self._active = True
        self._operation_lock = threading.Lock()

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact current account-index bytes."""
        with self._operation():
            filesystem = self._account_filesystem()
            return filesystem._commit_authority(payload, expected_source)

    def recover_or_discard_temporary(
        self,
        temporary: ManagedArtifact,
    ) -> None:
        """Complete link-2 publication or discard one exact link-1 temp."""
        with self._operation():
            self._filesystem._recover_or_discard_temporary(temporary)

    def _account_filesystem(self) -> AccountTransactionFilesystem:
        if not isinstance(self._filesystem, AccountTransactionFilesystem):
            raise TypeError("Account mutations require an account filesystem.")
        return self._filesystem

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
    filesystem: PrivateFilesystem,
) -> PersistenceTransaction:
    return PersistenceTransaction(filesystem, TRANSACTION_KEY)
