"""Managed Codex maintenance worker execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.codex.results import (
    codex_managed_worker_result,
)
from sidekick_usages.daemon.worker.ports import ManagedAccountService
from sidekick_usages.daemon.worker.runtime import (
    worker_failure,
    worker_success,
)
from sidekick_usages.heartbeat.service import heartbeat_exit_code
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.usage.models import activity_has_failure


class CodexManagedMaintenanceWorkerExecutor:
    """Maintain one managed Codex authority under its account lock."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        account_services: ManagedAccountService,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._account_services = account_services
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
        if operation.provider_id is not ProviderId.CODEX or not (
            scheduled or forced
        ):
            raise ValueError(
                "Worker operation is not managed Codex maintenance."
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
        if result.outcome is not CodexManagedOutcome.HEALTHY:
            return codex_managed_worker_result(
                operation,
                result,
                self._clock,
            )
        heartbeat = (
            self._account_services.heartbeat(
                operation.required_account_id,
                authority,
            )
            if scheduled
            else ()
        )
        metrics = self._account_services.collect_metrics(
            operation.required_account_id,
            authority,
        )
        heartbeat_code = heartbeat_exit_code(list(heartbeat))
        if heartbeat_code is ExitCode.MANUAL_ACTION:
            return worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                "codex_heartbeat_action_required",
                self._clock,
            )
        if metrics.failures:
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "codex_metrics_" + metrics.failures[0].kind.value,
                self._clock,
            )
        if any(activity_has_failure(item) for item in metrics.activities):
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "codex_activity_unavailable",
                self._clock,
            )
        if heartbeat_code is ExitCode.SYSTEM_ERROR:
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "codex_heartbeat_failed",
                self._clock,
            )
        return worker_success(operation, self._clock)
