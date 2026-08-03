"""Journal-bound provider selection worker validation."""

from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
    OperationKind,
    OperationPriority,
    SelectionCode,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.runtime import worker_failure
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)

_LEGAL_PHASES = {
    OperationKind.SELECTION_PREVALIDATE: frozenset(
        {SelectionPhase.PREVALIDATING}
    ),
    OperationKind.SELECTION_COMMIT: frozenset({SelectionPhase.COMMITTING}),
    OperationKind.SELECTION_READBACK: frozenset(
        {
            SelectionPhase.COMMITTING,
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        }
    ),
}
_AMBIGUOUS_CLAUDE_PHASES = frozenset(
    {
        ActivationPhase.TARGET_ACTIVATED,
        ActivationPhase.PROVIDER_PROOF_VERIFIED,
        ActivationPhase.NATIVE_REPAIR_STARTED,
    }
)


class SelectionWorkerBoundary:
    """Validate durable selection context and sanitize provider results."""

    def __init__(
        self,
        operations: SelectionOperationStore,
        selected: SelectedStateStore,
        activations: ActivationJournalStore,
        clock: Clock,
    ) -> None:
        self._operations = operations
        self._selected = selected
        self._activations = activations
        self._clock = clock

    def context(
        self,
        operation: DueOperation,
    ) -> tuple[OpenSelectionOperation, FinalizedSelection | None]:
        """Load one exact legal global-selection worker context."""
        active = self._operations.load(operation.provider_id).active
        phases = _LEGAL_PHASES.get(operation.kind)
        if (
            active is None
            or phases is None
            or operation.priority is not OperationPriority.INTERACTIVE
            or active.phase not in phases
            or active.operation_id != operation.required_selection_operation_id
            or active.provider_id is not operation.provider_id
            or active.target_account_id != operation.required_account_id
        ):
            raise ValueError(
                "Selection worker journal context is unavailable."
            )
        current = self._selected.load(operation.provider_id)
        baseline_matches = self._baseline_matches(active, current)
        finalized_target_matches = (
            operation.kind is OperationKind.SELECTION_READBACK
            and current is not None
            and current.provider_id is active.provider_id
            and current.account_id == active.target_account_id
            and current.epoch == active.pending_epoch
            and active.target_generation is not None
            and current.generation == active.target_generation
        )
        if not baseline_matches and not finalized_target_matches:
            raise ValueError("Selection worker baseline is unavailable.")
        return active, current if baseline_matches else None

    def account_ids(
        self,
        operation: DueOperation,
    ) -> tuple[SidekickAccountId, ...]:
        """Resolve one phase-specific provider authority lock set."""
        active, _baseline = self.context(operation)
        if operation.kind is OperationKind.SELECTION_PREVALIDATE:
            return (active.target_account_id,)
        return tuple(
            sorted(
                {active.target_account_id}
                if active.baseline_account_id is None
                else {
                    active.baseline_account_id,
                    active.target_account_id,
                }
            )
        )

    def finish(
        self,
        operation: DueOperation,
        active: OpenSelectionOperation,
        result: WorkerResult,
    ) -> WorkerResult:
        """Return one correlated, saved-related, phase-safe result."""
        sanitized = self._sanitize(operation, active, result)
        if not self._commit_is_ambiguous(operation, sanitized):
            return sanitized
        return worker_failure(
            operation,
            WorkerOutcome.TRANSIENT_FAILURE,
            SelectionCode.SELECTION_RECOVERY_REQUIRED.value,
            self._clock,
        )

    @staticmethod
    def _baseline_matches(
        active: OpenSelectionOperation,
        current: FinalizedSelection | None,
    ) -> bool:
        return (
            active.baseline_account_id is None
            and active.baseline_epoch.value == 0
            and current is None
        ) or (
            current is not None
            and current.provider_id is active.provider_id
            and current.account_id == active.baseline_account_id
            and current.epoch == active.baseline_epoch
        )

    @staticmethod
    def _sanitize(
        operation: DueOperation,
        active: OpenSelectionOperation,
        result: WorkerResult,
    ) -> WorkerResult:
        if result.outcome not in {
            WorkerOutcome.SUCCEEDED,
            WorkerOutcome.NO_CHANGE,
        }:
            return result
        metadata = result.selection
        if (
            metadata is None
            or metadata.operation_id
            != operation.required_selection_operation_id
            or metadata.provider_id is not operation.provider_id
            or metadata.kind is not operation.kind
            or metadata.pending_epoch != active.pending_epoch
        ):
            return WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                finished_at=result.finished_at,
                failure_code=SelectionCode.AUTHORITY_PROOF_FAILED.value,
            )
        related_ids = {active.target_account_id}
        if active.baseline_account_id is not None:
            related_ids.add(active.baseline_account_id)
        if operation.kind is OperationKind.SELECTION_READBACK:
            if metadata.observed_account_id not in related_ids:
                return replace(
                    result,
                    selection=replace(
                        metadata,
                        observed_account_id=None,
                        observed_generation=None,
                    ),
                )
            return result
        if metadata.observed_account_id != active.target_account_id:
            return WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.TRANSIENT_FAILURE,
                finished_at=result.finished_at,
                failure_code=SelectionCode.AUTHORITY_PROOF_FAILED.value,
            )
        return result

    def _commit_is_ambiguous(
        self,
        operation: DueOperation,
        result: WorkerResult,
    ) -> bool:
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind is not OperationKind.SELECTION_COMMIT
            or result.outcome
            in {WorkerOutcome.SUCCEEDED, WorkerOutcome.NO_CHANGE}
        ):
            return False
        record = self._activations.load(operation.provider_id).active
        return (
            record is not None
            and record.operation_id
            == operation.required_selection_operation_id
            and (
                record.phase in _AMBIGUOUS_CLAUDE_PHASES
                or (
                    record.phase is ActivationPhase.RECONCILIATION_REQUIRED
                    and record.reconciliation_origin_phase
                    in _AMBIGUOUS_CLAUDE_PHASES
                )
            )
        )
