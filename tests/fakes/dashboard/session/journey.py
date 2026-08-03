"""Load-bearing dashboard-session journeys."""

from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardMove,
    DashboardSelectionRefusal,
)
from sidekick_usages.cli.dashboard.session import InteractiveDashboardSession
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.persistence.errors import (
    ManagedFileReadError,
    PersistenceError,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    PersistenceCode,
    UsageSnapshotFailureKind,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardFooter,
    DashboardSnapshot,
)
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshObservation,
)
from tests.fakes.dashboard.render import interactive_dashboard_state
from tests.fakes.dashboard.runtime import (
    EXPECTED_SERVICE_SETUP_PROGRESS,
    SetupDaemon,
)
from tests.fakes.dashboard.session.control import (
    SessionConnectRecorder,
    SessionControlClient,
    SessionControlConnector,
)
from tests.fakes.dashboard.session.models import (
    SESSION_SOCKET,
    DashboardCacheRetryProof,
    DashboardConfirmationProof,
    DashboardMetricsRetryProof,
    DashboardSessionProof,
    DashboardStartupProof,
)
from tests.fakes.dashboard.session.snapshots import (
    SessionInvalidationProbe,
    SessionLookupWorker,
    SessionMetricsRefreshSink,
    SessionSnapshotSource,
    unavailable_session_snapshot,
)
from tests.fakes.dashboard.setup import dashboard_runtime, guided_setup
from tests.support.time import FixedClock


def _selection_refusal(
    snapshot: DashboardSnapshot,
    account_id: SidekickAccountId,
    state_root: Path,
) -> DashboardFooter:
    """Return one visible refusal without contacting the action owner."""
    claude, codex = snapshot.providers
    setup_rows = tuple(
        (
            replace(
                row,
                states=(DashboardActionState.SWITCH_SETUP_REQUIRED,),
            )
            if isinstance(row, DashboardAccount)
            and row.account_id == account_id
            else row
        )
        for row in claude.rows
    )
    setup_snapshot = replace(
        snapshot,
        providers=(
            replace(
                claude,
                runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                active_account_id=None,
                rows=setup_rows,
            ),
            codex,
        ),
    )
    blocked_snapshot = replace(
        setup_snapshot,
        providers=(
            replace(claude, actions_enabled=False, rows=setup_rows),
            codex,
        ),
    )
    blocked = DashboardController.start(blocked_snapshot).move(
        DashboardMove.UP
    )
    unavailable = tuple(
        replace(
            blocked,
            snapshot=replace(
                blocked.snapshot,
                providers=(
                    replace(
                        blocked.snapshot.providers[0],
                        runtime_state=runtime_state,
                        active_account_id=None,
                        actions_enabled=True,
                    ),
                    codex,
                ),
            ),
        )
        for runtime_state in (
            ProviderRuntimeState.UNREADABLE,
            ProviderRuntimeState.UNSUPPORTED,
        )
    )
    assert isinstance(
        blocked.select_account(),
        DashboardSelectionRefusal,
    )
    assert all(
        isinstance(
            controller.select_account(),
            DashboardSelectionRefusal,
        )
        for controller in unavailable
    )
    daemon = SetupDaemon(ServiceLifecycleState.READY)
    refusal_snapshots = SessionSnapshotSource(blocked_snapshot)
    refusal_session = InteractiveDashboardSession(
        blocked_snapshot,
        snapshots=refusal_snapshots,
        only=None,
        runtime=dashboard_runtime(
            refusal_snapshots,
            None,
            SessionLookupWorker(account_id),
            SessionMetricsRefreshSink(FixedClock(snapshot.reference_time)),
            SessionControlConnector(daemon, refusal_snapshots),
            SESSION_SOCKET,
            guided_setup(daemon, state_root / "selection-refusal.json"),
        ),
    )
    refusal_session.move(DashboardMove.UP)
    refusal_session.select_account()
    selection_refusal_footer = refusal_session.view.footer
    refusal_session.close()
    return selection_refusal_footer


def _partial_start_reaped(
    snapshot: DashboardSnapshot,
    account_id: SidekickAccountId,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> bool:
    """Interrupt the second owner start and prove the first owner exits."""
    snapshots = SessionSnapshotSource(snapshot)
    daemon = SetupDaemon(ServiceLifecycleState.READY)
    lookup = SessionLookupWorker(account_id, block=True)
    session = InteractiveDashboardSession(
        snapshot,
        snapshots=snapshots,
        only=None,
        runtime=dashboard_runtime(
            snapshots,
            None,
            lookup,
            SessionMetricsRefreshSink(FixedClock(snapshot.reference_time)),
            SessionControlConnector(daemon, snapshots),
            SESSION_SOCKET,
            guided_setup(daemon, state_root / "partial.json"),
        ),
    )
    start_thread = Thread.start

    def interrupt_action_start(thread: Thread) -> None:
        if thread.name == "sidekick-dashboard-actions":
            raise KeyboardInterrupt
        start_thread(thread)

    with monkeypatch.context() as start_boundary:
        start_boundary.setattr(Thread, "start", interrupt_action_start)
        with pytest.raises(KeyboardInterrupt):
            session.start()
    session.close()
    return (
        session.stopping
        and lookup.cancelled
        and lookup.finished.is_set()
        and daemon.cancelled
    )


def _confirmation_proof(
    session: InteractiveDashboardSession,
) -> DashboardConfirmationProof:
    """Capture one atomic confirmation view."""
    view = session.view
    return (
        (None if view.confirmation is None else view.confirmation.kind),
        view.footer,
    )


def _refuse_setup(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    *,
    active_account_id: SidekickAccountId,
    preview_account_id: SidekickAccountId,
) -> tuple[bool, bool, str | None, DashboardConfirmationProof]:
    """Refuse setup and return provider-read-back restoration proof."""
    session.move(DashboardMove.UP)
    session.select_account()
    session.select_account()
    session.restore()
    view = session.view
    selection_locked = (
        view.selection_in_flight
        and view.controller.account_id == preview_account_id
    )
    invalidation.wait_for(lambda: session.view.confirmation is not None)
    confirmation = _confirmation_proof(session)
    session.confirm(False)
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    status = view.footer.status
    return (
        selection_locked,
        view.controller.account_id == active_account_id,
        None if status is None else status.message,
        confirmation,
    )


def _approve_setup(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    daemon: SetupDaemon,
) -> tuple[
    tuple[str, ...],
    SidekickAccountId | None,
    DashboardFooter,
]:
    """Approve setup and capture the verified selection result."""
    session.move(DashboardMove.UP)
    session.select_account()
    invalidation.wait_for(lambda: session.view.confirmation is not None)
    session.confirm(True)
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    return (
        tuple(daemon.events),
        view.controller.account_id,
        view.footer,
    )


def _reject_contradictory_completion(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    connector: SessionControlConnector,
    daemon: SetupDaemon,
    setup_events: tuple[str, ...],
) -> tuple[bool, SidekickAccountId | None, DashboardFooter]:
    """Reject terminal success that provider read-back contradicts."""
    connector.skip_readback_next = True
    session.move(DashboardMove.DOWN)
    session.select_account()
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    return (
        daemon.events.count("install:claude")
        == setup_events.count("install:claude"),
        view.controller.account_id,
        view.footer,
    )


def _cache_read_retry(
    snapshot: DashboardSnapshot,
    account_id: SidekickAccountId,
    state_root: Path,
    second_result: DashboardSnapshot | PersistenceError,
    artifact_name: str,
) -> DashboardCacheRetryProof:
    """Capture one cache-only retry without repeating provider work."""
    snapshots = SessionSnapshotSource(snapshot)
    snapshots.queue_lookup_snapshots(
        replace(
            snapshot,
            activity_cache_issue=ActivitySnapshotFailureKind.MALFORMED,
            usage_cache_issue=UsageSnapshotFailureKind.READ,
        ),
        second_result,
    )
    daemon = SetupDaemon(ServiceLifecycleState.READY)
    lookup = SessionLookupWorker(account_id)
    metrics_refresh = SessionMetricsRefreshSink(
        FixedClock(snapshot.reference_time)
    )
    session = InteractiveDashboardSession(
        snapshot,
        snapshots=snapshots,
        only=None,
        runtime=dashboard_runtime(
            snapshots,
            None,
            lookup,
            metrics_refresh,
            SessionControlConnector(daemon, snapshots),
            SESSION_SOCKET,
            guided_setup(daemon, state_root / artifact_name),
        ),
    )
    session.start()
    try:
        metrics_refresh.wait_until_recorded()
        footer = session.view.footer
        observation = metrics_refresh.observations[-1]
    finally:
        session.close()
    return DashboardCacheRetryProof(
        lookup_runs=lookup.runs,
        snapshot_loads=snapshots.lookup_loads,
        footer=footer,
        observation=observation,
    )


def _worker_retry(
    snapshot: DashboardSnapshot,
    account_id: SidekickAccountId,
    state_root: Path,
) -> tuple[int, DashboardFooter, MetricsRefreshObservation]:
    """Capture one self-healed worker lock without a footer warning."""
    snapshots = SessionSnapshotSource(snapshot)
    daemon = SetupDaemon(ServiceLifecycleState.READY)
    lookup = SessionLookupWorker(
        account_id,
        account_failure=True,
        transient_failure=PersistenceCode.STORE_LOCKED,
    )
    metrics_refresh = SessionMetricsRefreshSink(
        FixedClock(snapshot.reference_time)
    )
    session = InteractiveDashboardSession(
        snapshot,
        snapshots=snapshots,
        only=None,
        runtime=dashboard_runtime(
            snapshots,
            None,
            lookup,
            metrics_refresh,
            SessionControlConnector(daemon, snapshots),
            SESSION_SOCKET,
            guided_setup(daemon, state_root / "worker-retry.json"),
        ),
    )
    session.start()
    try:
        metrics_refresh.wait_until_recorded()
        footer = session.view.footer
        observation = metrics_refresh.observations[-1]
    finally:
        session.close()
    return lookup.runs, footer, observation


def _metrics_retry_proof(
    snapshot: DashboardSnapshot,
    state_root: Path,
) -> DashboardMetricsRetryProof:
    """Capture the two bounded retry paths against saved metrics."""
    metrics_snapshot, _cursor, _footer = interactive_dashboard_state(
        snapshot.reference_time
    )
    account_id = metrics_snapshot.providers[0].active_account_id
    if account_id is None:
        raise AssertionError("Metrics retry snapshot has no active account.")
    recovered_cache = _cache_read_retry(
        metrics_snapshot,
        account_id,
        state_root,
        metrics_snapshot,
        "cache-retry.json",
    )
    failed_cache = _cache_read_retry(
        metrics_snapshot,
        account_id,
        state_root,
        ManagedFileReadError("usage-metrics.json"),
        "cache-retry-failure.json",
    )
    worker_runs, worker_footer, worker_observation = _worker_retry(
        metrics_snapshot,
        account_id,
        state_root,
    )
    return DashboardMetricsRetryProof(
        worker_runs=worker_runs,
        worker_footer=worker_footer,
        worker_observation=worker_observation,
        recovered_cache=recovered_cache,
        failed_cache=failed_cache,
    )


def exercise_dashboard_session(
    snapshot: DashboardSnapshot,
    *,
    active_account_id: SidekickAccountId,
    preview_account_id: SidekickAccountId,
    startup: DashboardStartupProof,
    state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> DashboardSessionProof:
    """Exercise setup, serialized selection, failure, and bounded close."""
    (
        startup_reconciliations,
        startup_account_id,
        startup_footer,
    ) = startup
    selection_refusal_footer = _selection_refusal(
        snapshot,
        preview_account_id,
        state_root,
    )
    metrics_retry = _metrics_retry_proof(snapshot, state_root)

    unavailable = unavailable_session_snapshot(snapshot)
    partial_start_reaped = _partial_start_reaped(
        unavailable, active_account_id, state_root, monkeypatch
    )
    snapshots = SessionSnapshotSource(
        replace(
            unavailable,
            activity_cache_issue=ActivitySnapshotFailureKind.MALFORMED,
        )
    )
    option_connector = SessionControlConnector(
        SetupDaemon(ServiceLifecycleState.READY),
        snapshots,
    )
    connect = SessionConnectRecorder(SessionControlClient(option_connector))
    monkeypatch.setattr(
        ControlClient,
        "connect",
        staticmethod(connect.connect),
    )
    dashboard_client = ControlClient.connect(SESSION_SOCKET)
    dashboard_client.close()

    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    lookup = SessionLookupWorker(active_account_id, account_failure=True)
    metrics_refresh = SessionMetricsRefreshSink(
        FixedClock(snapshot.reference_time)
    )
    connector = SessionControlConnector(daemon, snapshots)
    connector.snapshot_ready = False
    invalidation = SessionInvalidationProbe()
    session = InteractiveDashboardSession(
        unavailable,
        snapshots=snapshots,
        only=None,
        runtime=dashboard_runtime(
            snapshots,
            None,
            lookup,
            metrics_refresh,
            connector,
            SESSION_SOCKET,
            guided_setup(daemon, state_root / "setup.json"),
        ),
    )
    invalidation.bind_session(session)
    session.bind_invalidator(invalidation)
    session.start()
    try:
        lookup.wait_until_finished()
        metrics_refresh.wait_until_recorded()
        failed_account = next(
            row
            for provider in session.view.snapshot.providers
            for row in provider.rows
            if isinstance(row, DashboardAccount)
            and row.account_id == active_account_id
        )
        failed_freshness = failed_account.metrics_freshness
        assert failed_freshness is MetricsFreshness.UNAVAILABLE
        lookup_failure = (
            failed_freshness,
            not {
                "Refreshing account metrics.",
                "Updated account metrics.",
            }.intersection(invalidation.progress_messages),
        )
        (
            selection_locked,
            setup_refusal_restored,
            setup_refusal_message,
            service_confirmation,
        ) = _refuse_setup(
            session,
            invalidation,
            active_account_id=active_account_id,
            preview_account_id=preview_account_id,
        )
        (
            setup_events,
            verified_account_id,
            success_footer,
        ) = _approve_setup(
            session,
            invalidation,
            daemon,
        )
        finalized_epoch = session.view.snapshot.providers[0].finalized_epoch
        session.select_account()
        invalidation.wait_for(lambda: not session.view.action_in_flight)
        already_selected_footer = session.view.footer
        setup_not_repeated, restored_account_id, failure_footer = (
            _reject_contradictory_completion(
                session,
                invalidation,
                connector,
                daemon,
                setup_events,
            )
        )
        connector.pause_next = True
        session.focus_next_provider()
        session.move(DashboardMove.DOWN)
        session.select_account()
        connector.wait_for_stream()
        assert "Waiting for 0 active turns…" in invalidation.progress_messages
        invalidations_before_close = invalidation.count
    finally:
        session.close()
        session.close()
    return DashboardSessionProof(
        control_connect_calls=tuple(connect.calls),
        selection_refusal_footer=selection_refusal_footer,
        partial_start_reaped=partial_start_reaped,
        startup_reconciliations=startup_reconciliations,
        startup_account_id=startup_account_id,
        startup_footer=startup_footer,
        selection_locked=selection_locked,
        confirmations=(service_confirmation,),
        selections=tuple(connector.selections),
        setup_events=setup_events,
        setup_progress_sanitized=EXPECTED_SERVICE_SETUP_PROGRESS.issubset(
            invalidation.progress_messages
        ),
        setup_refusal_restored=setup_refusal_restored,
        setup_refusal_message=setup_refusal_message,
        verified_account_id=verified_account_id,
        finalized_epoch=finalized_epoch,
        success_footer=success_footer,
        already_selected_footer=already_selected_footer,
        setup_not_repeated=setup_not_repeated,
        restored_account_id=restored_account_id,
        failure_footer=failure_footer,
        lookup_failure=lookup_failure,
        metrics_refresh=metrics_refresh.observations[-1],
        metrics_retry=metrics_retry,
        lookup_cancelled=lookup.cancelled,
        daemon_cancelled=daemon.cancelled,
        stream_released=connector.stream_released.is_set(),
        closed_clients=connector.closed_clients,
        post_close_invalidations=(
            invalidation.count - invalidations_before_close
        ),
    )
