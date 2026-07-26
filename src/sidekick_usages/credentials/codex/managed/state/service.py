"""Validated managed Codex account state and persistence."""

from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.codex.managed.account import (
    managed_codex_account,
)
from sidekick_usages.credentials.codex.managed.failures import (
    credential_health_for_outcome,
    managed_outcome_for_provider,
)
from sidekick_usages.credentials.codex.managed.home import (
    CodexPrivateHomeAuthority,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    CodexVerifiedAuthorityExchange,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.generation import codex_generation_order
from sidekick_usages.providers.codex.models import CodexAuthSnapshot


class CodexManagedAccountState:
    """Validate and persist one managed Codex account authority."""

    def __init__(
        self,
        store: AccountStore,
        home: CodexPrivateHomeAuthority,
        capabilities: CodexAppServerCapabilities,
        clock: Clock,
    ) -> None:
        self._store = store
        self._home = home
        self._capabilities = capabilities
        self._clock = clock

    def snapshot(
        self,
        account_id: SidekickAccountId,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Read the current private authority snapshot."""
        return self._home.snapshot(account_id)

    def expected_snapshot(
        self,
        account: SavedAccount,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Project durable metadata into its expected authority snapshot."""
        authority = require_managed_codex_authority(account)
        try:
            order = codex_generation_order(str(authority.generation))
        except ValueError:
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.MALFORMED,
                message=(
                    "The managed Codex credential generation is malformed."
                ),
            )
        return CodexAuthSnapshot(
            provider_identity=authority.provider_identity,
            generation=authority.generation,
            generation_order=order,
            plan=account.plan,
        )

    def saved_account(self, account_id: SidekickAccountId) -> SavedAccount:
        """Load one durable managed Codex account."""
        account = self._store.read_saved(account_id)
        if account is None:
            raise ValueError("Managed Codex account does not exist.")
        require_managed_codex_authority(account)
        return account

    def persist_provider_failure(
        self,
        account: SavedAccount,
        failure: ProviderFailure,
        *,
        refresh_attempted: bool = False,
    ) -> CodexManagedAuthorityResult:
        """Persist the account health derived from a provider failure."""
        return self.persist_failure(
            account,
            managed_outcome_for_provider(failure.kind),
            refresh_attempted=refresh_attempted,
        )

    def persist_failure(
        self,
        account: SavedAccount,
        outcome: CodexManagedOutcome,
        *,
        health: CredentialHealth | None = None,
        refresh_attempted: bool = False,
    ) -> CodexManagedAuthorityResult:
        """Persist one failed managed-authority outcome."""
        candidate = replace(
            account,
            credential_health=(
                credential_health_for_outcome(outcome)
                if health is None
                else health
            ),
            last_refresh_at=(
                self._clock.now()
                if refresh_attempted
                else account.last_refresh_at
            ),
            last_refresh_status=(
                RefreshStatus.FAILED
                if refresh_attempted
                else account.last_refresh_status
            ),
            last_refresh_error_code=(
                f"codex_managed_{outcome.value}"
                if refresh_attempted
                else account.last_refresh_error_code
            ),
        )
        self._store.persist_state(candidate, expected=account)
        return CodexManagedAuthorityResult(outcome, candidate)

    def persist_exchange(
        self,
        exchange: CodexVerifiedAuthorityExchange,
    ) -> CodexManagedAuthorityResult:
        """Persist one verified provider-owned authority generation."""
        previous = require_managed_codex_authority(exchange.source)
        verified_at = self._clock.now()
        candidate = managed_codex_account(
            exchange.source,
            previous.authority_id,
            exchange.after,
            plan=exchange.observation.plan,
            executable_version=str(self._capabilities.executable.version),
            verified_at=verified_at,
            refreshed=exchange.refreshed,
        )
        self._store.persist_state(candidate, expected=exchange.source)
        return CodexManagedAuthorityResult(
            CodexManagedOutcome.HEALTHY,
            candidate,
        )
