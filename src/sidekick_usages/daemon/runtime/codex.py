"""Durable Codex callback and runtime-observation dispatch."""

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    ProviderAuthObservation,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.worker.exchange import (
    SupervisorWorkerExchange,
    WorkerExchangeRegistry,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore


class CodexOperationDispatchError(RuntimeError):
    """Durable Codex operation dispatch is unavailable."""


class CallbackDispatchError(CodexOperationDispatchError):
    """A one-shot callback could not be correlated safely."""


class DurableCodexOperationDispatcher:
    """Persist and wake Codex callback and reconciliation work."""

    def __init__(
        self,
        queue: OperationQueueStore,
        observations: RuntimeAuthObservationStore,
        exchanges: WorkerExchangeRegistry,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float],
        wakeup: Callable[[], None],
    ) -> None:
        self._queue = queue
        self._observations = observations
        self._exchanges = exchanges
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._wakeup = wakeup

    def native_observation(self) -> ProviderAuthObservation | None:
        """Return the last durable effective native observation."""
        try:
            return self._observations.load_native(ProviderId.CODEX)
        except PersistenceError, ValueError:
            raise CodexOperationDispatchError from None

    def record_native(
        self,
        observation: ProviderAuthObservation,
    ) -> None:
        """Persist the newest effective native observation."""
        if observation.provider_id is not ProviderId.CODEX:
            raise ValueError("Runtime observation is not Codex.")
        try:
            self._observations.save_native(observation)
        except PersistenceError, ValueError:
            raise CodexOperationDispatchError from None

    def projection_observation(self) -> ProviderAuthObservation | None:
        """Return the last correlated Sidekick projection."""
        try:
            return self._observations.load_projection(ProviderId.CODEX)
        except PersistenceError, ValueError:
            raise CodexOperationDispatchError from None

    def record_projection(
        self,
        observation: ProviderAuthObservation,
    ) -> None:
        """Persist the newest correlated Sidekick projection."""
        if observation.provider_id is not ProviderId.CODEX:
            raise ValueError("Runtime observation is not Codex.")
        try:
            self._observations.save_projection(observation)
        except PersistenceError, ValueError:
            raise CodexOperationDispatchError from None

    def reconcile_native(
        self,
        observation: ProviderAuthObservation,
        priority: OperationPriority,
    ) -> DueOperation:
        """Persist one runtime observation and make reconciliation due."""
        if observation.provider_id is not ProviderId.CODEX:
            raise ValueError("Runtime observation is not Codex.")
        self.record_native(observation)
        now = self._wall_time()
        effective = self._queue.enqueue(
            DueOperation(
                operation_id=new_operation_id(),
                provider_id=ProviderId.CODEX,
                account_id=None,
                kind=OperationKind.RECONCILE_NATIVE,
                priority=priority,
                state=OperationState.SCHEDULED,
                due_at=now,
                updated_at=now,
            )
        )
        self._wakeup()
        return effective

    def dispatch(
        self,
        operation_id: OperationId,
        account_id: SidekickAccountId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> SupervisorWorkerExchange:
        """Persist and wake one exact Codex callback operation."""
        if response_deadline <= self._monotonic():
            raise CallbackDispatchError
        now = self._wall_time()
        exchange = self._exchanges.create(
            operation_id,
            instruction,
            response_deadline,
            completion_deadline,
        )
        try:
            effective = self._queue.enqueue(
                DueOperation(
                    operation_id=operation_id,
                    provider_id=ProviderId.CODEX,
                    account_id=account_id,
                    kind=OperationKind.CODEX_CALLBACK,
                    priority=OperationPriority.CODEX_CALLBACK,
                    state=OperationState.SCHEDULED,
                    due_at=now,
                    updated_at=now,
                )
            )
            if effective.operation_id != operation_id:
                raise CallbackDispatchError
        except Exception:
            self._exchanges.cancel(operation_id)
            raise
        self._wakeup()
        return exchange

    def cancel(self, operation_id: OperationId) -> None:
        """Close the exchange and remove work that has not launched."""
        try:
            operation = self._queue.find(operation_id)
            if (
                operation is not None
                and operation.kind is OperationKind.CODEX_CALLBACK
                and operation.state is OperationState.SCHEDULED
            ):
                with suppress(ManagedStateConflictError):
                    self._queue.remove(
                        operation_id,
                        expected_state=OperationState.SCHEDULED,
                    )
        finally:
            try:
                self._exchanges.cancel_if_awaiting_response(operation_id)
            finally:
                self._wakeup()
