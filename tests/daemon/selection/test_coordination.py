"""Concurrent participant selection coordination tests."""

import socket
from collections.abc import Callable, Generator
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages.core.accounts.identifiers import (
    new_operation_id,
    new_request_id,
)
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
    OperationKind,
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import OperationEventHub
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import SelectionWorkerMetadata
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
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
    SelectionRequestError,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import (
    SelectionSchedulerSink,
    SelectionWorkerGateway,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.providers.claude.structured.codec import (
    ClaudeProtectedChannelError,
    clear_secret_buffer,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeParticipantChannelRegistry,
    ClaudeProtectedHostChannel,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredInstallReceipt,
)
from tests.fakes.claude.session import start_claude_binding_reporter
from tests.fakes.daemon.control import (
    ExactProcessInspectorFake,
    ObservedSelectionOperationStore,
)
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
TARGET_GENERATION = AuthorityGeneration("generation-target-8")
_TARGET_EPOCH = SelectionEpoch(8)
CONFLICT_ACCOUNT_ID = SidekickAccountId("e45c490a-18cd-45a6-aee6-6de58cbc7b5a")
PARTICIPANT_A = ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337")
PARTICIPANT_B = ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc")
PARTICIPANT_C = ParticipantId("e9b1b25c-fae6-4998-a135-719ad3257972")
PARTICIPANT_D = ParticipantId("ed416c76-24b3-4934-9486-f910376e3a71")
TURN_A = TurnId("a99915d0-5d3a-497a-9696-c227d0903709")
TURN_B = TurnId("0168fe43-5c83-46f4-b8ef-6cc047293957")
INITIAL_COUNT, TEST_SELECTION_TIMEOUT_SECONDS = 2, 0.05


def _baseline_selection() -> FinalizedSelection:
    return FinalizedSelection(
        provider_id=PROVIDER_ID,
        account_id=SidekickAccountId("4d95b1fb-1ba4-4837-b6b7-452cd8aff462"),
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("generation-baseline-7"),
        finalized_at=REFERENCE_TIME,
    )


def _completion(
    operation_id: OperationId,
    kind: OperationKind,
    account_id: SidekickAccountId,
    generation: AuthorityGeneration,
    epoch: SelectionEpoch,
    *,
    worker_operation_id: OperationId | None = None,
) -> SchedulerCompletion:
    return SchedulerCompletion(
        PROVIDER_ID,
        operation_id,
        kind,
        None,
        WorkerOutcome.SUCCEEDED,
        None,
        selection=SelectionWorkerMetadata(
            operation_id=operation_id,
            provider_id=PROVIDER_ID,
            kind=kind,
            pending_epoch=epoch,
            observed_account_id=account_id,
            observed_generation=generation,
            authority_requires_participant=True
            if kind is OperationKind.SELECTION_READBACK
            else None,
        ),
        worker_operation_id=worker_operation_id,
    )


class _SelectionAdapter:
    def __init__(
        self,
        channels: ClaudeParticipantChannelRegistry | None = None,
        registry: ParticipantRegistry | None = None,
        protected_hosts: dict[ParticipantId, socket.socket] | None = None,
        scheduler: SelectionSchedulerSink | None = None,
        prove_commit: Callable[[SchedulerCompletion], None] | None = None,
    ) -> None:
        self.prevalidation_started = Event()
        self.allow_prevalidation = Event()
        self.committed = Event()
        self.crash_after_install = False
        self._prove_commit = prove_commit
        if channels is None:
            assert registry is protected_hosts is scheduler is None
            self._protected = None
        else:
            assert registry is not None
            assert protected_hosts is not None
            assert scheduler is not None
            assert prove_commit is not None
            self._protected = channels, registry, protected_hosts, scheduler

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        self.prevalidation_started.set()
        if not self.allow_prevalidation.wait(1):
            raise RuntimeError("Synthetic prevalidation gate timed out.")
        suffix = "target-8" if self.crash_after_install else "source-8"
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=operation.provider_id,
            target_account_id=operation.target_account_id,
            target_generation=AuthorityGeneration(f"generation-{suffix}"),
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        proof = self._proof(prepared)
        if self._protected is None:
            self.committed.set()
            return proof
        binding = ClaudeStructuredBinding(
            operation_id=prepared.operation_id,
            account_id=proof.account_id,
            generation=proof.generation,
            epoch=proof.epoch,
        )
        snapshot = self._protected[1].snapshot(prepared.provider_id)
        if set(snapshot.required_participant_ids) - set(self._protected[2]):
            assert self._prove_commit is not None
            self._prove_commit(
                _completion(
                    prepared.operation_id,
                    OperationKind.SELECTION_COMMIT,
                    proof.account_id,
                    proof.generation,
                    proof.epoch,
                )
            )
        self._install(binding, snapshot.required_participant_ids)
        self.committed.set()
        if self.crash_after_install:
            raise RuntimeError("Synthetic crash after protected install.")
        return proof

    def bind_participant(
        self,
        operation: OpenSelectionOperation,
        _participant_id: ParticipantId,
        _connection_generation: int,
    ) -> None:
        raise AssertionError(str(operation.operation_id))

    def bind_finalized(
        self,
        finalized: FinalizedSelection,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        if self._protected is None:
            return
        _channels, _registry, _hosts, scheduler = self._protected
        operation_id = new_operation_id()
        self._install(
            ClaudeStructuredBinding(
                operation_id=operation_id,
                account_id=finalized.account_id,
                generation=finalized.generation,
                epoch=finalized.epoch,
            ),
            (participant_id,),
            target=(participant_id, connection_generation),
        )
        scheduler.completed(
            _completion(
                operation_id,
                OperationKind.CLAUDE_PARTICIPANT_BIND,
                finalized.account_id,
                finalized.generation,
                finalized.epoch,
                worker_operation_id=operation_id,
            )
        )

    def _install(
        self,
        binding: ClaudeStructuredBinding,
        participant_ids: tuple[ParticipantId, ...] | None = None,
        *,
        target: tuple[ParticipantId, int] | None = None,
    ) -> None:
        assert self._protected is not None
        channels, _registry, protected_hosts, _scheduler = self._protected
        available = tuple(protected_hosts)
        targets = available if participant_ids is None else participant_ids
        targets = tuple(item for item in targets if item in protected_hosts)
        receivers: list[Thread] = []
        for participant_id in targets:
            host = ClaudeProtectedHostChannel(
                protected_hosts[participant_id], participant_id, 1
            )
            receiver = Thread(
                target=self._acknowledge_projection,
                args=(host, binding),
                daemon=True,
            )
            receiver.start()
            receivers.append(receiver)
        oauth = bytearray(b"protected-target-oauth-canary")
        try:
            if target is None:
                channels.install(binding, oauth)
            else:
                channels.install_target(binding, oauth, *target)
        finally:
            clear_secret_buffer(oauth)
        for receiver in receivers:
            receiver.join(timeout=1)
            assert not receiver.is_alive()

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
            generation=TARGET_GENERATION,
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )

    @staticmethod
    def _acknowledge_projection(
        host: ClaudeProtectedHostChannel,
        binding: ClaudeStructuredBinding,
    ) -> None:
        frame = host.receive()
        try:
            assert frame.protected_binding == binding
        finally:
            frame.close_protected_frame()
        receipt = ClaudeStructuredInstallReceipt(
            binding=binding, request_id=new_request_id()
        )
        host.acknowledge(receipt)


@dataclass(frozen=True, slots=True)
class _Journey:
    other: FinalizedSelection
    selected: SelectedStateStore
    operations: ObservedSelectionOperationStore
    registry: ParticipantRegistry
    adapter: _SelectionAdapter
    process_inspector: ExactProcessInspectorFake
    recovery_handoffs: list[ProviderId]
    coordinator: SelectionCoordinator
    subscriptions: tuple[Generator[ParticipantNotice], ...]
    protected_hosts: dict[ParticipantId, socket.socket]


def _register_unbound(
    coordinator: SelectionCoordinator,
    protected_hosts: dict[ParticipantId, socket.socket],
    participant_id: ParticipantId,
    process: ProcessIdentity,
) -> tuple[ParticipantRegistration, socket.socket]:
    host, supervisor = socket.socketpair(socket.AF_UNIX)
    protected_hosts[participant_id] = host
    start_claude_binding_reporter(host, participant_id, 1, None)
    registration = coordinator.register(
        _manifest(participant_id), process, protected_endpoint=supervisor
    )
    return registration, host


def _complete_bootstrap(
    journey: _Journey,
    process: ProcessIdentity,
) -> socket.socket:
    _, host = _register_unbound(
        journey.coordinator,
        journey.protected_hosts,
        PARTICIPANT_A,
        process,
    )
    snapshot = journey.registry.snapshot(PROVIDER_ID)
    assert snapshot.registered_count == snapshot.reachable_count == 1
    journey.adapter.allow_prevalidation.set()
    _canonical_id, stream = journey.coordinator.select_events(
        REPLAY_OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID
    )
    for _index in range(4):
        next(stream)
    notices = journey.coordinator.subscribe(
        new_request_id(),
        ParticipantConnectionRequest(PARTICIPANT_A, 1),
        process,
    )
    assert next(notices).kind is ParticipantNoticeKind.READY
    journey.coordinator.ready_request(
        _ready_request(PARTICIPANT_A, SelectionEpoch(1)), process
    )
    assert isinstance(result := next(stream), SelectionResult)
    assert result.outcome is SelectionOutcome.READY
    assert next(notices).kind is ParticipantNoticeKind.OPEN
    notices.close()
    return host


def _build_journey(
    tmp_path: Path,
    *,
    include_participants: bool = True,
    participant_required: bool = True,
    baseline_exists: bool = True,
) -> _Journey:
    paths = make_application_paths(tmp_path)
    baseline = _baseline_selection()
    other = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=SidekickAccountId("73a5ad0d-fba3-4d60-bc85-9c3cf7ec7b68"),
        epoch=SelectionEpoch(4),
        generation=AuthorityGeneration("generation-codex-4"),
        finalized_at=REFERENCE_TIME,
    )
    states = (baseline, other) if baseline_exists else (other,)
    seed_finalized_selections(paths, *states)
    selected = SelectedStateStore(paths.selected_state)
    operations = ObservedSelectionOperationStore(paths.selection_journals)
    registry = ParticipantRegistry(selected)
    failed = partial(registry.disconnect, attachment_failure=True)
    channels = ClaudeParticipantChannelRegistry(
        lambda _: participant_required, failed
    )
    registry.add_attachment_registry(channels)
    protected_hosts: dict[ParticipantId, socket.socket] = {}
    queue = OperationQueueStore(paths.durable_operations)
    gateway = SelectionWorkerGateway(queue, FixedClock(), lambda: None)
    recovery = SelectionRecovery(
        selected, operations, registry, gateway, FixedClock()
    )
    scheduler = SelectionSchedulerSink(OperationEventHub(), gateway, recovery)
    adapter = _SelectionAdapter(
        channels, registry, protected_hosts, scheduler, recovery.prove_commit
    )
    process_inspector = ExactProcessInspectorFake()
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
    participant_specs = ()
    if include_participants:
        participant_specs = (
            (PARTICIPANT_A, 1),
            (PARTICIPANT_B, 2),
            (PARTICIPANT_D, 4),
        )
    for participant_id, process_index in participant_specs:
        process = _process(process_index)
        _registration, host = _register_unbound(
            coordinator, protected_hosts, participant_id, process
        )
        host.settimeout(1)
    subscriptions = tuple(
        registry.subscribe(
            new_request_id(),
            ParticipantConnectionRequest(participant_id, 1),
        )
        for participant_id, _process_index in participant_specs
    )
    for subscription in subscriptions:
        assert next(subscription).kind is ParticipantNoticeKind.OPEN
    if include_participants:
        registry.disconnect(PARTICIPANT_D, 1)
        subscriptions[-1].close()
        process_inspector.dead.add(_process(4))
        coordinator.reconcile_disconnected(PROVIDER_ID)
        assert registry.snapshot(PROVIDER_ID).registered_count == INITIAL_COUNT
        removed_host = protected_hosts.pop(PARTICIPANT_D)
        assert removed_host.recv(1) == b""
        removed_host.close()
        registry.begin_turn(TurnBeginRequest(PARTICIPANT_A, 1, TURN_A))
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
    return _Journey(
        other,
        selected,
        operations,
        registry,
        adapter,
        process_inspector,
        recovery_handoffs,
        coordinator,
        subscriptions[:-1] if include_participants else (),
        protected_hosts,
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


def _ready_request(
    participant_id: ParticipantId,
    epoch: SelectionEpoch = _TARGET_EPOCH,
) -> ParticipantReadyRequest:
    return ParticipantReadyRequest(
        participant_id=participant_id,
        connection_generation=1,
        proof=ParticipantReadyProof(
            account_id=TARGET_ACCOUNT_ID,
            generation=TARGET_GENERATION,
            epoch=epoch,
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
                generation=TARGET_GENERATION,
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
            ParticipantNoticeKind.PREPARE
            if active.target_generation is None
            else ParticipantNoticeKind.STATUS,
            None
            if active.target_generation is None
            else (SelectionCode.SELECTION_RECOVERY_REQUIRED),
        )
        degraded.close()
    expected_epoch = SelectionEpoch(8 if opens_target else 7)
    finalized = journey.selected.load(PROVIDER_ID)
    assert finalized is not None
    assert finalized.epoch == expected_epoch
    assert journey.selected.load(ProviderId.CODEX) == journey.other
    registered = registry.snapshot(PROVIDER_ID).registered_count
    assert registered == expected_participant_count
    assert journey.recovery_handoffs == ([] if opens_target else [PROVIDER_ID])


def _register_late(journey: _Journey) -> ParticipantRegistration:
    registration, _host = _register_unbound(
        journey.coordinator,
        journey.protected_hosts,
        PARTICIPANT_C,
        _process(3),
    )
    return registration


def _start_selection(
    coordinator: SelectionCoordinator,
) -> tuple[list[SelectionResult], Thread]:
    results: list[SelectionResult] = []
    selector = Thread(
        target=lambda: results.append(
            coordinator.select(OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID)
        ),
        daemon=True,
    )
    selector.start()
    return results, selector


def test_setup_requires_a_participant_before_commit(tmp_path: Path) -> None:
    """Require a host or its exact unbound prebootstrap endpoint proof."""
    setup = _build_journey(tmp_path / "setup", include_participants=False)
    setup.adapter.allow_prevalidation.set()
    with pytest.raises(SelectionRequestError) as refused:
        setup.coordinator.select(OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID)
    assert refused.value.code is SelectionCode.SESSION_CONFIGURATION_REQUIRED
    assert setup.selected.load(PROVIDER_ID) == _baseline_selection()
    setup_result = setup.operations.load(PROVIDER_ID).history[-1]
    assert setup_result.outcome is SelectionOutcome.FAILED_OLD_EPOCH
    native = _build_journey(
        tmp_path / "native",
        include_participants=False,
        participant_required=False,
    )
    native.adapter.allow_prevalidation.set()
    result = native.coordinator.select(
        REPLAY_OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID
    )
    assert result.outcome is SelectionOutcome.READY
    assert (selected := native.selected.load(PROVIDER_ID)) is not None
    assert selected.epoch == _TARGET_EPOCH
    bootstrap = _build_journey(
        tmp_path / "bootstrap",
        include_participants=False,
        baseline_exists=False,
    )
    process = _process(1)
    host = _complete_bootstrap(bootstrap, process)
    host.close()
    attachments = bootstrap.registry.attachment_registry(PROVIDER_ID)
    assert attachments is not None
    with pytest.raises(ClaudeProtectedChannelError):
        attachments.refresh_binding(PARTICIPANT_A, 1, process)
    snapshot = bootstrap.registry.snapshot(PROVIDER_ID)
    assert (
        snapshot.registered_count,
        snapshot.reachable_count,
        snapshot.unreachable_participant_ids,
    ) == (1, 0, (PARTICIPANT_A,))
    failed = _build_journey(
        tmp_path / "failed",
        include_participants=False,
        baseline_exists=False,
    )
    _registration, host = _register_unbound(
        failed.coordinator, failed.protected_hosts, PARTICIPANT_A, process
    )
    host.close()
    failed.protected_hosts.pop(PARTICIPANT_A)
    failed.process_inspector.dead_after_first.add(process)
    failed.adapter.allow_prevalidation.set()
    result = failed.coordinator.select(
        REPLAY_OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID
    )
    assert result.outcome is SelectionOutcome.RECOVERY_REQUIRED
    operation = failed.operations.load(PROVIDER_ID).active
    assert operation is not None
    assert operation.lost_after_commit_participant_ids == (PARTICIPANT_A,)


def _arm_postcommit_loss(
    journey: _Journey,
    loss_state: str,
    tmp_path: Path,
    selector: Thread,
) -> Event | None:
    operations = journey.operations
    if loss_state != "live_unreachable":
        assert operations.awaiting_ready.wait(1)
    else:
        selector.join(timeout=2)
        active = operations.load(PROVIDER_ID).active
        assert active is not None
        assert active.phase is SelectionPhase.RECOVERING
        baseline = _baseline_selection()
        queue = OperationQueueStore(
            make_application_paths(tmp_path).durable_operations
        )
        workers = SelectionWorkerGateway(queue, FixedClock(), lambda: None)
        channels = ClaudeParticipantChannelRegistry(lambda _: True)
        restarted = ParticipantRegistry(journey.selected, attachments=channels)
        recovery = SelectionRecovery(
            journey.selected, operations, restarted, workers, FixedClock()
        )
        assert recovery.restore(PROVIDER_ID)
        recovery.complete_readback(
            _completion(
                OPERATION_ID,
                OperationKind.SELECTION_READBACK,
                baseline.account_id,
                baseline.generation,
                SelectionEpoch(8),
            )
        )
        active = operations.load(PROVIDER_ID).active
        assert active is not None
        assert active.phase is SelectionPhase.RECOVERING
        assert active.target_generation is None
        assert journey.selected.load(PROVIDER_ID) == baseline
        journey.registry.disconnect(PARTICIPANT_C, 1)
    if loss_state == "dead_after_commit":
        operations.block_complete_once = True
        operations.crash_after_complete_once = True
        journey.process_inspector.dead.add(_process(3))
        disconnected = Event()
        Thread(
            target=lambda: (
                operations.complete_started.wait(1),
                journey.registry.disconnect(PARTICIPANT_B, 1),
                disconnected.set(),
            ),
            daemon=True,
        ).start()
        return disconnected
    if loss_state == "final_snapshot_failure":
        operations.reject_final_snapshot_once = True
    return None


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
            INITIAL_COUNT,
            (PARTICIPANT_B, PARTICIPANT_C),
        ),
        (
            "live_unreachable",
            SelectionOutcome.RECOVERY_REQUIRED,
            False,
            3,
            (),
        ),
        (
            "dead_after_commit",
            SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
            True,
            INITIAL_COUNT,
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
    adapter.crash_after_install = loss_state == "live_unreachable"
    results, selector = _start_selection(coordinator)
    assert adapter.prevalidation_started.wait(1)
    replay: Thread | None = None
    if loss_state == "dead_after_commit":
        replay = Thread(
            target=lambda: results.append(
                coordinator.select(
                    REPLAY_OPERATION_ID, PROVIDER_ID, TARGET_ACCOUNT_ID
                )
            ),
            daemon=True,
        )
        replay.start()
        with pytest.raises(SelectionRequestError) as conflict:
            coordinator.select(
                CONFLICT_OPERATION_ID, PROVIDER_ID, CONFLICT_ACCOUNT_ID
            )
        assert conflict.value.code is SelectionCode.UNCOORDINATED_AUTH_MUTATION
    adapter.allow_prevalidation.set()
    assert (
        operations.preparing.wait(1),
        adapter.committed.is_set(),
    ) == (True, False)
    queued = registry.begin_turn(TurnBeginRequest(PARTICIPANT_B, 1, TURN_B))
    late = _register_late(journey)
    assert (queued.state, late.pending_epoch) == (
        TurnAdmissionState.QUEUED,
        SelectionEpoch(8),
    )
    if loss_state == "dead_before_commit":
        journey.process_inspector.dead.add(_process(1))
        registry.disconnect(PARTICIPANT_A, 1)
        coordinator.reconcile_disconnected(PROVIDER_ID)
    else:
        registry.end_turn(TurnEndRequest(PARTICIPANT_A, 1, TURN_A))
    assert adapter.committed.wait(1)
    if loss_state != "live_unreachable":
        assert operations.awaiting_ready.wait(1)
    late_notices = coordinator.subscribe(
        new_request_id(),
        ParticipantConnectionRequest(PARTICIPANT_C, 1),
        _process(3),
    )
    assert next(late_notices).kind is (
        ParticipantNoticeKind.PREPARE
        if loss_state == "live_unreachable"
        else ParticipantNoticeKind.READY
    )
    disconnected = _arm_postcommit_loss(
        journey, loss_state, tmp_path, selector
    )
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
    if disconnected is not None:
        assert operations.complete_started.wait(1)
        assert not disconnected.wait(TEST_SELECTION_TIMEOUT_SECONDS)
        operations.allow_complete.set()
    selector.join(timeout=2)
    if loss_state == "final_snapshot_failure":
        completed = Event()
        Thread(
            target=lambda: (
                registry.ready_request(_ready_request(PARTICIPANT_A)),
                completed.set(),
            ),
            daemon=True,
        ).start()
        assert completed.wait(1)
    if disconnected is not None:
        assert disconnected.wait(2)
    if replay is not None:
        replay.join(timeout=2)
        assert (replay.is_alive(), results) == (
            False,
            [results[0], results[0]],
        )
    assert not selector.is_alive()
    _assert_journey_result(
        journey,
        results[0],
        expected_outcome,
        opens_target,
        expected_participant_count,
        expected_ready_participants,
    )
    late_notices.close()
    for host in journey.protected_hosts.values():
        host.close()
