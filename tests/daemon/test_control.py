"""Authenticated local control-channel tests."""

import socket
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
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
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
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
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
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
from tests.fakes.daemon.foundation import (
    CLAUDE_NATIVE_OPERATION_ID,
    FoundationState,
    foundation_state,
)
from tests.fakes.daemon.runtime import RuntimeClock
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME, FixedClock


def _assert_reused_operation_follows_current_events(
    state: FoundationState,
) -> None:
    """Ignore retained completion from an earlier recurrent operation run."""
    native = DueOperation(
        operation_id=CLAUDE_NATIVE_OPERATION_ID,
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

    state = foundation_state(tmp_path)
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
    short_socket_root: Path,
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

    runtime_directory = short_socket_root / "runtime"
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
        make_application_paths(short_socket_root),
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
