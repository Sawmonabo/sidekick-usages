"""Provider-neutral dispatch for one-shot isolated callbacks."""

from collections.abc import Callable
from contextlib import suppress
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.worker.exchange import (
    CallbackExchangeRegistry,
    SupervisorCallbackExchange,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore


class CallbackDispatchError(RuntimeError):
    """A one-shot callback could not be correlated safely."""


class DurableCallbackDispatcher:
    """Persist, wake, and cancel callback work without provider knowledge."""

    def __init__(
        self,
        queue: OperationQueueStore,
        exchanges: CallbackExchangeRegistry,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float],
        wakeup: Callable[[], None],
    ) -> None:
        self._queue = queue
        self._exchanges = exchanges
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._wakeup = wakeup

    def dispatch(
        self,
        operation_id: OperationId,
        account_id: SidekickAccountId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> SupervisorCallbackExchange:
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
