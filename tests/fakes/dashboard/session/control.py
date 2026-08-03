"""Synthetic dashboard control connection and event streams."""

from collections.abc import Iterator
from pathlib import Path
from threading import Event

from sidekick_usages import __version__
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    CONTROL_ACTION_TIMEOUT_SECONDS,
)
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
    PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
    ProgressPhase,
)
from tests.fakes.dashboard.runtime import SetupDaemon
from tests.fakes.dashboard.session.models import (
    DEFAULT_TEST_CONTROL_TIMEOUT_SECONDS,
    SESSION_OPERATION_ID,
    SESSION_REQUEST_ID,
    SESSION_WAIT_SECONDS,
)
from tests.fakes.dashboard.session.snapshots import SessionSnapshotSource


class SessionControlConnector:
    """Expose control only after the guided user service is ready."""

    def __init__(
        self,
        daemon: SetupDaemon,
        snapshots: SessionSnapshotSource,
    ) -> None:
        self.daemon = daemon
        self.snapshots = snapshots
        self.reconciliation_targets: dict[
            ProviderId,
            SidekickAccountId,
        ] = {}
        self.reconciliations: list[ProviderId] = []
        self.reconciliation_failures: set[ProviderId] = set()
        self.activations: list[tuple[ProviderId, SidekickAccountId]] = []
        self.closed_clients = 0
        self.skip_readback_next = False
        self.snapshot_ready = True
        self.pause_next = False
        self.allow_degraded = False
        self.stream_started = Event()
        self.stream_released = Event()

    def __call__(self, socket_path: Path) -> SessionControlClient:
        """Connect only when synthetic lifecycle state is ready."""
        del socket_path
        if (
            self.daemon.state is not ServiceLifecycleState.READY
            and not self.allow_degraded
        ):
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
            SnapshotPayload(
                revision=1,
                ready=self._owner.snapshot_ready,
            ),
        )

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Iterator[ControlEvent]:
        """Complete or fail one correlated synthetic activation."""
        self._owner.activations.append((provider_id, account_id))
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
        if self._owner.skip_readback_next:
            self._owner.skip_readback_next = False
        else:
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

    def reconcile(
        self,
        provider_id: ProviderId,
    ) -> Iterator[ControlEvent]:
        """Publish one synthetic provider-native read-back."""
        self._owner.reconciliations.append(provider_id)
        yield _event(
            EventKind.ACCEPTED,
            AcceptedPayload(SESSION_OPERATION_ID),
        )
        if provider_id in self._owner.reconciliation_failures:
            self._owner.reconciliation_failures.remove(provider_id)
            raise OSError("Synthetic provider read-back failed.")
        target = self._owner.reconciliation_targets.get(provider_id)
        if target is not None:
            self._owner.snapshots.activate(provider_id, target)
        yield _event(
            EventKind.COMPLETED,
            CompletedPayload(
                SESSION_OPERATION_ID,
                CompletionOutcome.SUCCEEDED,
            ),
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
