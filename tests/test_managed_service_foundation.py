"""Load-bearing durable state and recovery scenarios for the supervisor."""

import os
import socket
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.control.protocol import (
    PROTOCOL_VERSION,
    FrameDecoder,
    decode_event,
    decode_request,
    encode_frame,
    encode_request,
)
from sidekick_usages.daemon.control.server import (
    ControlConnection,
    LocalControlServer,
)
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    CompletedPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    EventPayload,
    ProgressPayload,
)
from sidekick_usages.daemon.models.worker import (
    WorkerLaunchSpec,
    WorkerResult,
)
from sidekick_usages.daemon.runtime.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.types.ports import (
    ControlDispatcher,
    WorkerHandle,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    ConnectedSocket,
    EventKind,
    ProgressPhase,
    RequestKind,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import (
    ExitNotifier,
    WorkerOutcome,
)
from sidekick_usages.daemon.worker.pool import (
    WorkerLaunchPlanner,
    WorkerPool,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import PeerIdentity
from sidekick_usages.platform.peer import PeerVerificationError
from sidekick_usages.platform.types import (
    PeerFailureCode,
    PeerSocket,
    PeerVerifier,
)
from tests.test_support import (
    REFERENCE_TIME,
    make_application_paths,
    saved_account,
)

_EXPECTED_WORKER_COUNT = 2
_MONOTONIC_START = 100.0


def _accounts() -> AccountIndex:
    accounts = (
        saved_account(
            Account(
                label=AccountLabel("claude-source"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-source-secret"
                ),
            )
        ),
        saved_account(
            Account(
                label=AccountLabel("claude-target"),
                credentials=ClaudeSetupTokenCredentials(
                    access_token="test-only-target-secret"
                ),
            )
        ),
        saved_account(
            Account(
                label=AccountLabel("codex-current"),
                credentials=CodexCredentials(
                    access_token="test-only-codex-secret",
                    account_id="acct-codex",
                ),
            )
        ),
    )
    return AccountIndex(accounts)


def _selected(
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    identity: str,
    generation: str,
    *,
    outcome: ActivationOutcome = ActivationOutcome.VERIFIED,
    verified_in: int = 0,
) -> SelectedAccountState:
    return SelectedAccountState(
        provider_id=provider_id,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        account_id=account_id,
        provider_identity=ProviderIdentity(identity),
        runtime_generation=AuthorityGeneration(generation),
        verified_at=REFERENCE_TIME + timedelta(seconds=verified_in),
        outcome=outcome,
    )


def _operation(
    account: SidekickAccountId,
    provider_id: ProviderId,
    operation_id: str,
    *,
    due_in: int = 0,
) -> DueOperation:
    return DueOperation(
        operation_id=OperationId(operation_id),
        provider_id=provider_id,
        account_id=account,
        kind=OperationKind.MAINTAIN,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME + timedelta(minutes=due_in),
        updated_at=REFERENCE_TIME,
    )


@dataclass(frozen=True, slots=True)
class _FoundationState:
    """One compact synthetic state graph shared by both scenarios."""

    paths: ApplicationPaths
    accounts: AccountIndex
    selected: SelectedStateStore
    journals: ActivationJournalStore
    queue: OperationQueueStore
    operations: tuple[DueOperation, ...]
    codex_state: SelectedAccountState


def _foundation_state(tmp_path: Path) -> _FoundationState:
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.selected_state).repair_parent_permissions()
    accounts = _accounts()
    source, target, codex = tuple(accounts)
    selected = SelectedStateStore(paths.selected_state)
    selected.save(
        _selected(
            ProviderId.CLAUDE,
            source.account_id,
            "claude-source-id",
            "claude-source-generation",
        )
    )
    codex_state = _selected(
        ProviderId.CODEX,
        codex.account_id,
        "codex-account-id",
        "codex-generation",
    )
    selected.save(codex_state)
    operations = (
        _operation(
            source.account_id,
            ProviderId.CLAUDE,
            "806fd66f-591b-4341-b31e-3d25405faf52",
        ),
        _operation(
            target.account_id,
            ProviderId.CLAUDE,
            "cf39e3c5-2517-4c79-937a-4f7d1fe5c916",
        ),
        _operation(
            codex.account_id,
            ProviderId.CODEX,
            "9630cd63-b9c3-4a24-8c78-b8ba4876411b",
        ),
    )
    queue = OperationQueueStore(paths.durable_operations)
    for operation in operations:
        assert queue.enqueue(operation) == operation
    return _FoundationState(
        paths=paths,
        accounts=accounts,
        selected=selected,
        journals=ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        ),
        queue=queue,
        operations=operations,
        codex_state=codex_state,
    )


def test_selection_and_queue_preserve_stable_independent_state(
    tmp_path: Path,
) -> None:
    """One provider selection changes without label or queue coupling."""
    state = _foundation_state(tmp_path)
    _source, target, _codex = tuple(state.accounts)
    duplicate = _operation(
        target.account_id,
        ProviderId.CLAUDE,
        "14c50df9-c216-4f99-a88f-4e1a3ab8eb5b",
        due_in=5,
    )
    assert (
        state.queue.enqueue(duplicate).operation_id
        == state.operations[1].operation_id
    )
    current = state.selected.load(ProviderId.CLAUDE)
    assert current is not None
    state.selected.compare_and_swap(
        _selected(
            ProviderId.CLAUDE,
            target.account_id,
            "claude-target-id",
            "claude-target-generation",
            verified_in=3,
        ),
        expected=current,
    )

    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert state.selected.load(ProviderId.CODEX) == state.codex_state
    codex_operation = state.operations[2]
    running = state.queue.transition(
        codex_operation.operation_id,
        OperationState.RUNNING,
        updated_at=REFERENCE_TIME,
    )
    state.queue.transition(
        running.operation_id,
        OperationState.ACTION_REQUIRED,
        updated_at=REFERENCE_TIME,
        failure_code="login_required",
    )
    redue = state.queue.enqueue(
        _operation(
            codex_operation.required_account_id,
            ProviderId.CODEX,
            "d101095e-7bda-43ad-b55d-b8ecb5a7ec66",
        )
    )
    assert redue.state is OperationState.SCHEDULED
    assert redue.due_at == REFERENCE_TIME
    assert len(state.queue.load()) == len(state.operations)
    assert state.accounts.rename(
        ProviderId.CLAUDE,
        target.label,
        AccountLabel("claude-renamed"),
    )
    renamed = state.accounts.get(target.account_id)
    assert renamed is not None
    assert renamed.label == "claude-renamed"
    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert (
        state.queue.get(
            target.provider_id,
            target.account_id,
            OperationKind.MAINTAIN,
        )
        is not None
    )
    pending_switch = DueOperation(
        operation_id=OperationId("bb413f38-2b11-418a-a4a7-b0e45666067e"),
        provider_id=ProviderId.CLAUDE,
        account_id=target.account_id,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    state.queue.enqueue(pending_switch)
    approved_switch = replace(
        pending_switch,
        operation_id=OperationId("e16508f9-aea0-4c51-9d16-1b4168b3411a"),
        allow_remote_control_disconnect=True,
    )
    coalesced = state.queue.enqueue(approved_switch)
    assert coalesced.operation_id == pending_switch.operation_id
    assert coalesced.allow_remote_control_disconnect
    assert state.queue.find(coalesced.operation_id) == coalesced
    with pytest.raises(
        ValueError,
        match="only valid for Claude activation",
    ):
        replace(
            approved_switch,
            provider_id=ProviderId.CODEX,
        )


class _FragmentingSocket:
    """Send deliberately small fragments through a real socket."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def recv(self, size: int, /) -> bytes:
        return self._connection.recv(size)

    def sendall(self, data: bytes, /) -> None:
        for offset in range(0, len(data), 3):
            self._connection.sendall(data[offset : offset + 3])

    def close(self) -> None:
        with suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        self._connection.close()


class _VerifiedPeer:
    """Synthetic proof boundary for protocol contract tests."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        del connection
        return PeerIdentity(1000)


class _RejectedPeer:
    """Synthetic operating-system proof failure."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        del connection
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)


@dataclass(slots=True)
class _RecordingDispatcher:
    """Record authenticated actions and expose one cancellable stream."""

    requests: list[ControlRequest]
    cancellations: list[RequestId]
    release_subscription: Event

    def dispatch(self, request: ControlRequest) -> Iterator[ControlEvent]:
        self.requests.append(request)
        if request.kind is RequestKind.ACTIVATE:
            operation_id = OperationId("f619cb29-9f6e-40dd-b35d-cf6a6ed93f79")
            yield _service_event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id),
            )
            yield _service_event(
                request,
                EventKind.PROGRESS,
                ProgressPayload(operation_id, ProgressPhase.VERIFYING),
            )
            yield _service_event(
                request,
                EventKind.COMPLETED,
                CompletedPayload(
                    operation_id,
                    CompletionOutcome.SUCCEEDED,
                ),
            )
            return
        if request.kind is RequestKind.SUBSCRIBE:
            yield _service_event(
                request,
                EventKind.ACCEPTED,
                AcceptedPayload(operation_id=None),
            )
            self.release_subscription.wait(timeout=2)
            yield _service_event(
                request,
                EventKind.PROGRESS,
                ProgressPayload(
                    operation_id=None,
                    phase=ProgressPhase.RUNNING,
                ),
            )
            return
        raise AssertionError("Unexpected synthetic dispatch request.")

    def cancel(self, request_id: RequestId) -> None:
        self.cancellations.append(request_id)


def _service_event(
    request: ControlRequest,
    kind: EventKind,
    payload: EventPayload,
) -> ControlEvent:
    return ControlEvent(
        protocol_version=PROTOCOL_VERSION,
        request_id=request.request_id,
        kind=kind,
        payload=payload,
        package_version=__version__,
    )


def _serve_protocol_connection(
    connection: socket.socket,
    verifier: PeerVerifier,
    dispatcher: ControlDispatcher,
) -> None:
    ControlConnection(connection, verifier, dispatcher).serve()


def _rejected_protocol_response(
    verifier: PeerVerifier,
    outbound: bytes,
    dispatcher: _RecordingDispatcher,
) -> bytes:
    server_socket, client_socket = socket.socketpair()
    client_socket.sendall(outbound)
    server = Thread(
        target=_serve_protocol_connection,
        args=(server_socket, verifier, dispatcher),
    )
    server.start()
    server.join(timeout=2)
    assert not server.is_alive()
    response = bytearray()
    while True:
        try:
            chunk = client_socket.recv(65_540)
        except ConnectionResetError:
            break
        if not chunk:
            break
        response.extend(chunk)
    client_socket.close()
    return bytes(response)


def test_authenticated_control_stream_frames_completes_and_cancels() -> None:
    """One peer-proven stream frames, completes, and cancels safely."""
    account_id = SidekickAccountId("69b33871-dcd9-4e47-8ef8-f77d9944a956")
    fragmented_request = ControlRequest(
        protocol_version=PROTOCOL_VERSION,
        request_id=new_request_id(),
        kind=RequestKind.ACTIVATE,
        payload=AccountPayload(ProviderId.CLAUDE, account_id),
        package_version=__version__,
    )
    frame = encode_request(fragmented_request)
    decoder = FrameDecoder()
    decoded_frames: list[bytes] = []
    for byte in frame:
        decoded_frames.extend(decoder.feed(bytes((byte,))))
    decoder.finish()
    assert len(decoded_frames) == 1
    assert decode_request(decoded_frames[0]) == fragmented_request

    server_socket, client_socket = socket.socketpair()
    dispatcher = _RecordingDispatcher([], [], Event())
    server = Thread(
        target=_serve_protocol_connection,
        args=(server_socket, _VerifiedPeer(), dispatcher),
    )
    server.start()
    fragmented_client: ConnectedSocket = _FragmentingSocket(client_socket)
    client = ControlClient(fragmented_client)

    activation = tuple(client.activate(ProviderId.CLAUDE, account_id))
    assert tuple(event.kind for event in activation) == (
        EventKind.ACCEPTED,
        EventKind.PROGRESS,
        EventKind.COMPLETED,
    )
    subscription = client.subscribe()
    accepted = next(subscription)
    assert accepted.kind is EventKind.ACCEPTED
    subscription.close()
    dispatcher.release_subscription.set()
    server.join(timeout=2)

    assert not server.is_alive()
    assert tuple(request.kind for request in dispatcher.requests) == (
        RequestKind.ACTIVATE,
        RequestKind.SUBSCRIBE,
    )
    assert dispatcher.cancellations == [accepted.request_id]


def test_control_protocol_fails_closed_at_each_trust_boundary() -> None:
    """Unproved peers, malformed input, and mismatches never dispatch."""
    malformed = encode_frame(
        b'{"credential":"test-only-secret","kind":"activate"}'
    )
    incompatible = encode_request(
        ControlRequest(
            protocol_version=PROTOCOL_VERSION + 1,
            request_id=new_request_id(),
            kind=RequestKind.HANDSHAKE,
            payload=EmptyPayload(),
            package_version=__version__,
        )
    )
    cases: tuple[tuple[PeerVerifier, bytes, EventKind | None], ...] = (
        (_RejectedPeer(), malformed, None),
        (_VerifiedPeer(), malformed, EventKind.FAILED),
        (_VerifiedPeer(), incompatible, EventKind.INCOMPATIBLE),
    )
    for verifier, outbound, expected_kind in cases:
        dispatcher = _RecordingDispatcher([], [], Event())
        response = _rejected_protocol_response(
            verifier,
            outbound,
            dispatcher,
        )
        assert dispatcher.requests == []
        assert b"test-only-secret" not in response
        if expected_kind is None:
            assert response == b""
            continue
        decoder = FrameDecoder()
        frames = decoder.feed(response)
        decoder.finish()
        assert len(frames) == 1
        assert decode_event(frames[0]).kind is expected_kind


@dataclass(slots=True)
class _RuntimeClock:
    """Deterministic wall and monotonic clocks for worker scheduling."""

    wall_time: datetime = REFERENCE_TIME
    monotonic_time: float = _MONOTONIC_START

    def now(self) -> datetime:
        return self.wall_time

    def monotonic(self) -> float:
        return self.monotonic_time

    def advance(self, seconds: float) -> None:
        self.wall_time += timedelta(seconds=seconds)
        self.monotonic_time += seconds


@dataclass(slots=True)
class _FakeWorkerHandle:
    """Controllable killable process boundary."""

    operation_id: OperationId
    events: list[str]
    initial_exit_code: int | None
    requires_kill: bool
    exchange_endpoint: socket.socket | None = None
    terminated: bool = False
    killed: bool = False

    @property
    def process_id(self) -> int:
        return 4242

    def poll(self) -> int | None:
        if self.initial_exit_code is not None:
            self._close_exchange()
        return self.initial_exit_code

    def wait(self, timeout_seconds: float | None) -> int | None:
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
        return (
            self.initial_exit_code is None
            and not self.killed
            and not (self.terminated and not self.requires_kill)
        )

    def terminate_group(self) -> None:
        self.events.append(f"terminate:{self.operation_id}")
        self.terminated = True

    def kill_group(self) -> None:
        self.events.append(f"kill:{self.operation_id}")
        self.killed = True

    def _close_exchange(self) -> None:
        endpoint = self.exchange_endpoint
        self.exchange_endpoint = None
        if endpoint is not None:
            endpoint.close()


class _FakeWorkerLauncher:
    """Persist configured synthetic results when exact workers launch."""

    def __init__(
        self,
        results: WorkerResultStore,
        clock: _RuntimeClock,
        successful: frozenset[OperationId],
        requires_kill: frozenset[OperationId] = frozenset(),
    ) -> None:
        self._results = results
        self._clock = clock
        self._successful = successful
        self._requires_kill = requires_kill
        self.specs: list[WorkerLaunchSpec] = []
        self.events: list[str] = []
        self.handles: dict[OperationId, _FakeWorkerHandle] = {}

    def launch(
        self,
        spec: WorkerLaunchSpec,
        notify_exit: ExitNotifier,
    ) -> WorkerHandle:
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
        handle = _FakeWorkerHandle(
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


class _NoopControlDispatcher:
    """Unused control boundary for direct runtime-cycle scenarios."""

    def dispatch(self, request: ControlRequest) -> Iterator[ControlEvent]:
        del request
        return iter(())

    def cancel(self, request_id: RequestId) -> None:
        del request_id


class _NoopResidentService:
    """Inactive resident boundary for direct scheduler-cycle tests."""

    @property
    def ready(self) -> bool:
        """Return readiness for direct runtime-cycle testing."""
        return True

    def start(self) -> None:
        pass

    def request_stop(self) -> None:
        pass

    def close(self) -> None:
        pass


def _worker_planner() -> WorkerLaunchPlanner:
    return WorkerLaunchPlanner(
        Path("/opt/sidekick/bin/sidekick-usages-worker"),
        {
            "ANTHROPIC_AUTH_TOKEN": "test-only-secret",
            "CODEX_HOME": "/test-only-secret-home",
            "HOME": "/synthetic/home",
            "PATH": "/usr/bin",
        },
    )


def _runtime_for(
    state: _FoundationState,
    scheduler: DurableScheduler,
    recovery: ActivationRecoveryScheduler,
    clock: _RuntimeClock,
    wakeup: WakeupChannel,
) -> SupervisorRuntime:
    return SupervisorRuntime(
        LocalControlServer(
            state.paths.runtime_directory,
            state.paths.supervisor_socket,
            _NoopControlDispatcher(),
            peer_verifier=_VerifiedPeer(),
        ),
        scheduler,
        recovery,
        ServiceStateStore(state.paths.service_state),
        clock,
        wakeup,
        Event(),
        _NoopResidentService(),
    )


def test_supervisor_isolates_timeout_and_recovers_without_duplicate_work(
    tmp_path: Path,
) -> None:
    """A timed-out account cannot block completion or restart recovery."""
    state = _foundation_state(tmp_path)
    first, second, third = state.operations
    assert state.queue.remove_account(third.required_account_id) == 1
    results = WorkerResultStore(state.paths.durable_operations)
    clock = _RuntimeClock()
    wakeup = WakeupChannel()
    launcher = _FakeWorkerLauncher(
        results,
        clock,
        frozenset({second.operation_id}),
    )
    workers = WorkerPool(
        launcher,
        _worker_planner(),
        wakeup.notify,
        general_timeout_seconds=5,
        termination_grace_seconds=0.01,
    )
    scheduler = DurableScheduler(
        state.queue,
        results,
        workers,
        clock,
        monotonic=clock.monotonic,
    )
    recovery = ActivationRecoveryScheduler(
        state.journals,
        state.queue,
    )
    runtime = _runtime_for(state, scheduler, recovery, clock, wakeup)

    runtime.recover()
    runtime.run_cycle()
    runtime.run_cycle()
    clock.advance(6)
    runtime.run_cycle()

    first_state = state.queue.find(first.operation_id)
    second_state = state.queue.find(second.operation_id)
    assert first_state is not None
    assert first_state.state is OperationState.RETRY_WAIT
    assert first_state.failure_code == "worker_timed_out"
    assert second_state is not None
    assert second_state.state is OperationState.SCHEDULED
    service_state = ServiceStateStore(state.paths.service_state).load()
    assert service_state is not None
    assert service_state.phase is ServicePhase.READY
    assert service_state.active_workers == 0

    assert len(launcher.specs) == _EXPECTED_WORKER_COUNT
    for spec in launcher.specs:
        assert spec.argv == (
            "/opt/sidekick/bin/sidekick-usages-worker",
            str(spec.operation_id),
        )
        assert spec.environment_map() == {
            "HOME": "/synthetic/home",
            "PATH": "/usr/bin",
        }
    restarted_workers = WorkerPool(
        _FakeWorkerLauncher(results, clock, frozenset()),
        _worker_planner(),
        lambda: None,
    )
    restarted = DurableScheduler(
        OperationQueueStore(state.paths.durable_operations),
        results,
        restarted_workers,
        clock,
        monotonic=clock.monotonic,
    )
    assert restarted.recover() == ()
    durable = OperationQueueStore(state.paths.durable_operations).load()
    assert len(durable) == _EXPECTED_WORKER_COUNT
    assert len({operation.operation_id for operation in durable}) == len(
        durable
    )
    wakeup.close()
