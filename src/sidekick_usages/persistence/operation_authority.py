"""Stable-ID account locks shared by isolated provider workers."""

from contextlib import AbstractContextManager
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.transaction import PersistenceTransaction

__all__ = ["OperationAuthorityLock"]

_ACCOUNT_LOCK_DIRECTORY = "account-locks"


class OperationAuthorityLock:
    """Serialize provider work for one stable saved account."""

    def __init__(
        self,
        operations_root: Path,
        account_id: SidekickAccountId,
    ) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        filesystem = PersistenceFilesystem(
            operations_root / _ACCOUNT_LOCK_DIRECTORY / f"{account_id}.state"
        )
        self._lock = PersistenceLock(filesystem)

    def hold(self) -> AbstractContextManager[PersistenceTransaction]:
        """Return a single-use exclusive account authority lock."""
        return self._lock.hold()
