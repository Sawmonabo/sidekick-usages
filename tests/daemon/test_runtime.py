"""Resident supervisor scheduling and recovery tests."""

from dataclasses import replace
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
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    PreparedSelection,
    RelatedRuntimeAuthority,
    SelectionAuthorityObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import OperationEventHub
from sidekick_usages.daemon.lifecycle.constants import (
    CLAUDE_LAUNCHER_OPTION,
    CODEX_LAUNCHER_OPTION,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.models.worker import (
    WORKER_CLAUDE_LAUNCHER_ENVIRONMENT_KEY,
    WORKER_CODEX_LAUNCHER_ENVIRONMENT_KEY,
    ProviderLaunchers,
    SelectionWorkerMetadata,
    WorkerResult,
)
from sidekick_usages.daemon.runtime.recovery import (
    ActivationRecoveryScheduler,
)
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    MAX_CONTROL_CONNECTIONS,
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRequestError,
)
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.pool import WorkerPool
from sidekick_usages.entrypoints.supervisor import (
    parse_provider_launchers,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectionOperationStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import ProcessIdentity
from tests.fakes.claude.activation import claude_activation_scenario
from tests.fakes.claude.selection import existing_selection_operation
from tests.fakes.daemon.foundation import (
    CLAUDE_NATIVE_OPERATION_ID,
    FoundationState,
    foundation_state,
)
from tests.fakes.daemon.runtime import (
    SYNTHETIC_CLAUDE_LAUNCHER,
    SYNTHETIC_CODEX_LAUNCHER,
    SYNTHETIC_WORKER_EXECUTABLE,
    FakeWorkerLauncher,
    GlobalSelectionRecovery,
    RuntimeClock,
    foundation_runtime,
    selection_phase_action,
    selection_scheduler,
    worker_planner,
)
from tests.support.persistence import (
    make_application_paths,
)
from tests.support.time import FixedClock

EXPECTED_WORKER_COUNT = 2
EXPECTED_CONTROL_CONNECTIONS = 68
NATIVE_SUPERSEDED_CODE = "superseded_by_native_login"
CLAUDE_RECOVERY_OPERATION_ID = OperationId(
    "cc413f38-2b11-418a-a4a7-b0e45666067e"
)
_PARTICIPANT_A = ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337")
_PARTICIPANT_B = ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc")
_TARGET_GENERATION = AuthorityGeneration("generation-target-restored")
_LEGACY_SERVICE_STATE = b"""{
  "active_workers": 0,
  "broker_ready": true,
  "failure_code": null,
  "journals_reconciled": true,
  "observed_at": "2025-01-15T12:00:00.000000Z",
  "package_version": "0.7.0",
  "phase": "ready",
  "protocol_version": 3,
  "queue_recovered": true,
  "revision": 9,
  "schema_version": 2
}
"""


def _prove_legacy_starting_upgrade(
    runtime: SupervisorRuntime,
    state_path: Path,
) -> SupervisorRuntime:
    """Prove exact canonical v2 state upgrades on STARTING publication."""
    state_path.write_bytes(_LEGACY_SERVICE_STATE)
    state_path.chmod(0o600)
    runtime._publish(ServicePhase.STARTING)
    upgraded = ServiceStateStore(state_path).load()
    assert upgraded is not None
    assert (
        upgraded.phase,
        upgraded.revision,
        upgraded.preparation_report,
    ) == (ServicePhase.STARTING, 10, None)
    assert b'"schema_version": 3' in state_path.read_bytes()
    return runtime


def _recover_runtime_selection(
    runtime: SupervisorRuntime,
    selection_recovery: GlobalSelectionRecovery,
) -> None:
    """Prove runtime recovery invokes selection before later cycles."""
    runtime.recover()
    assert (
        selection_recovery.restore_calls,
        selection_recovery.enqueue_calls,
        MAX_CONTROL_CONNECTIONS,
    ) == (1, 0, EXPECTED_CONTROL_CONNECTIONS)


def test_runtime_enqueues_restored_selection_without_a_client(
    tmp_path: Path,
) -> None:
    """Schedule restored readback once after the first accept drain."""
    state = foundation_state(tmp_path)
    for queued in state.operations:
        state.queue.remove(
            queued.operation_id,
            expected_state=OperationState.SCHEDULED,
        )
    clock = RuntimeClock()
    wakeup = WakeupChannel()
    scheduler = DurableScheduler(
        state.queue,
        WorkerResultStore(state.paths.durable_operations),
        WorkerPool(
            FakeWorkerLauncher(
                WorkerResultStore(state.paths.durable_operations),
                clock,
                frozenset(),
            ),
            worker_planner(),
            wakeup.notify,
        ),
        clock,
        monotonic=clock.monotonic,
    )
    selection = GlobalSelectionRecovery()
    stop_requested = Event()
    stop_requested.set()
    runtime = foundation_runtime(
        state.paths,
        scheduler,
        ActivationRecoveryScheduler(
            state.journals,
            state.queue,
            selection_recovery=selection,
        ),
        clock,
        wakeup,
        stop_requested,
    )

    runtime.run()

    assert (
        selection.restore_calls,
        selection.enqueue_calls,
        selection.close_calls,
    ) == (1, 1, 1)


def _assert_native_login_cancels_stale_activation(
    state: FoundationState,
    results: WorkerResultStore,
    clock: RuntimeClock,
    scheduler: DurableScheduler,
    events: OperationEventHub,
) -> None:
    """Prove native read-back cancels and terminates an older switch."""
    stale_activation = state.operations[0]
    target = tuple(state.accounts)[1]
    clock.advance(1)
    observed_at = clock.now()
    native = DueOperation(
        operation_id=CLAUDE_NATIVE_OPERATION_ID,
        provider_id=ProviderId.CLAUDE,
        account_id=None,
        kind=OperationKind.RECONCILE_NATIVE,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.SCHEDULED,
        due_at=clock.now(),
        updated_at=clock.now(),
    )
    state.queue.enqueue(native)
    running = state.queue.transition(
        native.operation_id,
        OperationState.RUNNING,
        updated_at=clock.now(),
    )
    result = WorkerResult(
        operation_id=native.operation_id,
        outcome=WorkerOutcome.NO_CHANGE,
        finished_at=clock.now(),
        related_runtime_authority=RelatedRuntimeAuthority(
            provider_id=ProviderId.CLAUDE,
            account_id=target.account_id,
            generation=AuthorityGeneration("claude-target-generation"),
            observed_at=observed_at,
        ),
    )
    results.save(result)
    assert results.load(native.operation_id) == result
    completions = scheduler.recover()
    update = next(
        events.follow_operation(
            new_request_id(),
            stale_activation.operation_id,
        )
    )
    assert len(completions) == 1
    assert completions[0].operation_id == running.operation_id
    assert completions[0].outcome is WorkerOutcome.NO_CHANGE
    assert (
        completions[0].related_runtime_authority
        == result.related_runtime_authority
    )
    assert state.queue.find(stale_activation.operation_id) is None
    assert update.completion is not None
    assert update.completion.outcome is WorkerOutcome.CANCELLED
    assert update.completion.failure_code == NATIVE_SUPERSEDED_CODE


def test_provider_operations_use_independent_durable_slots(
    tmp_path: Path,
) -> None:
    """Provider-owned reconciliation never collides across providers."""
    queue = OperationQueueStore(
        make_application_paths(tmp_path).durable_operations
    )
    timestamp = RuntimeClock().now()
    for provider_id in ProviderId:
        queue.enqueue(
            DueOperation(
                operation_id=new_operation_id(),
                provider_id=provider_id,
                account_id=None,
                kind=OperationKind.RECONCILE_NATIVE,
                priority=OperationPriority.INTERACTIVE,
                state=OperationState.SCHEDULED,
                due_at=timestamp,
                updated_at=timestamp,
            )
        )
    assert tuple(operation.provider_id for operation in queue.load()) == tuple(
        ProviderId
    )


def _run_restarted_readback(
    state: FoundationState,
    results: WorkerResultStore,
    clock: RuntimeClock,
    wake: Event,
    prepared: PreparedSelection,
    readback_id: OperationId,
    release_commit: Event,
) -> tuple[
    FakeWorkerLauncher,
    tuple[SelectionAuthorityObservation, ...],
    bool,
    int,
]:
    restarted_queue = OperationQueueStore(state.paths.durable_operations)
    restarted_gateway = SelectionWorkerGateway(
        restarted_queue,
        clock,
        wake.set,
        operation_id_factory=iter((readback_id,)).__next__,
    )
    readback_entered = Event()
    launcher = FakeWorkerLauncher(
        results,
        clock,
        frozenset(),
        worker_actions={
            readback_id: selection_phase_action(
                state.paths.durable_operations,
                restarted_queue,
                results,
                readback_id,
                readback_entered,
            )
        },
    )
    restarted, _workers, _recovery = selection_scheduler(
        restarted_queue,
        results,
        launcher,
        restarted_gateway,
        clock,
    )
    recovered = restarted.recover() == ()
    observations: list[SelectionAuthorityObservation] = []
    reader = Thread(
        target=lambda: observations.append(
            restarted_gateway.readback(prepared)
        ),
        daemon=True,
    )
    wake.clear()
    reader.start()
    woke = wake.wait(1)
    dispatched = len(restarted.dispatch_due())
    blocked = not readback_entered.wait(0.05)
    release_commit.set()
    entered = readback_entered.wait(1)
    worker_exit = launcher.handles[readback_id].wait(1)
    clock.advance(121)
    completed = len(restarted.collect())
    reader.join(1)
    valid = recovered and woke and dispatched == 1 and entered
    valid = valid and worker_exit == 0 and not reader.is_alive()
    return launcher, tuple(observations), blocked and valid, completed


def test_selection_worker_lifetime_and_phase_ownership(
    tmp_path: Path,
) -> None:
    """Retain an orphan phase until one isolated READBACK completes."""
    state = foundation_state(tmp_path)
    for queued in state.operations:
        state.queue.remove(
            queued.operation_id,
            expected_state=OperationState.SCHEDULED,
        )
    target = tuple(state.accounts)[1]
    parent_id, phase_id, readback_id = (
        new_operation_id() for _index in range(3)
    )
    clock = RuntimeClock()
    wake = Event()
    leader_exit_code = 3
    phase_entered, leader_exited, release_phase, residual_released = (
        Event() for _index in range(4)
    )
    results = WorkerResultStore(state.paths.durable_operations)
    launcher = FakeWorkerLauncher(
        results,
        clock,
        frozenset(),
        natural_completions={phase_id: leader_exited},
        natural_exit_codes={phase_id: leader_exit_code},
        residual_completions={phase_id: residual_released},
        worker_actions={
            phase_id: selection_phase_action(
                state.paths.durable_operations,
                state.queue,
                results,
                phase_id,
                phase_entered,
                release_phase,
            )
        },
    )
    gateway = SelectionWorkerGateway(state.queue, clock, wake.set)
    scheduler, workers, recovery = selection_scheduler(
        state.queue,
        results,
        launcher,
        gateway,
        clock,
        timeout_seconds=1,
    )
    prepared = PreparedSelection(
        operation_id=parent_id,
        provider_id=ProviderId.CLAUDE,
        target_account_id=target.account_id,
        target_generation=AuthorityGeneration("generation-source"),
        baseline_epoch=SelectionEpoch(7),
        pending_epoch=SelectionEpoch(8),
    )
    state.queue.enqueue(
        DueOperation(
            operation_id=phase_id,
            selection_operation_id=parent_id,
            provider_id=ProviderId.CLAUDE,
            account_id=target.account_id,
            kind=OperationKind.SELECTION_PREVALIDATE,
            priority=OperationPriority.INTERACTIVE,
            state=OperationState.SCHEDULED,
            due_at=clock.now(),
            updated_at=clock.now(),
        )
    )
    assert len(scheduler.dispatch_due()) == 1
    assert phase_entered.wait(1)
    assert (running := state.queue.find(phase_id)) is not None
    assert running.selection_operation_id == parent_id
    leader_exited.set()
    assert scheduler.collect() == ()
    clock.advance(2)
    assert scheduler.collect() == ()
    assert scheduler.collect() == ()
    assert (recovery.orphan_calls, workers.active_count) == (1, 1)
    del scheduler, recovery
    restarted_launcher, observations, blocked, completed = (
        _run_restarted_readback(
            state,
            results,
            clock,
            wake,
            prepared,
            readback_id,
            release_phase,
        )
    )
    assert blocked
    assert completed == 1
    assert len(observations) == 1
    assert results.load(phase_id) is None
    assert results.load(readback_id) is None
    assert len(launcher.specs) == len(restarted_launcher.specs) == 1
    assert not any(
        "terminate" in event or "kill" in event for event in launcher.events
    )
    residual_released.set()
    clock.advance(4)
    (released,) = workers.reap_completed(clock.monotonic())
    assert released.exit_code == leader_exit_code
    assert (
        state.queue.find(phase_id),
        results.load(phase_id),
        launcher.events,
    ) == (None, None, [f"launch:{phase_id}"])


def test_recovery_restores_durable_participant_loss(tmp_path: Path) -> None:
    """Restore durable loss without inventing one process identity."""
    scenario = claude_activation_scenario(tmp_path)
    journal = SelectionOperationStore(scenario.paths.selection_journals)
    operation, _baseline = existing_selection_operation(scenario)
    journal.begin(operation)
    for phase in (
        SelectionPhase.PREPARING,
        SelectionPhase.WAITING_OLD_TURNS,
        SelectionPhase.COMMITTING,
    ):
        replacement = replace(
            operation,
            phase=phase,
            prepared_generation=AuthorityGeneration("generation-source"),
        )
        journal.compare_and_swap(operation, replacement)
        operation = replacement
    replacement = replace(
        operation,
        phase=SelectionPhase.RECOVERING,
        required_participant_ids=(_PARTICIPANT_A, _PARTICIPANT_B),
        lost_after_commit_participant_ids=(_PARTICIPANT_B,),
        outcome_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
    )
    journal.compare_and_swap(operation, replacement)
    operation = replacement
    registry = ParticipantRegistry(scenario.selected)
    manifest = ParticipantManifest(
        participant_id=_PARTICIPANT_A,
        provider_id=ProviderId.CLAUDE,
        client_kind=ParticipantClientKind.CLAUDE_CODE,
        capability_version=1,
        connection_generation=1,
    )
    recovery = SelectionRecovery(
        scenario.selected,
        journal,
        registry,
        SelectionWorkerGateway(
            OperationQueueStore(scenario.paths.durable_operations),
            FixedClock(),
            lambda: None,
        ),
        FixedClock(),
    )
    assert recovery.restore(ProviderId.CLAUDE)
    registered = Event()
    Thread(
        target=lambda: (
            registry.register(manifest, ProcessIdentity(1001, 1)),
            registered.set(),
        ),
        daemon=True,
    ).start()
    assert registered.wait(1)
    notices = registry.subscribe(
        new_request_id(),
        ParticipantConnectionRequest(_PARTICIPANT_A, 1),
    )
    next(notices)
    proof = AuthorityReadyProof(
        provider_id=ProviderId.CLAUDE,
        account_id=operation.target_account_id,
        generation=_TARGET_GENERATION,
        epoch=operation.pending_epoch,
        safe_code=SelectionCode.SELECTION_SUCCEEDED,
    )
    registry.prepare_target(operation.operation_id, proof)
    registry.ready_request(
        ParticipantReadyRequest(
            participant_id=_PARTICIPANT_A,
            connection_generation=1,
            proof=ParticipantReadyProof(
                account_id=proof.account_id,
                generation=proof.generation,
                epoch=proof.epoch,
            ),
        )
    )
    snapshot = registry.snapshot(ProviderId.CLAUDE)
    assert (
        snapshot.registered_count,
        snapshot.reachable_count,
        snapshot.required_participant_ids,
        snapshot.ready_participant_ids,
        snapshot.confirmed_dead_participant_ids,
        snapshot.unreachable_participant_ids,
    ) == (
        2,
        1,
        (_PARTICIPANT_A, _PARTICIPANT_B),
        (_PARTICIPANT_A,),
        (_PARTICIPANT_B,),
        (),
    )
    with pytest.raises(ParticipantRequestError):
        registry.register(
            replace(manifest, participant_id=_PARTICIPANT_B),
            ProcessIdentity(1002, 2),
        )
    recovery.complete_readback(
        SchedulerCompletion(
            provider_id=ProviderId.CLAUDE,
            operation_id=operation.operation_id,
            operation_kind=OperationKind.SELECTION_READBACK,
            state=None,
            outcome=WorkerOutcome.SUCCEEDED,
            failure_code=None,
            selection=SelectionWorkerMetadata(
                operation_id=operation.operation_id,
                provider_id=ProviderId.CLAUDE,
                kind=OperationKind.SELECTION_READBACK,
                pending_epoch=operation.pending_epoch,
                observed_account_id=operation.target_account_id,
                observed_generation=_TARGET_GENERATION,
            ),
        )
    )
    (result,) = journal.load(ProviderId.CLAUDE).history
    assert (result.outcome, result.required_count, result.lost_count) == (
        SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT,
        2,
        1,
    )
    registry.register(
        replace(manifest, participant_id=_PARTICIPANT_B),
        ProcessIdentity(1002, 2),
    )
    notices.close()


def test_supervisor_and_workers_isolate_failures_and_recover_durably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker failures remain isolated, truthful, and restart-safe."""
    assert parse_provider_launchers(
        (
            CLAUDE_LAUNCHER_OPTION,
            str(SYNTHETIC_CLAUDE_LAUNCHER),
            CODEX_LAUNCHER_OPTION,
            str(SYNTHETIC_CODEX_LAUNCHER),
        )
    ) == ProviderLaunchers(
        claude=SYNTHETIC_CLAUDE_LAUNCHER,
        codex=SYNTHETIC_CODEX_LAUNCHER,
    )
    state = foundation_state(tmp_path)
    first, second, third = state.operations
    assert state.queue.remove_account(first.required_account_id) == 1
    assert state.queue.remove_account(third.required_account_id) == 1
    first = replace(
        first,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
    )
    codex_selection = replace(
        third,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
    )
    claude_recovery = replace(
        first,
        operation_id=CLAUDE_RECOVERY_OPERATION_ID,
        kind=OperationKind.RECONCILE,
    )
    assert state.queue.enqueue(first) == first
    results = WorkerResultStore(state.paths.durable_operations)
    clock = RuntimeClock()
    wakeup = WakeupChannel()
    launcher = FakeWorkerLauncher(
        results,
        clock,
        frozenset({second.operation_id}),
    )
    workers = WorkerPool(
        launcher,
        worker_planner(),
        wakeup.notify,
        general_timeout_seconds=5,
        termination_grace_seconds=0.01,
    )
    scheduler = DurableScheduler(
        state.queue,
        results,
        workers,
        clock,
        monotonic=clock.monotonic,
    )
    selection_recovery = GlobalSelectionRecovery()
    recovery = ActivationRecoveryScheduler(
        state.journals,
        state.queue,
        selection_recovery=selection_recovery,
    )
    runtime = _prove_legacy_starting_upgrade(
        foundation_runtime(
            state.paths,
            scheduler,
            recovery,
            clock,
            wakeup,
        ),
        state.paths.service_state,
    )

    _recover_runtime_selection(runtime, selection_recovery)
    runtime.run_cycle()
    runtime.run_cycle()
    assert workers.has_capacity_for(codex_selection)
    assert not workers.has_capacity_for(claude_recovery)
    clock.advance(6)
    runtime.run_cycle()

    first_state = state.queue.find(first.operation_id)
    second_state = state.queue.find(second.operation_id)
    assert first_state is not None
    assert first_state.state is OperationState.RETRY_WAIT
    assert first_state.failure_code == "worker_timed_out"
    assert second_state is not None
    assert second_state.state is OperationState.SCHEDULED
    service_state = ServiceStateStore(state.paths.service_state).load()
    assert service_state is not None
    assert service_state.phase is ServicePhase.READY
    assert service_state.active_workers == 0

    assert len(launcher.specs) == EXPECTED_WORKER_COUNT
    for spec in launcher.specs:
        assert spec.argv == (
            str(SYNTHETIC_WORKER_EXECUTABLE),
            str(spec.operation_id),
        )
        assert spec.environment_map() == {
            "HOME": "/synthetic/home",
            "PATH": "/usr/bin",
            WORKER_CLAUDE_LAUNCHER_ENVIRONMENT_KEY: str(
                SYNTHETIC_CLAUDE_LAUNCHER
            ),
            WORKER_CODEX_LAUNCHER_ENVIRONMENT_KEY: str(
                SYNTHETIC_CODEX_LAUNCHER
            ),
        }
    restarted_workers = WorkerPool(
        FakeWorkerLauncher(results, clock, frozenset()),
        worker_planner(),
        lambda: None,
    )
    restarted_events = OperationEventHub()
    restarted = DurableScheduler(
        OperationQueueStore(state.paths.durable_operations),
        results,
        restarted_workers,
        clock,
        events=restarted_events,
        monotonic=clock.monotonic,
    )
    assert restarted.recover() == ()
    _assert_native_login_cancels_stale_activation(
        state,
        results,
        clock,
        restarted,
        restarted_events,
    )
    durable = OperationQueueStore(state.paths.durable_operations).load()
    assert len(durable) == EXPECTED_WORKER_COUNT
    assert len({operation.operation_id for operation in durable}) == len(
        durable
    )
    wakeup.close()
