"""Deterministic recovery for interrupted Claude native activation."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ClaudeAuthObservation,
    SelectedAccountState,
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
    ClaudeActivationRecoveryContext,
    ClaudeNativeObservation,
    claude_auth_observation,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeExchangeSuccess,
    native_authority_expectation,
)
from sidekick_usages.credentials.claude.exchange.service import (
    claude_native_propagation_proven,
    verified_claude_exchange,
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

_MUTATION_CAPABLE_PHASES = frozenset(
    {
        ActivationPhase.OUTGOING_RETAINED,
        ActivationPhase.TARGET_ACTIVATED,
        ActivationPhase.PROVIDER_PROOF_VERIFIED,
    }
)


class ClaudeActivationRecoveryService:
    """Resolve one incomplete Claude switch from current provider truth."""

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

    def recover(
        self,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState | None:
        """Recover one unfinished switch without restoring credential bytes."""
        authority.require(ProviderId.CLAUDE)
        record = self._journals.load(ProviderId.CLAUDE).active
        if record is None:
            return self._selected.load(ProviderId.CLAUDE)
        source_account_id = self._source_account_id(
            record,
            target_account_id,
        )
        authority.account(source_account_id)
        authority.account(target_account_id)
        transaction = self._journals.transaction(
            ProviderId.CLAUDE,
            tuple(sorted(record.account_ids)),
            authority,
        )
        try:
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
            native = self._authorities.observe_native(native_capabilities)
            self._authorities.record_native_observation(native)
            current = transaction.load().active
            if current != record:
                raise ClaudeActivationError(
                    ClaudeActivationFailure.STATE_CHANGED
                )
            (
                source,
                source_authority,
                source_private,
            ) = self._authorities.reconcile_interrupted_source(
                source,
                source_authority,
                source_capabilities,
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
            context = ClaudeActivationRecoveryContext(
                source=source,
                source_authority=source_authority,
                source_capabilities=source_capabilities,
                source_private=source_private,
                target=target,
                target_authority=target_authority,
                target_capabilities=target_capabilities,
                target_private=target_private,
                native_capabilities=native_capabilities,
            )
            resolved = self._resolve(
                transaction,
                record,
                native,
                context,
                authority,
            )
            self._authorities.record_selected_runtime(resolved)
            return resolved
        except ClaudeActivationError as error:
            transaction.require_reconciliation(
                record.operation_id,
                updated_at=self._clock.now(),
                failure_code=error.failure_code,
            )
            raise
        except (
            SourceChangedError,
            ManagedStateConflictError,
        ) as error:
            recovery_error = ClaudeActivationError(
                ClaudeActivationFailure.STATE_CHANGED
            )
            transaction.require_reconciliation(
                record.operation_id,
                updated_at=self._clock.now(),
                failure_code=recovery_error.failure_code,
            )
            raise recovery_error from error

    def _resolve(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        if native.state is ProviderAuthState.ACTIVE:
            return self._resolve_active(
                transaction,
                record,
                native,
                context,
                authority,
            )
        return self._resolve_inactive(
            transaction,
            record,
            native,
            context,
            authority,
        )

    def _resolve_inactive(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        if native.state is not ProviderAuthState.LOGGED_OUT:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        if self._may_rollback(record):
            return self._official_rollback(
                transaction,
                record,
                native,
                context,
                authority,
            )
        self._authorities.require_native_current(
            context.native_capabilities,
            native,
        )
        return self._commit_inactive(transaction, record)

    def _resolve_active(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        snapshot = native.snapshot
        if snapshot is None:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        if (
            snapshot.provider_identity
            == context.source_authority.provider_identity
        ):
            return self._resolve_source(
                transaction,
                record,
                native,
                context,
                authority,
            )
        if (
            snapshot.provider_identity
            == context.target_authority.provider_identity
        ):
            return self._resolve_target(
                transaction,
                record,
                native,
                context,
                authority,
            )
        self._authorities.require_usable(
            snapshot,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
        )
        return self._commit_external_active(
            transaction,
            record,
            native,
            context.source_capabilities,
            context.native_capabilities,
            authority,
        )

    def _resolve_source(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        snapshot = native.snapshot
        if (
            snapshot is not None
            and snapshot.health is not CredentialHealth.LOGIN_REQUIRED
        ):
            if self._matches_journal_native(record, snapshot):
                self._authorities.require_native_current(
                    context.native_capabilities,
                    native,
                )
                return self._commit_rollback(
                    transaction,
                    record,
                    context.source,
                    snapshot,
                )
            if (
                not self._may_rollback(record)
                and self._effective_phase(record)
                is not ActivationPhase.RECONCILIATION_REQUIRED
            ):
                self._authorities.require_native_current(
                    context.native_capabilities,
                    native,
                )
                return self._commit_external_saved(
                    transaction,
                    record,
                    context.source,
                    snapshot,
                )
        if self._may_rollback(record):
            return self._official_rollback(
                transaction,
                record,
                native,
                context,
                authority,
            )
        raise ClaudeActivationError(
            ClaudeActivationFailure.RECONCILIATION_REQUIRED
        )

    def _resolve_target(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        snapshot = native.snapshot
        if (
            snapshot is not None
            and snapshot.health is not CredentialHealth.LOGIN_REQUIRED
            and self._effective_phase(record) is ActivationPhase.PREPARED
        ):
            self._authorities.require_native_current(
                context.native_capabilities,
                native,
            )
            return self._commit_external_active(
                transaction,
                record,
                native,
                context.source_capabilities,
                context.native_capabilities,
                authority,
            )
        if (
            snapshot is not None
            and snapshot.health is not CredentialHealth.LOGIN_REQUIRED
            and self._recovered_target_proven(record, context, snapshot)
        ):
            self._authorities.require_native_current(
                context.native_capabilities,
                native,
            )
            return self._commit_target(
                transaction,
                record,
                context.target,
                snapshot,
            )
        if self._may_rollback(record):
            return self._official_rollback(
                transaction,
                record,
                native,
                context,
                authority,
            )
        raise ClaudeActivationError(
            ClaudeActivationFailure.RECONCILIATION_REQUIRED
        )

    def _commit_target(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        target: SavedAccount,
        native: ClaudeAuthoritySnapshot,
    ) -> SelectedAccountState:
        active = transaction.load().active
        if active is None or active.operation_id != record.operation_id:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        if active.phase is ActivationPhase.PROVIDER_PROOF_VERIFIED:
            if active.verified_runtime_generation != native.generation:
                return self._commit_external_saved(
                    transaction,
                    active,
                    target,
                    native,
                )
        else:
            if active.phase in {
                ActivationPhase.PREPARED,
                ActivationPhase.OUTGOING_RETAINED,
                ActivationPhase.NATIVE_REPAIR_STARTED,
                ActivationPhase.RECONCILIATION_REQUIRED,
            }:
                active = transaction.advance(
                    active.operation_id,
                    ActivationPhase.TARGET_ACTIVATED,
                    updated_at=self._clock.now(),
                )
            if active.phase is ActivationPhase.TARGET_ACTIVATED:
                active = transaction.advance(
                    active.operation_id,
                    ActivationPhase.PROVIDER_PROOF_VERIFIED,
                    updated_at=self._clock.now(),
                    verified_runtime_generation=native.generation,
                )
        if active.phase is not ActivationPhase.PROVIDER_PROOF_VERIFIED:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        selected = SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=target.account_id,
            provider_identity=native.provider_identity,
            runtime_generation=native.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.VERIFIED,
        )
        transaction.commit_verified(
            active.operation_id,
            selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _official_rollback(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        record = transaction.advance(
            record.operation_id,
            ActivationPhase.NATIVE_REPAIR_STARTED,
            updated_at=self._clock.now(),
            verified_runtime_generation=record.verified_runtime_generation,
        )
        try:
            rolled_back = self._authorities.provision_native(
                context.source_capabilities,
                context.source_private,
                context.native_capabilities,
                native,
                ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            )
            self._authorities.require_usable(
                rolled_back,
                ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            )
            if (
                rolled_back.provider_identity
                != context.source_authority.provider_identity
            ):
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
            source_proof = self._authorities.read_saved_private(
                context.source_capabilities,
                context.source_authority,
                context.source,
                ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            )
            target_proof = self._authorities.read_saved_private(
                context.target_capabilities,
                context.target_authority,
                context.target,
                ClaudeActivationFailure.TARGET_UNAVAILABLE,
            )
            native_proof = self._authorities.read_native(
                context.native_capabilities,
                expected_identity=(context.source_authority.provider_identity),
            )
            if (
                source_proof != context.source_private
                or target_proof != context.target_private
            ):
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
            self._authorities.require_same_native_proof(
                rolled_back,
                native_proof,
                ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            )
            return self._commit_rollback(
                transaction,
                record,
                context.source,
                rolled_back,
            )
        except (
            ClaudeActivationError,
            ManagedStateConflictError,
            SourceChangedError,
        ):
            current = self._authorities.observe_native(
                context.native_capabilities
            )
            self._authorities.record_native_observation(current)
            return self._resolve_after_rollback(
                transaction,
                record,
                current,
                context,
                authority,
            )

    def _resolve_after_rollback(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        context: ClaudeActivationRecoveryContext,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        if native.state is not ProviderAuthState.ACTIVE:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        snapshot = native.snapshot
        if snapshot is None:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        self._authorities.require_usable(
            snapshot,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
        )
        source_proof = self._authorities.read_saved_private(
            context.source_capabilities,
            context.source_authority,
            context.source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        target_proof = self._authorities.read_saved_private(
            context.target_capabilities,
            context.target_authority,
            context.target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if (
            source_proof != context.source_private
            or target_proof != context.target_private
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        self._authorities.require_native_current(
            context.native_capabilities,
            native,
        )
        if (
            snapshot.provider_identity
            == context.source_authority.provider_identity
        ):
            if self._matches_journal_native(record, snapshot):
                return self._commit_rollback(
                    transaction,
                    record,
                    context.source,
                    snapshot,
                )
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        if (
            snapshot.provider_identity
            == context.target_authority.provider_identity
        ):
            if self._recovered_target_proven(record, context, snapshot):
                return self._commit_target(
                    transaction,
                    record,
                    context.target,
                    snapshot,
                )
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return self._commit_external_active(
            transaction,
            record,
            native,
            context.source_capabilities,
            context.native_capabilities,
            authority,
        )

    def _commit_external_active(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        native: ClaudeNativeObservation,
        reference_capabilities: ClaudeCapabilities,
        native_capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        snapshot = native.snapshot
        if snapshot is None:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        account = self._authorities.relate_native_account(
            snapshot,
            reference_capabilities,
            authority,
        )
        self._authorities.require_native_current(
            native_capabilities,
            native,
        )
        if account is None:
            selected = SelectedAccountState(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                account_id=None,
                provider_identity=snapshot.provider_identity,
                runtime_generation=snapshot.generation,
                verified_at=self._clock.now(),
                outcome=ActivationOutcome.EXTERNAL_RECONCILED,
            )
            transaction.commit_external(
                record.operation_id,
                selected,
                updated_at=self._clock.now(),
            )
            return selected
        return self._commit_external_saved(
            transaction,
            record,
            account,
            snapshot,
        )

    def _commit_rollback(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        source: SavedAccount,
        native: ClaudeAuthoritySnapshot,
    ) -> SelectedAccountState:
        selected = SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=source.account_id,
            provider_identity=native.provider_identity,
            runtime_generation=native.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.ROLLED_BACK,
        )
        transaction.commit_rollback(
            record.operation_id,
            selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _commit_external_saved(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        account: SavedAccount,
        native: ClaudeAuthoritySnapshot,
    ) -> SelectedAccountState:
        selected = SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=account.account_id,
            provider_identity=native.provider_identity,
            runtime_generation=native.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )
        transaction.commit_external(
            record.operation_id,
            selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _commit_inactive(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
    ) -> SelectedAccountState:
        selected = SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.LOGGED_OUT,
            account_id=None,
            provider_identity=None,
            runtime_generation=None,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.LOGGED_OUT,
        )
        transaction.commit_external(
            record.operation_id,
            selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _recovered_target_proven(
        self,
        record: ActivationRecord,
        context: ClaudeActivationRecoveryContext,
        native: ClaudeAuthoritySnapshot,
    ) -> bool:
        baseline = self._journal_native(record)
        if not claude_native_propagation_proven(
            context.native_capabilities,
            baseline.modified_milliseconds,
            native.modified_milliseconds,
        ):
            return False
        result = verified_claude_exchange(
            native_authority_expectation(
                context.target_private,
                baseline.modified_milliseconds,
            ),
            native,
        )
        return isinstance(result, ClaudeExchangeSuccess)

    def _matches_journal_native(
        self,
        record: ActivationRecord,
        native: ClaudeAuthoritySnapshot,
    ) -> bool:
        baseline = self._journal_native(record)
        return (
            claude_auth_observation(native, baseline.observed_at) == baseline
        )

    @staticmethod
    def _journal_native(record: ActivationRecord) -> ClaudeAuthObservation:
        baseline = record.native_auth_baseline
        if not isinstance(baseline, ClaudeAuthObservation):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return baseline

    @staticmethod
    def _may_rollback(record: ActivationRecord) -> bool:
        return record.phase in _MUTATION_CAPABLE_PHASES or (
            record.phase is ActivationPhase.RECONCILIATION_REQUIRED
            and record.reconciliation_origin_phase in _MUTATION_CAPABLE_PHASES
        )

    @staticmethod
    def _effective_phase(record: ActivationRecord) -> ActivationPhase:
        if (
            record.phase is ActivationPhase.RECONCILIATION_REQUIRED
            and record.reconciliation_origin_phase is not None
        ):
            return record.reconciliation_origin_phase
        return record.phase

    @staticmethod
    def _source_account_id(
        record: ActivationRecord,
        target_account_id: SidekickAccountId,
    ) -> SidekickAccountId:
        baseline = record.selected_baseline
        if (
            record.provider_id is not ProviderId.CLAUDE
            or record.target_account_id != target_account_id
            or baseline is None
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or baseline.account_id is None
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return baseline.account_id
