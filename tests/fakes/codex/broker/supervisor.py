"""Real resident supervisor harness around synthetic Codex boundaries."""

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Self

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.models.worker import ProviderLaunchers
from sidekick_usages.daemon.runtime.codex import (
    DurableCodexOperationDispatcher,
)
from sidekick_usages.daemon.runtime.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.worker.exchange import WorkerExchangeRegistry
from sidekick_usages.daemon.worker.pool import (
    SubprocessWorkerLauncher,
    WorkerLaunchPlanner,
    WorkerPool,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
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
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.responder import CodexRuntimeBroker
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime

_READINESS_TIMEOUT_SECONDS = 10.0
_SUPERVISOR_JOIN_SECONDS = 10.0
_WAIT_INTERVAL_SECONDS = 0.01


def _runtime_factory(
    executable: CodexExecutable,
    session_home: Path,
    environment: Mapping[str, str],
) -> Callable[[Callable[[], bool]], CodexSharedRuntime]:
    """Build the shared-runtime factory used by resident test harnesses."""
    runtime_environment = dict(environment)

    def create(cancelled: Callable[[], bool]) -> CodexSharedRuntime:
        return CodexSharedRuntime.create(
            executable,
            session_home,
            environment=runtime_environment,
            cancelled=cancelled,
        )

    return create


def _compose_broker(
    paths: ApplicationPaths,
    runtime_factory: Callable[[Callable[[], bool]], CodexSharedRuntime],
    clock: SystemClock,
    queue: OperationQueueStore,
    journals: ActivationJournalStore,
    observations: RuntimeAuthObservationStore,
    exchanges: WorkerExchangeRegistry,
    status_changed: Callable[[], None] | None = None,
) -> CodexRuntimeBroker:
    """Compose one production broker around reusable synthetic boundaries."""
    wakeup = status_changed if status_changed is not None else lambda: None
    accounts = AccountStore(
        paths.accounts,
        PrivateCredentialTree(
            paths.private_credentials,
            account_path=paths.accounts,
        ),
    ).load()
    return CodexRuntimeBroker(
        runtime_factory,
        RuntimeStateReader(
            ProviderId.CODEX,
            SelectedStateStore(paths.selected_state),
            journals,
            queue,
            observations,
            clock,
        ),
        accounts,
        DurableCodexOperationDispatcher(
            queue,
            observations,
            exchanges,
            clock.now,
            time.monotonic,
            wakeup,
        ),
        exchanges,
        wall_time=clock.now,
        status_changed=status_changed,
    )


class FakeCodexBroker:
    """Run only the resident production broker around synthetic boundaries."""

    def __init__(
        self,
        paths: ApplicationPaths,
        executable: CodexExecutable,
        session_home: Path,
        environment: Mapping[str, str],
    ) -> None:
        clock = SystemClock()
        queue = OperationQueueStore(paths.durable_operations)
        observations = RuntimeAuthObservationStore(paths.durable_operations)
        self._broker = _compose_broker(
            paths,
            _runtime_factory(executable, session_home, environment),
            clock,
            queue,
            ActivationJournalStore(
                paths.activation_journals,
                paths.durable_operations,
            ),
            observations,
            WorkerExchangeRegistry(time.monotonic),
        )

    @property
    def available(self) -> bool:
        """Return the broker's live shared-runtime qualification."""
        return self._broker.available

    @property
    def ready(self) -> bool:
        """Return whether the broker has proven projection readiness."""
        return self._broker.ready

    @property
    def failure_code(self) -> str | None:
        """Return the broker's retained safe typed failure."""
        return self._broker.failure_code

    def wait_until_available(self) -> None:
        """Wait for one qualified shared runtime or a typed failure."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while not self.available:
            if self.failure_code is not None:
                raise AssertionError(
                    "Fake broker reported a qualification failure: "
                    f"{self.failure_code}."
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Fake broker did not qualify.")
            time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))

    def wait_until_failure(self, expected: str) -> None:
        """Wait until one exact terminal qualification failure is retained."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while self.failure_code != expected:
            if self.available:
                raise AssertionError("Fake broker unexpectedly qualified.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Fake broker did not retain the expected failure."
                )
            time.sleep(min(_WAIT_INTERVAL_SECONDS, remaining))

    def __enter__(self) -> Self:
        """Start and return this harness."""
        self._broker.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the harness."""
        del exception_type, exception, traceback
        self._broker.close()


class FakeCodexSupervisor:
    """Run the production supervisor, scheduler, broker, and worker process."""

    def __init__(
        self,
        paths: ApplicationPaths,
        executable: CodexExecutable,
        session_home: Path,
        environment: Mapping[str, str],
        worker_executable: Path,
    ) -> None:
        self._stop = Event()
        self._wakeup = WakeupChannel()
        self._failures: list[BaseException] = []
        self._thread: Thread | None = None
        clock = SystemClock()
        queue = OperationQueueStore(paths.durable_operations)
        results = WorkerResultStore(paths.durable_operations)
        service_state = ServiceStateStore(paths.service_state)
        journals = ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        )
        observations = RuntimeAuthObservationStore(paths.durable_operations)
        recovery = ActivationRecoveryScheduler(journals, queue)
        events = OperationEventHub()
        exchanges = WorkerExchangeRegistry(time.monotonic)
        worker_environment = dict(environment)
        worker_environment["PATH"] = os.defpath
        workers = WorkerPool(
            SubprocessWorkerLauncher(),
            WorkerLaunchPlanner(
                worker_executable,
                worker_environment,
                ProviderLaunchers(
                    claude=None,
                    codex=executable.launcher,
                ),
            ),
            self._wakeup.notify,
            exchanges=exchanges,
        )
        broker = _compose_broker(
            paths,
            _runtime_factory(executable, session_home, environment),
            clock,
            queue,
            journals,
            observations,
            exchanges,
            self._wakeup.notify,
        )
        scheduler = DurableScheduler(
            queue,
            results,
            workers,
            clock,
            events=events,
            exchange_preparer=broker,
        )
        dispatcher = SupervisorDispatcher(
            queue,
            service_state,
            events,
            broker,
            clock,
            self._wakeup.notify,
            self._request_stop,
        )
        self._broker = broker
        self._service_state = service_state
        self._runtime = SupervisorRuntime(
            LocalControlServer(
                paths.runtime_directory,
                paths.supervisor_socket,
                dispatcher,
            ),
            scheduler,
            recovery,
            service_state,
            clock,
            self._wakeup,
            self._stop,
            broker,
        )

    @property
    def ready(self) -> bool:
        """Return the published broker and supervisor readiness state."""
        state = self._service_state.load()
        return (
            state is not None
            and state.phase is ServicePhase.READY
            and state.broker_ready
            and self._broker.ready
        )

    @property
    def broker_available(self) -> bool:
        """Return the broker's live shared-runtime qualification."""
        return self._broker.available

    @property
    def broker_failure_code(self) -> str | None:
        """Return the broker's retained safe typed failure."""
        return self._broker.failure_code

    def start(self) -> None:
        """Start the production runtime in one owned background thread."""
        if self._thread is not None:
            raise RuntimeError("Fake supervisor already started.")
        thread = Thread(
            target=self._run,
            daemon=True,
            name="test-sidekick-supervisor",
        )
        self._thread = thread
        thread.start()

    def wait_until_ready(self) -> None:
        """Wait for one published ready state or surface runtime failure."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while not self.ready:
            self._raise_failure()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Fake supervisor did not become ready.")
            self._stop.wait(min(_WAIT_INTERVAL_SECONDS, remaining))

    def wait_until_broker_failure(self, failure_code: str) -> None:
        """Wait for one exact live broker failure observation."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            state = self._service_state.load()
            if (
                self.broker_failure_code == failure_code
                and state is not None
                and not state.broker_ready
                and state.failure_code == failure_code
            ):
                return
            self._raise_failure()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(
                    "Fake supervisor did not report broker failure."
                )
            self._stop.wait(min(_WAIT_INTERVAL_SECONDS, remaining))

    def notify(self) -> None:
        """Wake the real selector after a test persists due work."""
        self._wakeup.notify()

    def request_stop(self) -> None:
        """Request a non-blocking supervisor shutdown."""
        self._request_stop()

    def _request_stop(self) -> None:
        self._stop.set()
        self._wakeup.notify()

    def close(self) -> None:
        """Stop, join, and surface any production runtime failure."""
        self._stop.set()
        self._wakeup.notify()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_SUPERVISOR_JOIN_SECONDS)
            if thread.is_alive():
                raise AssertionError("Fake supervisor did not stop.")
        self._raise_failure()

    def __enter__(self) -> Self:
        """Start and return this harness."""
        self.start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the harness."""
        del exception_type, exception, traceback
        self.close()

    def _run(self) -> None:
        try:
            self._runtime.run()
        except BaseException as error:
            self._failures.append(error)

    def _raise_failure(self) -> None:
        if self._failures:
            raise AssertionError(
                "Production supervisor failed in its test harness."
            ) from self._failures[0]
