"""Selector-driven resident supervisor and readiness owner."""

import selectors
import socket
from contextlib import suppress
from dataclasses import replace
from threading import (
    BoundedSemaphore,
    Event,
    Lock,
    Thread,
    current_thread,
)

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
from sidekick_usages.daemon.control import LocalControlServer
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.scheduler import DurableScheduler
from sidekick_usages.daemon.types.service import (
    PackageVersion,
    ServicePhase,
)
from sidekick_usages.persistence.service_state import ServiceStateStore

__all__ = [
    "SupervisorRuntime",
    "WakeupChannel",
]

_MAX_CONTROL_CONNECTIONS = 4
_CONNECTION_JOIN_SECONDS = 1.0
_WAKE_BYTE = b"\x00"
_WAKE_READ_BYTES = 4096


class WakeupChannel:
    """Coalescing explicit wakeup descriptor for selector work."""

    def __init__(self) -> None:
        self._reader, self._writer = socket.socketpair()
        self._reader.setblocking(False)
        self._writer.setblocking(False)
        self._closed = False

    def fileno(self) -> int:
        """Return the selector-readable descriptor."""
        return self._reader.fileno()

    def notify(self) -> None:
        """Wake the selector once; a pending byte already suffices."""
        if self._closed:
            return
        with suppress(BlockingIOError, OSError):
            self._writer.send(_WAKE_BYTE)

    def drain(self) -> None:
        """Drain all currently coalesced wake bytes."""
        while not self._closed:
            try:
                chunk = self._reader.recv(_WAKE_READ_BYTES)
            except BlockingIOError:
                return
            if not chunk:
                return

    def close(self) -> None:
        """Close both descriptors exactly once."""
        if self._closed:
            return
        self._closed = True
        self._reader.close()
        self._writer.close()


class _ConnectionGroup:
    """Bound concurrent authenticated connection threads."""

    def __init__(self, server: LocalControlServer) -> None:
        self._server = server
        self._capacity = BoundedSemaphore(_MAX_CONTROL_CONNECTIONS)
        self._lock = Lock()
        self._connections: set[socket.socket] = set()
        self._threads: set[Thread] = set()

    def accept_ready(self) -> None:
        """Accept one selector-ready peer or reject excess capacity."""
        connection = self._server.accept_connection()
        if connection is None:
            return
        if not self._capacity.acquire(blocking=False):
            connection.close()
            return
        thread = Thread(
            target=self._serve,
            args=(connection,),
            daemon=True,
            name="sidekick-control",
        )
        with self._lock:
            self._connections.add(connection)
            self._threads.add(thread)
        thread.start()

    def close(self) -> None:
        """Close active streams and join their bounded handler threads."""
        with self._lock:
            connections = tuple(self._connections)
            threads = tuple(self._threads)
        for connection in connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        for thread in threads:
            thread.join(timeout=_CONNECTION_JOIN_SECONDS)

    def _serve(self, connection: socket.socket) -> None:
        try:
            self._server.serve_connection(connection)
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._threads.discard(current_thread())
            self._capacity.release()


class SupervisorRuntime:
    """Own the local selector, durable scheduler, and service truth."""

    def __init__(
        self,
        server: LocalControlServer,
        scheduler: DurableScheduler,
        recovery: ActivationRecoveryScheduler,
        service_state: ServiceStateStore,
        clock: Clock,
        wakeup: WakeupChannel,
        stop_requested: Event,
        *,
        package_version: str = __version__,
    ) -> None:
        self._server = server
        self._scheduler = scheduler
        self._recovery = recovery
        self._service_state = service_state
        self._clock = clock
        self._wakeup = wakeup
        self._stop_requested = stop_requested
        self._package_version = PackageVersion(package_version)
        self._queue_recovered = False

    def run(self) -> None:
        """Run until explicit shutdown using only events and deadlines."""
        connections = _ConnectionGroup(self._server)
        self._publish(ServicePhase.STARTING)
        self._server.open()
        selector = selectors.DefaultSelector()
        selector.register(self._server, selectors.EVENT_READ, "control")
        selector.register(self._wakeup, selectors.EVENT_READ, "wakeup")
        try:
            self.recover()
            self.run_cycle()
            while not self._stop_requested.is_set():
                timeout = self._scheduler.next_wait_seconds()
                for key, _mask in selector.select(timeout):
                    if key.data == "control":
                        connections.accept_ready()
                    else:
                        self._wakeup.drain()
                self.run_cycle()
        finally:
            self._publish(ServicePhase.STOPPING)
            connections.close()
            self._scheduler.shutdown()
            selector.close()
            self._server.close()
            self._wakeup.close()

    def recover(self) -> None:
        """Recover durable queue and journal work before readiness."""
        self._scheduler.recover()
        self._queue_recovered = True
        self._recovery.enroll(self._clock.now())

    def run_cycle(self) -> None:
        """Collect, dispatch, and publish one event-driven work cycle."""
        self._scheduler.collect()
        self._scheduler.dispatch_due()
        self._publish(
            ServicePhase.READY
            if self._recovery.reconciled()
            else ServicePhase.DEGRADED,
            failure_code=(
                None
                if self._recovery.reconciled()
                else "reconciliation_required"
            ),
        )

    def _publish(
        self,
        phase: ServicePhase,
        *,
        failure_code: str | None = None,
    ) -> None:
        current = self._service_state.load()
        journals_reconciled = self._recovery.reconciled()
        candidate = ServiceState(
            protocol_version=PROTOCOL_VERSION,
            package_version=self._package_version,
            phase=phase,
            revision=1 if current is None else current.revision + 1,
            observed_at=self._clock.now(),
            queue_recovered=self._queue_recovered,
            journals_reconciled=journals_reconciled,
            active_workers=self._scheduler.active_count,
            failure_code=failure_code,
        )
        if (
            current is not None
            and replace(
                candidate,
                revision=current.revision,
                observed_at=current.observed_at,
            )
            == current
        ):
            return
        self._service_state.save(candidate)
