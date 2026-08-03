"""Typed supervisor runtime boundaries for daemon tests."""

import os
import socket
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.types import OperationKind
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.models.control import VerifiedControlRequest
from sidekick_usages.daemon.models.protocol import (
    ControlEvent,
)
from sidekick_usages.daemon.models.service import ServicePreparationReport
from sidekick_usages.daemon.models.worker import (
    ProviderLaunchers,
    WorkerLaunchSpec,
    WorkerResult,
)
from sidekick_usages.daemon.runtime.recovery import (
    ActivationRecoveryScheduler,
)
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.types.ports import WorkerHandle
from sidekick_usages.daemon.types.worker import (
    ExitNotifier,
    WorkerOutcome,
)
from sidekick_usages.daemon.worker.pool import WorkerLaunchPlanner
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from tests.fakes.daemon.control import VerifiedPeer
from tests.support.time import REFERENCE_TIME

SYNTHETIC_WORKER_EXECUTABLE = (
    Path(sys.executable).resolve().parent / "sidekick-usages-worker"
)
SYNTHETIC_CLAUDE_LAUNCHER = Path("/synthetic/bin/claude")
SYNTHETIC_CODEX_LAUNCHER = Path("/synthetic/bin/codex")
_MONOTONIC_START = 100.0


@dataclass(slots=True)
class RuntimeClock:
    """Provide deterministic wall and monotonic worker time."""

    wall_time: datetime = REFERENCE_TIME
    monotonic_time: float = _MONOTONIC_START

    def now(self) -> datetime:
        """Return the current synthetic wall time."""
        return self.wall_time

    def monotonic(self) -> float:
        """Return the current synthetic monotonic time."""
        return self.monotonic_time

    def advance(self, seconds: float) -> None:
        """Advance both synthetic clocks by ``seconds``."""
        self.wall_time += timedelta(seconds=seconds)
        self.monotonic_time += seconds


@dataclass(slots=True)
class FakeWorkerHandle:
    """Expose one controllable killable process boundary."""

    operation_id: OperationId
    events: list[str]
    initial_exit_code: int | None
    requires_kill: bool
    exchange_endpoint: socket.socket | None = None
    terminated: bool = False
    killed: bool = False

    @property
    def process_id(self) -> int:
        """Return one stable synthetic process identifier."""
        return 4242

    def poll(self) -> int | None:
        """Return the configured immediate exit status."""
        if self.initial_exit_code is not None:
            self._close_exchange()
        return self.initial_exit_code

    def wait(self, timeout_seconds: float | None) -> int | None:
        """Return the synthetic exit status within the caller's bound."""
        del timeout_seconds
        if self.initial_exit_code is not None:
            self._close_exchange()
            return self.initial_exit_code
        if self.killed:
            self._close_exchange()
            return -9
        if self.terminated and not self.requires_kill:
            self._close_exchange()
            return -15
        return None

    def group_alive(self) -> bool:
        """Return whether the synthetic worker group remains alive."""
        return (
            self.initial_exit_code is None
            and not self.killed
            and not (self.terminated and not self.requires_kill)
        )

    def terminate_group(self) -> None:
        """Record graceful process-group termination."""
        self.events.append(f"terminate:{self.operation_id}")
        self.terminated = True

    def kill_group(self) -> None:
        """Record forced process-group termination."""
        self.events.append(f"kill:{self.operation_id}")
        self.killed = True

    def _close_exchange(self) -> None:
        endpoint = self.exchange_endpoint
        self.exchange_endpoint = None
        if endpoint is not None:
            endpoint.close()


class FakeWorkerLauncher:
    """Persist configured synthetic results when exact workers launch."""

    def __init__(
        self,
        results: WorkerResultStore,
        clock: RuntimeClock,
        successful: frozenset[OperationId],
        requires_kill: frozenset[OperationId] = frozenset(),
    ) -> None:
        self._results = results
        self._clock = clock
        self._successful = successful
        self._requires_kill = requires_kill
        self.specs: list[WorkerLaunchSpec] = []
        self.events: list[str] = []
        self.handles: dict[OperationId, FakeWorkerHandle] = {}

    def launch(
        self,
        spec: WorkerLaunchSpec,
        notify_exit: ExitNotifier,
    ) -> WorkerHandle:
        """Launch one configured synthetic worker."""
        self.specs.append(spec)
        self.events.append(f"launch:{spec.operation_id}")
        succeeded = spec.operation_id in self._successful
        if succeeded:
            self._results.save(
                WorkerResult(
                    operation_id=spec.operation_id,
                    outcome=WorkerOutcome.SUCCEEDED,
                    finished_at=self._clock.now(),
                )
            )
        handle = FakeWorkerHandle(
            spec.operation_id,
            self.events,
            0 if succeeded else None,
            spec.operation_id in self._requires_kill,
            (
                None
                if spec.exchange_descriptor is None
                else socket.socket(fileno=os.dup(spec.exchange_descriptor))
            ),
        )
        self.handles[spec.operation_id] = handle
        if succeeded:
            notify_exit()
        return handle


class EntrypointWorkerLauncher:
    """Run the real isolated entrypoint behind a controllable handle."""

    def __init__(self, execute: Callable[[OperationId], int]) -> None:
        self._execute = execute
        self.specs: list[WorkerLaunchSpec] = []

    def launch(
        self,
        spec: WorkerLaunchSpec,
        notify_exit: ExitNotifier,
    ) -> WorkerHandle:
        """Execute one operation-ID-only worker launch contract."""
        self.specs.append(spec)
        exit_code = self._execute(spec.operation_id)
        handle = FakeWorkerHandle(
            spec.operation_id,
            [],
            exit_code,
            False,
        )
        notify_exit()
        return handle


def entrypoint_worker_launcher(
    queue: OperationQueueStore,
    execute: Callable[[OperationId], int],
) -> tuple[EntrypointWorkerLauncher, list[OperationKind]]:
    """Build an entrypoint launcher that records durable phase kinds."""
    kinds: list[OperationKind] = []

    def run(operation_id: OperationId) -> int:
        due = queue.find(operation_id)
        if due is None:
            raise AssertionError("Launched selection phase is unavailable.")
        kinds.append(due.kind)
        return execute(operation_id)

    return EntrypointWorkerLauncher(run), kinds


def run_scheduled_gateway_call[T](
    call: Callable[[], T],
    wake: Event,
    scheduler: DurableScheduler,
) -> T:
    """Complete one blocking gateway call through the real scheduler."""
    values: list[T] = []
    failures: list[Exception] = []

    def invoke() -> None:
        try:
            values.append(call())
        except Exception as error:
            failures.append(error)

    wake.clear()
    thread = Thread(target=invoke, daemon=True)
    thread.start()
    run_scheduler_phase(wake, scheduler)
    thread.join(2)
    if failures:
        raise failures[0]
    if thread.is_alive() or len(values) != 1:
        raise AssertionError("Selection gateway phase did not complete.")
    return values[0]


def run_scheduler_phase(
    wake: Event,
    scheduler: DurableScheduler,
) -> None:
    """Dispatch and collect one exact woken selection worker phase."""
    if not wake.wait(2):
        raise AssertionError("Selection gateway did not wake its scheduler.")
    wake.clear()
    if len(scheduler.dispatch_due()) != 1:
        raise AssertionError("Selection scheduler did not dispatch one phase.")
    if len(scheduler.collect()) != 1:
        raise AssertionError("Selection scheduler did not collect one phase.")


class _NoopControlDispatcher:
    """Expose an unused control boundary for direct runtime cycles."""

    def dispatch(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]:
        del context
        return iter(())

    def cancel(self, context: VerifiedControlRequest) -> None:
        del context


@dataclass(slots=True)
class ResidentState:
    """Expose controllable resident availability without provider work."""

    available: bool = True
    failure_code: str | None = None
    preparation_report: ServicePreparationReport | None = None

    def start(self) -> None:
        pass

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def worker_planner() -> WorkerLaunchPlanner:
    """Build one planner that proves secret environment stripping."""
    return WorkerLaunchPlanner(
        SYNTHETIC_WORKER_EXECUTABLE,
        {
            "ANTHROPIC_AUTH_TOKEN": "test-only-secret",
            "CODEX_HOME": "/test-only-secret-home",
            "HOME": "/synthetic/home",
            "PATH": "/usr/bin",
        },
        ProviderLaunchers(
            claude=SYNTHETIC_CLAUDE_LAUNCHER,
            codex=SYNTHETIC_CODEX_LAUNCHER,
        ),
    )


def foundation_runtime(
    paths: ApplicationPaths,
    scheduler: DurableScheduler,
    recovery: ActivationRecoveryScheduler,
    clock: RuntimeClock,
    wakeup: WakeupChannel,
    stop_requested: Event | None = None,
) -> SupervisorRuntime:
    """Build one direct-cycle supervisor around typed daemon fakes."""
    return SupervisorRuntime(
        LocalControlServer(
            paths.runtime_directory,
            paths.supervisor_socket,
            _NoopControlDispatcher(),
            peer_verifier=VerifiedPeer(),
        ),
        scheduler,
        recovery,
        ServiceStateStore(paths.service_state),
        clock,
        wakeup,
        Event() if stop_requested is None else stop_requested,
        ResidentState(),
    )
