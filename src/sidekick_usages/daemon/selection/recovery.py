"""Forward-only active selection recovery before supervisor readiness."""

from dataclasses import replace
from threading import Lock

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.ports import (
    FinalizedSelectionStore,
    SelectionAuthorityAdapter,
    SelectionJournal,
)
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
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
        adapter: SelectionAuthorityAdapter,
        clock: Clock,
        process_inspector: ProcessIdentityInspector | None = None,
    ) -> None:
        self._selected = selected
        self._journal = journal
        self._participants = participants
        self._adapter = adapter
        self._clock = clock
        self._process_inspector = (
            OperatingSystemProcessInspector()
            if process_inspector is None
            else process_inspector
        )
        self._provider_locks = {
            provider_id: Lock() for provider_id in ProviderId
        }

    def recover(self, provider_id: ProviderId) -> SelectionResult | None:
        """Recover one active journal before provider admission is ready."""
        with self._provider_locks[provider_id]:
            return self._recover(provider_id)

    def _recover(self, provider_id: ProviderId) -> SelectionResult | None:
        operation = self._journal.load(provider_id).active
        if operation is None:
            return None
        self._participants.restore_admission(
            provider_id,
            operation.pending_epoch,
            operation.required_participant_ids,
        )
        self._reconcile_unresolved(provider_id)
        if operation.phase in {
            SelectionPhase.PREVALIDATING,
            SelectionPhase.PREPARING,
            SelectionPhase.WAITING_OLD_TURNS,
        }:
            return self._recover_baseline(operation)
        prepared = (
            None
            if operation.target_generation is None
            else PreparedSelection(
                operation_id=operation.operation_id,
                provider_id=operation.provider_id,
                target_account_id=operation.target_account_id,
                target_generation=operation.target_generation,
                baseline_epoch=operation.baseline_epoch,
                pending_epoch=operation.pending_epoch,
            )
        )
        proof = None if prepared is None else self._adapter.readback(prepared)
        if prepared is None or proof is None:
            return self._recovery_required(operation)
        if self._target_proven(prepared, proof):
            return self._recover_target(operation, prepared, proof)
        baseline = self._selected.load(provider_id)
        if (
            operation.phase is SelectionPhase.COMMITTING
            and self._baseline_proven(operation, baseline, proof)
        ):
            return self._recover_baseline(operation)
        return self._recovery_required(operation)

    def recover_all(self) -> tuple[SelectionResult, ...]:
        """Reconcile every provider journal without waiting for reconnects."""
        return tuple(
            result
            for provider_id in ProviderId
            if (result := self.recover(provider_id)) is not None
        )

    def reconciled(self) -> bool:
        """Return whether every selection journal is durably closed."""
        return all(
            self._journal.load(provider_id).active is None
            for provider_id in ProviderId
        )

    def _recover_target(
        self,
        operation: OpenSelectionOperation,
        prepared: PreparedSelection,
        proof: AuthorityReadyProof,
    ) -> SelectionResult:
        self._participants.prepare_target(proof)
        if operation.phase is SelectionPhase.RECOVERING:
            operation = self._journal.advance_with_required_additions(
                operation,
                replace(
                    operation,
                    phase=SelectionPhase.AWAITING_READY,
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
                    updated_at=self._clock.now(),
                ),
            )
        if operation.phase is not SelectionPhase.AWAITING_READY:
            return self._recovery_required(operation)
        if not self._participants.ready_resolved(operation.provider_id):
            return self._recovery_required(operation)
        snapshot = self._participants.seal_ready(operation.provider_id)
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
                generation=prepared.target_generation,
                finalized_at=self._clock.now(),
            )
            current = self._selected.load(prepared.provider_id)
            if not self._same_authority(current, finalized):
                baseline_matches = (
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
            raise
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
        self._journal.complete(result)
        self._participants.publish_status(
            operation.provider_id,
            operation.pending_epoch,
            SelectionCode.SELECTION_RECOVERY_REQUIRED,
        )
        return result

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
        prepared: PreparedSelection,
        proof: AuthorityReadyProof,
    ) -> bool:
        return (
            proof.provider_id is prepared.provider_id
            and proof.account_id == prepared.target_account_id
            and proof.generation == prepared.target_generation
            and proof.epoch == prepared.pending_epoch
        )

    @staticmethod
    def _baseline_proven(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        proof: AuthorityReadyProof,
    ) -> bool:
        return (
            baseline is not None
            and baseline.account_id == operation.baseline_account_id
            and baseline.epoch == operation.baseline_epoch
            and proof.provider_id is operation.provider_id
            and proof.account_id == baseline.account_id
            and proof.generation == baseline.generation
            and proof.epoch == baseline.epoch
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
