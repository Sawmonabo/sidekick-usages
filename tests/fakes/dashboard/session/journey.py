"""Load-bearing dashboard-session journeys."""

from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    ClaudeAssociationRequest,
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
from sidekick_usages.entrypoints.dashboard import _connect_dashboard_control
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
    DashboardStatusKind,
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
from tests.fakes.dashboard.setup import guided_setup
from tests.support.time import FixedClock


def _association_handoff(
    snapshot: DashboardSnapshot,
    account_id: SidekickAccountId,
    state_root: Path,
) -> tuple[ClaudeAssociationRequest | None, bool]:
    """Return setup-only work without contacting the action owner."""
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
    snapshots = SessionSnapshotSource(setup_snapshot)
    daemon = SetupDaemon(ServiceLifecycleState.READY)
    connector = SessionControlConnector(daemon, snapshots)
    session = InteractiveDashboardSession(
        setup_snapshot,
        snapshots=snapshots,
        only=None,
        lookup=SessionLookupWorker(account_id),
        metrics_refresh=SessionMetricsRefreshSink(
            FixedClock(snapshot.reference_time)
        ),
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, state_root / "association.json"),
        environment={},
    )
    request = session.activate()
    blocked = DashboardController.start(
        replace(
            setup_snapshot,
            providers=(
                replace(claude, actions_enabled=False, rows=setup_rows),
                codex,
            ),
        )
    ).move(DashboardMove.UP)
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
    skipped_daemon = (
        not session.view.action_in_flight
        and not connector.activations
        and isinstance(
            blocked.activate_or_repair(),
            DashboardSelectionRefusal,
        )
        and all(
            isinstance(
                controller.activate_or_repair(),
                DashboardSelectionRefusal,
            )
            for controller in unavailable
        )
    )
    session.close()
    return request, skipped_daemon


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
        lookup=lookup,
        metrics_refresh=SessionMetricsRefreshSink(
            FixedClock(snapshot.reference_time)
        ),
        connector=SessionControlConnector(daemon, snapshots),
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, state_root / "partial.json"),
        environment={},
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
    session.activate()
    session.activate()
    session.restore()
    view = session.view
    activation_locked = (
        view.activation_in_flight
        and view.controller.account_id == preview_account_id
    )
    invalidation.wait_for(lambda: session.view.confirmation is not None)
    confirmation = _confirmation_proof(session)
    session.confirm(False)
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    status = view.footer.status
    return (
        activation_locked,
        view.controller.account_id == active_account_id,
        None if status is None else status.message,
        confirmation,
    )


def _approve_setup(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    daemon: SetupDaemon,
) -> tuple[
    DashboardConfirmationProof,
    tuple[str, ...],
    SidekickAccountId | None,
    DashboardFooter,
]:
    """Approve setup and the exact Claude Remote Control retry."""
    session.move(DashboardMove.UP)
    session.activate()
    invalidation.wait_for(lambda: session.view.confirmation is not None)
    session.confirm(True)
    invalidation.wait_for(lambda: session.view.confirmation is not None)
    remote_confirmation = _confirmation_proof(session)
    session.confirm(True)
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    return (
        remote_confirmation,
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
    session.activate()
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    return (
        daemon.events.count("install:claude")
        == setup_events.count("install:claude"),
        view.controller.account_id,
        view.footer,
    )


def _reject_codex_remote_control_code(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    connector: SessionControlConnector,
) -> bool:
    """Treat a malformed Codex Remote Control code as ordinary failure."""
    connector.require_remote_control_next = True
    session.focus_next_provider()
    session.move(DashboardMove.UP)
    session.activate()
    invalidation.wait_for(lambda: not session.view.action_in_flight)
    view = session.view
    status = view.footer.status
    return (
        view.confirmation is None
        and status is not None
        and status.kind is DashboardStatusKind.ERROR
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
        lookup=lookup,
        metrics_refresh=metrics_refresh,
        connector=SessionControlConnector(daemon, snapshots),
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, state_root / artifact_name),
        environment={},
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
        lookup=lookup,
        metrics_refresh=metrics_refresh,
        connector=SessionControlConnector(daemon, snapshots),
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, state_root / "worker-retry.json"),
        environment={},
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
    """Exercise setup, serialized activation, failure, and bounded close."""
    (
        startup_reconciliations,
        startup_account_id,
        startup_footer,
    ) = startup
    association_request, association_skipped_daemon = _association_handoff(
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
    dashboard_client = _connect_dashboard_control(SESSION_SOCKET)
    dashboard_client.close()

    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    lookup = SessionLookupWorker(active_account_id, account_failure=True)
    metrics_refresh = SessionMetricsRefreshSink(
        FixedClock(snapshot.reference_time)
    )
    connector = SessionControlConnector(daemon, snapshots)
    connector.require_remote_control_next = True
    connector.snapshot_ready = False
    invalidation = SessionInvalidationProbe()
    environment: dict[str, str] = {}
    session = InteractiveDashboardSession(
        unavailable,
        snapshots=snapshots,
        only=None,
        lookup=lookup,
        metrics_refresh=metrics_refresh,
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, state_root / "setup.json"),
        environment=environment,
    )
    environment["ANTHROPIC_API_KEY"] = "synthetic-late-secret"
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
            activation_locked,
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
            remote_confirmation,
            setup_events,
            verified_account_id,
            success_footer,
        ) = _approve_setup(
            session,
            invalidation,
            daemon,
        )
        setup_not_repeated, restored_account_id, failure_footer = (
            _reject_contradictory_completion(
                session,
                invalidation,
                connector,
                daemon,
                setup_events,
            )
        )
        remote_control_scoped_to_claude = _reject_codex_remote_control_code(
            session,
            invalidation,
            connector,
        )

        connector.pause_next = True
        session.focus_next_provider()
        session.move(DashboardMove.DOWN)
        session.activate()
        connector.wait_for_stream()
        invalidations_before_close = invalidation.count
    finally:
        session.close()
        session.close()
    return DashboardSessionProof(
        control_connect_calls=tuple(connect.calls),
        association_request=association_request,
        association_skipped_daemon=association_skipped_daemon,
        partial_start_reaped=partial_start_reaped,
        startup_reconciliations=startup_reconciliations,
        startup_account_id=startup_account_id,
        startup_footer=startup_footer,
        activation_locked=activation_locked,
        confirmations=(service_confirmation, remote_confirmation),
        activations=tuple(connector.activations),
        setup_events=setup_events,
        setup_progress_sanitized=EXPECTED_SERVICE_SETUP_PROGRESS.issubset(
            invalidation.progress_messages
        ),
        setup_refusal_restored=setup_refusal_restored,
        setup_refusal_message=setup_refusal_message,
        verified_account_id=verified_account_id,
        success_footer=success_footer,
        setup_not_repeated=setup_not_repeated,
        restored_account_id=restored_account_id,
        failure_footer=failure_footer,
        remote_control_scoped_to_claude=remote_control_scoped_to_claude,
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
