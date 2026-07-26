"""Deterministic dashboard-session boundaries."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event
from time import monotonic

import pytest

from sidekick_usages import __version__
from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
)
from sidekick_usages.cli.dashboard.session import InteractiveDashboardSession
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    CONTROL_ACTION_TIMEOUT_SECONDS,
    ControlClient,
)
from sidekick_usages.daemon.control.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    ControlEvent,
    FailedPayload,
    ProgressPayload,
    SnapshotPayload,
)
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
    ProgressPhase,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.entrypoints.dashboard import _connect_dashboard_control
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardFooterKind,
    DashboardService,
    DashboardSnapshot,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupEventObserver,
    UsageLookupWorkerEvent,
    UsageLookupWorkerResult,
)
from tests.fakes.dashboard.runtime import SetupDaemon

SESSION_WAIT_SECONDS = 2.0
DEFAULT_TEST_CONTROL_TIMEOUT_SECONDS = 5.0
SESSION_SOCKET = Path("/synthetic/sidekick-supervisor.sock")
SESSION_REQUEST_ID = RequestId("66666666-6666-4666-8666-666666666666")
SESSION_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
REMOTE_CONTROL_REQUIRED_CODE = (
    ClaudeActivationGuardFailure.REMOTE_CONTROL_DISCONNECT_REQUIRED
).failure_code


@dataclass(frozen=True, slots=True)
class DashboardSessionProof:
    """Load-bearing states captured from one serialized session journey."""

    control_connect_calls: tuple[tuple[Path, float | None], ...]
    activation_locked: bool
    confirmations: tuple[
        tuple[
            DashboardConfirmationKind | None,
            DashboardFooterKind,
            str | None,
        ],
        ...,
    ]
    activations: tuple[tuple[ProviderId, SidekickAccountId, bool], ...]
    setup_events: tuple[str, ...]
    verified_account_id: SidekickAccountId | None
    setup_not_repeated: bool
    restored_account_id: SidekickAccountId | None
    failure_footer_kind: DashboardFooterKind
    lookup_cancelled: bool
    daemon_cancelled: bool
    stream_released: bool
    closed_clients: int
    post_close_invalidations: int


class SessionSnapshotSource:
    """Return mutable synthetic cached truth through immutable snapshots."""

    def __init__(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot
        self.loads = 0

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return the latest synthetic provider-proven state."""
        del only
        self.loads += 1
        return self.snapshot

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> None:
        """Publish one provider-verified active account."""
        providers = tuple(
            (
                provider
                if provider.provider_id is not provider_id
                else replace(
                    provider,
                    runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                    active_account_id=account_id,
                    actions_enabled=True,
                    rows=tuple(
                        (
                            replace(
                                row,
                                active=row.account_id == account_id,
                            )
                            if isinstance(row, DashboardAccount)
                            else row
                        )
                        for row in provider.rows
                    ),
                )
            )
            for provider in self.snapshot.providers
        )
        self.snapshot = replace(
            self.snapshot,
            providers=providers,
            service=DashboardService(
                ready=True,
                compatible=True,
                phase=ServicePhase.READY,
                observed_at=self.snapshot.reference_time,
                failure_code=None,
            ),
        )


class SessionLookupWorker:
    """Complete one stable lookup wave and record cancellation."""

    def __init__(self, account_id: SidekickAccountId) -> None:
        self._account_id = account_id
        self.cancelled = False

    def run(
        self,
        observe: UsageLookupEventObserver | None = None,
    ) -> UsageLookupWorkerResult:
        """Publish one stable-ID completion without provider work."""
        if observe is not None:
            observe(
                UsageLookupWorkerEvent(
                    UsageLookupEventKind.ACCOUNT_COMPLETED,
                    account_id=self._account_id,
                )
            )
        return UsageLookupWorkerResult((self._account_id,))

    def cancel(self) -> None:
        """Record one idempotent session cleanup request."""
        self.cancelled = True


class SessionControlConnector:
    """Expose control only after the guided user service is ready."""

    def __init__(
        self,
        daemon: SetupDaemon,
        snapshots: SessionSnapshotSource,
    ) -> None:
        self.daemon = daemon
        self.snapshots = snapshots
        self.activations: list[tuple[ProviderId, SidekickAccountId, bool]] = []
        self.closed_clients = 0
        self.fail_next = False
        self.require_remote_control_next = False
        self.pause_next = False
        self.stream_started = Event()
        self.stream_released = Event()

    def __call__(self, socket_path: Path) -> SessionControlClient:
        """Connect only when synthetic lifecycle state is ready."""
        del socket_path
        if self.daemon.state is not ServiceLifecycleState.READY:
            raise ConnectionRefusedError
        return SessionControlClient(self)

    def wait_for_stream(self) -> None:
        """Wait for one synthetic accepted stream to become observable."""
        if not self.stream_started.wait(SESSION_WAIT_SECONDS):
            raise AssertionError("Synthetic control stream did not start.")


class SessionControlClient:
    """Return one strict correlated control-event stream."""

    def __init__(self, owner: SessionControlConnector) -> None:
        self._owner = owner
        self._closed = False
        self._paused = False

    def snapshot(self) -> Iterator[ControlEvent]:
        """Report one ready supervisor snapshot."""
        yield _event(
            EventKind.SNAPSHOT,
            SnapshotPayload(revision=1, ready=True),
        )

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        *,
        allow_remote_control_disconnect: bool = False,
    ) -> Iterator[ControlEvent]:
        """Complete or fail one correlated synthetic activation."""
        self._owner.activations.append(
            (
                provider_id,
                account_id,
                allow_remote_control_disconnect,
            )
        )
        yield _event(
            EventKind.ACCEPTED,
            AcceptedPayload(SESSION_OPERATION_ID),
        )
        if self._owner.pause_next:
            self._owner.pause_next = False
            self._paused = True
            self._owner.stream_started.set()
            if not self._owner.stream_released.wait(SESSION_WAIT_SECONDS):
                raise AssertionError(
                    "Synthetic control stream was not released."
                )
            if self._closed:
                return
        yield _event(
            EventKind.PROGRESS,
            ProgressPayload(
                SESSION_OPERATION_ID,
                ProgressPhase.VERIFYING,
            ),
        )
        if (
            self._owner.require_remote_control_next
            and not allow_remote_control_disconnect
        ):
            self._owner.require_remote_control_next = False
            yield _event(
                EventKind.FAILED,
                FailedPayload(
                    SESSION_OPERATION_ID,
                    REMOTE_CONTROL_REQUIRED_CODE,
                ),
            )
            return
        if self._owner.fail_next:
            self._owner.fail_next = False
            yield _event(
                EventKind.FAILED,
                FailedPayload(
                    SESSION_OPERATION_ID,
                    "synthetic_activation_failure",
                ),
            )
            return
        self._owner.snapshots.activate(provider_id, account_id)
        yield _event(
            EventKind.COMPLETED,
            CompletedPayload(
                SESSION_OPERATION_ID,
                CompletionOutcome.SUCCEEDED,
            ),
        )

    def refresh_account(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Iterator[ControlEvent]:
        """Return an unused but structurally valid account refresh."""
        del provider_id, account_id
        yield _event(
            EventKind.ACCEPTED,
            AcceptedPayload(SESSION_OPERATION_ID),
        )
        yield _event(
            EventKind.COMPLETED,
            CompletedPayload(
                SESSION_OPERATION_ID,
                CompletionOutcome.SUCCEEDED,
            ),
        )

    def refresh_all(self) -> Iterator[ControlEvent]:
        """Return an unused but structurally valid global refresh."""
        yield _event(EventKind.ACCEPTED, AcceptedPayload(None))
        yield _event(
            EventKind.COMPLETED,
            CompletedPayload(None, CompletionOutcome.SUCCEEDED),
        )

    def close(self) -> None:
        """Record closing observation without a service mutation."""
        if self._closed:
            return
        self._closed = True
        if self._paused:
            self._owner.stream_released.set()
        self._owner.closed_clients += 1


class SessionConnectRecorder:
    """Record the dashboard-only control read deadline."""

    def __init__(self, client: SessionControlClient) -> None:
        self._client = client
        self.calls: list[tuple[Path, float | None]] = []

    def connect(
        self,
        socket_path: Path,
        *,
        package_version: str = __version__,
        connect_timeout_seconds: float = DEFAULT_TEST_CONTROL_TIMEOUT_SECONDS,
        response_timeout_seconds: float = (
            DEFAULT_TEST_CONTROL_TIMEOUT_SECONDS
        ),
        action_timeout_seconds: float | None = CONTROL_ACTION_TIMEOUT_SECONDS,
    ) -> SessionControlClient:
        """Return one fake while retaining only timeout-safe metadata."""
        del (
            package_version,
            connect_timeout_seconds,
            response_timeout_seconds,
        )
        self.calls.append((socket_path, action_timeout_seconds))
        return self._client


class SessionInvalidationProbe:
    """Wait for background invalidations without polling or sleeping."""

    def __init__(self) -> None:
        self._event = Event()
        self.count = 0

    def __call__(self) -> None:
        """Record one thread-safe redraw request."""
        self.count += 1
        self._event.set()

    def wait_for(self, condition: Callable[[], bool]) -> None:
        """Wait for one bounded deterministic session transition."""
        deadline = monotonic() + SESSION_WAIT_SECONDS
        while not condition():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AssertionError("Dashboard session did not advance.")
            self._event.clear()
            self._event.wait(remaining)


def unavailable_session_snapshot(
    snapshot: DashboardSnapshot,
) -> DashboardSnapshot:
    """Make one controller snapshot require guided service setup."""
    return replace(
        snapshot,
        providers=tuple(
            replace(provider, actions_enabled=False)
            for provider in snapshot.providers
        ),
        service=DashboardService(
            ready=False,
            compatible=False,
            phase=None,
            observed_at=None,
            failure_code=None,
        ),
    )


def exercise_dashboard_session(
    snapshot: DashboardSnapshot,
    *,
    active_account_id: SidekickAccountId,
    preview_account_id: SidekickAccountId,
    monkeypatch: pytest.MonkeyPatch,
) -> DashboardSessionProof:
    """Exercise setup, serialized activation, failure, and bounded close."""
    unavailable = unavailable_session_snapshot(snapshot)
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
    lookup = SessionLookupWorker(active_account_id)
    connector = SessionControlConnector(daemon, snapshots)
    connector.require_remote_control_next = True
    invalidation = SessionInvalidationProbe()
    environment: dict[str, str] = {}
    session = InteractiveDashboardSession(
        unavailable,
        snapshots=snapshots,
        only=None,
        lookup=lookup,
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=GuidedServiceSetup(daemon),
        environment=environment,
    )
    environment["ANTHROPIC_API_KEY"] = "synthetic-late-secret"
    session.bind_invalidator(invalidation)
    session.start()
    try:
        session.move(DashboardMove.UP)
        session.activate()
        session.activate()
        session.restore()
        activation_locked = (
            session.view.activation_in_flight
            and session.view.controller.account_id == preview_account_id
        )
        invalidation.wait_for(lambda: session.view.confirmation is not None)
        confirmation = session.view.confirmation
        service_confirmation = (
            None if confirmation is None else confirmation.kind,
            session.view.footer.kind,
            session.view.footer.message,
        )
        session.confirm(True)
        invalidation.wait_for(lambda: session.view.confirmation is not None)
        confirmation = session.view.confirmation
        remote_confirmation = (
            None if confirmation is None else confirmation.kind,
            session.view.footer.kind,
            session.view.footer.message,
        )
        session.confirm(True)
        invalidation.wait_for(lambda: not session.view.action_in_flight)
        setup_events, verified_account_id = (
            tuple(daemon.events),
            session.view.controller.account_id,
        )

        connector.fail_next = True
        session.move(DashboardMove.DOWN)
        session.activate()
        invalidation.wait_for(lambda: not session.view.action_in_flight)
        setup_not_repeated, restored_account_id, failure_footer_kind = (
            tuple(daemon.events) == setup_events,
            session.view.controller.account_id,
            session.view.footer.kind,
        )

        connector.pause_next = True
        session.move(DashboardMove.DOWN)
        session.activate()
        connector.wait_for_stream()
        invalidations_before_close = invalidation.count
    finally:
        session.close()
        session.close()
    return DashboardSessionProof(
        control_connect_calls=tuple(connect.calls),
        activation_locked=activation_locked,
        confirmations=(service_confirmation, remote_confirmation),
        activations=tuple(connector.activations),
        setup_events=setup_events,
        verified_account_id=verified_account_id,
        setup_not_repeated=setup_not_repeated,
        restored_account_id=restored_account_id,
        failure_footer_kind=failure_footer_kind,
        lookup_cancelled=lookup.cancelled,
        daemon_cancelled=daemon.cancelled,
        stream_released=connector.stream_released.is_set(),
        closed_clients=connector.closed_clients,
        post_close_invalidations=(
            invalidation.count - invalidations_before_close
        ),
    )


def _event(
    kind: EventKind,
    payload: (
        AcceptedPayload
        | CompletedPayload
        | FailedPayload
        | ProgressPayload
        | SnapshotPayload
    ),
) -> ControlEvent:
    return ControlEvent(
        protocol_version=PROTOCOL_VERSION,
        request_id=SESSION_REQUEST_ID,
        kind=kind,
        payload=payload,
        package_version=__version__,
    )
