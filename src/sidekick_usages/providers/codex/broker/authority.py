"""Secret-free saved-account relation for the resident Codex broker."""

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    ProviderAuthObservation,
)
from sidekick_usages.core.selection.types import ProviderAuthState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexSavedAccountReader,
)


class CodexSavedAuthorityResolver:
    """Relate runtime identities through secret-free saved metadata."""

    def __init__(self, accounts: CodexSavedAccountReader) -> None:
        self._accounts = accounts

    def expectation(
        self,
        selected: FinalizedSelection,
    ) -> CodexProjectionExpectation | None:
        """Resolve one finalized pointer through saved authority metadata."""
        if selected.provider_id is not ProviderId.CODEX:
            return None
        managed = self._managed_authority(selected.account_id)
        if managed is None or managed.generation != selected.generation:
            return None
        return CodexProjectionExpectation(
            selected.account_id,
            managed.provider_identity,
            selected.generation,
        )

    def matches(
        self,
        selected: FinalizedSelection,
        observation: ProviderAuthObservation,
    ) -> bool:
        """Return whether both facts name one current managed Codex login."""
        return (
            selected.provider_id is ProviderId.CODEX
            and self._matches_account(
                selected.account_id,
                observation,
                expected_generation=selected.generation,
            )
        )

    def matches_account(
        self,
        account_id: SidekickAccountId,
        observation: ProviderAuthObservation,
    ) -> bool:
        """Return whether an observation names one saved Codex account."""
        return self._matches_account(
            account_id,
            observation,
            expected_generation=None,
        )

    def _matches_account(
        self,
        account_id: SidekickAccountId,
        observation: ProviderAuthObservation,
        *,
        expected_generation: AuthorityGeneration | None,
    ) -> bool:
        """Relate a strong identity while enforcing saved-state freshness."""
        if (
            observation.provider_id is not ProviderId.CODEX
            or observation.state is not ProviderAuthState.ACTIVE
            or observation.provider_identity is None
            or observation.generation is None
        ):
            return False
        managed = self._managed_authority(account_id)
        return managed is not None and (
            managed.provider_identity == observation.provider_identity
            and (
                expected_generation is None
                or managed.generation == expected_generation
            )
        )

    def _managed_authority(
        self,
        account_id: SidekickAccountId,
    ) -> CodexManagedAuthority | None:
        """Return one valid secret-free managed authority."""
        account = self._accounts.read_saved(account_id)
        if account is None:
            return None
        return _managed_authority(account)


def _managed_authority(account: SavedAccount) -> CodexManagedAuthority | None:
    """Return managed Codex metadata without opening private credentials."""
    authority = account.authority
    if (
        account.provider_id is not ProviderId.CODEX
        or not isinstance(authority, CodexAccountAuthority)
        or not isinstance(authority.subscription, CodexManagedAuthority)
    ):
        return None
    return authority.subscription
