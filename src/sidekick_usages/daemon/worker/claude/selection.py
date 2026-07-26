"""Isolated verified Claude selection execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
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
        try:
            if operation.kind is OperationKind.ACTIVATE:
                self._activation.activate(
                    operation.operation_id,
                    operation.required_account_id,
                    authority,
                    allow_remote_control_disconnect=(
                        operation.allow_remote_control_disconnect
                    ),
                )
            elif operation.kind is OperationKind.RECONCILE:
                self._recovery.recover(
                    operation.required_account_id,
                    authority,
                )
            else:
                reconciled = self._native_reconciliation.reconcile(authority)
                if not reconciled.changed:
                    return worker_no_change(operation, self._clock)
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
        return worker_success(operation, self._clock)
