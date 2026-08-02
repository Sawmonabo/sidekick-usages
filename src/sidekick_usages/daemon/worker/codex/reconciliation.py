"""Native Codex reconciliation worker execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import (
    DueOperation,
    NativeReconciliationResult,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.reconciliation import (
    CodexNativeReconciliationError,
    CodexNativeReconciliationService,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.runtime import (
    worker_failure,
    worker_no_change,
    worker_success,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)


class CodexNativeReconciliationWorkerExecutor:
    """Relate one effective Codex runtime observation without credentials."""

    def __init__(
        self,
        service: CodexNativeReconciliationService,
        observations: RuntimeAuthObservationStore,
        clock: Clock,
    ) -> None:
        self._service = service
        self._observations = observations
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Reconcile one exact provider-scoped observation."""
        authority.require(operation.provider_id)
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind is not OperationKind.RECONCILE_NATIVE
            or operation.account_id is not None
        ):
            raise ValueError(
                "Worker operation is not native Codex reconciliation."
            )
        observation = self._observations.load_native(ProviderId.CODEX)
        if observation is None:
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_observation_missing",
                self._clock,
            )
        try:
            reconciled = self._service.reconcile(observation, authority)
        except CodexNativeReconciliationError as error:
            return worker_failure(
                operation,
                (
                    WorkerOutcome.ACTION_REQUIRED
                    if error.action_required
                    else WorkerOutcome.TRANSIENT_FAILURE
                ),
                error.code,
                self._clock,
            )
        except ManagedStateConflictError:
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_state_changed",
                self._clock,
            )
        if self._observations.load_native(ProviderId.CODEX) != observation:
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_observation_changed",
                self._clock,
            )
        return self._selected_result(operation, reconciled)

    def _selected_result(
        self,
        operation: DueOperation,
        reconciled: NativeReconciliationResult,
    ) -> WorkerResult:
        selected = reconciled.selected
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.UNREADABLE
        ):
            return worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                "native_auth_unreadable",
                self._clock,
            )
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.UNSUPPORTED
        ):
            return worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                "native_auth_unsupported",
                self._clock,
            )
        if not reconciled.changed:
            return worker_no_change(
                operation,
                self._clock,
                reconciled.related_runtime_authority,
            )
        return worker_success(
            operation,
            self._clock,
            reconciled.related_runtime_authority,
        )
