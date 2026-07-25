"""Machine-wide login and stable-account provider-operation locks."""

from collections.abc import Iterator
from contextlib import (
    AbstractContextManager,
    ExitStack,
    contextmanager,
)
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem

_ACCOUNT_LOCK_DIRECTORY = "account-locks"
_PROVIDER_LOCK_DIRECTORY = "provider-locks"
_CODEX_LOGIN_LOCK_FILE = "codex-login.state"
_AUTHORITY_CONSTRUCTION_KEY = object()
_PROVIDER_AUTHORITY_CONSTRUCTION_KEY = object()


class OperationAuthority:
    """Proof that one worker currently owns an account operation lock."""

    __slots__ = ("_account_id", "_active")

    def __init__(
        self,
        account_id: SidekickAccountId,
        construction_key: object,
    ) -> None:
        if construction_key is not _AUTHORITY_CONSTRUCTION_KEY:
            raise RuntimeError(
                "Account operation authority requires a held lock."
            )
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


class ProviderMutationAuthority:
    """Proof of provider-first ownership over exact account authorities."""

    __slots__ = ("_accounts", "_active", "_provider_id")

    def __init__(
        self,
        provider_id: ProviderId,
        accounts: dict[SidekickAccountId, OperationAuthority],
        construction_key: object,
    ) -> None:
        if construction_key is not _PROVIDER_AUTHORITY_CONSTRUCTION_KEY:
            raise RuntimeError(
                "Provider mutation authority requires a held lock."
            )
        self._provider_id = provider_id
        self._accounts = accounts
        self._active = True

    def require(self, provider_id: ProviderId) -> None:
        """Require this live capability to match the provider."""
        if not self._active or provider_id is not self._provider_id:
            raise RuntimeError("Provider mutation authority is unavailable.")

    def account(
        self,
        account_id: SidekickAccountId,
    ) -> OperationAuthority:
        """Return one exact account authority under the provider lock."""
        if not self._active:
            raise RuntimeError("Provider mutation authority is unavailable.")
        authority = self._accounts.get(account_id)
        if authority is None:
            raise RuntimeError("Account operation authority is unavailable.")
        authority.require(account_id)
        return authority

    def _invalidate(self) -> None:
        self._active = False

    def __repr__(self) -> str:
        """Return a representation without provider or account identity."""
        return (
            "<ProviderMutationAuthority active>"
            if self._active
            else "<ProviderMutationAuthority closed>"
        )


class CodexLoginLock:
    """Serialize machine-local interactive Codex login flows."""

    def __init__(self, operations_root: Path) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        filesystem = PrivateFilesystem(
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
        filesystem = PrivateFilesystem(
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
            authority = OperationAuthority(
                self._account_id,
                _AUTHORITY_CONSTRUCTION_KEY,
            )
            try:
                yield authority
            finally:
                authority._invalidate()


class ProviderMutationLock:
    """Acquire provider authority before canonical account authorities."""

    def __init__(
        self,
        operations_root: Path,
        provider_id: ProviderId,
        account_ids: tuple[SidekickAccountId, ...],
        *,
        timeout_seconds: float,
    ) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        self._operations_root = operations_root
        self._provider_id = provider_id
        self._account_ids = tuple(sorted(set(account_ids)))
        provider_filesystem = PrivateFilesystem(
            operations_root
            / _PROVIDER_LOCK_DIRECTORY
            / f"{provider_id.value}.state"
        )
        self._provider_lock = PersistenceLock(
            provider_filesystem,
            timeout_seconds=timeout_seconds,
        )

    def hold(self) -> AbstractContextManager[ProviderMutationAuthority]:
        """Return one provider-first mutation capability."""
        return self._hold()

    @contextmanager
    def _hold(self) -> Iterator[ProviderMutationAuthority]:
        with ExitStack() as stack:
            stack.enter_context(self._provider_lock.hold())
            accounts = {
                account_id: stack.enter_context(
                    OperationAuthorityLock(
                        self._operations_root,
                        account_id,
                    ).hold()
                )
                for account_id in self._account_ids
            }
            authority = ProviderMutationAuthority(
                self._provider_id,
                accounts,
                _PROVIDER_AUTHORITY_CONSTRUCTION_KEY,
            )
            try:
                yield authority
            finally:
                authority._invalidate()
