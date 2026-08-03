"""Isolated verified Claude selection execution."""

from typing import Protocol

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    SelectionCode,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.activation.reconciliation import (
    ClaudeNativeReconciliationService,
)
from sidekick_usages.credentials.claude.activation.recovery import (
    ClaudeActivationRecoveryService,
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.worker.runtime import (
    managed_worker_result,
    worker_no_change,
    worker_success,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)

_CLAUDE_SELECTION_KINDS = frozenset(
    {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }
)


class ClaudeSelectionWorkerLane(Protocol):
    """Run Claude phases through Task 4's bounded worker composition."""

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        """Prove one protected refreshable target."""

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        """Officially select and prove the native target."""

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> AuthorityReadyProof | None:
        """Read exact provider state without mutation."""


class ClaudeSelectionAuthorityAdapter:
    """Expose qualified Claude worker phases to selection coordination."""

    def __init__(self, worker: ClaudeSelectionWorkerLane) -> None:
        self._worker = worker

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        """Prove one target through the bounded Claude worker lane."""
        return self._worker.prevalidate(operation, baseline)

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        """Commit one target through the bounded Claude worker lane."""
        return self._worker.commit(prepared)

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> AuthorityReadyProof | None:
        """Read one target through the bounded Claude worker lane."""
        return self._worker.readback(prepared)


class ClaudeSelectionWorkerExecutor:
    """Run Claude selection and native reads without credential exchange."""

    def __init__(
        self,
        activation: ClaudeActivationService,
        recovery: ClaudeActivationRecoveryService,
        native_reconciliation: ClaudeNativeReconciliationService,
        clock: Clock,
    ) -> None:
        self._activation = activation
        self._recovery = recovery
        self._native_reconciliation = native_reconciliation
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one admitted Claude selection transaction."""
        authority.require(ProviderId.CLAUDE)
        supported_priority = (
            operation.priority is OperationPriority.INTERACTIVE
            or (
                operation.kind is OperationKind.RECONCILE_NATIVE
                and operation.priority is OperationPriority.SCHEDULED
            )
        )
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind not in _CLAUDE_SELECTION_KINDS
            or not supported_priority
        ):
            raise ValueError("Worker operation is not Claude selection.")
        related = None
        try:
            if operation.kind is OperationKind.ACTIVATE:
                self._activation.activate(
                    operation.operation_id,
                    operation.required_account_id,
                    authority,
                )
            elif operation.kind is OperationKind.RECONCILE:
                self._recovery.recover(
                    operation.required_account_id,
                    authority,
                )
            else:
                reconciled = self._native_reconciliation.reconcile(authority)
                related = reconciled.related_runtime_authority
                if not reconciled.changed:
                    return worker_no_change(
                        operation,
                        self._clock,
                        reconciled.related_runtime_authority,
                    )
        except ClaudeActivationError as error:
            return managed_worker_result(
                operation,
                self._clock,
                succeeded=False,
                action_required=(
                    error.action_required
                    and not (
                        operation.kind is OperationKind.RECONCILE_NATIVE
                        and error.failure
                        is ClaudeActivationFailure.NATIVE_CHANGED
                    )
                ),
                timed_out=error.timed_out,
                failure_code=error.failure_code,
            )
        return worker_success(
            operation,
            self._clock,
            related,
        )

    def prevalidate_selection(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> PreparedSelection:
        """Prove one target before participant admission closes."""
        self._require_open_operation(operation, baseline)
        generation = self._activation.prevalidate(
            operation.target_account_id,
            authority,
        )
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=ProviderId.CLAUDE,
            target_account_id=operation.target_account_id,
            target_generation=generation,
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit_selection(
        self,
        prepared: PreparedSelection,
        authority: ProviderMutationAuthority,
    ) -> AuthorityReadyProof:
        """Run the official activation and return its runtime generation."""
        self._require_prepared(prepared)
        selected = self._activation.activate(
            prepared.operation_id,
            prepared.target_account_id,
            authority,
        )
        if (
            selected.provider_id is not ProviderId.CLAUDE
            or selected.account_id != prepared.target_account_id
            or selected.runtime_generation is None
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return self._proof(prepared, selected.runtime_generation)

    def readback_selection(
        self,
        prepared: PreparedSelection,
        authority: ProviderMutationAuthority,
    ) -> AuthorityReadyProof | None:
        """Read exact native proof for one committed operation."""
        self._require_prepared(prepared)
        selected = self._activation.readback(
            prepared.operation_id,
            prepared.target_account_id,
            prepared.target_generation,
            authority,
        )
        if selected is None or selected.runtime_generation is None:
            return None
        return self._proof(prepared, selected.runtime_generation)

    @staticmethod
    def _require_open_operation(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> None:
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or baseline is None
            or baseline.provider_id is not ProviderId.CLAUDE
            or operation.baseline_account_id != baseline.account_id
            or operation.baseline_epoch != baseline.epoch
            or operation.target_account_id == baseline.account_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _require_prepared(prepared: PreparedSelection) -> None:
        if prepared.provider_id is not ProviderId.CLAUDE:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _proof(
        prepared: PreparedSelection,
        generation: AuthorityGeneration,
    ) -> AuthorityReadyProof:
        return AuthorityReadyProof(
            provider_id=ProviderId.CLAUDE,
            account_id=prepared.target_account_id,
            generation=generation,
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )
