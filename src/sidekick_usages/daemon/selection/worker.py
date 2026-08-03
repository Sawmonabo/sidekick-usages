"""Durable scheduler gateway for provider selection worker phases."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from typing import Protocol

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionAuthorityObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
    SelectionCode,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import SelectionWorkerMetadata
from sidekick_usages.daemon.selection.models import SelectionRequestError
from sidekick_usages.daemon.types.ports import OperationEventSink
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore

_WorkerKey = tuple[OperationId, OperationKind]
_RECOVERY_READBACK_PHASES = frozenset(
    {
        SelectionPhase.COMMITTING,
        SelectionPhase.AWAITING_READY,
        SelectionPhase.RECOVERING,
    }
)


class SelectionWorkerRecovery(Protocol):
    """Consume orphan readback completions through durable recovery."""

    def complete_readback(self, completion: SchedulerCompletion) -> None:
        """Apply one safe completed readback to the active journal."""

    def fail_readback(self, operation: DueOperation, code: str) -> None:
        """Retain one failed readback as recovery-required truth."""


@dataclass(slots=True)
class _SelectionWaiter:
    completion: SchedulerCompletion | None = None
    failure_code: SelectionCode | None = None


class SelectionWorkerGateway:
    """Submit selection phases to the existing durable worker scheduler."""

    def __init__(
        self,
        queue: OperationQueueStore,
        clock: Clock,
        wake: Callable[[], None],
    ) -> None:
        self._queue = queue
        self._clock = clock
        self._wake = wake
        self._condition = Condition()
        self._waiters: dict[_WorkerKey, _SelectionWaiter] = {}
        self._closed = False

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        """Prove one target through a bounded isolated worker."""
        self._require_prevalidation(operation, baseline)
        metadata = self._submit(
            operation.operation_id,
            operation.provider_id,
            operation.target_account_id,
            OperationKind.SELECTION_PREVALIDATE,
        )
        self._require_metadata(
            metadata,
            operation.operation_id,
            operation.provider_id,
            OperationKind.SELECTION_PREVALIDATE,
            operation.pending_epoch,
        )
        if (
            metadata.observed_account_id != operation.target_account_id
            or metadata.observed_generation is None
        ):
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=operation.provider_id,
            target_account_id=operation.target_account_id,
            target_generation=metadata.observed_generation,
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        """Commit one prepared target through a bounded isolated worker."""
        metadata = self._submit(
            prepared.operation_id,
            prepared.provider_id,
            prepared.target_account_id,
            OperationKind.SELECTION_COMMIT,
        )
        self._require_metadata(
            metadata,
            prepared.operation_id,
            prepared.provider_id,
            OperationKind.SELECTION_COMMIT,
            prepared.pending_epoch,
        )
        if (
            metadata.observed_account_id != prepared.target_account_id
            or metadata.observed_generation is None
        ):
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return AuthorityReadyProof(
            provider_id=prepared.provider_id,
            account_id=prepared.target_account_id,
            generation=metadata.observed_generation,
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> SelectionAuthorityObservation:
        """Read provider authority without inferring its durable relation."""
        metadata = self._submit(
            prepared.operation_id,
            prepared.provider_id,
            prepared.target_account_id,
            OperationKind.SELECTION_READBACK,
        )
        self._require_metadata(
            metadata,
            prepared.operation_id,
            prepared.provider_id,
            OperationKind.SELECTION_READBACK,
            prepared.pending_epoch,
        )
        return SelectionAuthorityObservation(
            provider_id=prepared.provider_id,
            account_id=metadata.observed_account_id,
            generation=metadata.observed_generation,
        )

    def enqueue_recovery_readback(
        self,
        operation: OpenSelectionOperation,
    ) -> DueOperation:
        """Coalesce one event-driven recovery readback without waiting."""
        if (
            operation.phase not in _RECOVERY_READBACK_PHASES
            or operation.prepared_generation is None
        ):
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        due = self._operation(
            operation.operation_id,
            operation.provider_id,
            operation.target_account_id,
            OperationKind.SELECTION_READBACK,
        )
        effective = self._queue.enqueue(due)
        self._require_effective(effective, due)
        self._wake()
        return effective

    def complete_waiter(self, completion: SchedulerCompletion) -> bool:
        """Deliver one selector-published completion to its live waiter."""
        key = (completion.operation_id, completion.operation_kind)
        with self._condition:
            waiter = self._waiters.get(key)
            if waiter is None:
                return False
            waiter.completion = completion
            self._condition.notify_all()
            return True

    def fail_waiter(self, operation: DueOperation, code: str) -> bool:
        """Deliver one pre-launch scheduler failure to its live waiter."""
        key = (operation.operation_id, operation.kind)
        with self._condition:
            waiter = self._waiters.get(key)
            if waiter is None:
                return False
            waiter.failure_code = _selection_code(code)
            self._condition.notify_all()
            return True

    def close(self) -> None:
        """Wake live waiters without deleting durable provider work."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def _submit(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        kind: OperationKind,
    ) -> SelectionWorkerMetadata:
        due = self._operation(
            operation_id,
            provider_id,
            account_id,
            kind,
        )
        key = (operation_id, kind)
        waiter = _SelectionWaiter()
        with self._condition:
            if self._closed:
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            if key in self._waiters:
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            self._waiters[key] = waiter
        try:
            effective = self._queue.enqueue(due)
            self._require_effective(effective, due)
            self._wake()
            return self._wait(key, waiter)
        finally:
            with self._condition:
                if self._waiters.get(key) is waiter:
                    self._waiters.pop(key)

    def _wait(
        self,
        key: _WorkerKey,
        waiter: _SelectionWaiter,
    ) -> SelectionWorkerMetadata:
        with self._condition:
            while (
                waiter.completion is None
                and waiter.failure_code is None
                and not self._closed
            ):
                self._condition.wait()
            if waiter.failure_code is not None:
                raise SelectionRequestError(waiter.failure_code)
            if self._closed or waiter.completion is None:
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            completion = waiter.completion
        if (
            completion.operation_id != key[0]
            or completion.operation_kind is not key[1]
            or completion.state is not None
            or completion.outcome
            not in {WorkerOutcome.SUCCEEDED, WorkerOutcome.NO_CHANGE}
            or completion.selection is None
        ):
            raise SelectionRequestError(
                _selection_code(completion.failure_code)
            )
        return completion.selection

    def _operation(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        kind: OperationKind,
    ) -> DueOperation:
        now = self._clock.now()
        return DueOperation(
            operation_id=operation_id,
            provider_id=provider_id,
            account_id=account_id,
            kind=kind,
            priority=OperationPriority.INTERACTIVE,
            state=OperationState.SCHEDULED,
            due_at=now,
            updated_at=now,
        )

    @staticmethod
    def _require_prevalidation(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> None:
        if operation.phase is not SelectionPhase.PREVALIDATING:
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        if baseline is None:
            valid = (
                operation.baseline_account_id is None
                and operation.baseline_epoch.value == 0
            )
        else:
            valid = (
                baseline.provider_id is operation.provider_id
                and baseline.account_id == operation.baseline_account_id
                and baseline.epoch == operation.baseline_epoch
            )
        if not valid:
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)

    @staticmethod
    def _require_effective(
        effective: DueOperation,
        requested: DueOperation,
    ) -> None:
        if (
            effective.operation_id != requested.operation_id
            or effective.provider_id is not requested.provider_id
            or effective.account_id != requested.account_id
            or effective.kind is not requested.kind
        ):
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )

    @staticmethod
    def _require_metadata(
        metadata: SelectionWorkerMetadata,
        operation_id: OperationId,
        provider_id: ProviderId,
        kind: OperationKind,
        pending_epoch: SelectionEpoch,
    ) -> None:
        if (
            metadata.operation_id != operation_id
            or metadata.provider_id is not provider_id
            or metadata.kind is not kind
            or metadata.pending_epoch != pending_epoch
        ):
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)


def _selection_code(code: str | None) -> SelectionCode:
    if code == "worker_timed_out":
        return SelectionCode.ACTIVE_OPERATION_TIMEOUT
    if code is not None:
        try:
            return SelectionCode(code)
        except ValueError:
            pass
    return SelectionCode.PROVIDER_UNAVAILABLE


class SelectionSchedulerSink:
    """Route selection worker completion without provider work."""

    def __init__(
        self,
        downstream: OperationEventSink,
        gateway: SelectionWorkerGateway,
        recovery: SelectionWorkerRecovery,
    ) -> None:
        self._downstream = downstream
        self._gateway = gateway
        self._recovery = recovery

    def started(self, operation: DueOperation) -> None:
        """Publish legacy progress but keep selection work internal."""
        if not operation.kind.is_selection_worker:
            self._downstream.started(operation)

    def completed(self, completion: SchedulerCompletion) -> None:
        """Route one selection result to its waiter or recovery owner."""
        if not completion.operation_kind.is_selection_worker:
            self._downstream.completed(completion)
            return
        if not self._gateway.complete_waiter(completion):
            self._recovery.complete_readback(completion)

    def failed(self, operation: DueOperation, code: str) -> None:
        """Route one launch failure without scheduling an automatic retry."""
        if not operation.kind.is_selection_worker:
            self._downstream.failed(operation, code)
            return
        if not self._gateway.fail_waiter(operation, code):
            self._recovery.fail_readback(operation, code)
