"""Claude setup-token attachment and renewal."""

from dataclasses import replace
from datetime import timedelta

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.identifiers import new_authority_id
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeSetupTokenAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityIdFactory,
    CredentialHealth,
)
from sidekick_usages.core.models import ClaudeSetupTokenCredentials
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.setup.models import (
    ClaudeSetupTokenUpdate,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.types.credential import StoredCredentialKind

_SETUP_TOKEN_LIFETIME = timedelta(days=365)


class ClaudeSetupTokenCoordinator:
    """Attach or renew setup tokens without replacing subscriptions."""

    def __init__(
        self,
        store: AccountStore,
        clock: Clock,
        *,
        authority_id_factory: AuthorityIdFactory = new_authority_id,
    ) -> None:
        self._store = store
        self._clock = clock
        self._authority_id_factory = authority_id_factory

    def save(
        self,
        account: SavedAccount,
        credentials: ClaudeSetupTokenCredentials,
    ) -> SavedAccount:
        """Atomically save a token while preserving all other account state."""
        update = self._prepare(account, credentials)
        self._store.persist_claude_setup_token(
            update.account,
            update.authority,
            expected=account,
        )
        return update.account

    def _prepare(
        self,
        account: SavedAccount,
        credentials: ClaudeSetupTokenCredentials,
    ) -> ClaudeSetupTokenUpdate:
        authority = account.authority
        if account.provider_id is not ProviderId.CLAUDE or not isinstance(
            authority, ClaudeAccountAuthority
        ):
            raise ValueError("Setup tokens require a saved Claude account.")
        existing = authority.setup_token
        authority_id = (
            existing.authority_id
            if existing is not None
            else self._authority_id_factory()
        )
        subscription = authority.subscription
        if (
            subscription is not None
            and subscription.authority_id == authority_id
        ):
            raise ValueError(
                "Setup-token and subscription authorities must be distinct."
            )
        observed_at = self._clock.now()
        candidate = replace(
            account,
            authority=ClaudeAccountAuthority(
                setup_token=ClaudeSetupTokenAuthority(
                    authority_id=authority_id,
                    expires_at=observed_at + _SETUP_TOKEN_LIFETIME,
                    health=CredentialHealth.UNKNOWN,
                    observed_at=observed_at,
                ),
                subscription=subscription,
            ),
        )
        protected = StoredCredentialAuthority(
            authority_id=authority_id,
            account_id=account.account_id,
            provider_id=ProviderId.CLAUDE,
            kind=StoredCredentialKind.CLAUDE_SETUP,
            credentials=credentials,
        )
        return ClaudeSetupTokenUpdate(
            account=candidate,
            authority=protected,
        )
