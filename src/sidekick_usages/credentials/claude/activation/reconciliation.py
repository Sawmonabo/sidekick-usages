"""Steady-state Claude native-account reconciliation."""

from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    NativeReconciliationResult,
    RelatedRuntimeAuthority,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
    ClaudeNativeObservation,
)
from sidekick_usages.credentials.claude.activation.recovery import (
    ClaudeActivationRecoveryService,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities


class ClaudeNativeReconciliationService:
    """Relate current native Claude truth without changing credentials."""

    def __init__(
        self,
        authorities: ClaudeActivationAuthorityCoordinator,
        recovery: ClaudeActivationRecoveryService,
        journals: ActivationJournalStore,
        selected: SelectedStateStore,
        clock: Clock,
    ) -> None:
        self._authorities = authorities
        self._recovery = recovery
        self._journals = journals
        self._selected = selected
        self._clock = clock

    def reconcile(
        self,
        authority: ProviderMutationAuthority,
    ) -> NativeReconciliationResult:
        """Read native Claude and commit only the still-current relation."""
        authority.require(ProviderId.CLAUDE)
        baseline = self._selected.load(ProviderId.CLAUDE)
        active = self._journals.load(ProviderId.CLAUDE).active
        if active is not None:
            self._recovery.recover(
                active.target_account_id,
                authority,
            )
        current = self._selected.load(ProviderId.CLAUDE)
        try:
            capabilities = self._authorities.prepare_native()
        except ClaudeActivationError as error:
            if error.failure is not ClaudeActivationFailure.INCOMPATIBLE:
                raise
            self._authorities.record_native_observation(
                ClaudeNativeObservation(
                    state=ProviderAuthState.UNSUPPORTED,
                )
            )
            return NativeReconciliationResult(None, baseline != current)
        observed = self._authorities.observe_native(capabilities)
        self._authorities.record_native_observation(observed)
        if self._proof_incomplete(observed):
            return NativeReconciliationResult(None, baseline != current)
        confirmed = self._authorities.observe_native(capabilities)
        confirmed_proof = self._authorities.record_native_observation(
            confirmed
        )
        if self._proof_incomplete(confirmed):
            return NativeReconciliationResult(None, baseline != current)
        if confirmed != observed:
            self._authorities.require_native_current(
                capabilities,
                confirmed,
            )
        candidate = self._candidate(
            confirmed,
            capabilities,
            authority,
            confirmed_proof.observed_at,
        )
        return NativeReconciliationResult(
            candidate,
            not _finalized_matches_runtime(current, candidate),
            _related_runtime_authority(candidate),
        )

    def _candidate(
        self,
        observed: ClaudeNativeObservation,
        capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
        observed_at: datetime,
    ) -> SelectedAccountState:
        if observed.state is ProviderAuthState.ACTIVE:
            snapshot = observed.snapshot
            if snapshot is None:
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
            return self._active_candidate(
                snapshot,
                capabilities,
                authority,
                observed_at,
            )
        if observed.state is not ProviderAuthState.LOGGED_OUT:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return self._inactive_candidate(observed_at)

    @staticmethod
    def _proof_incomplete(observed: ClaudeNativeObservation) -> bool:
        return observed.state in {
            ProviderAuthState.UNREADABLE,
            ProviderAuthState.UNSUPPORTED,
        }

    def _active_candidate(
        self,
        snapshot: ClaudeAuthoritySnapshot,
        capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
        observed_at: datetime,
    ) -> SelectedAccountState:
        account = self._authorities.relate_native_account(
            snapshot,
            capabilities,
            authority,
        )
        if account is None:
            return self._external_candidate(
                snapshot.provider_identity,
                snapshot.generation,
                observed_at,
            )
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=account.account_id,
            provider_identity=snapshot.provider_identity,
            runtime_generation=snapshot.generation,
            verified_at=observed_at,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )

    def _external_candidate(
        self,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
        observed_at: datetime,
    ) -> SelectedAccountState:
        """Return one unassociated native login without label inference."""
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
            account_id=None,
            provider_identity=provider_identity,
            runtime_generation=generation,
            verified_at=observed_at,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )

    def _inactive_candidate(
        self,
        observed_at: datetime,
    ) -> SelectedAccountState:
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.LOGGED_OUT,
            account_id=None,
            provider_identity=None,
            runtime_generation=None,
            verified_at=observed_at,
            outcome=ActivationOutcome.LOGGED_OUT,
        )


def _finalized_matches_runtime(
    finalized: FinalizedSelection | None,
    runtime: SelectedAccountState,
) -> bool:
    """Return whether finalized and provider-proven runtime facts agree."""
    return (
        runtime.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and finalized is not None
        and finalized.account_id == runtime.account_id
        and finalized.generation == runtime.runtime_generation
    )


def _related_runtime_authority(
    runtime: SelectedAccountState,
) -> RelatedRuntimeAuthority | None:
    """Return the safe relation only for strong saved-runtime proof."""
    if (
        runtime.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
        or runtime.account_id is None
        or runtime.runtime_generation is None
    ):
        return None
    return RelatedRuntimeAuthority(
        provider_id=runtime.provider_id,
        account_id=runtime.account_id,
        generation=runtime.runtime_generation,
        observed_at=runtime.verified_at,
    )
