"""Load-bearing durable state and recovery scenarios for the supervisor."""

import socket
import sys
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
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
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.endpoint import (
    SOCKET_MODE,
    control_endpoint_state,
)
from sidekick_usages.daemon.control.protocol import (
    PROTOCOL_VERSION,
    FrameDecoder,
    decode_event,
    decode_request,
    encode_frame,
    encode_request,
)
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.lifecycle.readiness import SupervisorReadiness
from sidekick_usages.daemon.models.lifecycle import ServiceBackendStatus
from sidekick_usages.daemon.models.protocol import (
    ActivationPayload,
    ControlRequest,
    EmptyPayload,
    ProviderPayload,
)
from sidekick_usages.daemon.models.scheduler import (
    SchedulerCompletion,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.runtime.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import WakeupChannel
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.protocol import (
    ConnectedSocket,
    EventKind,
    RequestKind,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.pool import WorkerPool
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
from sidekick_usages.platform.types import PeerVerifier
from tests.fakes.daemon.control import (
    FragmentingSocket,
    RecordingDispatcher,
    RejectedPeer,
    VerifiedPeer,
    exercise_blocked_stream_cancellation,
    rejected_protocol_response,
    serve_protocol_connection,
)
from tests.fakes.daemon.runtime import (
    FakeWorkerLauncher,
    RuntimeClock,
    foundation_runtime,
    worker_planner,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
    saved_account,
)

_EXPECTED_WORKER_COUNT = 2
_CLAUDE_RECOVERY_OPERATION_ID = OperationId(
    "cc413f38-2b11-418a-a4a7-b0e45666067e"
)
_CLAUDE_NATIVE_OPERATION_ID = OperationId(
    "ddd13f38-2b11-418a-a4a7-b0e45666067e"
)
_NATIVE_SUPERSEDED_CODE = "superseded_by_native_login"


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
        queue.enqueue(operation)
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


def _assert_native_login_cancels_stale_activation(
    state: _FoundationState,
    results: WorkerResultStore,
    clock: RuntimeClock,
    scheduler: DurableScheduler,
    events: OperationEventHub,
) -> None:
    """Prove native read-back cancels and terminates an older switch."""
    stale_activation = state.operations[0]
    target = tuple(state.accounts)[1]
    clock.advance(1)
    state.selected.save(
        _selected(
            ProviderId.CLAUDE,
            target.account_id,
            "claude-target-id",
            "claude-target-generation",
            verified_in=7,
        )
    )
    native = DueOperation(
        operation_id=_CLAUDE_NATIVE_OPERATION_ID,
        provider_id=ProviderId.CLAUDE,
        account_id=None,
        kind=OperationKind.RECONCILE_NATIVE,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.SCHEDULED,
        due_at=clock.now(),
        updated_at=clock.now(),
    )
    state.queue.enqueue(native)
    running = state.queue.transition(
        native.operation_id,
        OperationState.RUNNING,
        updated_at=clock.now(),
    )
    results.save(
        WorkerResult(
            operation_id=native.operation_id,
            outcome=WorkerOutcome.NO_CHANGE,
            finished_at=clock.now(),
        )
    )
    completions = scheduler.recover()
    update = next(
        events.follow_operation(
            new_request_id(),
            stale_activation.operation_id,
        )
    )

    assert len(completions) == 1
    assert completions[0].operation_id == running.operation_id
    assert completions[0].outcome is WorkerOutcome.NO_CHANGE
    assert state.queue.find(stale_activation.operation_id) is None
    assert update.completion is not None
    assert update.completion.outcome is WorkerOutcome.CANCELLED
    assert update.completion.failure_code == _NATIVE_SUPERSEDED_CODE


def _assert_reused_operation_follows_current_events(
    state: _FoundationState,
) -> None:
    """Ignore retained completion from an earlier recurrent operation run."""
    native = DueOperation(
        operation_id=_CLAUDE_NATIVE_OPERATION_ID,
        provider_id=ProviderId.CLAUDE,
        account_id=None,
        kind=OperationKind.RECONCILE_NATIVE,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    state.queue.enqueue(native)
    running = state.queue.transition(
        native.operation_id,
        OperationState.RUNNING,
        updated_at=REFERENCE_TIME,
    )
    events = OperationEventHub()
    completion = SchedulerCompletion(
        operation_id=native.operation_id,
        state=OperationState.SCHEDULED,
        outcome=WorkerOutcome.SUCCEEDED,
        failure_code=None,
    )
    events.completed(completion)
    dispatcher = SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        events,
        RuntimeClock(),
        Event().set,
        Event().set,
    )
    stream = dispatcher.dispatch(
        ControlRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=new_request_id(),
            kind=RequestKind.RECONCILE,
            payload=ProviderPayload(ProviderId.CLAUDE),
            package_version=__version__,
        )
    )
    assert next(stream).kind is EventKind.ACCEPTED
    events.started(running)
    events.completed(completion)
    assert tuple(event.kind for event in stream) == (
        EventKind.PROGRESS,
        EventKind.COMPLETED,
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


def test_authenticated_control_stream_frames_completes_and_cancels(
    tmp_path: Path,
) -> None:
    """One peer-proven stream frames, completes, and cancels safely."""
    account_id = SidekickAccountId("69b33871-dcd9-4e47-8ef8-f77d9944a956")
    default_payload = ActivationPayload(ProviderId.CLAUDE, account_id)
    assert not default_payload.allow_remote_control_disconnect
    fragmented_request = ControlRequest(
        protocol_version=PROTOCOL_VERSION,
        request_id=new_request_id(),
        kind=RequestKind.ACTIVATE,
        payload=default_payload,
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
    dispatcher = RecordingDispatcher([], [], Event())
    server = Thread(
        target=serve_protocol_connection,
        args=(server_socket, VerifiedPeer(), dispatcher),
    )
    server.start()
    fragmented_socket = FragmentingSocket(client_socket)
    fragmented_client: ConnectedSocket = fragmented_socket
    client = ControlClient(fragmented_client)

    activation = tuple(
        client.activate(
            ProviderId.CLAUDE,
            account_id,
            allow_remote_control_disconnect=True,
        )
    )
    assert tuple(event.kind for event in activation) == (
        EventKind.ACCEPTED,
        EventKind.PROGRESS,
        EventKind.COMPLETED,
    )
    subscription = client.subscribe()
    accepted = next(subscription)
    assert accepted.kind is EventKind.ACCEPTED
    cancellation_failure = exercise_blocked_stream_cancellation(
        client,
        subscription,
        fragmented_socket,
    )
    dispatcher.release_subscription.set()
    server.join(timeout=2)

    assert isinstance(cancellation_failure, ConnectionError)
    assert not server.is_alive()
    assert tuple(request.kind for request in dispatcher.requests) == (
        RequestKind.ACTIVATE,
        RequestKind.SUBSCRIBE,
    )
    recorded_payload = dispatcher.requests[0].payload
    assert isinstance(recorded_payload, ActivationPayload)
    assert recorded_payload.allow_remote_control_disconnect
    assert dispatcher.cancellations == [accepted.request_id]

    state = _foundation_state(tmp_path)
    durable_dispatcher = SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        OperationEventHub(),
        RuntimeClock(),
        Event().set,
        Event().set,
    )
    approved_request = replace(
        fragmented_request,
        request_id=new_request_id(),
        payload=recorded_payload,
    )
    next(durable_dispatcher.dispatch(approved_request))
    restarted_queue = OperationQueueStore(state.paths.durable_operations)
    persisted = restarted_queue.get(
        ProviderId.CLAUDE,
        account_id,
        OperationKind.ACTIVATE,
    )
    assert persisted is not None
    assert persisted.allow_remote_control_disconnect
    _assert_reused_operation_follows_current_events(state)


def test_control_protocol_fails_closed_at_each_trust_boundary(
    tmp_path: Path,
) -> None:
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
        (RejectedPeer(), malformed, None),
        (VerifiedPeer(), malformed, EventKind.FAILED),
        (VerifiedPeer(), incompatible, EventKind.INCOMPATIBLE),
    )
    for verifier, outbound, expected_kind in cases:
        dispatcher = RecordingDispatcher([], [], Event())
        response = rejected_protocol_response(
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

    runtime_directory = tmp_path / "runtime"
    socket_path = runtime_directory / "supervisor.sock"
    if sys.platform == "win32":
        with pytest.raises(ConnectionError):
            ControlClient.connect(socket_path)
        return
    dispatcher = RecordingDispatcher([], [], Event())
    server = LocalControlServer(
        runtime_directory,
        socket_path,
        dispatcher,
    )
    server.open()
    assert (
        control_endpoint_state(runtime_directory, socket_path)
        is ServiceComponentState.HEALTHY
    )
    server_thread = Thread(target=server.serve_once)
    server_thread.start()
    client = ControlClient.connect(socket_path)
    client.handshake()
    client.close()
    server_thread.join(timeout=2)
    assert not server_thread.is_alive()
    server.close()

    server.open()
    socket_path.chmod(SOCKET_MODE | 0o066)
    assert (
        control_endpoint_state(runtime_directory, socket_path)
        is ServiceComponentState.UNHEALTHY
    )
    with pytest.raises(PermissionError, match="unsafe_control_endpoint"):
        ControlClient.connect(socket_path)
    server.close()

    stale_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale_socket.bind(str(socket_path))
    socket_path.chmod(SOCKET_MODE)
    stale_socket.close()
    paths = replace(
        make_application_paths(tmp_path),
        runtime_directory=runtime_directory,
        supervisor_socket=socket_path,
    )
    health = SupervisorReadiness(paths, FixedClock()).health(
        ServiceBackendStatus.single(
            ServiceBackendId.SYSTEMD,
            ServiceLifecycleState.READY,
        )
    )
    assert (
        health.socket,
        health.peer,
        health.protocol,
    ) == (
        ServiceComponentState.HEALTHY,
        ServiceComponentState.UNAVAILABLE,
        ServiceComponentState.UNAVAILABLE,
    )
    socket_path.unlink()


def test_supervisor_isolates_timeout_and_recovers_without_duplicate_work(
    tmp_path: Path,
) -> None:
    """A timed-out account cannot block completion or restart recovery."""
    state = _foundation_state(tmp_path)
    first, second, third = state.operations
    assert state.queue.remove_account(first.required_account_id) == 1
    assert state.queue.remove_account(third.required_account_id) == 1
    first = replace(
        first,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
    )
    codex_selection = replace(
        third,
        kind=OperationKind.ACTIVATE,
        priority=OperationPriority.INTERACTIVE,
    )
    claude_recovery = replace(
        first,
        operation_id=_CLAUDE_RECOVERY_OPERATION_ID,
        kind=OperationKind.RECONCILE,
    )
    assert state.queue.enqueue(first) == first
    results = WorkerResultStore(state.paths.durable_operations)
    clock = RuntimeClock()
    wakeup = WakeupChannel()
    launcher = FakeWorkerLauncher(
        results,
        clock,
        frozenset({second.operation_id}),
    )
    workers = WorkerPool(
        launcher,
        worker_planner(),
        wakeup.notify,
        general_timeout_seconds=5,
        termination_grace_seconds=0.01,
    )
    scheduler = DurableScheduler(
        state.queue,
        results,
        state.selected,
        workers,
        clock,
        monotonic=clock.monotonic,
    )
    recovery = ActivationRecoveryScheduler(
        state.journals,
        state.queue,
    )
    runtime = foundation_runtime(
        state.paths,
        scheduler,
        recovery,
        clock,
        wakeup,
    )

    runtime.recover()
    runtime.run_cycle()
    runtime.run_cycle()
    assert workers.has_capacity_for(codex_selection)
    assert not workers.has_capacity_for(claude_recovery)
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
        FakeWorkerLauncher(results, clock, frozenset()),
        worker_planner(),
        lambda: None,
    )
    restarted_events = OperationEventHub()
    restarted = DurableScheduler(
        OperationQueueStore(state.paths.durable_operations),
        results,
        state.selected,
        restarted_workers,
        clock,
        events=restarted_events,
        monotonic=clock.monotonic,
    )
    assert restarted.recover() == ()
    _assert_native_login_cancels_stale_activation(
        state,
        results,
        clock,
        restarted,
        restarted_events,
    )
    durable = OperationQueueStore(state.paths.durable_operations).load()
    assert len(durable) == _EXPECTED_WORKER_COUNT
    assert len({operation.operation_id for operation in durable}) == len(
        durable
    )
    wakeup.close()
