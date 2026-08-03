"""Isolated verified Claude selection execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    ProviderRuntimeState,
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
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.runtime import (
    managed_worker_result,
    worker_no_change,
    worker_success,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)

_CLAUDE_SELECTION_KINDS = frozenset(
    {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }
)

_CLAUDE_ACTIVATION_SELECTION_FAILURES = {
    ClaudeActivationFailure.INCOMPATIBLE: (
        WorkerOutcome.UNSUPPORTED,
        SelectionCode.UNSUPPORTED_PROVIDER_VERSION,
    ),
    ClaudeActivationFailure.NATIVE_CHANGED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION,
    ),
    ClaudeActivationFailure.NATIVE_UNAVAILABLE: (
        WorkerOutcome.TRANSIENT_FAILURE,
        SelectionCode.PROVIDER_UNAVAILABLE,
    ),
    ClaudeActivationFailure.RECONCILIATION_REQUIRED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION,
    ),
    ClaudeActivationFailure.SOURCE_UNAVAILABLE: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.AUTHORITY_PROOF_FAILED,
    ),
    ClaudeActivationFailure.STATE_CHANGED: (
        WorkerOutcome.TRANSIENT_FAILURE,
        SelectionCode.AUTHORITY_PROOF_FAILED,
    ),
    ClaudeActivationFailure.TARGET_UNAVAILABLE: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_REFRESH_REQUIRED,
    ),
    ClaudeActivationFailure.TIMED_OUT: (
        WorkerOutcome.TIMED_OUT,
        SelectionCode.ACTIVE_OPERATION_TIMEOUT,
    ),
}


def claude_selection_failure(
    operation: DueOperation,
    error: ClaudeActivationError,
    clock: Clock,
    *,
    recovery_required: bool = False,
) -> WorkerResult:
    """Map every Claude refusal into one closed selection worker result."""
    if recovery_required:
        outcome = WorkerOutcome.TRANSIENT_FAILURE
        code = SelectionCode.SELECTION_RECOVERY_REQUIRED
    elif isinstance(error.failure, ClaudeActivationGuardFailure):
        outcome = WorkerOutcome.ACTION_REQUIRED
        code = (
            SelectionCode.REMOTE_CONTROL_STATE_INCOMPATIBLE
            if error.failure
            is ClaudeActivationGuardFailure.REMOTE_CONTROL_INCOMPATIBLE
            else SelectionCode.SESSION_CONFIGURATION_REQUIRED
        )
    else:
        outcome, code = _CLAUDE_ACTIVATION_SELECTION_FAILURES[error.failure]
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=outcome,
        finished_at=clock.now(),
        failure_code=code.value,
    )


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
        if baseline is None:
            native = self._native_reconciliation.observe_selection(
                (operation.target_account_id,),
                authority,
            )
            admissible = native is not None and (
                native.runtime_state is ProviderRuntimeState.LOGGED_OUT
                or (
                    native.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                    and native.account_id == operation.target_account_id
                )
            )
            if not admissible:
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
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
            expected_target_generation=prepared.target_generation,
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
        operation: OpenSelectionOperation,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState | None:
        """Observe native truth against one open journal context."""
        self._require_readback_operation(operation)
        account_ids = (
            (operation.target_account_id,)
            if operation.baseline_account_id is None
            else (
                operation.baseline_account_id,
                operation.target_account_id,
            )
        )
        return self._native_reconciliation.observe_selection(
            account_ids,
            authority,
        )

    @staticmethod
    def _require_open_operation(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> None:
        valid_unselected = (
            baseline is None
            and operation.baseline_account_id is None
            and operation.baseline_epoch.value == 0
        )
        valid_selected = (
            baseline is not None
            and baseline.provider_id is ProviderId.CLAUDE
            and operation.baseline_account_id == baseline.account_id
            and operation.baseline_epoch == baseline.epoch
            and operation.target_account_id != baseline.account_id
        )
        if operation.provider_id is not ProviderId.CLAUDE or not (
            valid_unselected or valid_selected
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _require_prepared(prepared: PreparedSelection) -> None:
        if prepared.provider_id is not ProviderId.CLAUDE:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _require_readback_operation(
        operation: OpenSelectionOperation,
    ) -> None:
        valid_baseline = (
            operation.baseline_account_id is None
            and operation.baseline_epoch.value == 0
        ) or (
            operation.baseline_account_id is not None
            and operation.baseline_account_id != operation.target_account_id
        )
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or not valid_baseline
        ):
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
