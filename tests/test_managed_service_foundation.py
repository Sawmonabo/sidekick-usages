"""Load-bearing durable state and recovery scenarios for the supervisor."""

import socket
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
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
    ActivationRecord,
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    decide_activation_recovery,
    transition_activation,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ActivationRecoveryAction,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.control.peer import PeerVerificationError
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
from sidekick_usages.daemon.models.peer import PeerIdentity
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
from sidekick_usages.daemon.types.peer import PeerFailureCode
from sidekick_usages.daemon.types.ports import (
    ControlDispatcher,
    PeerSocket,
    PeerVerifier,
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
        journals=ActivationJournalStore(paths.activation_journals),
        queue=queue,
        operations=operations,
        codex_state=codex_state,
    )


def _activation_record(
    state: _FoundationState,
    operation_id: str,
) -> ActivationRecord:
    source, target, _codex = tuple(state.accounts)
    return ActivationRecord(
        provider_id=ProviderId.CLAUDE,
        operation_id=OperationId(operation_id),
        source_account_id=source.account_id,
        target_account_id=target.account_id,
        source_provider_identity=ProviderIdentity("claude-source-id"),
        source_generation=AuthorityGeneration("claude-source-generation"),
        expected_target_identity=ProviderIdentity("claude-target-id"),
        phase=ActivationPhase.PREPARED,
        started_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )


def test_selection_journal_and_queue_preserve_stable_independent_state(
    tmp_path: Path,
) -> None:
    """A legal switch changes one provider without label or queue coupling."""
    state = _foundation_state(tmp_path)
    source, target, _codex = tuple(state.accounts)
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
    record = _activation_record(
        state,
        "fbd44d2b-d774-4328-be10-00b5d3a8650b",
    )
    with state.journals.hold(
        ProviderId.CLAUDE,
        (source.account_id, target.account_id),
    ) as activation:
        activation.begin(record)
        activation.advance(
            record.operation_id,
            ActivationPhase.OUTGOING_RETAINED,
            updated_at=REFERENCE_TIME + timedelta(seconds=1),
        )
        activation.advance(
            record.operation_id,
            ActivationPhase.TARGET_ACTIVATED,
            updated_at=REFERENCE_TIME + timedelta(seconds=2),
        )
        activation.advance(
            record.operation_id,
            ActivationPhase.READ_BACK_VERIFIED,
            updated_at=REFERENCE_TIME + timedelta(seconds=3),
        )
        activation.commit_verified(
            record.operation_id,
            _selected(
                ProviderId.CLAUDE,
                target.account_id,
                "claude-target-id",
                "claude-target-generation",
                verified_in=3,
            ),
            state.selected,
            updated_at=REFERENCE_TIME + timedelta(seconds=4),
        )

    claude_selected = state.selected.load(ProviderId.CLAUDE)
    assert claude_selected is not None
    assert claude_selected.account_id == target.account_id
    assert state.selected.load(ProviderId.CODEX) == state.codex_state
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
        state.queue.get(target.account_id, OperationKind.MAINTAIN) is not None
    )
    with pytest.raises(ValueError, match="Illegal activation"):
        transition_activation(
            record,
            ActivationPhase.COMMITTED,
            updated_at=REFERENCE_TIME,
        )


def test_interrupted_activation_recovers_from_provider_read_back(
    tmp_path: Path,
) -> None:
    """Restart follows native truth and retains every account's due work."""
    state = _foundation_state(tmp_path)
    _source, target, codex = tuple(state.accounts)
    record = _activation_record(
        state,
        "4a85762c-e517-4f68-85be-a2ee2e027a66",
    )
    state.journals.begin(record)
    state.journals.advance(
        ProviderId.CLAUDE,
        record.operation_id,
        ActivationPhase.TARGET_ACTIVATED,
        updated_at=REFERENCE_TIME + timedelta(seconds=1),
    )

    restarted = ActivationJournalStore(state.paths.activation_journals)
    interrupted = restarted.load(ProviderId.CLAUDE).active
    assert interrupted is not None
    target_read_back = _selected(
        ProviderId.CLAUDE,
        target.account_id,
        "claude-target-id",
        "claude-target-generation",
        verified_in=2,
    )
    assert (
        decide_activation_recovery(interrupted, target_read_back)
        is ActivationRecoveryAction.COMMIT_VERIFIED
    )
    logged_out = SelectedAccountState(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.LOGGED_OUT,
        account_id=None,
        provider_identity=None,
        runtime_generation=None,
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.LOGGED_OUT,
    )
    assert (
        decide_activation_recovery(interrupted, logged_out)
        is ActivationRecoveryAction.REQUEST_OFFICIAL_ROLLBACK
    )
    unreadable = SelectedAccountState(
        provider_id=ProviderId.CLAUDE,
        runtime_state=ProviderRuntimeState.UNREADABLE,
        account_id=None,
        provider_identity=None,
        runtime_generation=None,
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.RECONCILIATION_REQUIRED,
    )
    assert (
        decide_activation_recovery(interrupted, unreadable)
        is ActivationRecoveryAction.RECONCILIATION_REQUIRED
    )
    assert (
        restarted.recover_from_read_back(target_read_back, state.selected)
        is ActivationRecoveryAction.COMMIT_VERIFIED
    )

    recovered = restarted.load(ProviderId.CLAUDE)
    assert recovered.active is None
    assert recovered.history[-1].phase is ActivationPhase.COMMITTED
    assert state.selected.load(ProviderId.CLAUDE) == target_read_back
    codex_selected = state.selected.load(ProviderId.CODEX)
    assert codex_selected is not None
    assert codex_selected.account_id == codex.account_id
    assert (
        OperationQueueStore(state.paths.durable_operations).load()
        == state.queue.load()
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
    terminated: bool = False
    killed: bool = False

    @property
    def process_id(self) -> int:
        return 4242

    def poll(self) -> int | None:
        return self.initial_exit_code

    def wait(self, timeout_seconds: float | None) -> int | None:
        del timeout_seconds
        if self.initial_exit_code is not None:
            return self.initial_exit_code
        if self.killed:
            return -9
        if self.terminated and not self.requires_kill:
            return -15
        return None

    def terminate_group(self) -> None:
        self.events.append(f"terminate:{self.operation_id}")
        self.terminated = True

    def kill_group(self) -> None:
        self.events.append(f"kill:{self.operation_id}")
        self.killed = True


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
    )


def test_supervisor_isolates_timeout_and_recovers_without_duplicate_work(
    tmp_path: Path,
) -> None:
    """A timed-out account cannot block completion or restart recovery."""
    state = _foundation_state(tmp_path)
    first, second, third = state.operations
    assert state.queue.remove_account(third.account_id) == 1
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


def test_codex_callback_preempts_and_reaps_same_authority_worker(
    tmp_path: Path,
) -> None:
    """The reserved callback lane reaps a hung lower-priority owner."""
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.service_state).repair_parent_permissions()
    clock = _RuntimeClock()
    queue = OperationQueueStore(paths.durable_operations)
    results = WorkerResultStore(paths.durable_operations)
    account_id = SidekickAccountId("cd87ea15-3087-42b9-93b8-43d6ee429a5c")
    maintenance = _operation(
        account_id,
        ProviderId.CODEX,
        "b6b3c584-7ca9-49fa-845b-57b74c81980e",
    )
    callback = DueOperation(
        operation_id=OperationId("5fac56f4-95d5-4ced-8b72-13c756bebc47"),
        provider_id=ProviderId.CODEX,
        account_id=account_id,
        kind=OperationKind.REFRESH,
        priority=OperationPriority.CODEX_CALLBACK,
        state=OperationState.SCHEDULED,
        due_at=clock.now(),
        updated_at=clock.now(),
    )
    queue.enqueue(maintenance)
    launcher = _FakeWorkerLauncher(
        results,
        clock,
        frozenset({callback.operation_id}),
        frozenset({maintenance.operation_id}),
    )
    workers = WorkerPool(
        launcher,
        _worker_planner(),
        lambda: None,
        general_limit=1,
        callback_timeout_seconds=8,
        termination_grace_seconds=0.01,
    )
    scheduler = DurableScheduler(
        queue,
        results,
        workers,
        clock,
        monotonic=clock.monotonic,
    )
    scheduler.recover()
    assert scheduler.dispatch_due()[0].operation_id == (
        maintenance.operation_id
    )
    queue.enqueue(callback)
    assert scheduler.dispatch_due()[0].operation_id == callback.operation_id
    scheduler.collect()

    assert launcher.events == [
        f"launch:{maintenance.operation_id}",
        f"terminate:{maintenance.operation_id}",
        f"kill:{maintenance.operation_id}",
        f"launch:{callback.operation_id}",
    ]
    retained = queue.find(maintenance.operation_id)
    assert retained is not None
    assert retained.state is OperationState.RETRY_WAIT
    assert retained.failure_code == "worker_preempted"
    assert queue.find(callback.operation_id) is None
    assert workers.active_count == 0
    assert clock.monotonic_time == _MONOTONIC_START
