"""In-memory account and credential boundaries for usage services."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import replace
from datetime import timedelta
from threading import Lock

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.models import (
    CredentialRefreshResult,
    CredentialRefreshSuccess,
    CredentialUpdateResult,
    CredentialUpdateSuccess,
)
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.errors import UsageError
from sidekick_usages.maintenance import CredentialRefresher
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.providers.base import ProviderFailure
from tests.support.accounts import saved_account
from tests.support.time import REFERENCE_TIME


def _copy_account(account: Account) -> Account:
    """Return an independent mutable account for the test boundary."""
    resets = account.heartbeat_window_resets
    return replace(
        account,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
    )


class InMemoryAccountStore(AccountStore):
    """Small AccountStore test double preserving its public contract."""

    def __init__(
        self,
        accounts: tuple[Account, ...],
        *,
        persist_error: PersistenceError | None = None,
    ) -> None:
        self._saved = {
            str(account.label): _copy_account(account) for account in accounts
        }
        self.persist_error = persist_error
        self.persisted: list[Account] = []
        self.iterations = 0
        self.filters: list[ProviderId] = []

    def __iter__(self) -> Iterator[Account]:
        self.iterations += 1
        return iter(tuple(_copy_account(a) for a in self._saved.values()))

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        self.filters.append(provider_id)
        return [
            _copy_account(account)
            for account in self._saved.values()
            if account.provider_id is provider_id
        ]

    def get(
        self,
        label: str,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        account = self._saved.get(label)
        if account is None or (
            provider_id is not None and account.provider_id is not provider_id
        ):
            return None
        return _copy_account(account)

    def persist(self, account: Account) -> None:
        if self.persist_error is not None:
            raise self.persist_error
        saved = _copy_account(account)
        self._saved[str(saved.label)] = saved
        self.persisted.append(_copy_account(saved))

    def saved_accounts(
        self,
        provider_id: ProviderId | None = None,
    ) -> tuple[SavedAccount, ...]:
        """Return stable synthetic metadata for resolver-bound scenarios."""
        if provider_id is not None:
            self.filters.append(provider_id)
        return tuple(
            saved_account(account)
            for account in self._saved.values()
            if provider_id is None or account.provider_id is provider_id
        )

    def read_saved(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | None:
        """Return one stable account by exact synthetic ID."""
        return next(
            (
                account
                for account in self.saved_accounts()
                if account.account_id == account_id
            ),
            None,
        )

    def persist_state(
        self,
        account: SavedAccount,
        *,
        expected: SavedAccount | None = None,
    ) -> None:
        """Persist only non-secret state against the expected snapshot."""
        if self.persist_error is not None:
            raise self.persist_error
        current = self.read_saved(account.account_id)
        if expected is not None and current != expected:
            raise AssertionError("Synthetic account state changed.")
        runtime = self._saved[str(account.label)]
        runtime.plan = account.plan
        runtime.last_refresh_at = account.last_refresh_at
        runtime.last_refresh_status = account.last_refresh_status
        runtime.last_refresh_error = account.last_refresh_error_code
        self.persisted.append(_copy_account(runtime))

    def saved(self, label: str) -> Account:
        """Return one independent durable account for assertions."""
        return _copy_account(self._saved[label])


class _InMemoryOperationAuthority(OperationAuthority):
    """Test-only exact-account capability held by an in-memory lock."""

    __slots__ = ("_test_account_id",)

    def __init__(self, account_id: SidekickAccountId) -> None:
        self._test_account_id = account_id

    def require(self, account_id: SidekickAccountId) -> None:
        """Require this capability to match the locked synthetic account."""
        if account_id != self._test_account_id:
            raise RuntimeError("Synthetic account authority is unavailable.")


class InMemoryOperationLocks:
    """Serialize synthetic provider work by stable account ID."""

    def __init__(self) -> None:
        self._catalog_lock = Lock()
        self._locks: dict[SidekickAccountId, Lock] = {}
        self.entries: list[SidekickAccountId] = []

    def hold(
        self,
        account_id: SidekickAccountId,
    ) -> AbstractContextManager[OperationAuthority]:
        """Hold one synthetic account operation lock."""
        return self._hold(account_id)

    @contextmanager
    def _hold(
        self,
        account_id: SidekickAccountId,
    ) -> Iterator[OperationAuthority]:
        with self._catalog_lock:
            lock = self._locks.setdefault(account_id, Lock())
        with lock:
            self.entries.append(account_id)
            yield _InMemoryOperationAuthority(account_id)


class ScriptedCredentialCoordinator(CredentialRefresher):
    """Script the already-coordinated credential-service boundary."""

    def __init__(
        self,
        store: InMemoryAccountStore,
        steps: tuple[CredentialRefreshResult | UsageError, ...] = (),
    ) -> None:
        self.store = store
        self.steps = list(steps)
        self.calls: list[str] = []

    def refresh(
        self,
        *,
        provider_id: ProviderId,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        del reason
        account = self.store.get(str(label))
        if account is None or account.provider_id is not provider_id:
            raise AssertionError("Scripted refresh target disappeared.")
        self.calls.append(str(label))
        step = (
            self.steps.pop(0)
            if self.steps
            else CredentialRefreshSuccess(account.label)
        )
        if isinstance(step, UsageError):
            raise step
        if isinstance(step, ProviderFailure):
            return step
        saved = _copy_account(account)
        credentials = saved.credentials
        access_token = f"test-only-{account.label}-refreshed"
        if isinstance(credentials, ClaudeLoginCredentials):
            saved.credentials = replace(
                credentials,
                access_token=access_token,
                access_expiry=KnownExpiry(
                    REFERENCE_TIME + timedelta(hours=1)
                ),
            )
        elif isinstance(credentials, CodexCredentials):
            saved.credentials = replace(
                credentials,
                access_token=access_token,
                expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            )
        else:
            saved.credentials = replace(
                credentials,
                access_token=access_token,
            )
        self.store.persist(saved)
        return step

    def persist_provider_update(
        self,
        account: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
    ) -> CredentialUpdateResult:
        current = self.store.get(str(account.label))
        assert current is not None
        assert current.credentials == expected_credentials
        assert current.plan == expected_plan
        current.credentials = account.credentials
        current.plan = account.plan
        self.store.persist(current)
        return CredentialUpdateSuccess(account.label)
