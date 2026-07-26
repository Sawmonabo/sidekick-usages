"""Resident supervisor scheduling and recovery tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.identifiers import (
    new_operation_id,
    new_request_id,
)
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.control.dispatch import OperationEventHub
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.runtime.recovery import (
    ActivationRecoveryScheduler,
)
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import WakeupChannel
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.pool import WorkerPool
from sidekick_usages.entrypoints import worker
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from tests.fakes.credentials.refresh import login_account
from tests.fakes.daemon.foundation import (
    CLAUDE_NATIVE_OPERATION_ID,
    FoundationState,
    foundation_state,
    operation,
    selected,
)
from tests.fakes.daemon.runtime import (
    SYNTHETIC_WORKER_EXECUTABLE,
    FakeWorkerLauncher,
    RuntimeClock,
    foundation_runtime,
    worker_planner,
)
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
)

EXPECTED_WORKER_COUNT = 2
CLAUDE_RECOVERY_OPERATION_ID = OperationId(
    "cc413f38-2b11-418a-a4a7-b0e45666067e"
)
NATIVE_SUPERSEDED_CODE = "superseded_by_native_login"
MANAGED_AUTH_MIGRATION_REQUIRED_CODE = "managed_auth_migration_required"


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
    state.selected.save(
        selected(
            ProviderId.CLAUDE,
            target.account_id,
            "claude-target-id",
            "claude-target-generation",
            verified_in=7,
        )
    )
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
    results.save(
        WorkerResult(
            operation_id=native.operation_id,
            outcome=WorkerOutcome.NO_CHANGE,
            finished_at=clock.now(),
        )
    )
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


def test_supervisor_and_workers_isolate_failures_and_recover_durably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker failures remain isolated, truthful, and restart-safe."""
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
        state.selected,
        workers,
        clock,
        monotonic=clock.monotonic,
    )
    recovery = ActivationRecoveryScheduler(
        state.journals,
        state.queue,
    )
    runtime = foundation_runtime(
        state.paths,
        scheduler,
        recovery,
        clock,
        wakeup,
    )

    runtime.recover()
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
        state.selected,
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
