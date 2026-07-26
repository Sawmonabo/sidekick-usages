"""Steady-state Claude native-account reconciliation."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import (
    NativeReconciliationResult,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    same_selected_runtime_authority,
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
            candidate = self._inactive_candidate(
                ProviderRuntimeState.UNSUPPORTED,
                ActivationOutcome.UNSUPPORTED,
            )
        else:
            observed = self._authorities.observe_native(capabilities)
            candidate = self._candidate(
                observed,
                capabilities,
                authority,
            )
            confirmed = self._authorities.observe_native(capabilities)
            if confirmed != observed:
                observed = confirmed
                candidate = self._candidate(
                    observed,
                    capabilities,
                    authority,
                )
                self._authorities.require_native_current(
                    capabilities,
                    observed,
                )
        committed = self._selected.compare_and_swap(
            candidate,
            expected=current,
        )
        return NativeReconciliationResult(
            committed,
            not same_selected_runtime_authority(baseline, committed),
        )

    def _candidate(
        self,
        observed: ClaudeNativeObservation,
        capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
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
            )
        runtime_state, outcome = {
            ProviderAuthState.LOGGED_OUT: (
                ProviderRuntimeState.LOGGED_OUT,
                ActivationOutcome.LOGGED_OUT,
            ),
            ProviderAuthState.UNREADABLE: (
                ProviderRuntimeState.UNREADABLE,
                ActivationOutcome.RECONCILIATION_REQUIRED,
            ),
            ProviderAuthState.UNSUPPORTED: (
                ProviderRuntimeState.UNSUPPORTED,
                ActivationOutcome.UNSUPPORTED,
            ),
        }[observed.state]
        return self._inactive_candidate(runtime_state, outcome)

    def _active_candidate(
        self,
        snapshot: ClaudeAuthoritySnapshot,
        capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        account = self._authorities.relate_native_account(
            snapshot,
            capabilities,
            authority,
        )
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=(
                ProviderRuntimeState.EXTERNAL_ACTIVE
                if account is None
                else ProviderRuntimeState.SAVED_ACTIVE
            ),
            account_id=None if account is None else account.account_id,
            provider_identity=snapshot.provider_identity,
            runtime_generation=snapshot.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )

    def _inactive_candidate(
        self,
        runtime_state: ProviderRuntimeState,
        outcome: ActivationOutcome,
    ) -> SelectedAccountState:
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=runtime_state,
            account_id=None,
            provider_identity=None,
            runtime_generation=None,
            verified_at=self._clock.now(),
            outcome=outcome,
        )
