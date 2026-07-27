"""Journaled official Claude native-account activation."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ClaudeAuthObservation,
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
        allow_remote_control_disconnect: bool = False,
    ) -> SelectedAccountState:
        """Retain the native source, activate the target, and commit proof."""
        authority.require(ProviderId.CLAUDE)
        self._authorities.require_activation_environment()
        baseline = self._selected.load(ProviderId.CLAUDE)
        source_account_id = self._source_account_id(
            baseline,
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
        self._authorities.require_native_switch(
            native_capabilities,
            allow_remote_control_disconnect=allow_remote_control_disconnect,
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
        self._authorities.require_usable(
            target_private,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        native_source = self._authorities.read_native(native_capabilities)
        native_baseline = self._relate_source(
            baseline,
            source,
            source_authority,
            native_source,
        )
        transaction = self._transaction(
            baseline,
            target_account_id,
            authority,
        )
        now = self._clock.now()
        record = transaction.begin(
            ActivationRecord(
                provider_id=ProviderId.CLAUDE,
                operation_id=operation_id,
                selected_baseline=baseline,
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
                self._selected,
                updated_at=self._clock.now(),
            )
            self._authorities.record_selected_runtime(selected)
        except (
            ClaudeActivationError,
            SourceChangedError,
            ManagedStateConflictError,
        ) as error:
            activation_error = (
                error
                if isinstance(error, ClaudeActivationError)
                else ClaudeActivationError(
                    ClaudeActivationFailure.STATE_CHANGED
                )
            )
            transaction.require_reconciliation(
                record.operation_id,
                updated_at=self._clock.now(),
                failure_code=activation_error.failure_code,
            )
            self._authorities.record_native_observation(
                self._authorities.observe_native(native_capabilities)
            )
            if isinstance(error, ClaudeActivationError):
                raise
            raise activation_error from error
        return selected

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
        baseline: SelectedAccountState | None,
        target_account_id: SidekickAccountId,
    ) -> SidekickAccountId:
        if (
            baseline is None
            or baseline.provider_id is not ProviderId.CLAUDE
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or baseline.account_id is None
            or baseline.account_id == target_account_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return baseline.account_id

    def _relate_source(
        self,
        baseline: SelectedAccountState | None,
        source: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        native: ClaudeAuthoritySnapshot,
    ) -> ClaudeAuthObservation:
        if (
            baseline is None
            or baseline.account_id != source.account_id
            or baseline.provider_identity != authority.provider_identity
            or native.provider_identity != authority.provider_identity
            or baseline.runtime_generation != native.generation
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.NATIVE_CHANGED)
        self._authorities.require_usable(
            native,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
        )
        return claude_auth_observation(native, self._clock.now())

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
