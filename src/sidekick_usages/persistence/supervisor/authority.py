"""Machine-wide login and stable-account provider-operation locks."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
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


class OperationAuthority:
    """Proof that one worker currently owns an account operation lock."""

    __slots__ = ("_account_id", "_active")

    def __init__(self, account_id: SidekickAccountId) -> None:
        self._account_id = account_id
        self._active = True

    def require(self, account_id: SidekickAccountId) -> None:
        """Require this live capability to match the requested account."""
        if not self._active or account_id != self._account_id:
            raise RuntimeError("Account operation authority is unavailable.")

    def _invalidate(self) -> None:
        self._active = False

    def __repr__(self) -> str:
        """Return a representation without account identity."""
        return (
            "<OperationAuthority active>"
            if self._active
            else ("<OperationAuthority closed>")
        )


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
        self._account_id = account_id
        self._lock = PersistenceLock(filesystem)

    def hold(self) -> AbstractContextManager[OperationAuthority]:
        """Return a single-use account-bound operation capability."""
        return self._hold()

    @contextmanager
    def _hold(self) -> Iterator[OperationAuthority]:
        with self._lock.hold():
            authority = OperationAuthority(self._account_id)
            try:
                yield authority
            finally:
                authority._invalidate()
