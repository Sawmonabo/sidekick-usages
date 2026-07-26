"""Resident supervisor console entry point."""

import os
import signal
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from threading import Event
from types import FrameType

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.runtime.codex import (
    DurableCodexOperationDispatcher,
)
from sidekick_usages.daemon.runtime.diagnostics import (
    CompositeOperationSink,
    DiagnosticOperationSink,
    SanitizedDiagnosticLog,
)
from sidekick_usages.daemon.runtime.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.worker.exchange import WorkerExchangeRegistry
from sidekick_usages.daemon.worker.pool import (
    SubprocessWorkerLauncher,
    WorkerLaunchPlanner,
    WorkerPool,
    resolve_worker_executable,
)
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.runtime import (
    RuntimeStateReader,
)
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.broker.responder import CodexRuntimeBroker
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime

_EXIT_OK = 0


def _request_stop(stop: Event, wakeup: WakeupChannel) -> None:
    stop.set()
    wakeup.notify()


def _signal_stop(
    request_stop: Callable[[], None],
    _signal_number: int,
    _frame: FrameType | None,
) -> None:
    request_stop()


def _create_codex_runtime(
    native_home: Path,
    cancelled: Callable[[], bool],
) -> CodexSharedRuntime:
    executable = discover_codex_executable(
        os.environ,
        cancelled=cancelled,
    )
    return CodexSharedRuntime.create(
        executable,
        native_home,
        environment=os.environ,
        cancelled=cancelled,
    )


def main() -> int:
    """Compose and run one lean per-user resident supervisor."""
    paths = discover_application_paths()
    clock = SystemClock()
    wakeup = WakeupChannel()
    stop_requested = Event()
    queue = OperationQueueStore(paths.durable_operations)
    results = WorkerResultStore(paths.durable_operations)
    service_state = ServiceStateStore(paths.service_state)
    journals = ActivationJournalStore(
        paths.activation_journals,
        paths.durable_operations,
    )
    selected = SelectedStateStore(paths.selected_state)
    recovery = ActivationRecoveryScheduler(journals, queue)
    events = OperationEventHub()
    exchanges = WorkerExchangeRegistry(time.monotonic)
    workers = WorkerPool(
        SubprocessWorkerLauncher(),
        WorkerLaunchPlanner(
            resolve_worker_executable(),
            os.environ,
        ),
        wakeup.notify,
        exchanges=exchanges,
    )
    broker = CodexRuntimeBroker(
        partial(_create_codex_runtime, default_codex_home()),
        RuntimeStateReader(
            ProviderId.CODEX,
            selected,
            journals,
            queue,
            clock,
        ),
        DurableCodexOperationDispatcher(
            queue,
            RuntimeAuthObservationStore(paths.durable_operations),
            exchanges,
            clock.now,
            time.monotonic,
            wakeup.notify,
        ),
        exchanges,
        wall_time=clock.now,
        status_changed=wakeup.notify,
    )
    scheduler = DurableScheduler(
        queue,
        results,
        selected,
        workers,
        clock,
        events=CompositeOperationSink(
            events,
            DiagnosticOperationSink(
                SanitizedDiagnosticLog(paths.service_logs),
                clock,
                time.monotonic,
            ),
        ),
        exchange_preparer=broker,
    )
    request_stop = partial(_request_stop, stop_requested, wakeup)
    dispatcher = SupervisorDispatcher(
        queue,
        service_state,
        events,
        clock,
        wakeup.notify,
        request_stop,
    )
    server = LocalControlServer(
        paths.runtime_directory,
        paths.supervisor_socket,
        dispatcher,
    )
    runtime = SupervisorRuntime(
        server,
        scheduler,
        recovery,
        service_state,
        clock,
        wakeup,
        stop_requested,
        broker,
    )
    signal.signal(signal.SIGTERM, partial(_signal_stop, request_stop))
    signal.signal(signal.SIGINT, partial(_signal_stop, request_stop))
    runtime.run()
    return _EXIT_OK
