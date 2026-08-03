"""Concurrent participant selection coordination tests."""

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
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.coordinator import (
    SelectionCoordinator,
    SelectionRequestError,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRegistration,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.persistence.models.selection import (
    SelectionOperationDocument,
)
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


class _SelectionAdapter:
    """Coordinate one synthetic transition without provider I/O."""

    def __init__(self) -> None:
        self.prevalidation_started = Event()
        self.allow_prevalidation = Event()
        self.committed = Event()
        self.commit_count = 0
        self.signals: list[str] = []

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
            target_generation=AuthorityGeneration("generation-target-8"),
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        self.commit_count += 1
        self.committed.set()
        return self._proof(prepared)

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> AuthorityReadyProof | None:
        return self._proof(prepared)

    @staticmethod
    def _proof(prepared: PreparedSelection) -> AuthorityReadyProof:
        return AuthorityReadyProof(
            provider_id=prepared.provider_id,
            account_id=prepared.target_account_id,
            generation=prepared.target_generation,
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )


class _ObservedOperationStore:
    """Expose one exact persisted phase to the concurrent test."""

    def __init__(self, store: SelectionOperationStore) -> None:
        self._store = store
        self.preparing = Event()
        self.awaiting_ready = Event()
        self.reject_required_once = False

    def begin(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        return self._store.begin(operation)

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        result = self._store.compare_and_swap(expected, replacement)
        if replacement.phase is SelectionPhase.PREPARING:
            self.preparing.set()
        if replacement.phase is SelectionPhase.AWAITING_READY:
            self.awaiting_ready.set()
        return result

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        result = self._store.advance_with_required_additions(
            expected,
            replacement,
        )
        if replacement.phase is SelectionPhase.PREPARING:
            self.preparing.set()
        if replacement.phase is SelectionPhase.AWAITING_READY:
            self.awaiting_ready.set()
        return result

    def complete(self, result: SelectionResult) -> SelectionResult:
        return self._store.complete(result)

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
        return self._store.add_required(
            provider_id,
            operation_id,
            pending_epoch,
            participant_id,
            updated_at=updated_at,
        )

    def load(
        self,
        provider_id: ProviderId,
    ) -> SelectionOperationDocument:
        return self._store.load(provider_id)


class _ProcessInspector:
    """Return configured exact process-start liveness."""

    def __init__(self) -> None:
        self.dead: set[ProcessIdentity] = set()

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        if identity in self.dead:
            return ProcessLiveness.DEAD
        return ProcessLiveness.UNKNOWN


@dataclass(frozen=True, slots=True)
class _Journey:
    """State shared across one concurrent selection journey."""

    other: FinalizedSelection
    selected: SelectedStateStore
    operations: _ObservedOperationStore
    registry: ParticipantRegistry
    adapter: _SelectionAdapter
    process_inspector: _ProcessInspector
    recovery_handoffs: list[ProviderId]
    coordinator: SelectionCoordinator
    first: ParticipantRegistration


def _build_journey(tmp_path: Path) -> _Journey:
    paths = make_application_paths(tmp_path)
    baseline = FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=SidekickAccountId("4d95b1fb-1ba4-4837-b6b7-452cd8aff462"),
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-baseline-7"),
        finalized_at=REFERENCE_TIME,
    )
    other = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=SidekickAccountId("73a5ad0d-fba3-4d60-bc85-9c3cf7ec7b68"),
        epoch=SelectionEpoch(4),
        generation=AuthorityGeneration("generation-codex-4"),
        finalized_at=REFERENCE_TIME,
    )
    seed_finalized_selections(paths, baseline, other)
    selected = SelectedStateStore(paths.selected_state)
    operations = _ObservedOperationStore(
        SelectionOperationStore(paths.selection_journals)
    )
    registry = ParticipantRegistry(selected)
    first = registry.register(_manifest(PARTICIPANT_A), _process(1))
    registry.register(_manifest(PARTICIPANT_B), _process(2))
    registry.register(_manifest(PARTICIPANT_D), _process(4))
    registry.disconnect(PARTICIPANT_D, 1)
    assert registry.registered_count(PROVIDER_ID) == INITIAL_PARTICIPANT_COUNT
    first_turn = registry.begin_turn(
        TurnBeginRequest(PARTICIPANT_A, 1, TURN_A)
    )
    assert first_turn.epoch == SelectionEpoch(7)
    assert (
        registry.begin_turn(TurnBeginRequest(PARTICIPANT_A, 1, TURN_A))
        == first_turn
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
        first,
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
    return ProcessIdentity(process_id=1000 + index, start_identity=index)


def _assert_journey_result(
    journey: _Journey,
    result: SelectionResult,
    expected_outcome: SelectionOutcome,
    opens_target: bool,
    expected_participant_count: int,
) -> None:
    assert result.outcome is expected_outcome
    next_turn = journey.registry.begin_turn(
        TurnBeginRequest(PARTICIPANT_B, 1, TURN_B)
    )
    if opens_target:
        assert next_turn.state is TurnAdmissionState.ADMITTED
        assert next_turn.epoch == SelectionEpoch(8)
        assert (
            journey.registry.begin_turn(
                TurnBeginRequest(PARTICIPANT_B, 1, TURN_B)
            )
            == next_turn
        )
        journey.registry.adopt(
            PARTICIPANT_B,
            1,
            ParticipantAdoptionProof(
                turn_id=TURN_B,
                account_id=TARGET_ACCOUNT_ID,
                generation=AuthorityGeneration("generation-target-8"),
                epoch=SelectionEpoch(8),
            ),
        )
        reopened = journey.coordinator.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(PARTICIPANT_B, 1),
            _process(2),
        )
        current_notice = next(reopened)
        assert current_notice.kind is ParticipantNoticeKind.OPEN
        assert current_notice.epoch == SelectionEpoch(8)
        reopened.close()
    else:
        assert next_turn.state is TurnAdmissionState.QUEUED
        assert result.required_count == expected_participant_count
        assert result.ready_count == INITIAL_PARTICIPANT_COUNT
        assert result.lost_count == 0
        active = journey.operations.load(PROVIDER_ID).active
        assert active is not None
        assert set(active.ready_participant_ids) == {
            PARTICIPANT_A,
            PARTICIPANT_B,
        }
        degraded = journey.coordinator.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(PARTICIPANT_B, 1),
            _process(2),
        )
        current_notice = next(degraded)
        assert current_notice.kind is ParticipantNoticeKind.STATUS
        assert current_notice.code is SelectionCode.SELECTION_RECOVERY_REQUIRED
        degraded.close()
    assert journey.first.registered_epoch == SelectionEpoch(7)
    expected_epoch = SelectionEpoch(8 if opens_target else 7)
    finalized = journey.selected.load(PROVIDER_ID)
    assert finalized is not None
    assert finalized.epoch == expected_epoch
    assert journey.selected.load(ProviderId.CODEX) == journey.other
    assert (
        journey.registry.registered_count(PROVIDER_ID)
        == expected_participant_count
    )
    assert journey.adapter.signals == []
    assert journey.recovery_handoffs == ([] if opens_target else [PROVIDER_ID])


def _register_late(journey: _Journey) -> ParticipantRegistration:
    """Prove durable registration failure leaves no in-memory client."""
    journey.operations.reject_required_once = True
    with pytest.raises(RuntimeError, match="late-registration write failed"):
        journey.coordinator.register(
            _manifest(PARTICIPANT_C),
            _process(3),
        )
    assert (
        journey.registry.registered_count(PROVIDER_ID)
        == INITIAL_PARTICIPANT_COUNT
    )
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


@pytest.mark.parametrize(
    (
        "loss_state",
        "expected_outcome",
        "opens_target",
        "expected_participant_count",
    ),
    [
        (
            "dead_before_commit",
            SelectionOutcome.READY,
            True,
            INITIAL_PARTICIPANT_COUNT,
        ),
        (
            "live_unreachable",
            SelectionOutcome.RECOVERY_REQUIRED,
            False,
            3,
        ),
        (
            "dead_after_commit",
            SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
            True,
            INITIAL_PARTICIPANT_COUNT,
        ),
    ],
)
def test_three_participants_switch_without_interrupting_turns(
    tmp_path: Path,
    loss_state: str,
    expected_outcome: SelectionOutcome,
    opens_target: bool,
    expected_participant_count: int,
) -> None:
    """Old work drains and queued work opens once on the target epoch."""
    journey = _build_journey(tmp_path)
    operations, registry, adapter, coordinator = (
        journey.operations,
        journey.registry,
        journey.adapter,
        journey.coordinator,
    )
    results: list[SelectionResult] = []
    selector = Thread(
        target=lambda: results.append(
            coordinator.select(
                OPERATION_ID,
                PROVIDER_ID,
                TARGET_ACCOUNT_ID,
            )
        ),
        daemon=True,
    )
    selector.start()
    assert adapter.prevalidation_started.wait(1)
    replay_results: list[SelectionResult] = []
    replay: Thread | None = None
    if loss_state == "dead_after_commit":
        replay = Thread(
            target=lambda: replay_results.append(
                coordinator.select(
                    REPLAY_OPERATION_ID,
                    PROVIDER_ID,
                    TARGET_ACCOUNT_ID,
                )
            ),
            daemon=True,
        )
        replay.start()
        with pytest.raises(SelectionRequestError) as conflict:
            coordinator.select(
                CONFLICT_OPERATION_ID,
                PROVIDER_ID,
                CONFLICT_ACCOUNT_ID,
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
    if loss_state == "dead_after_commit":
        journey.process_inspector.dead.add(_process(3))
    ready_participants = (
        (PARTICIPANT_B, PARTICIPANT_C)
        if loss_state == "dead_before_commit"
        else (PARTICIPANT_A, PARTICIPANT_B)
    )
    for participant_id in ready_participants:
        process_index = {
            PARTICIPANT_A: 1,
            PARTICIPANT_B: 2,
            PARTICIPANT_C: 3,
        }[participant_id]
        coordinator.ready_request(
            ParticipantReadyRequest(
                participant_id=participant_id,
                connection_generation=1,
                proof=ParticipantReadyProof(
                    account_id=TARGET_ACCOUNT_ID,
                    generation=AuthorityGeneration("generation-target-8"),
                    epoch=SelectionEpoch(8),
                ),
            ),
            _process(process_index),
        )
    selector.join(timeout=2)
    if replay is not None:
        replay.join(timeout=2)
        assert not replay.is_alive()
        assert replay_results == results

    assert not selector.is_alive()
    assert len(results) == 1
    _assert_journey_result(
        journey,
        results[0],
        expected_outcome,
        opens_target,
        expected_participant_count,
    )


def test_recovery_finalizes_forward_from_target_provider_proof(
    tmp_path: Path,
) -> None:
    """Restart recovery gates admission and never restores baseline auth."""
    paths = make_application_paths(tmp_path)
    baseline = FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=SidekickAccountId("4d95b1fb-1ba4-4837-b6b7-452cd8aff462"),
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-baseline-7"),
        finalized_at=REFERENCE_TIME,
    )
    seed_finalized_selections(paths, baseline)
    selected = SelectedStateStore(paths.selected_state)
    journal = SelectionOperationStore(paths.selection_journals)
    operation = OpenSelectionOperation(
        operation_id=OPERATION_ID,
        provider_id=PROVIDER_ID,
        baseline_account_id=baseline.account_id,
        target_account_id=TARGET_ACCOUNT_ID,
        target_generation=None,
        baseline_epoch=baseline.epoch,
        pending_epoch=baseline.epoch.next(),
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
            target_generation=AuthorityGeneration("generation-target-8"),
            outcome_code=(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
                if phase is SelectionPhase.RECOVERING
                else None
            ),
        )
        journal.compare_and_swap(operation, replacement)
        operation = replacement

    adapter = _SelectionAdapter()
    registry = ParticipantRegistry(selected)
    result = SelectionRecovery(
        selected,
        journal,
        registry,
        adapter,
        FixedClock(),
    ).recover(PROVIDER_ID)

    assert result is not None
    assert result.outcome is SelectionOutcome.READY
    finalized = selected.load(PROVIDER_ID)
    assert finalized is not None
    assert finalized.account_id == TARGET_ACCOUNT_ID
    assert finalized.epoch == SelectionEpoch(8)
    assert journal.load(PROVIDER_ID).active is None
    assert adapter.commit_count == 0
    registration = registry.register(_manifest(PARTICIPANT_A), _process(1))
    assert registration.registered_epoch == SelectionEpoch(8)
    assert registration.pending_epoch is None
    registry.register(_manifest(PARTICIPANT_B), _process(2))
    registry.close_admission(PROVIDER_ID, SelectionEpoch(9))
    registry.confirm_dead(PARTICIPANT_A, _process(1))
    registry.reopen_baseline(PROVIDER_ID)
    assert registry.registered_count(PROVIDER_ID) == 1
    assert registry.snapshot(PROVIDER_ID).required_participant_ids == ()
