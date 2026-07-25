"""Isolated managed Claude maintenance execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.managed.maintenance.service import (
    ClaudeManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.worker.runtime import managed_worker_result
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)


class ClaudeManagedMaintenanceWorkerExecutor:
    """Maintain one managed Claude authority under its account lock."""

    def __init__(
        self,
        coordinator: ClaudeManagedAuthorityCoordinator,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: OperationAuthority,
    ) -> WorkerResult:
        """Run one scheduled maintenance or explicit forced refresh."""
        authority.require(operation.required_account_id)
        scheduled = (
            operation.kind is OperationKind.MAINTAIN
            and operation.priority is OperationPriority.SCHEDULED
        )
        forced = (
            operation.kind is OperationKind.REFRESH
            and operation.priority is OperationPriority.INTERACTIVE
        )
        if operation.provider_id is not ProviderId.CLAUDE or not (
            scheduled or forced
        ):
            raise ValueError(
                "Worker operation is not managed Claude maintenance."
            )
        result = (
            self._coordinator.refresh_with_authority(
                operation.required_account_id,
                authority,
            )
            if forced
            else self._coordinator.maintain_with_authority(
                operation.required_account_id,
                authority,
            )
        )
        return managed_worker_result(
            operation,
            self._clock,
            succeeded=result.outcome.succeeded,
            action_required=result.outcome.action_required,
            timed_out=result.outcome is ClaudeManagedOutcome.TIMED_OUT,
            failure_code=result.outcome.failure_code,
        )
