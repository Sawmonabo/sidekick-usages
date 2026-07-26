"""Crash-recoverable Codex shared-runtime activation."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ProviderAuthObservation,
    SelectedAccountState,
    activation_account_ids,
)
from sidekick_usages.core.selection.policy import (
    same_provider_auth_authority,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.ports import (
    CodexNativeAuthObserver,
    CodexProjectionInstaller,
)
from sidekick_usages.credentials.codex.types import (
    CodexActivationFailure,
    CodexManagedOutcome,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
    ActivationJournalTransaction,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.app_server.types import (
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection

_UNTRUSTED_NATIVE_STATES = frozenset(
    {
        ProviderAuthState.UNREADABLE,
        ProviderAuthState.UNSUPPORTED,
    }
)


class CodexActivationError(RuntimeError):
    """One secret-safe activation failure."""

    def __init__(
        self,
        failure: CodexActivationFailure,
        *,
        action_required: bool = False,
    ) -> None:
        self.failure = failure
        self.action_required = action_required
        super().__init__(failure.value)


class CodexActivationService:
    """Activate and recover one managed Codex account transaction."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        journals: ActivationJournalStore,
        selected: SelectedStateStore,
        native_auth: CodexNativeAuthObserver,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._journals = journals
        self._selected = selected
        self._native_auth = native_auth
        self._clock = clock

    def activate(
        self,
        operation_id: OperationId,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
        installer: CodexProjectionInstaller,
    ) -> SelectedAccountState:
        """Refresh, install, prove, and commit one new global selection."""
        authority.require(ProviderId.CODEX)
        baseline = self._selected.load(ProviderId.CODEX)
        if (
            baseline is not None
            and baseline.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
            and baseline.account_id == target_account_id
        ):
            return baseline
        target_authority = authority.account(target_account_id)
        expectation = self._projection_expectation(
            target_account_id,
            target_authority,
        )
        native_baseline = self._native_auth.observe()
        self._require_trusted_native(native_baseline)
        account_ids = tuple(
            sorted(activation_account_ids(baseline, target_account_id))
        )
        transaction = self._journals.transaction(
            ProviderId.CODEX,
            account_ids,
            authority,
        )
        if transaction.load().active is not None:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        refreshed = self._coordinator.refresh_with_authority(
            target_account_id,
            target_authority,
            process_group=CodexProcessGroupPolicy.INHERITED,
        )
        self._require_healthy(refreshed)
        target = require_managed_codex_authority(refreshed.account)
        if target.provider_identity != expectation.provider_identity:
            raise CodexActivationError(
                CodexActivationFailure.TARGET_UNAVAILABLE
            )
        self._require_native_unchanged(native_baseline)
        now = self._clock.now()
        record = transaction.begin(
            ActivationRecord(
                provider_id=ProviderId.CODEX,
                operation_id=operation_id,
                selected_baseline=baseline,
                native_auth_baseline=native_baseline,
                target_account_id=target_account_id,
                expected_target_identity=target.provider_identity,
                target_authority_generation=target.generation,
                phase=ActivationPhase.PREPARED,
                started_at=now,
                updated_at=now,
            )
        )
        guarded = _NativeGuardedInstaller(
            installer,
            self._native_auth,
            record.native_auth_baseline,
        )
        try:
            receipt = self._install_current(
                target_account_id,
                target_authority,
                guarded,
            )
            return self._commit_receipt(transaction, record, receipt)
        except Exception as error:
            self._require_reconciliation(transaction, record, error)
            raise

    def recover(
        self,
        operation_id: OperationId,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
        installer: CodexProjectionInstaller,
    ) -> SelectedAccountState:
        """Recover one journal without inferring unavailable daemon state."""
        del operation_id
        authority.require(ProviderId.CODEX)
        active = self._journals.load(ProviderId.CODEX).active
        if active is None or active.target_account_id != target_account_id:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        transaction = self._journals.transaction(
            ProviderId.CODEX,
            tuple(sorted(active.account_ids)),
            authority,
        )
        current = transaction.load().active
        if current is None or current.operation_id != active.operation_id:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        native = self._native_auth.observe()
        if native.state in _UNTRUSTED_NATIVE_STATES:
            error = CodexActivationError(
                CodexActivationFailure.NATIVE_UNREADABLE,
                action_required=True,
            )
            self._require_reconciliation(transaction, current, error)
            raise error
        baseline = current.selected_baseline
        native_unchanged = same_provider_auth_authority(
            native,
            current.native_auth_baseline,
        )
        target_chosen = (
            not native_unchanged
            and native.state is ProviderAuthState.ACTIVE
            and native.provider_identity == current.expected_target_identity
        )
        rollback_chosen = (
            not native_unchanged
            and native.state is ProviderAuthState.ACTIVE
            and baseline is not None
            and baseline.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
            and baseline.account_id is not None
            and baseline.provider_identity == native.provider_identity
        )
        if not native_unchanged and not target_chosen and not rollback_chosen:
            error = CodexActivationError(
                CodexActivationFailure.NATIVE_CHANGED,
                action_required=True,
            )
            self._require_reconciliation(transaction, current, error)
            raise error
        selected_account_id = (
            baseline.account_id
            if rollback_chosen and baseline is not None
            else target_account_id
        )
        if selected_account_id is None:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        selected_authority = authority.account(selected_account_id)
        expected_identity = (
            baseline.provider_identity
            if rollback_chosen and baseline is not None
            else current.expected_target_identity
        )
        expectation = self._projection_expectation(
            selected_account_id,
            selected_authority,
        )
        if expectation.provider_identity != expected_identity:
            error = CodexActivationError(
                CodexActivationFailure.TARGET_UNAVAILABLE
            )
            self._require_reconciliation(transaction, current, error)
            raise error
        guarded = _NativeGuardedInstaller(
            installer,
            self._native_auth,
            (current.native_auth_baseline if native_unchanged else native),
        )
        try:
            receipt = self._install_current(
                selected_account_id,
                selected_authority,
                guarded,
            )
            if rollback_chosen:
                return self._commit_rollback_receipt(
                    transaction,
                    current,
                    receipt,
                    expectation.generation,
                )
            return self._commit_receipt(transaction, current, receipt)
        except Exception as error:
            self._require_reconciliation(transaction, current, error)
            raise

    def _commit_receipt(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        receipt: CodexProjectionReceipt,
    ) -> SelectedAccountState:
        if (
            receipt.account_id != record.target_account_id
            or receipt.provider_identity != record.expected_target_identity
            or receipt.generation != record.target_authority_generation
        ):
            raise CodexActivationError(CodexActivationFailure.RECEIPT_MISMATCH)
        active = transaction.load().active
        if active is None or active.operation_id != record.operation_id:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        if active.phase in {
            ActivationPhase.PREPARED,
            ActivationPhase.OUTGOING_RETAINED,
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
                verified_runtime_generation=receipt.generation,
            )
        if active.phase is not ActivationPhase.PROVIDER_PROOF_VERIFIED:
            raise CodexActivationError(CodexActivationFailure.STATE_CHANGED)
        selected = SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=receipt.account_id,
            provider_identity=receipt.provider_identity,
            runtime_generation=receipt.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.VERIFIED,
        )
        transaction.commit_verified(
            active.operation_id,
            selected,
            self._selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _commit_rollback_receipt(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        receipt: CodexProjectionReceipt,
        source_authority_generation: AuthorityGeneration,
    ) -> SelectedAccountState:
        baseline = record.selected_baseline
        if (
            baseline is None
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or receipt.account_id != baseline.account_id
            or receipt.provider_identity != baseline.provider_identity
            or receipt.generation != source_authority_generation
        ):
            raise CodexActivationError(CodexActivationFailure.RECEIPT_MISMATCH)
        selected = SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=receipt.account_id,
            provider_identity=receipt.provider_identity,
            runtime_generation=receipt.generation,
            verified_at=self._clock.now(),
            outcome=ActivationOutcome.ROLLED_BACK,
        )
        transaction.commit_rollback(
            record.operation_id,
            selected,
            self._selected,
            updated_at=self._clock.now(),
        )
        return selected

    def _projection_expectation(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> CodexProjectionExpectation:
        result = self._coordinator.projection_expectation_with_authority(
            account_id,
            authority,
        )
        if isinstance(result, CodexManagedAuthorityResult):
            self._require_healthy(result)
            raise CodexActivationError(
                CodexActivationFailure.TARGET_UNAVAILABLE
            )
        return result

    def _install_current(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt:
        result = self._coordinator.install_projection_with_authority(
            account_id,
            authority,
            installer,
        )
        if isinstance(result, CodexManagedAuthorityResult):
            self._require_healthy(result)
            raise CodexActivationError(
                CodexActivationFailure.TARGET_UNAVAILABLE
            )
        return result

    def _require_native_unchanged(
        self,
        baseline: ProviderAuthObservation,
    ) -> None:
        current = self._native_auth.observe()
        self._require_trusted_native(current)
        if not same_provider_auth_authority(current, baseline):
            raise CodexActivationError(CodexActivationFailure.NATIVE_CHANGED)

    @staticmethod
    def _require_trusted_native(
        observation: ProviderAuthObservation,
    ) -> None:
        if observation.state in _UNTRUSTED_NATIVE_STATES:
            raise CodexActivationError(
                CodexActivationFailure.NATIVE_UNREADABLE,
                action_required=True,
            )

    @staticmethod
    def _require_healthy(result: CodexManagedAuthorityResult) -> None:
        if result.outcome is CodexManagedOutcome.HEALTHY:
            return
        raise CodexActivationError(
            CodexActivationFailure.TARGET_UNAVAILABLE,
            action_required=result.outcome.action_required,
        )

    def _require_reconciliation(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        error: Exception,
    ) -> None:
        active = transaction.load().active
        if (
            active is None
            or active.operation_id != record.operation_id
            or active.phase is ActivationPhase.RECONCILIATION_REQUIRED
            or active.phase.terminal
        ):
            return
        try:
            transaction.advance(
                active.operation_id,
                ActivationPhase.RECONCILIATION_REQUIRED,
                updated_at=self._clock.now(),
                verified_runtime_generation=(
                    active.verified_runtime_generation
                ),
                failure_code="activation_interrupted",
            )
        except Exception as journal_error:
            raise journal_error from error


class _NativeGuardedInstaller:
    """Reject an install when native authentication changed mid-operation."""

    def __init__(
        self,
        installer: CodexProjectionInstaller,
        native_auth: CodexNativeAuthObserver,
        baseline: ProviderAuthObservation,
    ) -> None:
        self._installer = installer
        self._native_auth = native_auth
        self._baseline = baseline

    def prepare(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
    ) -> CodexProjectionReceipt | None:
        """Guard and delegate one shared-runtime preflight."""
        self._require_unchanged()
        return self._installer.prepare(
            account_id,
            provider_identity,
            generation,
        )

    def install(
        self,
        projection: CodexProjection,
    ) -> CodexProjectionReceipt:
        """Guard and delegate the official shared-runtime mutation."""
        self._require_unchanged()
        return self._installer.install(projection)

    def _require_unchanged(self) -> None:
        current = self._native_auth.observe()
        if current.state in _UNTRUSTED_NATIVE_STATES:
            raise CodexActivationError(
                CodexActivationFailure.NATIVE_UNREADABLE,
                action_required=True,
            )
        if not same_provider_auth_authority(current, self._baseline):
            raise CodexActivationError(CodexActivationFailure.NATIVE_CHANGED)
