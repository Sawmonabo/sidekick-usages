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
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    RelatedRuntimeAuthority,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
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
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import (
    SelectionSchedulerSink,
    SelectionWorkerGateway,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.pool import WorkerPool
from sidekick_usages.daemon.worker.runtime import selection_worker_success
from sidekick_usages.entrypoints import worker
from sidekick_usages.entrypoints.supervisor import (
    parse_provider_launchers,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectionOperationStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from tests.fakes.claude.activation import claude_activation_scenario
from tests.fakes.claude.managed import use_synthetic_claude
from tests.fakes.claude.selection import existing_selection_operation
from tests.fakes.credentials.refresh import login_account
from tests.fakes.daemon.foundation import (
    CLAUDE_NATIVE_OPERATION_ID,
    FoundationState,
    foundation_state,
    operation,
)
from tests.fakes.daemon.runtime import (
    SYNTHETIC_CLAUDE_LAUNCHER,
    SYNTHETIC_CODEX_LAUNCHER,
    SYNTHETIC_WORKER_EXECUTABLE,
    FakeWorkerLauncher,
    RuntimeClock,
    entrypoint_worker_launcher,
    foundation_runtime,
    run_scheduled_gateway_call,
    run_scheduler_phase,
    worker_planner,
)
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
)
from tests.support.time import REFERENCE_TIME, FixedClock

EXPECTED_WORKER_COUNT = 2
EXPECTED_CONTROL_CONNECTIONS = 68
CLAUDE_RECOVERY_OPERATION_ID = OperationId(
    "cc413f38-2b11-418a-a4a7-b0e45666067e"
)
NATIVE_SUPERSEDED_CODE = "superseded_by_native_login"
MANAGED_AUTH_MIGRATION_REQUIRED_CODE = "managed_auth_migration_required"


class _GlobalSelectionRecovery:
    """Record pre-readiness selection recovery without provider work."""

    def __init__(self) -> None:
        self.restore_calls = 0
        self.enqueue_calls = 0
        self.close_calls = 0
        self.orphan_calls = 0

    def restore_all(self) -> tuple[ProviderId, ...]:
        self.restore_calls += 1
        return ()

    def enqueue_restored_readbacks(self) -> tuple[DueOperation, ...]:
        self.enqueue_calls += 1
        return ()

    def reconciled(self) -> bool:
        return True

    def close(self) -> None:
        self.close_calls += 1

    def complete_readback(self, completion: SchedulerCompletion) -> None:
        """Record an unexpected orphan selection completion."""
        del completion
        self.orphan_calls += 1

    def fail_readback(self, operation: DueOperation, code: str) -> None:
        """Record an unexpected orphan selection failure."""
        del operation, code
        self.orphan_calls += 1


def _recover_runtime_selection(
    runtime: SupervisorRuntime,
    selection_recovery: _GlobalSelectionRecovery,
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
    selection = _GlobalSelectionRecovery()
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
        priority=OperationPriority.INTERACTIVE,
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
    assert state.queue.find(stale_activation.operation_id) is None
    assert update.completion is not None
    assert update.completion.outcome is WorkerOutcome.CANCELLED
    assert update.completion.failure_code == NATIVE_SUPERSEDED_CODE


def _assert_unmanaged_workers_require_migration(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove legacy authorities stop before any provider composition."""
    store = make_account_store(
        root,
        (
            Account(
                label=AccountLabel("claude-setup"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-claude-setup-secret"
                ),
            ),
            login_account("claude-legacy"),
            Account(
                label=AccountLabel("codex-legacy"),
                credentials=CodexCredentials(
                    access_token="test-only-codex-secret",
                    account_id="test-only-codex-account",
                ),
            ),
        ),
    )
    paths = make_application_paths(root)
    queue = OperationQueueStore(paths.durable_operations)
    operations = tuple(
        operation(
            account.account_id,
            account.provider_id,
            str(new_operation_id()),
        )
        for account in store.saved_accounts()
    )
    for due_operation in operations:
        queue.enqueue(due_operation)
        queue.transition(
            due_operation.operation_id,
            OperationState.RUNNING,
            updated_at=due_operation.updated_at,
        )

    def reject_provider_composition(
        *arguments: object,
        **keywords: object,
    ) -> None:
        del arguments, keywords
        raise AssertionError(
            "Providers must not be composed before migration."
        )

    monkeypatch.setattr(
        worker,
        "discover_application_paths",
        lambda: paths,
    )
    monkeypatch.setattr(
        worker,
        "ClaudeProfileCapabilityFactory",
        reject_provider_composition,
    )
    monkeypatch.setattr(
        worker,
        "compose_codex_managed_authority",
        reject_provider_composition,
    )

    results = WorkerResultStore(paths.durable_operations)
    outcomes: list[tuple[WorkerOutcome, str | None]] = []
    for due_operation in operations:
        assert worker.main((str(due_operation.operation_id),)) == 0
        result = results.load(due_operation.operation_id)
        assert result is not None
        outcomes.append((result.outcome, result.failure_code))

    migration_required = (
        WorkerOutcome.ACTION_REQUIRED,
        MANAGED_AUTH_MIGRATION_REQUIRED_CODE,
    )
    assert outcomes == [
        (WorkerOutcome.SUCCEEDED, None),
        migration_required,
        migration_required,
    ]
    assert not paths.private_claude_profiles.exists()
    assert not paths.private_codex_profiles.exists()


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


def test_selection_worker_result_retains_only_safe_authority_relation(
    tmp_path: Path,
) -> None:
    """Sanitize related and unrelated provider selection observations."""
    state = foundation_state(tmp_path)
    target = tuple(state.accounts)[1]
    phase = replace(
        state.operations[0],
        account_id=target.account_id,
        kind=OperationKind.SELECTION_PREVALIDATE,
    )
    pending_epoch = SelectionEpoch(1)
    related = selection_worker_success(
        phase,
        pending_epoch,
        SelectionAuthorityObservation(
            provider_id=phase.provider_id,
            account_id=target.account_id,
            generation=AuthorityGeneration("generation-source-1"),
        ),
        RuntimeClock(),
    )
    unrelated = selection_worker_success(
        replace(phase, kind=OperationKind.SELECTION_READBACK),
        pending_epoch,
        SelectionAuthorityObservation(
            provider_id=phase.provider_id,
            account_id=None,
            generation=None,
        ),
        RuntimeClock(),
    )

    assert related.selection is not None
    assert (
        related.selection.observed_account_id,
        related.selection.observed_generation,
        unrelated.selection,
    ) == (
        target.account_id,
        AuthorityGeneration("generation-source-1"),
        SelectionWorkerMetadata(
            operation_id=phase.operation_id,
            provider_id=phase.provider_id,
            kind=OperationKind.SELECTION_READBACK,
            pending_epoch=pending_epoch,
            observed_account_id=None,
            observed_generation=None,
        ),
    )


def test_selection_worker_gateway_runs_every_phase_through_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Run all selection phases through queue, worker, result, and gateway."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(tmp_path)
    paths = scenario.paths
    queue = OperationQueueStore(paths.durable_operations)
    journal = SelectionOperationStore(paths.selection_journals)
    wake = Event()
    gateway = SelectionWorkerGateway(queue, FixedClock(), wake.set)
    open_operation, baseline = existing_selection_operation(scenario)
    active = journal.begin(open_operation)
    monkeypatch.setattr(worker, "discover_application_paths", lambda: paths)
    monkeypatch.setattr(
        worker,
        "_claude_selection_executor",
        lambda *arguments, **keywords: scenario.executor,
    )
    results = WorkerResultStore(paths.durable_operations)
    launcher, _kinds = entrypoint_worker_launcher(
        queue,
        lambda operation_id: worker.main((str(operation_id),)),
    )
    recovery = _GlobalSelectionRecovery()
    scheduler = DurableScheduler(
        queue,
        results,
        WorkerPool(launcher, worker_planner(), lambda: None),
        RuntimeClock(),
        events=SelectionSchedulerSink(
            OperationEventHub(),
            gateway,
            recovery,
        ),
    )

    prepared = run_scheduled_gateway_call(
        lambda: gateway.prevalidate(active, baseline),
        wake,
        scheduler,
    )
    active = journal.compare_and_swap(
        active,
        replace(
            active,
            phase=SelectionPhase.PREPARING,
            prepared_generation=prepared.target_generation,
        ),
    )
    for phase in (
        SelectionPhase.WAITING_OLD_TURNS,
        SelectionPhase.COMMITTING,
    ):
        active = journal.compare_and_swap(
            active,
            replace(active, phase=phase),
        )
    proof = run_scheduled_gateway_call(
        lambda: gateway.commit(prepared),
        wake,
        scheduler,
    )
    active = journal.compare_and_swap(
        active,
        replace(
            active,
            phase=SelectionPhase.AWAITING_READY,
            target_generation=proof.generation,
        ),
    )
    observation = run_scheduled_gateway_call(
        lambda: gateway.readback(prepared),
        wake,
        scheduler,
    )

    assert (
        prepared.target_account_id,
        proof.account_id,
        observation.account_id,
    ) == (scenario.target.account_id,) * 3
    assert observation.generation == proof.generation
    assert queue.find(active.operation_id) is None
    assert results.load(active.operation_id) is None
    assert recovery.orphan_calls == 0


def test_postcommit_failure_reads_back_without_replaying_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resolve ambiguous native mutation through one queued readback."""
    use_synthetic_claude(monkeypatch)
    scenario = claude_activation_scenario(
        tmp_path,
        advance_native_mtime=False,
    )
    paths = scenario.paths
    queue = OperationQueueStore(paths.durable_operations)
    journal = SelectionOperationStore(paths.selection_journals)
    registry = ParticipantRegistry(scenario.selected)
    wake = Event()
    gateway = SelectionWorkerGateway(queue, FixedClock(), wake.set)
    recovery = SelectionRecovery(
        scenario.selected,
        journal,
        registry,
        gateway,
        FixedClock(),
    )
    coordinator = SelectionCoordinator(
        scenario.selected,
        journal,
        registry,
        gateway,
        FixedClock(),
        resume_recovery=recovery.resume,
    )
    monkeypatch.setattr(worker, "discover_application_paths", lambda: paths)
    monkeypatch.setattr(
        worker,
        "_claude_selection_executor",
        lambda *arguments, **keywords: scenario.executor,
    )
    results = WorkerResultStore(paths.durable_operations)
    launcher, launched_kinds = entrypoint_worker_launcher(
        queue,
        lambda operation_id: worker.main((str(operation_id),)),
    )
    scheduler = DurableScheduler(
        queue,
        results,
        WorkerPool(launcher, worker_planner(), lambda: None),
        RuntimeClock(),
        events=SelectionSchedulerSink(
            OperationEventHub(),
            gateway,
            recovery,
        ),
    )
    selections: list[SelectionResult] = []
    selector = Thread(
        target=lambda: selections.append(
            coordinator.select(
                scenario.operation.operation_id,
                ProviderId.CLAUDE,
                scenario.target.account_id,
            )
        ),
        daemon=True,
    )
    selector.start()
    for _phase in range(3):
        run_scheduler_phase(wake, scheduler)
    selector.join(2)

    finalized = scenario.selected.load(ProviderId.CLAUDE)
    assert not selector.is_alive()
    assert len(selections) == 1
    assert selections[0].outcome is SelectionOutcome.RECOVERY_REQUIRED
    assert finalized is not None
    assert finalized.account_id == scenario.target.account_id
    assert (
        tuple(spec.operation_id for spec in launcher.specs)
        == (scenario.operation.operation_id,) * 3
    )
    assert launched_kinds == [
        OperationKind.SELECTION_PREVALIDATE,
        OperationKind.SELECTION_COMMIT,
        OperationKind.SELECTION_READBACK,
    ]
    assert queue.find(scenario.operation.operation_id) is None
    assert journal.load(ProviderId.CLAUDE).active is None


def test_restart_discards_orphan_selection_worker_phase(
    tmp_path: Path,
) -> None:
    """Let the durable selection journal alone choose restart recovery."""
    state = foundation_state(tmp_path)
    phase = replace(
        state.operations[0],
        kind=OperationKind.SELECTION_COMMIT,
    )
    state.queue.remove(
        state.operations[0].operation_id,
        expected_state=OperationState.SCHEDULED,
    )
    state.queue.enqueue(phase)
    results = WorkerResultStore(state.paths.durable_operations)
    results.save(
        WorkerResult(
            operation_id=phase.operation_id,
            outcome=WorkerOutcome.TRANSIENT_FAILURE,
            finished_at=REFERENCE_TIME,
            failure_code="worker_interrupted",
        )
    )
    clock = RuntimeClock()
    scheduler = DurableScheduler(
        state.queue,
        results,
        WorkerPool(
            FakeWorkerLauncher(results, clock, frozenset()),
            worker_planner(),
            lambda: None,
        ),
        clock,
        monotonic=clock.monotonic,
    )

    assert scheduler.recover() == ()
    assert state.queue.find(phase.operation_id) is None
    assert results.load(phase.operation_id) is None


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
    selection_recovery = _GlobalSelectionRecovery()
    recovery = ActivationRecoveryScheduler(
        state.journals,
        state.queue,
        selection_recovery=selection_recovery,
    )
    runtime = foundation_runtime(
        state.paths,
        scheduler,
        recovery,
        clock,
        wakeup,
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
    _assert_unmanaged_workers_require_migration(
        tmp_path / "unmanaged-workers",
        monkeypatch,
    )
