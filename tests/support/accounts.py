"""Synthetic saved accounts and credential leases."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from uuid import UUID, uuid5

from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import Account
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialLease,
)
from sidekick_usages.persistence.accounts.index import (
    saved_account_from_runtime,
)
from sidekick_usages.persistence.accounts.runtime_bridge import (
    active_stored_reference,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.repository import (
    authority_for_account,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.providers.base import (
    CredentialAccountLease,
    ProviderAuthenticatedAccount,
)

_TEST_ACCOUNT_NAMESPACE = UUID("75cc2b04-05ea-43d2-b897-bc960c85cd63")
_TEST_AUTHORITY_NAMESPACE = UUID("a050a4a2-357b-4923-aeed-ed5866475853")


@dataclass(frozen=True, slots=True)
class _TestCredentialLease:
    """Expose a synthetic account at the provider test boundary."""

    account: Account


def saved_account(account: Account) -> SavedAccount:
    """Return secret-free metadata for one synthetic runtime account."""
    identity = f"{account.provider_id.value}\0{account.label}"
    return saved_account_from_runtime(
        account,
        account_id=SidekickAccountId(
            str(uuid5(_TEST_ACCOUNT_NAMESPACE, identity))
        ),
        authority_id=AuthorityId(
            str(uuid5(_TEST_AUTHORITY_NAMESPACE, identity))
        ),
    )


def authenticated_account(account: Account) -> ProviderAuthenticatedAccount:
    """Wrap one synthetic runtime account for a direct provider test."""
    lease: CredentialAccountLease = _TestCredentialLease(account)
    return AuthenticatedAccount(account=saved_account(account), lease=lease)


class RuntimeCredentialResolver:
    """Open typed test leases from one in-memory runtime account source."""

    def __init__(self, source: AccountStore) -> None:
        self._source = source
        self.events: list[str] = []

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return one unopened synthetic credential lease."""
        return self._open(account)

    def open_authorized(
        self,
        account: SavedAccount,
        authority: OperationAuthority,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return a lease under the synthetic lookup's held authority."""
        authority.require(account.account_id)
        return self._open(account)

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
    ) -> Iterator[AuthenticatedSavedAccount]:
        self.events.append(f"open:{account.label}")
        runtime = self._source.get(
            str(account.label),
            provider_id=account.provider_id,
        )
        if runtime is None:
            raise AssertionError("Resolver target disappeared.")
        authority = authority_for_account(
            runtime,
            account_id=account.account_id,
            authority_id=active_stored_reference(account),
        )
        lease = CredentialLease(
            account,
            authority.account_id,
            authority.authority_id,
            authority.credentials,
        )
        with lease:
            yield AuthenticatedAccount(account=account, lease=lease)
        self.events.append(f"close:{account.label}")
