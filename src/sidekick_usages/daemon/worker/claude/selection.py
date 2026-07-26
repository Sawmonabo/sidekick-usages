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
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.worker.runtime import (
    managed_worker_result,
    worker_success,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)


class ClaudeActivationWorkerExecutor:
    """Run one Claude activation without a supervisor credential exchange."""

    def __init__(
        self,
        service: ClaudeActivationService,
        clock: Clock,
    ) -> None:
        self._service = service
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one interactive Claude activation transaction."""
        authority.require(ProviderId.CLAUDE)
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind is not OperationKind.ACTIVATE
            or operation.priority is not OperationPriority.INTERACTIVE
        ):
            raise ValueError("Worker operation is not a Claude activation.")
        try:
            self._service.activate(
                operation.operation_id,
                operation.required_account_id,
                authority,
            )
        except ClaudeActivationError as error:
            return managed_worker_result(
                operation,
                self._clock,
                succeeded=False,
                action_required=error.action_required,
                timed_out=error.timed_out,
                failure_code=error.failure_code,
            )
        return worker_success(operation, self._clock)
