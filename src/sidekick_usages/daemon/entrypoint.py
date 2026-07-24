"""Internal resident supervisor console entry point."""

import os
import signal
import time
from collections.abc import Callable
from functools import partial
from threading import Event
from types import FrameType

from sidekick_usages.clock import SystemClock
from sidekick_usages.daemon.control import LocalControlServer
from sidekick_usages.daemon.diagnostics import (
    CompositeOperationSink,
    DiagnosticOperationSink,
    SanitizedDiagnosticLog,
)
from sidekick_usages.daemon.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.scheduler import DurableScheduler
from sidekick_usages.daemon.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.workers import (
    SubprocessWorkerLauncher,
    WorkerLaunchPlanner,
    WorkerPool,
    resolve_worker_executable,
)
from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.activation_journal import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.operation_queue import OperationQueueStore
from sidekick_usages.persistence.service_state import ServiceStateStore
from sidekick_usages.persistence.worker_results import WorkerResultStore

__all__ = ["main"]

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


def main() -> int:
    """Compose and run one lean per-user resident supervisor."""
    paths = discover_application_paths()
    clock = SystemClock()
    wakeup = WakeupChannel()
    stop_requested = Event()
    queue = OperationQueueStore(paths.durable_operations)
    results = WorkerResultStore(paths.durable_operations)
    journals = ActivationJournalStore(paths.activation_journals)
    recovery = ActivationRecoveryScheduler(journals, queue)
    events = OperationEventHub()
    workers = WorkerPool(
        SubprocessWorkerLauncher(),
        WorkerLaunchPlanner(
            resolve_worker_executable(),
            os.environ,
        ),
        wakeup.notify,
    )
    scheduler = DurableScheduler(
        queue,
        results,
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
    )
    request_stop = partial(_request_stop, stop_requested, wakeup)
    dispatcher = SupervisorDispatcher(
        queue,
        ServiceStateStore(paths.service_state),
        recovery,
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
        ServiceStateStore(paths.service_state),
        clock,
        wakeup,
        stop_requested,
    )
    signal.signal(signal.SIGTERM, partial(_signal_stop, request_stop))
    signal.signal(signal.SIGINT, partial(_signal_stop, request_stop))
    runtime.run()
    return _EXIT_OK
