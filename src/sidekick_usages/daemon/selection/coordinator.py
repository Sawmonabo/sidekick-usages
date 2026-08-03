"""Provider-neutral no-interruption selection coordinator."""

from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from threading import Event, Lock

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantReadyRequest,
    ParticipantRegistration,
    ParticipantSnapshot,
    SelectionRequestError,
    SelectionStatus,
    TurnAdmission,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.ports import (
    FinalizedSelectionStore,
    SelectionAuthorityAdapter,
    SelectionJournal,
)
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.platform.peer import OperatingSystemProcessInspector
from sidekick_usages.platform.types import (
    ProcessIdentityInspector,
    ProcessLiveness,
)

OLD_TURN_DRAIN_TIMEOUT_SECONDS = 120.0
PARTICIPANT_READY_TIMEOUT_SECONDS = 30.0


@dataclass(slots=True)
class _SelectionFlight:
    target_account_id: SidekickAccountId
    completed: Event = field(default_factory=Event)
    result: SelectionResult | None = None
    failure_code: SelectionCode | None = None
    recovery_pending: bool = False


class SelectionCoordinator:
    """Serialize one provider transition and preserve admitted work."""

    def __init__(
        self,
        selected: FinalizedSelectionStore,
        journal: SelectionJournal,
        participants: ParticipantRegistry,
        adapter: SelectionAuthorityAdapter,
        clock: Clock,
        *,
        process_inspector: ProcessIdentityInspector | None = None,
        old_turn_timeout_seconds: float = OLD_TURN_DRAIN_TIMEOUT_SECONDS,
        ready_timeout_seconds: float = PARTICIPANT_READY_TIMEOUT_SECONDS,
        resume_recovery: Callable[[ProviderId], None] | None = None,
    ) -> None:
        if min(old_turn_timeout_seconds, ready_timeout_seconds) <= 0:
            raise ValueError("Selection deadlines must be positive.")
        self._selected = selected
        self._journal = journal
        self._participants = participants
        self._adapter = adapter
        self._clock = clock
        self._old_turn_timeout = old_turn_timeout_seconds
        self._ready_timeout = ready_timeout_seconds
        self._resume_recovery = resume_recovery
        self._process_inspector = (
            OperatingSystemProcessInspector()
            if process_inspector is None
            else process_inspector
        )
        self._flight_lock = Lock()
        self._flights: dict[ProviderId, _SelectionFlight] = {}

    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
    ) -> ParticipantRegistration:
        """Durably register one exact kernel-proven participant."""

        def persist_required(pending_epoch: SelectionEpoch) -> None:
            active = self._journal.load(manifest.provider_id).active
            if active is None or active.pending_epoch != pending_epoch:
                raise RuntimeError("selection_journal_unavailable")
            self._journal.add_required(
                manifest.provider_id,
                active.operation_id,
                active.pending_epoch,
                manifest.participant_id,
                updated_at=self._clock.now(),
            )

        registration = self._participants.register(
            manifest,
            peer,
            persist_required=persist_required,
        )
        if registration.pending_epoch is not None:
            self._resume_if_recovered(manifest.provider_id)
        return registration

    def subscribe(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> Generator[ParticipantNotice]:
        """Subscribe one exactly authenticated participant to notices."""
        self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        return self._participants.subscribe(request_id, request)

    def begin_turn(
        self,
        request: TurnBeginRequest,
        peer: ProcessIdentity,
    ) -> TurnAdmission:
        """Admit or queue one turn for its exact registered process."""
        self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        return self._participants.begin_turn(request)

    def end_turn(
        self,
        request: TurnEndRequest,
        peer: ProcessIdentity,
    ) -> None:
        """End one exact turn and wake a waiting selection."""
        provider_id = self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        self._participants.end_turn(request)
        self._resume_if_recovered(provider_id)

    def ready_request(
        self,
        request: ParticipantReadyRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Record exact readiness and resume crash recovery if needed."""
        provider_id = self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        self._participants.ready_request(request)
        self._resume_if_recovered(provider_id)

    def adopt_request(
        self,
        request: ParticipantAdoptionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Record first-real-turn adoption from the exact process."""
        self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        self._participants.adopt_request(request)

    def cancel_subscription(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Disconnect only the exact authenticated participant stream."""
        provider_id = self._participants.require_peer(
            request.participant_id,
            request.connection_generation,
            peer,
        )
        self._participants.cancel_subscription(request_id)
        self._participants.disconnect(
            request.participant_id,
            request.connection_generation,
        )
        self.reconcile_disconnected(provider_id)
        self._resume_if_recovered(provider_id)

    def status(self, provider_id: ProviderId) -> SelectionStatus:
        """Return finalized and active selection state for one provider."""
        finalized = self._selected.load(provider_id)
        active = self._journal.load(provider_id).active
        snapshot = self._participants.snapshot(provider_id)
        return SelectionStatus(
            provider_id=provider_id,
            operation_id=None if active is None else active.operation_id,
            finalized_account_id=(
                None if finalized is None else finalized.account_id
            ),
            finalized_epoch=None if finalized is None else finalized.epoch,
            target_account_id=(
                None if active is None else active.target_account_id
            ),
            pending_epoch=None if active is None else active.pending_epoch,
            phase=None if active is None else active.phase,
            code=None if active is None else active.outcome_code,
            registered_count=snapshot.registered_count,
            reachable_count=snapshot.reachable_count,
            required_count=len(snapshot.required_participant_ids),
            ready_count=len(snapshot.ready_participant_ids),
            adopted_count=snapshot.adopted_count,
            unreachable_count=len(snapshot.unreachable_participant_ids),
            active_turn_count=snapshot.active_turn_count,
            queued_turn_count=snapshot.queued_turn_count,
        )

    def select(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> SelectionResult:
        """Select one saved target through the exact durable phase order."""
        flight, owner = self._join_flight(provider_id, target_account_id)
        if not owner:
            flight.completed.wait()
            if flight.result is not None:
                return flight.result
            raise SelectionRequestError(
                flight.failure_code or SelectionCode.PROVIDER_UNAVAILABLE
            )
        try:
            flight.result = self._select_or_resume(
                operation_id,
                provider_id,
                target_account_id,
            )
        except SelectionRequestError as error:
            flight.failure_code = error.code
            raise
        except Exception:
            flight.failure_code = SelectionCode.PROVIDER_UNAVAILABLE
            raise
        finally:
            try:
                self._handoff_recovery(
                    flight,
                    provider_id,
                )
            finally:
                with self._flight_lock:
                    if self._flights.get(provider_id) is flight:
                        self._flights.pop(provider_id)
                flight.completed.set()
        if flight.result is None:
            raise RuntimeError("Selection flight completed without a result.")
        return flight.result

    def _select_or_resume(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> SelectionResult:
        """Run a new selection or resume its exact durable replay."""
        active = self._journal.load(provider_id).active
        if active is not None:
            if active.target_account_id != target_account_id:
                raise SelectionRequestError(
                    SelectionCode.UNCOORDINATED_AUTH_MUTATION
                )
            if self._resume_recovery is not None:
                self._resume_recovery(provider_id)
                recovered = self._completed_replay(active)
                if recovered is not None:
                    return recovered
            raise SelectionRequestError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            )
        baseline = self._selected.load(provider_id)
        if baseline is not None and baseline.account_id == target_account_id:
            raise SelectionRequestError(SelectionCode.ALREADY_SELECTED)
        return self._select(operation_id, provider_id, target_account_id)

    def _handoff_recovery(
        self,
        flight: _SelectionFlight,
        provider_id: ProviderId,
    ) -> None:
        """Consume one participant event suppressed by a live flight."""
        if (
            flight.result is None
            or flight.result.outcome is not SelectionOutcome.RECOVERY_REQUIRED
            or self._resume_recovery is None
        ):
            return
        try:
            self._resume_recovery(provider_id)
        except Exception:
            # Durable recovery-required truth remains the control result.
            return

    def _join_flight(
        self,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> tuple[_SelectionFlight, bool]:
        with self._flight_lock:
            current = self._flights.get(provider_id)
            if current is not None:
                if current.target_account_id != target_account_id:
                    raise SelectionRequestError(
                        SelectionCode.UNCOORDINATED_AUTH_MUTATION
                    )
                return current, False
            flight = _SelectionFlight(target_account_id)
            self._flights[provider_id] = flight
            return flight, True

    def _resume_if_recovered(self, provider_id: ProviderId) -> None:
        with self._flight_lock:
            flight = self._flights.get(provider_id)
            if flight is not None:
                flight.recovery_pending = True
                return
        if self._resume_recovery is not None:
            self._resume_recovery(provider_id)

    def _select(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> SelectionResult:
        baseline = self._selected.load(provider_id)
        baseline_epoch = (
            SelectionEpoch(0) if baseline is None else baseline.epoch
        )
        now = self._clock.now()
        operation = self._journal.begin(
            OpenSelectionOperation(
                operation_id=operation_id,
                provider_id=provider_id,
                baseline_account_id=(
                    None if baseline is None else baseline.account_id
                ),
                target_account_id=target_account_id,
                prepared_generation=None,
                target_generation=None,
                baseline_epoch=baseline_epoch,
                pending_epoch=baseline_epoch.next(),
                phase=SelectionPhase.PREVALIDATING,
                required_participant_ids=(),
                ready_participant_ids=(),
                lost_after_commit_participant_ids=(),
                confirmed_dead_before_commit_count=0,
                confirmed_dead_before_commit_code=None,
                outcome_code=None,
                started_at=now,
                updated_at=now,
            )
        )
        try:
            prepared = self._adapter.prevalidate(operation, baseline)
            self._require_prepared(operation, prepared)
        except SelectionRequestError:
            self._fail_old_epoch(operation)
            raise
        except Exception:
            return self._fail_old_epoch(operation)
        operation = self._prepare_gate(operation, prepared)
        commit_state = self._enter_commit(operation)
        if isinstance(commit_state, SelectionResult):
            return commit_state
        operation = commit_state

        try:
            proof = self._adapter.commit(prepared)
            self._require_proof(prepared, proof)
        except Exception:
            return self._recovering(operation)
        self._participants.prepare_target(proof)
        snapshot = self._participants.snapshot(provider_id)
        operation = self._persist(
            operation,
            replace(
                operation,
                phase=SelectionPhase.AWAITING_READY,
                target_generation=proof.generation,
                required_participant_ids=snapshot.required_participant_ids,
                updated_at=self._clock.now(),
            ),
        )
        return self._finalize_ready(operation, prepared, baseline)

    def _prepare_gate(
        self,
        operation: OpenSelectionOperation,
        prepared: PreparedSelection,
    ) -> OpenSelectionOperation:
        operation = self._persist(
            operation,
            replace(
                operation,
                phase=SelectionPhase.PREPARING,
                prepared_generation=prepared.target_generation,
                updated_at=self._clock.now(),
            ),
        )
        snapshot = self._participants.close_admission(
            operation.provider_id,
            prepared.pending_epoch,
        )
        operation = self._persist(
            operation,
            replace(
                operation,
                required_participant_ids=snapshot.required_participant_ids,
                updated_at=self._clock.now(),
            ),
        )
        return self._persist(
            operation,
            replace(
                operation,
                phase=SelectionPhase.WAITING_OLD_TURNS,
                updated_at=self._clock.now(),
            ),
        )

    def _enter_commit(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation | SelectionResult:
        provider_id = operation.provider_id
        if not self._participants.wait_for_old_turns(
            provider_id,
            self._old_turn_timeout,
        ):
            self._reconcile_unresolved(provider_id)
            if self._participants.snapshot(provider_id).active_turn_count:
                return self._fail_old_epoch(operation)
        self.reconcile_disconnected(provider_id)
        if self._participants.snapshot(
            provider_id
        ).unreachable_participant_ids:
            return self._fail_old_epoch(operation)
        self._participants.seal_precommit(provider_id)
        try:
            operation = self._capture_precommit_participants(operation)
            operation = self._persist(
                operation,
                replace(
                    operation,
                    phase=SelectionPhase.COMMITTING,
                    updated_at=self._clock.now(),
                ),
            )
        finally:
            self._participants.unseal(provider_id)
        return operation

    def _finalize_ready(
        self,
        operation: OpenSelectionOperation,
        prepared: PreparedSelection,
        baseline: FinalizedSelection | None,
    ) -> SelectionResult:
        provider_id = operation.provider_id
        if not self._participants.wait_for_ready(
            provider_id,
            self._ready_timeout,
        ):
            self._reconcile_unresolved(provider_id)
            if not self._participants.ready_resolved(provider_id):
                return self._recovering(operation)
        snapshot = self._participants.seal_ready(provider_id)
        snapshot_persisted = False
        try:
            try:
                operation = self._persist(
                    operation,
                    self._snapshot_operation(operation, snapshot),
                )
                snapshot_persisted = True
            finally:
                if not snapshot_persisted:
                    self._participants.unseal(provider_id)
        except PersistenceError:
            return self._recovering(operation)
        finalized = FinalizedSelection(
            provider_id=provider_id,
            account_id=prepared.target_account_id,
            epoch=prepared.pending_epoch,
            generation=self._require_target_generation(operation),
            finalized_at=self._clock.now(),
        )
        try:
            self._selected.compare_and_swap(finalized, expected=baseline)
        except Exception:
            self._participants.unseal(provider_id)
            return self._recovering(operation)
        outcome = (
            SelectionOutcome.READY
            if not snapshot.confirmed_dead_participant_ids
            else SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT
        )
        result = self._result(operation, outcome)
        try:
            self._journal.complete(result)
        except Exception:
            if not self._completion_is_durable(result):
                self._participants.unseal(provider_id)
                return self._recovering(operation)
        self._participants.prune_confirmed_dead(
            provider_id,
            snapshot.confirmed_dead_participant_ids,
        )
        self._participants.open_admission(
            provider_id,
            prepared.pending_epoch,
        )
        return result

    def _capture_precommit_participants(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        operation = self._latest(operation)
        snapshot = self._participants.snapshot(operation.provider_id)
        confirmed_dead = set(snapshot.confirmed_dead_participant_ids)
        required = tuple(
            participant_id
            for participant_id in snapshot.required_participant_ids
            if participant_id not in confirmed_dead
        )
        replacement = replace(
            operation,
            required_participant_ids=required,
            confirmed_dead_before_commit_count=(
                operation.confirmed_dead_before_commit_count
                + len(confirmed_dead)
            ),
            confirmed_dead_before_commit_code=(
                None
                if not confirmed_dead
                else SelectionCode.PARTICIPANT_CONFIRMED_DEAD
            ),
            updated_at=self._clock.now(),
        )
        if replacement != operation:
            operation = self._journal.compare_and_swap(
                operation,
                replacement,
            )
        self._participants.prune_confirmed_dead(
            operation.provider_id,
            tuple(sorted(confirmed_dead)),
        )
        return operation

    def reconcile_disconnected(self, provider_id: ProviderId) -> None:
        """Confirm only exact dead process-start identities without signals."""
        self._confirm_dead(
            self._participants.unreachable_processes(provider_id)
        )

    def _reconcile_unresolved(self, provider_id: ProviderId) -> None:
        self._confirm_dead(
            self._participants.unresolved_processes(provider_id)
        )

    def _confirm_dead(
        self,
        participants: tuple[tuple[ParticipantId, ProcessIdentity], ...],
    ) -> None:
        for participant_id, identity in participants:
            if (
                self._process_inspector.inspect(identity)
                is ProcessLiveness.DEAD
            ):
                self._participants.confirm_dead(participant_id, identity)

    def _fail_old_epoch(
        self,
        operation: OpenSelectionOperation,
    ) -> SelectionResult:
        operation = self._latest(operation)
        result = self._result(operation, SelectionOutcome.FAILED_OLD_EPOCH)
        self._journal.complete(result)
        self._participants.reopen_baseline(operation.provider_id)
        return result

    def _recovering(
        self,
        operation: OpenSelectionOperation,
    ) -> SelectionResult:
        operation = self._latest(operation)
        if operation.phase is SelectionPhase.AWAITING_READY:
            snapshot = self._participants.snapshot(operation.provider_id)
            operation = self._persist(
                operation,
                self._snapshot_operation(operation, snapshot),
            )
        result = self._result(operation, SelectionOutcome.RECOVERY_REQUIRED)
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

    def _snapshot_operation(
        self,
        operation: OpenSelectionOperation,
        snapshot: ParticipantSnapshot,
    ) -> OpenSelectionOperation:
        return replace(
            operation,
            required_participant_ids=snapshot.required_participant_ids,
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
        )

    def _completed_replay(
        self,
        operation: OpenSelectionOperation,
    ) -> SelectionResult | None:
        document = self._journal.load(operation.provider_id)
        result = next(
            (
                item
                for item in reversed(document.history)
                if item.operation_id == operation.operation_id
            ),
            None,
        )
        if (
            document.active is not None
            or result is None
            or result.target_account_id != operation.target_account_id
        ):
            return None
        finalized = self._selected.load(operation.provider_id)
        if (
            result.outcome
            in {
                SelectionOutcome.READY,
                SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
            }
            and finalized is not None
            and result.target_generation is not None
            and (
                finalized.account_id,
                finalized.epoch,
                finalized.generation,
            )
            == (
                result.target_account_id,
                result.epoch,
                result.target_generation,
            )
        ):
            return result
        if result.outcome is SelectionOutcome.FAILED_OLD_EPOCH and (
            finalized is None
            if operation.baseline_account_id is None
            else finalized is not None
            and (finalized.account_id, finalized.epoch)
            == (operation.baseline_account_id, operation.baseline_epoch)
        ):
            return result
        return None

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

    def _persist(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        return self._journal.advance_with_required_additions(
            expected,
            replacement,
        )

    def _result(
        self,
        operation: OpenSelectionOperation,
        outcome: SelectionOutcome,
    ) -> SelectionResult:
        code = {
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
        }[outcome]
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
            safe_code=code,
            required_count=len(operation.required_participant_ids),
            ready_count=len(operation.ready_participant_ids),
            adopted_count=0,
            lost_count=len(operation.lost_after_commit_participant_ids),
            started_at=operation.started_at,
            completed_at=self._clock.now(),
        )

    @staticmethod
    def _require_prepared(
        operation: OpenSelectionOperation,
        prepared: PreparedSelection,
    ) -> None:
        if (
            prepared.operation_id != operation.operation_id
            or prepared.provider_id is not operation.provider_id
            or prepared.target_account_id != operation.target_account_id
            or prepared.baseline_epoch != operation.baseline_epoch
            or prepared.pending_epoch != operation.pending_epoch
        ):
            raise ValueError("Prepared selection is unrelated.")

    @staticmethod
    def _require_proof(
        prepared: PreparedSelection,
        proof: AuthorityReadyProof,
    ) -> None:
        if (
            proof.provider_id is not prepared.provider_id
            or proof.account_id != prepared.target_account_id
            or proof.epoch != prepared.pending_epoch
        ):
            raise ValueError("Authority proof is unrelated.")

    @staticmethod
    def _require_target_generation(
        operation: OpenSelectionOperation,
    ) -> AuthorityGeneration:
        generation = operation.target_generation
        if generation is None:
            raise RuntimeError("Runtime authority generation is unavailable.")
        return generation
