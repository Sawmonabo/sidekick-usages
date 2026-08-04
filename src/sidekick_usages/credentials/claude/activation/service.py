"""Journaled official Claude native-account activation."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ClaudeAuthObservation,
    FinalizedSelection,
    ProviderAuthObservation,
    SelectedAccountState,
    activation_account_ids,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
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
    claude_auth_observation,
)
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
    ActivationJournalTransaction,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities


class ClaudeActivationService:
    """Retain the source and officially activate one verified target."""

    def __init__(
        self,
        authorities: ClaudeActivationAuthorityCoordinator,
        journals: ActivationJournalStore,
        selected: SelectedStateStore,
        clock: Clock,
    ) -> None:
        self._authorities = authorities
        self._journals = journals
        self._selected = selected
        self._clock = clock

    def activate(
        self,
        operation_id: OperationId,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
        *,
        expected_target_generation: AuthorityGeneration | None = None,
    ) -> SelectedAccountState:
        """Retain the native source, activate the target, and commit proof."""
        authority.require(ProviderId.CLAUDE)
        self._authorities.require_activation_environment()
        finalized = self._selected.load(ProviderId.CLAUDE)
        if finalized is None:
            return self._activate_initial(
                operation_id,
                target_account_id,
                authority,
                expected_target_generation,
            )
        source_account_id = self._source_account_id(
            finalized,
            target_account_id,
        )
        authority.account(source_account_id)
        authority.account(target_account_id)
        source, source_authority = self._authorities.managed_account(
            source_account_id,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        target, target_authority = self._authorities.managed_account(
            target_account_id,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        source_capabilities = self._authorities.prepare(source_account_id)
        target_capabilities = self._authorities.prepare(target_account_id)
        self._authorities.require_same_runtime(
            source_capabilities,
            target_capabilities,
        )
        native_capabilities = self._authorities.native_capabilities(
            target_capabilities
        )
        self._authorities.require_native_switch(native_capabilities)
        native_observation = self._authorities.observe_native(
            native_capabilities
        )
        if native_observation.state is ProviderAuthState.LOGGED_OUT:
            return self._activate_initial(
                operation_id,
                target_account_id,
                authority,
                expected_target_generation,
            )
        if native_observation.state is not ProviderAuthState.ACTIVE:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        self._authorities.read_saved_private(
            source_capabilities,
            source_authority,
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        target_private = self._authorities.read_saved_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if (
            expected_target_generation is not None
            and target_private.generation != expected_target_generation
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        self._authorities.require_usable(
            target_private,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        native_source = self._authorities.read_native(native_capabilities)
        native_baseline, selected_baseline = self._relate_source(
            finalized,
            source,
            source_authority,
            native_source,
        )
        transaction = self._transaction(
            selected_baseline,
            target_account_id,
            authority,
        )
        now = self._clock.now()
        record = transaction.begin(
            ActivationRecord(
                provider_id=ProviderId.CLAUDE,
                operation_id=operation_id,
                selected_baseline=selected_baseline,
                native_auth_baseline=native_baseline,
                target_account_id=target_account_id,
                expected_target_identity=(target_authority.provider_identity),
                target_authority_generation=target_private.generation,
                phase=ActivationPhase.PREPARED,
                started_at=now,
                updated_at=now,
            )
        )
        try:
            retained_source = self._authorities.retain_source(
                source,
                source_authority,
                source_capabilities,
                native_capabilities,
                native_source,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.OUTGOING_RETAINED,
                updated_at=self._clock.now(),
            )
            activated_target = self._authorities.provision_native(
                target_capabilities,
                target_private,
                native_capabilities,
                ClaudeNativeObservation(
                    state=ProviderAuthState.ACTIVE,
                    snapshot=native_source,
                ),
                ClaudeActivationFailure.TARGET_UNAVAILABLE,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.TARGET_ACTIVATED,
                updated_at=self._clock.now(),
            )
            self._prove_final_authorities(
                retained_source,
                source_authority,
                source_capabilities,
                target,
                target_authority,
                target_capabilities,
                target_private,
                native_capabilities,
                activated_target,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.PROVIDER_PROOF_VERIFIED,
                updated_at=self._clock.now(),
                verified_runtime_generation=activated_target.generation,
            )
            selected = SelectedAccountState(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                account_id=target.account_id,
                provider_identity=activated_target.provider_identity,
                runtime_generation=activated_target.generation,
                verified_at=self._clock.now(),
                outcome=ActivationOutcome.VERIFIED,
            )
            transaction.commit_verified(
                record.operation_id,
                selected,
                updated_at=self._clock.now(),
            )
            self._authorities.record_selected_runtime(selected)
        except (
            ClaudeActivationError,
            SourceChangedError,
            ManagedStateConflictError,
        ) as error:
            activation_error = ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
            transaction.require_reconciliation(
                record.operation_id,
                updated_at=self._clock.now(),
                failure_code=activation_error.failure_code,
            )
            self._authorities.record_native_observation(
                self._authorities.observe_native(native_capabilities)
            )
            raise activation_error from error
        return selected

    def _activate_initial(
        self,
        operation_id: OperationId,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
        expected_target_generation: AuthorityGeneration | None,
    ) -> SelectedAccountState:
        """Activate a first target from exact logged-out native truth."""
        if expected_target_generation is None:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        authority.account(target_account_id)
        target, target_authority = self._authorities.managed_account(
            target_account_id,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        target_capabilities = self._authorities.prepare_existing(
            target_account_id
        )
        native_capabilities = self._authorities.native_capabilities(
            target_capabilities
        )
        self._authorities.require_native_switch(native_capabilities)
        target_private = self._authorities.read_saved_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if target_private.generation != expected_target_generation:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        self._authorities.require_usable(
            target_private,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        native = self._authorities.observe_native(native_capabilities)
        if native.state is ProviderAuthState.ACTIVE:
            return self._adopt_initial_target(
                target,
                target_authority,
                native_capabilities,
                native,
            )
        if native.state is not ProviderAuthState.LOGGED_OUT:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        self._authorities.require_native_current(native_capabilities, native)
        baseline = self._authorities.record_native_observation(native)
        if (
            type(baseline) is not ProviderAuthObservation
            or baseline.state is not ProviderAuthState.LOGGED_OUT
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        transaction = self._transaction(None, target_account_id, authority)
        now = self._clock.now()
        record = transaction.begin(
            ActivationRecord(
                provider_id=ProviderId.CLAUDE,
                operation_id=operation_id,
                selected_baseline=None,
                native_auth_baseline=baseline,
                target_account_id=target_account_id,
                expected_target_identity=target_authority.provider_identity,
                target_authority_generation=target_private.generation,
                phase=ActivationPhase.PREPARED,
                started_at=now,
                updated_at=now,
            )
        )
        try:
            activated = self._authorities.provision_native(
                target_capabilities,
                target_private,
                native_capabilities,
                native,
                ClaudeActivationFailure.TARGET_UNAVAILABLE,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.TARGET_ACTIVATED,
                updated_at=self._clock.now(),
            )
            target_proof = self._authorities.read_saved_private(
                target_capabilities,
                target_authority,
                target,
                ClaudeActivationFailure.TARGET_UNAVAILABLE,
            )
            native_proof = self._authorities.read_native(
                native_capabilities,
                expected_identity=target_authority.provider_identity,
            )
            if target_proof != target_private:
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
            self._authorities.require_same_native_proof(
                activated,
                native_proof,
                ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.PROVIDER_PROOF_VERIFIED,
                updated_at=self._clock.now(),
                verified_runtime_generation=activated.generation,
            )
            selected = self._selected_target(target, activated)
            transaction.commit_verified(
                record.operation_id,
                selected,
                updated_at=self._clock.now(),
            )
            self._authorities.record_selected_runtime(selected)
            return selected
        except (
            ClaudeActivationError,
            SourceChangedError,
            ManagedStateConflictError,
        ) as error:
            activation_error = ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
            transaction.require_reconciliation(
                record.operation_id,
                updated_at=self._clock.now(),
                failure_code=activation_error.failure_code,
            )
            self._authorities.record_native_observation(
                self._authorities.observe_native(native_capabilities)
            )
            raise activation_error from error

    def _adopt_initial_target(
        self,
        target: SavedAccount,
        target_authority: ClaudeManagedLoginAuthority,
        native_capabilities: ClaudeCapabilities,
        native: ClaudeNativeObservation,
    ) -> SelectedAccountState:
        """Return stable exact target proof without native mutation."""
        snapshot = native.snapshot
        if (
            snapshot is None
            or snapshot.provider_identity != target_authority.provider_identity
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        self._authorities.require_usable(
            snapshot,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        self._authorities.require_native_current(native_capabilities, native)
        selected = self._selected_target(target, snapshot)
        self._authorities.record_selected_runtime(selected)
        return selected

    def _selected_target(
        self,
        target: SavedAccount,
        native: ClaudeAuthoritySnapshot,
    ) -> SelectedAccountState:
        """Project one exact active target into runtime proof."""
        return SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=target.account_id,
            provider_identity=native.provider_identity,
            runtime_generation=native.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.VERIFIED,
        )

    def prevalidate(
        self,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> AuthorityGeneration:
        """Prove one refreshable target without retaining its token."""
        authority.require(ProviderId.CLAUDE)
        self._authorities.require_activation_environment()
        authority.account(target_account_id)
        target, target_authority = self._authorities.managed_account(
            target_account_id,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        target_capabilities = self._authorities.prepare_existing(
            target_account_id
        )
        native_capabilities = self._authorities.native_capabilities(
            target_capabilities
        )
        self._authorities.require_native_switch(native_capabilities)
        target_private = self._authorities.read_saved_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        self._authorities.require_usable(
            target_private,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        return target_private.generation

    def _prove_final_authorities(
        self,
        source: SavedAccount,
        original_source_authority: ClaudeManagedLoginAuthority,
        source_capabilities: ClaudeCapabilities,
        target: SavedAccount,
        target_authority: ClaudeManagedLoginAuthority,
        target_capabilities: ClaudeCapabilities,
        target_private: ClaudeAuthoritySnapshot,
        native_capabilities: ClaudeCapabilities,
        activated_target: ClaudeAuthoritySnapshot,
    ) -> None:
        source_authority = self._authorities.managed_authority(
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        if (
            source_authority.authority_id
            != original_source_authority.authority_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        source_proof = self._authorities.read_saved_private(
            source_capabilities,
            source_authority,
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        if source_proof.generation != source_authority.generation:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        target_proof = self._authorities.read_saved_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if target_proof != target_private:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        native_proof = self._authorities.read_native(
            native_capabilities,
            expected_identity=activated_target.provider_identity,
        )
        self._authorities.require_same_native_proof(
            activated_target,
            native_proof,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
        )

    @staticmethod
    def _source_account_id(
        baseline: FinalizedSelection | None,
        target_account_id: SidekickAccountId,
    ) -> SidekickAccountId:
        if (
            baseline is None
            or baseline.provider_id is not ProviderId.CLAUDE
            or baseline.account_id == target_account_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return baseline.account_id

    def _relate_source(
        self,
        baseline: FinalizedSelection | None,
        source: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        native: ClaudeAuthoritySnapshot,
    ) -> tuple[ClaudeAuthObservation, SelectedAccountState]:
        if (
            baseline is None
            or baseline.account_id != source.account_id
            or native.provider_identity != authority.provider_identity
            or baseline.generation != native.generation
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.NATIVE_CHANGED)
        self._authorities.require_usable(
            native,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
        )
        observed_at = self._clock.now()
        return (
            claude_auth_observation(native, observed_at),
            SelectedAccountState(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                account_id=source.account_id,
                provider_identity=authority.provider_identity,
                runtime_generation=native.generation,
                verified_at=observed_at,
                outcome=ActivationOutcome.VERIFIED,
            ),
        )

    def _transaction(
        self,
        baseline: SelectedAccountState | None,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> ActivationJournalTransaction:
        transaction = self._journals.transaction(
            ProviderId.CLAUDE,
            tuple(
                sorted(
                    activation_account_ids(
                        baseline,
                        target_account_id,
                    )
                )
            ),
            authority,
        )
        if transaction.load().active is not None:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return transaction
