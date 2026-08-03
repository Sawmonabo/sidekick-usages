"""Concurrent participant selection coordination tests."""

from collections.abc import Generator
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    OperationState,
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import SelectionWorkerMetadata
from sidekick_usages.daemon.selection.coordinator import (
    SelectionCoordinator,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRegistration,
    ParticipantRequestError,
    SelectionRequestError,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import (
    MAX_RETAINED_PARTICIPANT_NOTICES,
    ParticipantRegistry,
)
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.errors import ReplaceFailedError
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.platform.types import ProcessLiveness
from tests.support.persistence import (
    make_application_paths,
    seed_finalized_selections,
)
from tests.support.time import REFERENCE_TIME, FixedClock

PROVIDER_ID = ProviderId.CLAUDE
OPERATION_ID = OperationId("52bbb5ad-b457-41ce-90ca-c52919051f8e")
REPLAY_OPERATION_ID = OperationId("25c10782-ae80-4f9b-a6fa-65bd029a4934")
CONFLICT_OPERATION_ID = OperationId("cc6af35a-20b1-43e3-8f61-69521210de6a")
TARGET_ACCOUNT_ID = SidekickAccountId("32b53411-10ef-4689-a5ea-6ec9daec4e2b")
CONFLICT_ACCOUNT_ID = SidekickAccountId("e45c490a-18cd-45a6-aee6-6de58cbc7b5a")
PARTICIPANT_A = ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337")
PARTICIPANT_B = ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc")
PARTICIPANT_C = ParticipantId("e9b1b25c-fae6-4998-a135-719ad3257972")
PARTICIPANT_D = ParticipantId("ed416c76-24b3-4934-9486-f910376e3a71")
TURN_A = TurnId("a99915d0-5d3a-497a-9696-c227d0903709")
TURN_B = TurnId("0168fe43-5c83-46f4-b8ef-6cc047293957")
INITIAL_PARTICIPANT_COUNT = 2
TEST_SELECTION_TIMEOUT_SECONDS = 0.05


def _baseline_selection() -> FinalizedSelection:
    return FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=SidekickAccountId("4d95b1fb-1ba4-4837-b6b7-452cd8aff462"),
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-baseline-7"),
        finalized_at=REFERENCE_TIME,
    )


class _SelectionAdapter:
    def __init__(self) -> None:
        self.prevalidation_started = Event()
        self.allow_prevalidation = Event()
        self.committed = Event()

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        self.prevalidation_started.set()
        if not self.allow_prevalidation.wait(1):
            raise RuntimeError("Synthetic prevalidation gate timed out.")
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=operation.provider_id,
            target_account_id=operation.target_account_id,
            target_generation=AuthorityGeneration("generation-source-8"),
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        self.committed.set()
        return self._proof(prepared)

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> SelectionAuthorityObservation:
        proof = self._proof(prepared)
        return SelectionAuthorityObservation(
            provider_id=proof.provider_id,
            account_id=proof.account_id,
            generation=proof.generation,
        )

    @staticmethod
    def _proof(prepared: PreparedSelection) -> AuthorityReadyProof:
        return AuthorityReadyProof(
            provider_id=prepared.provider_id,
            account_id=prepared.target_account_id,
            generation=AuthorityGeneration("generation-target-8"),
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )


class _ObservedOperationStore(SelectionOperationStore):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.preparing = Event()
        self.awaiting_ready = Event()
        self.reject_required_once = False
        self.crash_after_complete_once = False
        self.block_complete_once = False
        self.complete_started = Event()
        self.allow_complete = Event()
        self.reject_final_snapshot_once = False

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        result = super().compare_and_swap(expected, replacement)
        self._observe_phase(replacement)
        return result

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        if (
            self.reject_final_snapshot_once
            and expected.phase is SelectionPhase.AWAITING_READY
            and replacement.phase is SelectionPhase.AWAITING_READY
            and replacement.ready_participant_ids
        ):
            self.reject_final_snapshot_once = False
            raise ReplaceFailedError
        result = super().advance_with_required_additions(
            expected,
            replacement,
        )
        self._observe_phase(replacement)
        return result

    def complete(self, result: SelectionResult) -> SelectionResult:
        completed = super().complete(result)
        if self.block_complete_once:
            self.block_complete_once = False
            self.complete_started.set()
            if not self.allow_complete.wait(1):
                raise RuntimeError("Synthetic completion gate timed out.")
        if self.crash_after_complete_once:
            self.crash_after_complete_once = False
            raise RuntimeError("Synthetic crash after durable completion.")
        return completed

    def add_required(
        self,
        provider_id: ProviderId,
        operation_id: OperationId,
        pending_epoch: SelectionEpoch,
        participant_id: ParticipantId,
        *,
        updated_at: datetime,
    ) -> OpenSelectionOperation:
        if self.reject_required_once:
            self.reject_required_once = False
            raise RuntimeError("Synthetic late-registration write failed.")
        return super().add_required(
            provider_id,
            operation_id,
            pending_epoch,
            participant_id,
            updated_at=updated_at,
        )

    def _observe_phase(self, operation: OpenSelectionOperation) -> None:
        if operation.phase is SelectionPhase.PREPARING:
            self.preparing.set()
        if operation.phase is SelectionPhase.AWAITING_READY:
            self.awaiting_ready.set()


class _ProcessInspector:
    def __init__(self) -> None:
        self.dead: set[ProcessIdentity] = set()

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        return (
            ProcessLiveness.DEAD
            if identity in self.dead
            else ProcessLiveness.UNKNOWN
        )


@dataclass(frozen=True, slots=True)
class _Journey:
    other: FinalizedSelection
    selected: SelectedStateStore
    operations: _ObservedOperationStore
    registry: ParticipantRegistry
    adapter: _SelectionAdapter
    process_inspector: _ProcessInspector
    recovery_handoffs: list[ProviderId]
    coordinator: SelectionCoordinator
    subscriptions: tuple[Generator[ParticipantNotice], ...]


def _build_journey(tmp_path: Path) -> _Journey:
    paths = make_application_paths(tmp_path)
    baseline = _baseline_selection()
    other = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=SidekickAccountId("73a5ad0d-fba3-4d60-bc85-9c3cf7ec7b68"),
        epoch=SelectionEpoch(4),
        generation=AuthorityGeneration("generation-codex-4"),
        finalized_at=REFERENCE_TIME,
    )
    seed_finalized_selections(paths, baseline, other)
    selected = SelectedStateStore(paths.selected_state)
    operations = _ObservedOperationStore(paths.selection_journals)
    registry = ParticipantRegistry(selected)
    registry.register(_manifest(PARTICIPANT_A), _process(1))
    registry.register(_manifest(PARTICIPANT_B), _process(2))
    registry.register(_manifest(PARTICIPANT_D), _process(4))
    subscriptions = tuple(
        registry.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(participant_id, 1),
        )
        for participant_id in (PARTICIPANT_A, PARTICIPANT_B, PARTICIPANT_D)
    )
    for subscription in subscriptions:
        assert next(subscription).kind is ParticipantNoticeKind.OPEN
    for _index in range(MAX_RETAINED_PARTICIPANT_NOTICES + 1):
        registry.publish_status(
            PROVIDER_ID,
            SelectionEpoch(7),
            SelectionCode.SELECTION_RECOVERY_REQUIRED,
        )
    assert next(subscriptions[0]).kind is ParticipantNoticeKind.OPEN
    registry.disconnect(PARTICIPANT_D, 1)
    subscriptions[-1].close()
    assert registry.registered_count(PROVIDER_ID) == len(subscriptions)
    registry.confirm_dead(PARTICIPANT_D, _process(4))
    assert registry.registered_count(PROVIDER_ID) == INITIAL_PARTICIPANT_COUNT
    first_turn = registry.begin_turn(
        TurnBeginRequest(PARTICIPANT_A, 1, TURN_A)
    )
    assert (
        registry.begin_turn(TurnBeginRequest(PARTICIPANT_A, 1, TURN_A))
        == first_turn
    )
    registry.adopt(
        PARTICIPANT_A,
        1,
        ParticipantAdoptionProof(
            turn_id=TURN_A,
            account_id=baseline.account_id,
            generation=baseline.generation,
            epoch=baseline.epoch,
        ),
    )
    adapter = _SelectionAdapter()
    process_inspector = _ProcessInspector()
    recovery_handoffs: list[ProviderId] = []
    coordinator = SelectionCoordinator(
        selected,
        operations,
        registry,
        adapter,
        FixedClock(),
        process_inspector=process_inspector,
        old_turn_timeout_seconds=TEST_SELECTION_TIMEOUT_SECONDS,
        ready_timeout_seconds=TEST_SELECTION_TIMEOUT_SECONDS,
        resume_recovery=recovery_handoffs.append,
    )
    return _Journey(
        other,
        selected,
        operations,
        registry,
        adapter,
        process_inspector,
        recovery_handoffs,
        coordinator,
        subscriptions[:-1],
    )


def _manifest(participant_id: ParticipantId) -> ParticipantManifest:
    return ParticipantManifest(
        participant_id=participant_id,
        provider_id=PROVIDER_ID,
        client_kind=ParticipantClientKind.CLAUDE_CODE,
        capability_version=1,
        connection_generation=1,
    )


def _process(index: int) -> ProcessIdentity:
    return ProcessIdentity(1000 + index, index)


def _ready_request(participant_id: ParticipantId) -> ParticipantReadyRequest:
    return ParticipantReadyRequest(
        participant_id=participant_id,
        connection_generation=1,
        proof=ParticipantReadyProof(
            account_id=TARGET_ACCOUNT_ID,
            generation=AuthorityGeneration("generation-target-8"),
            epoch=SelectionEpoch(8),
        ),
    )


def _assert_journey_result(
    journey: _Journey,
    result: SelectionResult,
    expected_outcome: SelectionOutcome,
    opens_target: bool,
    expected_participant_count: int,
    expected_ready_participants: tuple[ParticipantId, ...],
) -> None:
    registry = journey.registry
    assert result.outcome is expected_outcome
    registry.disconnect(PARTICIPANT_B, 1)
    journey.subscriptions[1].close()
    next_turn = registry.begin_turn(TurnBeginRequest(PARTICIPANT_B, 1, TURN_B))
    if opens_target:
        assert registry.snapshot(PROVIDER_ID).adopted_count == 0
        assert next_turn.state is TurnAdmissionState.ADMITTED
        assert next_turn.epoch == SelectionEpoch(8)
        assert (
            registry.begin_turn(TurnBeginRequest(PARTICIPANT_B, 1, TURN_B))
            == next_turn
        )
        registry.adopt(
            PARTICIPANT_B,
            1,
            ParticipantAdoptionProof(
                turn_id=TURN_B,
                account_id=TARGET_ACCOUNT_ID,
                generation=AuthorityGeneration("generation-target-8"),
                epoch=SelectionEpoch(8),
            ),
        )
        assert registry.snapshot(PROVIDER_ID).adopted_count == 1
        reopened = journey.coordinator.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(PARTICIPANT_B, 1),
            _process(2),
        )
        current_notice = next(reopened)
        assert (current_notice.kind, current_notice.epoch) == (
            ParticipantNoticeKind.OPEN,
            SelectionEpoch(8),
        )
        reopened.close()
    else:
        assert next_turn.state is TurnAdmissionState.QUEUED
        assert (
            result.required_count,
            result.ready_count,
            result.lost_count,
        ) == (
            expected_participant_count,
            len(expected_ready_participants),
            0,
        )
        active = journey.operations.load(PROVIDER_ID).active
        assert active is not None
        assert set(active.ready_participant_ids) == set(
            expected_ready_participants
        )
        degraded = journey.coordinator.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(PARTICIPANT_B, 1),
            _process(2),
        )
        current_notice = next(degraded)
        assert (current_notice.kind, current_notice.code) == (
            ParticipantNoticeKind.STATUS,
            SelectionCode.SELECTION_RECOVERY_REQUIRED,
        )
        degraded.close()
    expected_epoch = SelectionEpoch(8 if opens_target else 7)
    finalized = journey.selected.load(PROVIDER_ID)
    assert finalized is not None
    assert (
        finalized.epoch,
        journey.selected.load(ProviderId.CODEX),
        registry.registered_count(PROVIDER_ID),
        journey.recovery_handoffs,
    ) == (
        expected_epoch,
        journey.other,
        expected_participant_count,
        [] if opens_target else [PROVIDER_ID],
    )


def _register_late(journey: _Journey) -> ParticipantRegistration:
    journey.operations.reject_required_once = True
    with pytest.raises(RuntimeError, match="late-registration write failed"):
        journey.coordinator.register(
            _manifest(PARTICIPANT_C),
            _process(3),
        )
    registered = journey.registry.registered_count(PROVIDER_ID)
    assert registered == INITIAL_PARTICIPANT_COUNT
    registration = journey.coordinator.register(
        _manifest(PARTICIPANT_C),
        _process(3),
    )
    notices = journey.coordinator.subscribe(
        new_request_id(),
        ParticipantConnectionRequest(PARTICIPANT_C, 1),
        _process(3),
    )
    assert next(notices).kind is ParticipantNoticeKind.PREPARE
    notices.close()
    return registration


def _arm_postcommit_loss(journey: _Journey, loss_state: str) -> None:
    if loss_state != "dead_after_commit":
        return
    journey.operations.block_complete_once = True
    journey.operations.crash_after_complete_once = True
    journey.process_inspector.dead.add(_process(3))


def _probe_final_seal_released(journey: _Journey, loss_state: str) -> None:
    if loss_state != "final_snapshot_failure":
        return
    completed = Event()

    def acknowledge_again() -> None:
        journey.registry.ready_request(_ready_request(PARTICIPANT_A))
        completed.set()

    Thread(target=acknowledge_again, daemon=True).start()
    assert completed.wait(1)


def _prove_initial_unselected_subscription(
    selected: SelectedStateStore,
) -> None:
    registry = ParticipantRegistry(selected)
    registry.register(_manifest(PARTICIPANT_A), _process(1))
    request = ParticipantConnectionRequest(PARTICIPANT_A, 1)
    failed = registry.subscribe(new_request_id(), request)
    with pytest.raises(ParticipantRequestError):
        next(failed)
    snapshot = registry.snapshot(PROVIDER_ID)
    assert snapshot.registered_count == 1
    assert snapshot.reachable_count == snapshot.adopted_count == 0
    assert snapshot.unreachable_participant_ids == (PARTICIPANT_A,)
    registry.close_admission(PROVIDER_ID, SelectionEpoch(1))
    retry = registry.subscribe(new_request_id(), request)
    assert next(retry).kind is ParticipantNoticeKind.PREPARE
    retry.close()


def _disconnect_during_final_seal(
    journey: _Journey,
    loss_state: str,
) -> tuple[Thread, Event] | None:
    if loss_state != "dead_after_commit":
        return None
    operations = journey.operations
    assert operations.complete_started.wait(1)
    disconnected = Event()

    def disconnect() -> None:
        journey.registry.disconnect(PARTICIPANT_B, 1)
        disconnected.set()

    thread = Thread(target=disconnect, daemon=True)
    thread.start()
    assert not disconnected.wait(TEST_SELECTION_TIMEOUT_SECONDS)
    operations.allow_complete.set()
    return thread, disconnected


@pytest.mark.parametrize(
    (
        "loss_state",
        "expected_outcome",
        "opens_target",
        "expected_participant_count",
        "expected_ready_participants",
    ),
    [
        (
            "dead_before_commit",
            SelectionOutcome.READY,
            True,
            INITIAL_PARTICIPANT_COUNT,
            (PARTICIPANT_B, PARTICIPANT_C),
        ),
        (
            "live_unreachable",
            SelectionOutcome.RECOVERY_REQUIRED,
            False,
            3,
            (PARTICIPANT_A, PARTICIPANT_B),
        ),
        (
            "dead_after_commit",
            SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
            True,
            INITIAL_PARTICIPANT_COUNT,
            (PARTICIPANT_A, PARTICIPANT_B),
        ),
        (
            "final_snapshot_failure",
            SelectionOutcome.RECOVERY_REQUIRED,
            False,
            3,
            (PARTICIPANT_A, PARTICIPANT_B, PARTICIPANT_C),
        ),
    ],
)
def test_three_participants_switch_without_interrupting_turns(
    tmp_path: Path,
    loss_state: str,
    expected_outcome: SelectionOutcome,
    opens_target: bool,
    expected_participant_count: int,
    expected_ready_participants: tuple[ParticipantId, ...],
) -> None:
    """Old work drains and queued work opens once on the target epoch."""
    journey = _build_journey(tmp_path)
    operations, registry, adapter, coordinator = (
        journey.operations,
        journey.registry,
        journey.adapter,
        journey.coordinator,
    )

    def select(operation_id: OperationId) -> SelectionResult:
        return coordinator.select(
            operation_id,
            PROVIDER_ID,
            TARGET_ACCOUNT_ID,
        )

    results: list[SelectionResult] = []
    selector = Thread(
        target=lambda: results.append(select(OPERATION_ID)),
        daemon=True,
    )
    selector.start()
    assert adapter.prevalidation_started.wait(1)
    replay_results: list[SelectionResult] = []
    replay: Thread | None = None
    if loss_state == "dead_after_commit":
        replay = Thread(
            target=lambda: replay_results.append(select(REPLAY_OPERATION_ID)),
            daemon=True,
        )
        replay.start()
        with pytest.raises(SelectionRequestError) as conflict:
            coordinator.select(
                CONFLICT_OPERATION_ID, PROVIDER_ID, CONFLICT_ACCOUNT_ID
            )
        assert conflict.value.code is SelectionCode.UNCOORDINATED_AUTH_MUTATION
    adapter.allow_prevalidation.set()
    assert operations.preparing.wait(1)

    queued = registry.begin_turn(TurnBeginRequest(PARTICIPANT_B, 1, TURN_B))
    late = _register_late(journey)
    assert queued.state is TurnAdmissionState.QUEUED
    assert late.pending_epoch == SelectionEpoch(8)
    assert registry.snapshot(PROVIDER_ID).active_turn_count == 1
    if loss_state == "dead_before_commit":
        journey.process_inspector.dead.add(_process(1))
    else:
        registry.end_turn(TurnEndRequest(PARTICIPANT_A, 1, TURN_A))
    assert adapter.committed.wait(1)
    assert operations.awaiting_ready.wait(1)

    if loss_state == "live_unreachable":
        registry.disconnect(PARTICIPANT_C, 1)
    _arm_postcommit_loss(journey, loss_state)
    if loss_state == "final_snapshot_failure":
        operations.reject_final_snapshot_once = True
    for participant_id in expected_ready_participants:
        process_index = {
            PARTICIPANT_A: 1,
            PARTICIPANT_B: 2,
            PARTICIPANT_C: 3,
        }[participant_id]
        coordinator.ready_request(
            _ready_request(participant_id),
            _process(process_index),
        )
    disconnect_probe = _disconnect_during_final_seal(journey, loss_state)
    selector.join(timeout=2)
    _probe_final_seal_released(journey, loss_state)
    if disconnect_probe is not None:
        disconnect, disconnected = disconnect_probe
        disconnect.join(timeout=2)
        assert not disconnect.is_alive()
        assert disconnected.is_set()
    if replay is not None:
        replay.join(timeout=2)
        assert not replay.is_alive()
        assert replay_results == results

    assert not selector.is_alive()
    _assert_journey_result(
        journey,
        results[0],
        expected_outcome,
        opens_target,
        expected_participant_count,
        expected_ready_participants,
    )


@pytest.mark.parametrize("baseline_exists", [False, True])
def test_recovery_finalizes_forward_from_target_provider_proof(
    tmp_path: Path,
    baseline_exists: bool,
) -> None:
    """Restart recovery gates admission and never restores baseline auth."""
    paths = make_application_paths(tmp_path)
    baseline = _baseline_selection() if baseline_exists else None
    if baseline is not None:
        seed_finalized_selections(paths, baseline)
    baseline_epoch = SelectionEpoch(0) if baseline is None else baseline.epoch
    selected = SelectedStateStore(paths.selected_state)
    if not baseline_exists:
        _prove_initial_unselected_subscription(selected)
    journal = SelectionOperationStore(paths.selection_journals)
    operation = OpenSelectionOperation(
        operation_id=OPERATION_ID,
        provider_id=PROVIDER_ID,
        baseline_account_id=None if baseline is None else baseline.account_id,
        target_account_id=TARGET_ACCOUNT_ID,
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
        started_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    journal.begin(operation)
    for phase in (
        SelectionPhase.PREPARING,
        SelectionPhase.WAITING_OLD_TURNS,
        SelectionPhase.COMMITTING,
        SelectionPhase.RECOVERING,
    ):
        replacement = replace(
            operation,
            phase=phase,
            prepared_generation=(AuthorityGeneration("generation-source-8")),
            outcome_code=(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
                if phase is SelectionPhase.RECOVERING
                else None
            ),
        )
        journal.compare_and_swap(operation, replacement)
        operation = replacement

    registry = ParticipantRegistry(selected)
    queue = OperationQueueStore(paths.durable_operations)
    recovery = SelectionRecovery(
        selected,
        journal,
        registry,
        SelectionWorkerGateway(queue, FixedClock(), lambda: None),
        FixedClock(),
    )
    assert recovery.restore(PROVIDER_ID)
    (readback,) = recovery.enqueue_restored_readbacks()
    queue.remove(
        readback.operation_id,
        expected_state=OperationState.SCHEDULED,
    )
    completion = SchedulerCompletion(
        operation_id=readback.operation_id,
        operation_kind=readback.kind,
        state=None,
        outcome=WorkerOutcome.SUCCEEDED,
        failure_code=None,
        selection=SelectionWorkerMetadata(
            operation_id=readback.operation_id,
            provider_id=PROVIDER_ID,
            kind=readback.kind,
            pending_epoch=operation.pending_epoch,
            observed_account_id=TARGET_ACCOUNT_ID,
            observed_generation=AuthorityGeneration("generation-target-8"),
        ),
    )
    result = SelectionCoordinator(
        selected,
        journal,
        registry,
        _SelectionAdapter(),
        FixedClock(),
        resume_recovery=lambda provider_id: recovery.complete_readback(
            completion
        ),
    ).select(REPLAY_OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID)

    (finalized,) = selected.load_all()
    assert finalized.account_id == TARGET_ACCOUNT_ID
    assert finalized.epoch == baseline_epoch.next()
    assert result == journal.load(PROVIDER_ID).history[-1]
    assert result.operation_id == OPERATION_ID
    assert result.outcome is SelectionOutcome.READY
    assert journal.load(PROVIDER_ID).active is None
    restore = registry.restore_admission
    with pytest.raises(ParticipantRequestError):
        restore(PROVIDER_ID, finalized.epoch, CONFLICT_ACCOUNT_ID, ())
    restore(PROVIDER_ID, finalized.epoch, TARGET_ACCOUNT_ID, (PARTICIPANT_A,))
    missing = registry.snapshot(PROVIDER_ID)
    assert missing.registered_count == 1
    assert missing.reachable_count == 0
    assert missing.unreachable_participant_ids == (PARTICIPANT_A,)
    persisted_epochs: list[SelectionEpoch] = []
    registration = registry.register(
        _manifest(PARTICIPANT_A),
        _process(1),
        persist_required=persisted_epochs.append,
    )
    assert registration.registered_epoch == baseline_epoch.next()
    assert registration.pending_epoch == registration.registered_epoch
    assert persisted_epochs == [registration.registered_epoch]
    registry.reopen_baseline(PROVIDER_ID)
    registry.register(_manifest(PARTICIPANT_B), _process(2))
    registry.close_admission(PROVIDER_ID, baseline_epoch.next().next())
    registry.confirm_dead(PARTICIPANT_A, _process(1))
    registry.reopen_baseline(PROVIDER_ID)
    assert registry.registered_count(PROVIDER_ID) == 1
    assert registry.snapshot(PROVIDER_ID).required_participant_ids == ()
