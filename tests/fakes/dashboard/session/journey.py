"""Load-bearing dashboard-session journeys."""

from pathlib import Path
from threading import Thread

import pytest

from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.cli.dashboard.session import InteractiveDashboardSession
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.entrypoints.dashboard import _connect_dashboard_control
from sidekick_usages.usage.dashboard.models import (
    DashboardFooterKind,
    DashboardSnapshot,
)
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
    DashboardConfirmationProof,
    DashboardSessionProof,
    DashboardStartupProof,
)
from tests.fakes.dashboard.session.snapshots import (
    SessionInvalidationProbe,
    SessionLookupWorker,
    SessionSnapshotSource,
    unavailable_session_snapshot,
)
from tests.fakes.dashboard.setup import guided_setup


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
        view.footer.kind,
        view.footer.message,
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
    return (
        activation_locked,
        view.controller.account_id == active_account_id,
        view.footer.message,
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
    DashboardFooterKind,
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
        view.footer.kind,
    )


def _reject_contradictory_completion(
    session: InteractiveDashboardSession,
    invalidation: SessionInvalidationProbe,
    connector: SessionControlConnector,
    daemon: SetupDaemon,
    setup_events: tuple[str, ...],
) -> tuple[bool, SidekickAccountId | None, DashboardFooterKind]:
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
        view.footer.kind,
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
    return (
        view.confirmation is None
        and view.footer.kind is DashboardFooterKind.ERROR
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
        startup_footer_kind,
    ) = startup

    unavailable = unavailable_session_snapshot(snapshot)
    partial_start_reaped = _partial_start_reaped(
        unavailable, active_account_id, state_root, monkeypatch
    )
    snapshots = SessionSnapshotSource(unavailable)
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
    lookup = SessionLookupWorker(active_account_id, fail=True)
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
        invalidation.wait_for(
            lambda: session.view.footer.kind is DashboardFooterKind.ERROR
        )
        lookup_failure_reported = (
            session.view.footer.kind is DashboardFooterKind.ERROR
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
            success_footer_kind,
        ) = _approve_setup(
            session,
            invalidation,
            daemon,
        )
        setup_not_repeated, restored_account_id, failure_footer_kind = (
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
        partial_start_reaped=partial_start_reaped,
        startup_reconciliations=startup_reconciliations,
        startup_account_id=startup_account_id,
        startup_footer_kind=startup_footer_kind,
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
        success_footer_kind=success_footer_kind,
        setup_not_repeated=setup_not_repeated,
        restored_account_id=restored_account_id,
        failure_footer_kind=failure_footer_kind,
        remote_control_scoped_to_claude=remote_control_scoped_to_claude,
        lookup_failure_reported=lookup_failure_reported,
        lookup_cancelled=lookup.cancelled,
        daemon_cancelled=daemon.cancelled,
        stream_released=connector.stream_released.is_set(),
        closed_clients=connector.closed_clients,
        post_close_invalidations=(
            invalidation.count - invalidations_before_close
        ),
    )
