"""Forward-only active selection recovery before supervisor readiness."""

from dataclasses import replace
from threading import Lock

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.selection.ports import (
    FinalizedSelectionStore,
    SelectionJournal,
)
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.platform.peer import OperatingSystemProcessInspector
from sidekick_usages.platform.types import (
    ProcessIdentityInspector,
    ProcessLiveness,
)


class SelectionRecovery:
    """Reconcile durable operations from provider truth without rollback."""

    def __init__(
        self,
        selected: FinalizedSelectionStore,
        journal: SelectionJournal,
        participants: ParticipantRegistry,
        workers: SelectionWorkerGateway,
        clock: Clock,
        process_inspector: ProcessIdentityInspector | None = None,
    ) -> None:
        self._selected = selected
        self._journal = journal
        self._participants = participants
        self._workers = workers
        self._clock = clock
        self._process_inspector = (
            OperatingSystemProcessInspector()
            if process_inspector is None
            else process_inspector
        )
        self._provider_locks = {
            provider_id: Lock() for provider_id in ProviderId
        }

    def restore(self, provider_id: ProviderId) -> bool:
        """Restore one durable admission gate without provider I/O."""
        operation = self._journal.load(provider_id).active
        if operation is None:
            return False
        self._participants.restore_admission(
            provider_id,
            operation.operation_id,
            operation.pending_epoch,
            operation.target_account_id,
            operation.required_participant_ids,
        )
        return True

    def restore_all(self) -> tuple[ProviderId, ...]:
        """Restore every active gate before accepting session reconnects."""
        return tuple(
            provider_id
            for provider_id in ProviderId
            if self.restore(provider_id)
        )

    def resume(self, provider_id: ProviderId) -> None:
        """Finalize ready state or coalesce one provider readback."""
        with self._provider_locks[provider_id]:
            self._resume(provider_id)

    def _resume(self, provider_id: ProviderId) -> SelectionResult | None:
        operation = self._journal.load(provider_id).active
        if operation is None:
            return None
        self._participants.restore_admission(
            provider_id,
            operation.operation_id,
            operation.pending_epoch,
            operation.target_account_id,
            operation.required_participant_ids,
        )
        self._reconcile_unresolved(provider_id)
        if operation.phase in {
            SelectionPhase.PREVALIDATING,
            SelectionPhase.PREPARING,
            SelectionPhase.WAITING_OLD_TURNS,
        }:
            return self._recover_baseline(operation)
        if (
            operation.phase is SelectionPhase.AWAITING_READY
            and operation.target_generation is not None
            and operation.prepared_generation is not None
        ):
            prepared = self._prepared(operation)
            proof = self._target_proof(
                operation,
                operation.target_generation,
            )
            return self._recover_target(operation, prepared, proof)
        self._workers.enqueue_recovery_readback(operation)
        return None

    def enqueue_restored_readbacks(self) -> tuple[DueOperation, ...]:
        """Resume every restored operation after initial socket acceptance."""
        enqueued: list[DueOperation] = []
        for provider_id in ProviderId:
            with self._provider_locks[provider_id]:
                operation = self._journal.load(provider_id).active
                if operation is None:
                    continue
                if operation.phase in {
                    SelectionPhase.PREVALIDATING,
                    SelectionPhase.PREPARING,
                    SelectionPhase.WAITING_OLD_TURNS,
                }:
                    self._recover_baseline(operation)
                    continue
                if (
                    operation.phase is SelectionPhase.AWAITING_READY
                    and operation.target_generation is not None
                ):
                    self._resume(provider_id)
                    continue
                enqueued.append(
                    self._workers.enqueue_recovery_readback(operation)
                )
        return tuple(enqueued)

    def complete_readback(self, completion: SchedulerCompletion) -> None:
        """Apply one safe orphan readback completion to durable recovery."""
        provider_id = self._completion_provider(completion)
        if provider_id is None:
            return
        with self._provider_locks[provider_id]:
            operation = self._journal.load(provider_id).active
            if operation is None:
                return
            try:
                observation = self._observation(operation, completion)
                self._recover_committed(operation, observation)
            except Exception:
                self._publish_recovery_required(operation)

    def fail_readback(self, operation: DueOperation, code: str) -> None:
        """Retain one failed orphan readback as gated recovery truth."""
        del code
        if operation.kind is not OperationKind.SELECTION_READBACK:
            return
        with self._provider_locks[operation.provider_id]:
            active = self._journal.load(operation.provider_id).active
            if (
                active is not None
                and active.operation_id == operation.operation_id
            ):
                self._publish_recovery_required(active)

    def _recover_committed(
        self,
        operation: OpenSelectionOperation,
        observation: SelectionAuthorityObservation,
    ) -> SelectionResult:
        """Resolve one provider-commit boundary from provider truth."""
        if operation.prepared_generation is None:
            return self._recovery_required(operation)
        prepared = self._prepared(operation)
        if self._target_proven(operation, observation):
            if observation.generation is None:
                return self._recovery_required(operation)
            proof = self._target_proof(operation, observation.generation)
            return self._recover_target(operation, prepared, proof)
        baseline = self._selected.load(operation.provider_id)
        if (
            operation.phase is SelectionPhase.COMMITTING
            and self._baseline_proven(operation, baseline, observation)
        ):
            return self._recover_baseline(operation)
        return self._recovery_required(operation)

    def reconciled(self) -> bool:
        """Return whether every selection journal is durably closed."""
        return all(
            self._journal.load(provider_id).active is None
            for provider_id in ProviderId
        )

    def close(self) -> None:
        """Release live phase waiters without cancelling provider work."""
        self._workers.close()

    def _recover_target(
        self,
        operation: OpenSelectionOperation,
        prepared: PreparedSelection,
        proof: AuthorityReadyProof,
    ) -> SelectionResult:
        self._participants.prepare_target(operation.operation_id, proof)
        if operation.phase is SelectionPhase.RECOVERING:
            operation = self._journal.advance_with_required_additions(
                operation,
                replace(
                    operation,
                    phase=SelectionPhase.AWAITING_READY,
                    target_generation=proof.generation,
                    outcome_code=None,
                    updated_at=self._clock.now(),
                ),
            )
        elif operation.phase is SelectionPhase.COMMITTING:
            operation = self._journal.advance_with_required_additions(
                operation,
                replace(
                    operation,
                    phase=SelectionPhase.AWAITING_READY,
                    target_generation=proof.generation,
                    updated_at=self._clock.now(),
                ),
            )
        if operation.phase is not SelectionPhase.AWAITING_READY:
            return self._recovery_required(operation)
        if not self._participants.ready_resolved(operation.provider_id):
            return self._recovery_required(operation)
        snapshot = self._participants.seal_ready(operation.provider_id)
        result: SelectionResult | None = None
        try:
            operation = self._journal.advance_with_required_additions(
                operation,
                replace(
                    operation,
                    required_participant_ids=(
                        snapshot.required_participant_ids
                    ),
                    ready_participant_ids=snapshot.ready_participant_ids,
                    lost_after_commit_participant_ids=(
                        snapshot.confirmed_dead_participant_ids
                    ),
                    outcome_code=(
                        None
                        if not snapshot.confirmed_dead_participant_ids
                        else SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
                    ),
                    updated_at=self._clock.now(),
                ),
            )
            finalized = FinalizedSelection(
                provider_id=prepared.provider_id,
                account_id=prepared.target_account_id,
                epoch=prepared.pending_epoch,
                generation=proof.generation,
                finalized_at=self._clock.now(),
            )
            current = self._selected.load(prepared.provider_id)
            if not self._same_authority(current, finalized):
                baseline_matches = (
                    current is None
                    and operation.baseline_account_id is None
                    and operation.baseline_epoch == SelectionEpoch(0)
                ) or (
                    current is not None
                    and current.account_id == operation.baseline_account_id
                    and current.epoch == operation.baseline_epoch
                )
                if not baseline_matches:
                    self._participants.unseal(operation.provider_id)
                    return self._recovery_required(operation)
                self._selected.compare_and_swap(finalized, expected=current)
            outcome = (
                SelectionOutcome.READY
                if not snapshot.confirmed_dead_participant_ids
                else SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT
            )
            result = self._result(operation, outcome)
            self._journal.complete(result)
        except Exception:
            self._participants.unseal(operation.provider_id)
            if result is None or not self._completion_is_durable(result):
                return self._recovery_required(operation)
        self._participants.prune_confirmed_dead(
            operation.provider_id,
            snapshot.confirmed_dead_participant_ids,
        )
        self._participants.open_admission(
            operation.provider_id,
            operation.pending_epoch,
        )
        return result

    def _reconcile_unresolved(self, provider_id: ProviderId) -> None:
        for (
            participant_id,
            identity,
        ) in self._participants.unresolved_processes(provider_id):
            if (
                self._process_inspector.inspect(identity)
                is ProcessLiveness.DEAD
            ):
                self._participants.confirm_dead(participant_id, identity)

    def _recover_baseline(
        self,
        operation: OpenSelectionOperation,
    ) -> SelectionResult:
        operation = self._latest(operation)
        result = self._result(
            operation,
            SelectionOutcome.FAILED_OLD_EPOCH,
        )
        self._journal.complete(result)
        self._participants.reopen_baseline(operation.provider_id)
        return result

    def _recovery_required(
        self,
        operation: OpenSelectionOperation,
    ) -> SelectionResult:
        operation = self._latest(operation)
        if operation.phase is SelectionPhase.AWAITING_READY:
            snapshot = self._participants.snapshot(operation.provider_id)
            operation = self._journal.advance_with_required_additions(
                operation,
                replace(
                    operation,
                    required_participant_ids=(
                        snapshot.required_participant_ids
                    ),
                    ready_participant_ids=snapshot.ready_participant_ids,
                    lost_after_commit_participant_ids=(
                        snapshot.confirmed_dead_participant_ids
                    ),
                    outcome_code=(
                        None
                        if not snapshot.confirmed_dead_participant_ids
                        else SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
                    ),
                    updated_at=self._clock.now(),
                ),
            )
        result = self._result(
            operation,
            SelectionOutcome.RECOVERY_REQUIRED,
        )
        try:
            self._journal.complete(result)
        except Exception:
            operation = self._latest(operation)
            result = self._result(
                operation,
                SelectionOutcome.RECOVERY_REQUIRED,
            )
        self._participants.publish_status(
            operation.provider_id,
            operation.pending_epoch,
            SelectionCode.SELECTION_RECOVERY_REQUIRED,
        )
        return result

    def _completion_is_durable(self, result: SelectionResult) -> bool:
        document = self._journal.load(result.provider_id)
        return document.active is None and result in document.history

    def _latest(
        self,
        expected: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        current = self._journal.load(expected.provider_id).active
        if (
            current is None
            or current.operation_id != expected.operation_id
            or current.pending_epoch != expected.pending_epoch
        ):
            raise RuntimeError("selection_journal_changed")
        return current

    def _result(
        self,
        operation: OpenSelectionOperation,
        outcome: SelectionOutcome,
    ) -> SelectionResult:
        codes = {
            SelectionOutcome.READY: SelectionCode.SELECTION_SUCCEEDED,
            SelectionOutcome.FAILED_OLD_EPOCH: (
                SelectionCode.SELECTION_ROLLED_BACK
            ),
            SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT: (
                SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
            ),
            SelectionOutcome.RECOVERY_REQUIRED: (
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ),
        }
        return SelectionResult(
            operation_id=operation.operation_id,
            provider_id=operation.provider_id,
            target_account_id=operation.target_account_id,
            target_generation=operation.target_generation,
            epoch=(
                operation.baseline_epoch
                if outcome is SelectionOutcome.FAILED_OLD_EPOCH
                else operation.pending_epoch
            ),
            outcome=outcome,
            safe_code=codes[outcome],
            required_count=len(operation.required_participant_ids),
            ready_count=len(operation.ready_participant_ids),
            adopted_count=0,
            lost_count=len(operation.lost_after_commit_participant_ids),
            started_at=operation.started_at,
            completed_at=self._clock.now(),
        )

    @staticmethod
    def _target_proven(
        operation: OpenSelectionOperation,
        observation: SelectionAuthorityObservation,
    ) -> bool:
        return (
            observation.provider_id is operation.provider_id
            and observation.account_id == operation.target_account_id
            and observation.generation is not None
            and (
                operation.target_generation is None
                or operation.target_generation == observation.generation
            )
        )

    @staticmethod
    def _baseline_proven(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        observation: SelectionAuthorityObservation,
    ) -> bool:
        if baseline is None:
            return (
                operation.baseline_account_id is None
                and operation.baseline_epoch == SelectionEpoch(0)
                and observation.provider_id is operation.provider_id
                and observation.account_id is None
            )
        return (
            baseline.account_id == operation.baseline_account_id
            and baseline.epoch == operation.baseline_epoch
            and observation.provider_id is operation.provider_id
            and observation.account_id == baseline.account_id
            and observation.generation == baseline.generation
        )

    def _completion_provider(
        self,
        completion: SchedulerCompletion,
    ) -> ProviderId | None:
        if completion.selection is not None:
            return completion.selection.provider_id
        return next(
            (
                provider_id
                for provider_id in ProviderId
                if (active := self._journal.load(provider_id).active)
                is not None
                and active.operation_id == completion.operation_id
            ),
            None,
        )

    @staticmethod
    def _observation(
        operation: OpenSelectionOperation,
        completion: SchedulerCompletion,
    ) -> SelectionAuthorityObservation:
        metadata = completion.selection
        if (
            completion.operation_kind is not OperationKind.SELECTION_READBACK
            or completion.state is not None
            or completion.outcome
            not in {WorkerOutcome.SUCCEEDED, WorkerOutcome.NO_CHANGE}
            or metadata is None
            or metadata.operation_id != operation.operation_id
            or metadata.provider_id is not operation.provider_id
            or metadata.kind is not OperationKind.SELECTION_READBACK
            or metadata.pending_epoch != operation.pending_epoch
        ):
            raise ValueError("Selection readback completion is unrelated.")
        return SelectionAuthorityObservation(
            provider_id=metadata.provider_id,
            account_id=metadata.observed_account_id,
            generation=metadata.observed_generation,
        )

    @staticmethod
    def _prepared(operation: OpenSelectionOperation) -> PreparedSelection:
        generation = operation.prepared_generation
        if generation is None:
            raise ValueError("Selection preparation is unavailable.")
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=operation.provider_id,
            target_account_id=operation.target_account_id,
            target_generation=generation,
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    @staticmethod
    def _target_proof(
        operation: OpenSelectionOperation,
        generation: AuthorityGeneration,
    ) -> AuthorityReadyProof:
        return AuthorityReadyProof(
            provider_id=operation.provider_id,
            account_id=operation.target_account_id,
            generation=generation,
            epoch=operation.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )

    def _publish_recovery_required(
        self,
        operation: OpenSelectionOperation,
    ) -> None:
        try:
            self._recovery_required(operation)
        except Exception:
            self._participants.publish_status(
                operation.provider_id,
                operation.pending_epoch,
                SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )

    @staticmethod
    def _same_authority(
        current: FinalizedSelection | None,
        target: FinalizedSelection,
    ) -> bool:
        return (
            current is not None
            and current.provider_id is target.provider_id
            and current.account_id == target.account_id
            and current.epoch == target.epoch
            and current.generation == target.generation
        )
