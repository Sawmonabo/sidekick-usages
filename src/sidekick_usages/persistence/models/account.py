"""Validated no-secret saved-account index models."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.limits import MAX_ACCOUNTS


@dataclass(frozen=True, slots=True)
class VersionThreeDocument:
    """Validated schema-version-three accounts in insertion order."""

    accounts: tuple[SavedAccount, ...]

    def __post_init__(self) -> None:
        """Reject duplicate IDs and provider-qualified labels."""
        if len(self.accounts) > MAX_ACCOUNTS:
            raise InvalidSchemaError
        account_ids: set[SidekickAccountId] = set()
        labels: set[tuple[ProviderId, AccountLabel]] = set()
        claude_associations: set[ProviderIdentity] = set()
        for account in self.accounts:
            if account.account_id in account_ids:
                raise InvalidSchemaError
            key = (account.provider_id, account.label)
            if key in labels:
                raise InvalidSchemaError
            account_ids.add(account.account_id)
            labels.add(key)
            authority = account.authority
            if not isinstance(authority, ClaudeAccountAuthority):
                continue
            subscription = authority.subscription
            if not isinstance(subscription, ClaudeManagedLoginAuthority):
                continue
            association = subscription.provider_identity
            if association in claude_associations:
                raise InvalidSchemaError
            claude_associations.add(association)
