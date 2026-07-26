"""Internal account transaction state."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.types import AuthorityId, SidekickAccountId
from sidekick_usages.core.models import Account
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.persistence.models.artifact import ExpectedAuthority
from sidekick_usages.persistence.types.artifact import AuthorityExpectation


@dataclass(frozen=True, slots=True, repr=False)
class AccountPersistenceState:
    """One validated account index and its protected runtime projection."""

    index: AccountIndex
    runtime: dict[SidekickAccountId, Account]
    authority_payloads: dict[tuple[SidekickAccountId, AuthorityId], bytes]
    baseline: ExpectedAuthority

    @classmethod
    def empty(cls) -> AccountPersistenceState:
        """Return a fresh state for a proven absent account authority."""
        return cls(
            AccountIndex(),
            {},
            {},
            AuthorityExpectation.ABSENT,
        )
