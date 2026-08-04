"""Real resident supervisor harness around synthetic Codex boundaries."""

import os
import socket
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Self

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    OpenSelectionOperation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    ParticipantId,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.protocol import FramedTransport
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.models.service import ServicePreparationReport
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
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import (
    SelectionSchedulerSink,
    SelectionWorkerGateway,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.worker.exchange import (
    SupervisorWorkerExchange,
    WorkerExchangeRegistry,
)
from sidekick_usages.daemon.worker.pool import (
    SubprocessWorkerLauncher,
    WorkerLaunchPlanner,
    WorkerPool,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import ReplaceFailedError
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
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    decode_codex_callback_instruction,
    encode_codex_callback_instruction,
)
from sidekick_usages.providers.codex.broker.external_auth.selection import (
    decode_codex_selection_instruction,
)
from sidekick_usages.providers.codex.broker.responder import CodexRuntimeBroker
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime

_READINESS_TIMEOUT_SECONDS = 10.0
_SUPERVISOR_JOIN_SECONDS = 10.0
_WAIT_INTERVAL_SECONDS = 0.01
_FAILED_PROOF_PARTICIPANT = ParticipantId(
    "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
)
_FAILED_PROOF_GENERATION = 1
_FAILED_PROOF_PEER = ProcessIdentity(1, 1)


class _JourneySelectionOperationStore(SelectionOperationStore):
    """Inject one post-commit snapshot failure into a full journey."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reject_final_snapshot_once = False

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Reject only the next ready snapshot after provider commit."""
        if (
            self.reject_final_snapshot_once
            and expected.phase is SelectionPhase.AWAITING_READY
            and replacement.phase is SelectionPhase.AWAITING_READY
            and replacement.ready_participant_ids
        ):
            self.reject_final_snapshot_once = False
            raise ReplaceFailedError
        return super().advance_with_required_additions(expected, replacement)


class _JourneyWorkerExchangeRegistry(WorkerExchangeRegistry):
    """Inject one-shot boundary races into the full broker journey."""

    def __init__(self) -> None:
        super().__init__(time.monotonic)
        self._selection_hooks: dict[OperationKind, Callable[[], None]] = {}
        self._callback_fault: str | None = None
        self._operations: _JourneyCodexOperationDispatcher | None = None

    def schedule_selection_hook(
        self,
        kind: OperationKind,
        hook: Callable[[], None],
    ) -> None:
        """Run one callback after the broker binds a selection instruction."""
        self._selection_hooks[kind] = hook

    def schedule_callback_fault(
        self,
        fault: str,
    ) -> None:
        """Corrupt one callback only after durable broker dispatch."""
        self._callback_fault = fault

    def bind_operations(
        self,
        operations: _JourneyCodexOperationDispatcher,
    ) -> None:
        """Bind the journey's real durable callback dispatcher."""
        self._operations = operations

    def schedule_wrong_home(self, account_id: SidekickAccountId) -> None:
        """Route the next durable callback through another account."""
        if self._operations is None:
            raise AssertionError("Journey dispatcher is unavailable.")
        self._operations.schedule_wrong_home(account_id)

    def create(
        self,
        operation_id: OperationId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> SupervisorWorkerExchange:
        """Apply one scheduled journey fault before worker inheritance."""
        adjusted = instruction
        with suppress(CodexBrokerError):
            selection = decode_codex_selection_instruction(instruction)
            hook = self._selection_hooks.pop(selection.kind, None)
            if hook is not None:
                hook()
        with suppress(CodexBrokerError):
            callback = decode_codex_callback_instruction(instruction)
            fault = self._callback_fault
            if fault == "request" and callback.request_id is None:
                fault = None
            if fault is not None:
                self._callback_fault = None
                if fault == "epoch":
                    callback = replace(
                        callback,
                        selection_epoch=SelectionEpoch(
                            callback.selection_epoch.value - 1
                        ),
                    )
                elif fault == "request":
                    assert callback.request_id is not None
                    callback = replace(
                        callback,
                        request_id=callback.request_id + 1,
                    )
                else:
                    raise AssertionError("Unknown callback journey fault.")
                adjusted = encode_codex_callback_instruction(callback)
        return super().create(
            operation_id,
            adjusted,
            response_deadline,
            completion_deadline,
        )


class _JourneyCodexOperationDispatcher(DurableCodexOperationDispatcher):
    """Route one callback operation through a scheduled wrong home."""

    def __init__(
        self,
        queue: OperationQueueStore,
        observations: RuntimeAuthObservationStore,
        exchanges: WorkerExchangeRegistry,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float],
        wakeup: Callable[[], None],
    ) -> None:
        super().__init__(
            queue,
            observations,
            exchanges,
            wall_time,
            monotonic,
            wakeup,
        )
        self._wrong_home: SidekickAccountId | None = None

    def schedule_wrong_home(self, account_id: SidekickAccountId) -> None:
        """Route the next operation to one non-authoritative private home."""
        self._wrong_home = account_id

    def dispatch(
        self,
        operation_id: OperationId,
        account_id: SidekickAccountId,
        callback_request_id: int | None,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> SupervisorWorkerExchange:
        """Keep response correlation while changing operation authority."""
        routed_account = self._wrong_home
        self._wrong_home = None
        return super().dispatch(
            operation_id,
            account_id if routed_account is None else routed_account,
            callback_request_id,
            instruction,
            response_deadline,
            completion_deadline,
        )


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
    if isinstance(exchanges, _JourneyWorkerExchangeRegistry):
        dispatcher = _JourneyCodexOperationDispatcher(
            queue,
            observations,
            exchanges,
            clock.now,
            time.monotonic,
            wakeup,
        )
        exchanges.bind_operations(dispatcher)
    else:
        dispatcher = DurableCodexOperationDispatcher(
            queue,
            observations,
            exchanges,
            clock.now,
            time.monotonic,
            wakeup,
        )
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
        dispatcher,
        exchanges,
        proof_transport_factory=FramedTransport,
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
            _runtime_factory(
                executable,
                session_home,
                environment,
            ),
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
        self._queue = queue
        results = WorkerResultStore(paths.durable_operations)
        service_state = ServiceStateStore(paths.service_state)
        journals = ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        )
        observations = RuntimeAuthObservationStore(paths.durable_operations)
        selected = SelectedStateStore(paths.selected_state)
        selection_journals = _JourneySelectionOperationStore(
            paths.selection_journals
        )
        self._selection_journals = selection_journals
        participants = ParticipantRegistry(selected)
        self._participants = participants
        selection_workers = SelectionWorkerGateway(
            queue,
            clock,
            self._wakeup.notify,
        )
        selection_recovery = SelectionRecovery(
            selected,
            selection_journals,
            participants,
            selection_workers,
            clock,
        )
        selection = SelectionCoordinator(
            selected,
            selection_journals,
            participants,
            selection_workers,
            clock,
            resume_recovery=selection_recovery.resume,
        )
        recovery = ActivationRecoveryScheduler(
            journals,
            queue,
            selection_recovery=selection_recovery,
        )
        events = OperationEventHub()
        exchanges = _JourneyWorkerExchangeRegistry()
        self._exchanges = exchanges
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
            _runtime_factory(
                executable,
                session_home,
                environment,
            ),
            clock,
            queue,
            journals,
            observations,
            exchanges,
            self._wakeup.notify,
        )
        participants.add_attachment_registry(broker.participant_proofs)
        scheduler = DurableScheduler(
            queue,
            results,
            workers,
            clock,
            events=SelectionSchedulerSink(
                events,
                selection_workers,
                selection_recovery,
            ),
            exchange_preparer=broker,
        )
        dispatcher = SupervisorDispatcher(
            queue,
            service_state,
            events,
            clock,
            self._wakeup.notify,
            self._request_stop,
            selection=selection,
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

    @property
    def broker_preparation_report(self) -> ServicePreparationReport | None:
        """Return the persisted supervised recovery guidance."""
        state = self._service_state.load()
        return None if state is None else state.preparation_report

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

    def wait_until_broker_available(self) -> None:
        """Wait for the runtime without requiring journal reconciliation."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while not self.broker_available:
            self._raise_failure()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Fake broker did not become available.")
            self._stop.wait(min(_WAIT_INTERVAL_SECONDS, remaining))

    def wait_until_selection_workers_collected(self) -> None:
        """Drive scheduler collection through the last selection child."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            active = self._selection_journals.load(ProviderId.CODEX).active
            workers = any(
                operation.kind.is_selection_worker
                for operation in self._queue.load()
            )
            if not workers and active is None:
                return
            self._raise_failure()
            self._wakeup.notify()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Selection worker was not collected.")
            self._stop.wait(min(_WAIT_INTERVAL_SECONDS, remaining))

    def wait_for_codex_participants(
        self,
        registered: int,
        active_turns: int,
        *,
        reachable: int | None = None,
    ) -> None:
        """Wait for exact safe Codex participant counts."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            snapshot = self._participants.snapshot(ProviderId.CODEX)
            if (
                snapshot.registered_count == registered
                and snapshot.active_turn_count == active_turns
                and (
                    reachable is None or snapshot.reachable_count == reachable
                )
            ):
                return
            self._raise_failure()
            self._stop.wait(_WAIT_INTERVAL_SECONDS)
        raise AssertionError("Codex participant counts did not converge.")

    def wait_until_callback_workers_collected(self) -> None:
        """Drive scheduler collection through the last callback child."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_SECONDS
        while any(
            operation.kind is OperationKind.CODEX_CALLBACK
            for operation in self._queue.load()
        ):
            self._raise_failure()
            self._wakeup.notify()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Callback worker was not collected.")
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

    def schedule_selection_hook(
        self,
        kind: OperationKind,
        hook: Callable[[], None],
    ) -> None:
        """Schedule one full-journey selection boundary action."""
        self._exchanges.schedule_selection_hook(kind, hook)

    def stage_failed_participant_proof(self) -> None:
        """Attach one closed proof peer for a prepare-stage refusal."""
        participant, broker = socket.socketpair()
        transaction = self._broker.participant_proofs.stage(
            _FAILED_PROOF_PARTICIPANT,
            _FAILED_PROOF_GENERATION,
            _FAILED_PROOF_PEER,
            broker,
        )
        transaction.commit()
        transaction.finalize()
        participant.close()

    def remove_failed_participant_proof(self) -> None:
        """Remove the synthetic failed proof after one refusal."""
        self._broker.participant_proofs.remove(
            _FAILED_PROOF_PARTICIPANT,
            _FAILED_PROOF_GENERATION,
            _FAILED_PROOF_PEER,
        )

    def reject_final_selection_snapshot_once(self) -> None:
        """Fail one final snapshot without changing provider auth."""
        self._selection_journals.reject_final_snapshot_once = True

    def schedule_stale_callback_epoch(self) -> None:
        """Dispatch one callback with an obsolete finalized epoch."""
        self._exchanges.schedule_callback_fault("epoch")

    def schedule_stale_callback_request(self) -> None:
        """Dispatch one callback with the wrong daemon request ID."""
        self._exchanges.schedule_callback_fault("request")

    def schedule_wrong_callback_home(
        self,
        account_id: SidekickAccountId,
    ) -> None:
        """Dispatch one callback naming a different private home."""
        self._exchanges.schedule_wrong_home(account_id)

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
