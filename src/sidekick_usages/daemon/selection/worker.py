"""Durable scheduler gateway for provider selection worker phases."""

from collections.abc import Callable
from dataclasses import dataclass
from threading import Condition
from typing import Protocol

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId, SidekickAccountId
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
    ParticipantId,
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

_WorkerKey = tuple[ProviderId, OperationId, OperationKind]
_ParentKey = tuple[ProviderId, OperationId]
_BindKey = tuple[ProviderId, OperationId, ParticipantId, int]
_RECOVERY_READBACK_PHASES = frozenset(
    {
        SelectionPhase.PREVALIDATING,
        SelectionPhase.COMMITTING,
        SelectionPhase.AWAITING_READY,
        SelectionPhase.RECOVERING,
    }
)


class SelectionWorkerRecovery(Protocol):
    """Consume orphan readback completions through durable recovery."""

    def prove_commit(self, completion: SchedulerCompletion) -> None:
        """Persist one safe worker proof before protected fan-out."""

    def complete_readback(self, completion: SchedulerCompletion) -> None:
        """Apply one safe completed readback to the active journal."""

    def fail_readback(self, operation: DueOperation, code: str) -> None:
        """Retain one failed readback as recovery-required truth."""

    def worker_released(self, completion: SchedulerCompletion) -> None:
        """Continue recovery after one orphan phase releases authority."""


class SelectionWorkerExchangeOwner(Protocol):
    """Own a child-aware protected exchange without scheduling work."""

    def prepare(
        self,
        child_operation_id: OperationId,
        parent_operation_id: OperationId,
        provider_id: ProviderId,
        kind: OperationKind,
        target: tuple[ParticipantId, int] | None = None,
    ) -> bool:
        """Prepare an exact child exchange before durable enqueue."""

    def receive(self, child_operation_id: OperationId) -> None:
        """Receive the child projection before waiting for completion."""

    def complete(
        self,
        child_operation_id: OperationId,
        metadata: SelectionWorkerMetadata,
    ) -> None:
        """Publish a received projection after scheduler success."""

    def abort(self, child_operation_id: OperationId) -> None:
        """Abort and clear the exact child exchange."""


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
        *,
        exchange_owner: SelectionWorkerExchangeOwner | None = None,
        operation_id_factory: Callable[[], OperationId] = new_operation_id,
    ) -> None:
        self._queue = queue
        self._clock = clock
        self._wake = wake
        self._exchange_owner = exchange_owner
        self._operation_id_factory = operation_id_factory
        self._condition = Condition()
        self._waiters: dict[_WorkerKey, _SelectionWaiter] = {}
        self._readbacks: dict[_ParentKey, OperationId] = {}
        self._binds: dict[_BindKey, OperationId] = {}
        self._exchange_children: dict[OperationId, _WorkerKey] = {}
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
        if operation.phase not in _RECOVERY_READBACK_PHASES or (
            operation.phase is not SelectionPhase.PREVALIDATING
            and operation.prepared_generation is None
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
        with self._condition:
            effective = self._owned_readback(due)
        self._wake()
        return effective

    def bind_participant(
        self,
        operation: OpenSelectionOperation,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        """Enqueue one protected bind without blocking registration I/O."""
        if operation.target_generation is None or operation.phase not in {
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        }:
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        if operation.provider_id is ProviderId.CODEX:
            self.enqueue_recovery_readback(operation)
            return
        if operation.provider_id is not ProviderId.CLAUDE:
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        due = self._operation(
            operation.operation_id,
            operation.provider_id,
            operation.target_account_id,
            OperationKind.CLAUDE_PARTICIPANT_BIND,
        )
        target = participant_id, connection_generation
        key = operation.provider_id, operation.operation_id, *target
        with self._condition:
            child_id = self._binds.get(key)
            if child_id is not None and self._queue.find(child_id) is not None:
                return
        try:
            if not self._prepare_exchange(due, target):
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            effective = self._queue.enqueue(due)
            self._require_effective(effective, due)
            with self._condition:
                self._binds[key] = due.operation_id
            self._wake()
        except BaseException:
            self.abort_exchange(due.operation_id)
            raise

    def bind_finalized(
        self,
        finalized: FinalizedSelection,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        """Enqueue one exact finalized bind before participant admission."""
        if finalized.provider_id is not ProviderId.CLAUDE:
            raise SelectionRequestError(SelectionCode.AUTHORITY_PROOF_FAILED)
        due = self._finalized_bind_operation(finalized)
        target = participant_id, connection_generation
        try:
            if not self._prepare_exchange(due, target):
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            effective = self._queue.enqueue(due)
            self._require_effective(effective, due)
            self._wake()
        except BaseException:
            self.abort_exchange(due.operation_id)
            raise

    def exchange_started(self, operation: DueOperation) -> None:
        """Receive one scheduler-launched protected response."""
        with self._condition:
            owned = operation.operation_id in self._exchange_children
        if owned and self._exchange_owner is not None:
            try:
                self._exchange_owner.receive(operation.operation_id)
            except BaseException:
                self.abort_exchange(operation.operation_id)

    def complete_exchange(self, completion: SchedulerCompletion) -> None:
        """Publish one received projection after scheduler completion."""
        child_id = completion.worker_operation_id
        key = (
            completion.provider_id,
            completion.operation_id,
            completion.operation_kind,
        )
        if child_id is None:
            with self._condition:
                children = tuple(
                    current
                    for current, owner in self._exchange_children.items()
                    if owner == key
                )
            for current in children:
                self.abort_exchange(current)
            if children:
                raise SelectionRequestError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            return
        with self._condition:
            owner = self._exchange_children.get(child_id)
        if owner is None:
            return
        if owner != key:
            self.abort_exchange(child_id)
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        metadata = completion.selection
        if (
            completion.outcome
            not in {WorkerOutcome.SUCCEEDED, WorkerOutcome.NO_CHANGE}
            or metadata is None
            or self._exchange_owner is None
        ):
            self.abort_exchange(child_id)
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        try:
            self._exchange_owner.complete(child_id, metadata)
        except BaseException:
            self.abort_exchange(child_id)
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ) from None
        with self._condition:
            self._exchange_children.pop(child_id, None)
            self._binds = {
                key: current
                for key, current in self._binds.items()
                if current != child_id
            }

    def complete_waiter(self, completion: SchedulerCompletion) -> bool:
        """Deliver one selector-published completion to its live waiter."""
        key = (
            completion.provider_id,
            completion.operation_id,
            completion.operation_kind,
        )
        with self._condition:
            if completion.operation_kind is OperationKind.SELECTION_READBACK:
                readback_key = (
                    completion.provider_id,
                    completion.operation_id,
                )
                child_id = self._readbacks.get(readback_key)
                if child_id is not None and self._queue.find(child_id) is None:
                    self._readbacks.pop(readback_key)
            waiter = self._waiters.get(key)
            if waiter is None:
                return False
            waiter.completion = completion
            self._condition.notify_all()
            return True

    def fail_waiter(self, operation: DueOperation, code: str) -> bool:
        """Deliver one pre-launch scheduler failure to its live waiter."""
        key = (
            operation.provider_id,
            operation.required_selection_operation_id,
            operation.kind,
        )
        with self._condition:
            if operation.kind is OperationKind.SELECTION_READBACK:
                readback_key = (operation.provider_id, key[1])
                current = self._readbacks.get(readback_key)
                if current == operation.operation_id:
                    self._readbacks.pop(readback_key, None)
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
            self._readbacks.clear()
            self._binds.clear()
            children = tuple(self._exchange_children)
            self._exchange_children.clear()
            self._condition.notify_all()
        if self._exchange_owner is not None:
            for child_id in children:
                self._exchange_owner.abort(child_id)

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
        key = (provider_id, operation_id, kind)
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
            effective = (
                self._owned_readback(due)
                if kind is OperationKind.SELECTION_READBACK
                else None
            )
            self._waiters[key] = waiter
        exchange_owned = False
        try:
            if effective is None:
                exchange_owned = self._prepare_exchange(due)
            if effective is None:
                effective = self._queue.enqueue(due)
                self._require_effective(effective, due)
            self._wake()
            return self._wait(key, waiter)
        except BaseException:
            if exchange_owned and self._exchange_owner is not None:
                self.abort_exchange(due.operation_id)
            raise
        finally:
            with self._condition:
                if self._waiters.get(key) is waiter:
                    self._waiters.pop(key)

    def _prepare_exchange(
        self,
        operation: DueOperation,
        target: tuple[ParticipantId, int] | None = None,
    ) -> bool:
        owner = self._exchange_owner
        if owner is None:
            return False
        owned = owner.prepare(
            operation.operation_id,
            operation.required_selection_operation_id,
            operation.provider_id,
            operation.kind,
            target,
        )
        if owned:
            with self._condition:
                self._exchange_children[operation.operation_id] = (
                    operation.provider_id,
                    operation.required_selection_operation_id,
                    operation.kind,
                )
        return owned

    def abort_exchange(self, child_operation_id: OperationId) -> None:
        """Abort the exact child exchange after scheduler refusal."""
        with self._condition:
            self._exchange_children.pop(child_operation_id, None)
            self._binds = {
                key: current
                for key, current in self._binds.items()
                if current != child_operation_id
            }
        if self._exchange_owner is not None:
            self._exchange_owner.abort(child_operation_id)

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
            completion.provider_id is not key[0]
            or completion.operation_id != key[1]
            or completion.operation_kind is not key[2]
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
            operation_id=self._operation_id_factory(),
            selection_operation_id=operation_id,
            provider_id=provider_id,
            account_id=account_id,
            kind=kind,
            priority=OperationPriority.INTERACTIVE,
            state=OperationState.SCHEDULED,
            due_at=now,
            updated_at=now,
        )

    def _finalized_bind_operation(
        self,
        finalized: FinalizedSelection,
    ) -> DueOperation:
        child_id = self._operation_id_factory()
        now = self._clock.now()
        return DueOperation(
            operation_id=child_id,
            selection_operation_id=child_id,
            provider_id=finalized.provider_id,
            account_id=finalized.account_id,
            kind=OperationKind.CLAUDE_PARTICIPANT_BIND,
            priority=OperationPriority.INTERACTIVE,
            state=OperationState.SCHEDULED,
            due_at=now,
            updated_at=now,
        )

    def _owned_readback(self, due: DueOperation) -> DueOperation:
        """Return or create this gateway's exact parent READBACK child."""
        parent_id = due.required_selection_operation_id
        key = (due.provider_id, parent_id)
        child_id = self._readbacks.get(key)
        existing = None if child_id is None else self._queue.find(child_id)
        if existing is not None:
            self._require_same_parent(existing, due)
            return existing
        effective = self._queue.enqueue(due)
        self._require_effective(effective, due)
        self._readbacks[key] = effective.operation_id
        return effective

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
            or effective.selection_operation_id
            != requested.selection_operation_id
            or effective.provider_id is not requested.provider_id
            or effective.account_id != requested.account_id
            or effective.kind is not requested.kind
        ):
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )

    @staticmethod
    def _require_same_parent(
        effective: DueOperation,
        requested: DueOperation,
    ) -> None:
        if (
            effective.selection_operation_id
            != requested.selection_operation_id
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
        if operation.kind.is_selection_worker:
            self._gateway.exchange_started(operation)
        else:
            self._downstream.started(operation)

    def completed(self, completion: SchedulerCompletion) -> None:
        """Route one selection result to its waiter or recovery owner."""
        if not completion.operation_kind.is_selection_worker:
            self._downstream.completed(completion)
            return
        try:
            self._recovery.prove_commit(completion)
            self._gateway.complete_exchange(completion)
        except SelectionRequestError:
            failed = _failed_completion(completion)
            if not self._gateway.complete_waiter(failed):
                self._recovery.worker_released(failed)
            return
        if not self._gateway.complete_waiter(completion):
            self._recovery.worker_released(completion)

    def failed(self, operation: DueOperation, code: str) -> None:
        """Route one launch failure without scheduling an automatic retry."""
        if not operation.kind.is_selection_worker:
            self._downstream.failed(operation, code)
            return
        self._gateway.abort_exchange(operation.operation_id)
        if not self._gateway.fail_waiter(operation, code):
            self._recovery.fail_readback(operation, code)


def _failed_completion(
    completion: SchedulerCompletion,
) -> SchedulerCompletion:
    return SchedulerCompletion(
        provider_id=completion.provider_id,
        operation_id=completion.operation_id,
        operation_kind=completion.operation_kind,
        state=completion.state,
        outcome=WorkerOutcome.TRANSIENT_FAILURE,
        failure_code=SelectionCode.SELECTION_RECOVERY_REQUIRED.value,
        worker_operation_id=completion.worker_operation_id,
    )
