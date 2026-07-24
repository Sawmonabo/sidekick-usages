"""Machine-wide login and stable-account provider-operation locks."""

from contextlib import AbstractContextManager
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
)
from sidekick_usages.persistence.locking import PersistenceLock

_ACCOUNT_LOCK_DIRECTORY = "account-locks"
_CODEX_LOGIN_LOCK_FILE = "codex-login.state"


class CodexLoginLock:
    """Serialize machine-local interactive Codex login flows."""

    def __init__(self, operations_root: Path) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        filesystem = PersistenceFilesystem(
            operations_root / _CODEX_LOGIN_LOCK_FILE
        )
        self._lock = PersistenceLock(filesystem)

    def hold(self) -> AbstractContextManager[PersistenceTransaction]:
        """Return a single-use exclusive Codex login lock."""
        return self._lock.hold()


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
