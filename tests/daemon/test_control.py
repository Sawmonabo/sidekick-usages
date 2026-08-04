"""Authenticated local control-channel tests."""

import os
import socket
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    RelatedRuntimeAuthority,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
    UnexpectedServiceEventError,
    consume_selection_action,
)
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.endpoint import (
    SOCKET_MODE,
    control_endpoint_state,
)
from sidekick_usages.daemon.control.protocol import (
    FrameDecoder,
    decode_event,
    decode_request,
    encode_event,
    encode_frame,
    encode_request,
)
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.lifecycle.readiness import SupervisorReadiness
from sidekick_usages.daemon.models.control import VerifiedControlRequest
from sidekick_usages.daemon.models.lifecycle import ServiceBackendStatus
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    ActivationPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    EventPayload,
    FailedPayload,
    ProviderPayload,
    RequestPayload,
)
from sidekick_usages.daemon.models.scheduler import SchedulerCompletion
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantAdoptionRequest,
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRegistration,
    SelectionRequestError,
    SelectionStatus,
    TurnAdmission,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.selection.ports import SelectionSupervisorPort
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    ConnectedSocket,
    EventKind,
    ProtocolErrorCode,
    RequestKind,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.models import PeerIdentity
from sidekick_usages.platform.peer import (
    _DARWIN_LOCAL_PEERPID,
    _DARWIN_SOL_LOCAL,
    OperatingSystemPeerVerifier,
    PeerVerificationError,
    _macos_peer_process_id,
)
from sidekick_usages.platform.types import PeerFailureCode, PeerVerifier
from tests.fakes.daemon.control import (
    FragmentingSocket,
    RecordingDispatcher,
    RegistryMonitorScenario,
    RejectedPeer,
    VerifiedPeer,
    exercise_blocked_stream_cancellation,
    exercise_closed_subscription_monitor,
    foundation_dispatcher,
    rejected_protocol_response,
    serve_protocol_connection,
)
from tests.fakes.daemon.foundation import (
    CLAUDE_NATIVE_OPERATION_ID,
    FoundationState,
    foundation_state,
)
from tests.fakes.daemon.runtime import ResidentState, RuntimeClock
from tests.support.persistence import make_application_paths
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME, FixedClock


class _OperatorSelection(SelectionSupervisorPort):
    """Return one immediate result through the operator control surface."""

    def __init__(self) -> None:
        self.operation_id: OperationId | None = None
        self.unsupported_target: SidekickAccountId | None = None
        self.block_after_waiting = False
        self.waiting = Event()
        self.allow_completion = Event()
        self.completed = Event()

    def reconcile_generation(
        self,
        operation_id: OperationId,
        authority: RelatedRuntimeAuthority,
    ) -> SelectionResult | None:
        """Drain one synthetic internal generation transition."""
        events = self.select_events(
            operation_id, authority.provider_id, authority.account_id
        )[1]
        return next(
            event for event in events if isinstance(event, SelectionResult)
        )

    def select_events(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> tuple[
        OperationId,
        Iterator[SelectionStatus | SelectionResult],
    ]:
        """Record canonical phase order before synthetic completion."""
        if target_account_id == self.unsupported_target:
            raise SelectionRequestError(
                SelectionCode.UNSUPPORTED_SESSION_CAPABILITY
            )
        canonical_id = self.operation_id or operation_id
        self.operation_id = canonical_id

        def events() -> Iterator[SelectionStatus | SelectionResult]:
            for phase in (
                SelectionPhase.PREVALIDATING,
                SelectionPhase.WAITING_OLD_TURNS,
                SelectionPhase.COMMITTING,
                SelectionPhase.AWAITING_READY,
            ):
                yield SelectionStatus(
                    provider_id=provider_id,
                    operation_id=canonical_id,
                    finalized_account_id=None,
                    finalized_epoch=None,
                    target_account_id=target_account_id,
                    pending_epoch=SelectionEpoch(1),
                    phase=phase,
                    code=None,
                    registered_count=1,
                    reachable_count=1,
                    required_count=(
                        0 if phase is SelectionPhase.PREVALIDATING else 1
                    ),
                    ready_count=(
                        1 if phase is SelectionPhase.AWAITING_READY else 0
                    ),
                )
                if phase is SelectionPhase.WAITING_OLD_TURNS:
                    self.waiting.set()
                    if (
                        self.block_after_waiting
                        and not self.allow_completion.wait(2)
                    ):
                        raise AssertionError(
                            "Synthetic selection stayed blocked."
                        )
            self.completed.set()
            yield SelectionResult(
                operation_id=canonical_id,
                provider_id=provider_id,
                target_account_id=target_account_id,
                target_generation=AuthorityGeneration("generation-target-1"),
                epoch=SelectionEpoch(1),
                outcome=SelectionOutcome.READY,
                safe_code=SelectionCode.SELECTION_SUCCEEDED,
                required_count=0,
                ready_count=0,
                adopted_count=0,
                lost_count=0,
                started_at=REFERENCE_TIME,
                completed_at=REFERENCE_TIME,
            )

        return canonical_id, events()


def _verified(request: ControlRequest) -> VerifiedControlRequest:
    """Pair one direct dispatcher request with same-user proof."""
    return VerifiedControlRequest(request, PeerIdentity(1000))


def _assert_monitor(state: FoundationState, resident: ResidentState) -> None:
    """Assert the complete transactional cancellation contract."""
    result = RegistryMonitorScenario(state, resident).exercise()
    assert result.states == (True, True, False, True, True, True, False, False)
    assert result.registry_state == (0, 0, (result.participant_id,), 0, 0)
    assert result.diagnostics.count("subscription_cancellation_failed") == 1
    assert "synthetic cancellation failure" not in result.diagnostics


def _assert_reconcile_follows_current_events(state: FoundationState) -> None:
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
    authority = RelatedRuntimeAuthority(
        provider_id=ProviderId.CLAUDE,
        account_id=state.operations[0].required_account_id,
        generation=AuthorityGeneration("generation-native-2"),
        observed_at=REFERENCE_TIME,
    )
    completion = SchedulerCompletion(
        provider_id=native.provider_id,
        operation_id=native.operation_id,
        operation_kind=native.kind,
        state=OperationState.SCHEDULED,
        outcome=WorkerOutcome.SUCCEEDED,
        failure_code=None,
        related_runtime_authority=authority,
    )
    events.completed(completion)
    selection = _OperatorSelection()
    dispatcher = SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        events,
        RuntimeClock(),
        Event().set,
        Event().set,
        selection=selection,
    )
    stream = dispatcher.dispatch(
        _verified(
            ControlRequest(
                protocol_version=PROTOCOL_VERSION,
                request_id=new_request_id(),
                kind=RequestKind.RECONCILE,
                payload=ProviderPayload(ProviderId.CLAUDE),
                package_version=__version__,
            )
        )
    )
    assert next(stream).kind is EventKind.ACCEPTED
    events.started(running)
    events.completed(completion)
    kinds = tuple(event.kind for event in stream)
    assert kinds == (EventKind.PROGRESS, EventKind.COMPLETED)
    assert selection.completed.is_set()


@REQUIRES_MANAGED_RUNTIME
def test_authenticated_control_stream_frames_completes_and_cancels(
    tmp_path: Path,
) -> None:
    """One peer-proven stream frames, completes, and cancels safely."""
    account_id = SidekickAccountId("69b33871-dcd9-4e47-8ef8-f77d9944a956")
    default_payload = AccountPayload(ProviderId.CLAUDE, account_id)
    fragmented_request = ControlRequest(
        protocol_version=PROTOCOL_VERSION,
        request_id=new_request_id(),
        kind=RequestKind.REFRESH_ACCOUNT,
        payload=default_payload,
        package_version=__version__,
    )
    frame = encode_request(fragmented_request)
    assert b"allow_remote_control_disconnect" not in frame
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

    refresh = tuple(client.refresh_account(ProviderId.CLAUDE, account_id))
    assert tuple(event.kind for event in refresh) == (
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
    server.join(timeout=2)

    assert isinstance(cancellation_failure, ConnectionError)
    assert not server.is_alive()
    assert tuple(request.kind for request in dispatcher.requests) == (
        RequestKind.REFRESH_ACCOUNT,
        RequestKind.SUBSCRIBE,
    )
    recorded_payload = dispatcher.requests[0].payload
    assert isinstance(recorded_payload, AccountPayload)
    assert dispatcher.cancellations == [accepted.request_id]
    exercise_closed_subscription_monitor(
        dispatcher,
        _verified(fragmented_request),
    )
    assert dispatcher.cancellations == [accepted.request_id]
    state, resident = foundation_state(tmp_path), ResidentState()
    durable_dispatcher = foundation_dispatcher(state)
    _assert_monitor(state, resident)
    approved_request = replace(
        fragmented_request,
        request_id=new_request_id(),
        payload=recorded_payload,
    )
    next(durable_dispatcher.dispatch(_verified(approved_request)))
    restarted_queue = OperationQueueStore(state.paths.durable_operations)
    persisted = restarted_queue.get(
        ProviderId.CLAUDE,
        account_id,
        OperationKind.REFRESH,
    )
    assert persisted is not None
    uncoordinated_request = replace(
        fragmented_request,
        request_id=new_request_id(),
        kind=RequestKind.ACTIVATE,
        payload=ActivationPayload(ProviderId.CLAUDE, account_id),
    )
    rejected = next(
        durable_dispatcher.dispatch(_verified(uncoordinated_request))
    )
    assert rejected.kind is EventKind.FAILED
    assert rejected.payload == FailedPayload(
        None,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION.value,
    )
    assert (
        restarted_queue.get(
            ProviderId.CLAUDE,
            account_id,
            OperationKind.ACTIVATE,
        )
        is None
    )
    _assert_reconcile_follows_current_events(state)


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
    assert (
        control_endpoint_state(runtime_directory, socket_path)
        is ServiceComponentState.ABSENT
    )
    with pytest.raises(FileNotFoundError):
        ControlClient.connect(socket_path)
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


def _verified_peer_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[PeerIdentity, PeerIdentity]:
    """Return exact-process and same-user-only kernel peer proofs."""
    server_socket, client_socket = socket.socketpair()
    try:
        peer = OperatingSystemPeerVerifier(os.geteuid()).verify(server_socket)
    finally:
        server_socket.close()
        client_socket.close()

    def unavailable_process_start(_process_id: int) -> int:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)

    process_reader = (
        "_macos_process_start"
        if sys.platform == "darwin"
        else "_linux_process_start"
    )
    monkeypatch.setattr(
        f"sidekick_usages.platform.peer.{process_reader}",
        unavailable_process_start,
    )
    server_socket, client_socket = socket.socketpair()
    try:
        operator = OperatingSystemPeerVerifier(os.geteuid()).verify(
            server_socket
        )
    finally:
        server_socket.close()
        client_socket.close()
    return peer, operator


def test_darwin_peer_pid_socket_constants_are_portable() -> None:
    """Use Darwin ABI constants even when Python omits their names."""
    assert (_DARWIN_SOL_LOCAL, _DARWIN_LOCAL_PEERPID) == (0, 0x002)


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Darwin")
def test_darwin_socketpair_proves_the_real_peer_process() -> None:
    """Read the kernel-owned peer PID from a real Darwin socketpair."""
    server_socket, client_socket = socket.socketpair()
    try:
        assert _macos_peer_process_id(server_socket) == os.getpid()
    finally:
        server_socket.close()
        client_socket.close()


def test_select_accepts_with_the_correlated_operation_id(
    tmp_path: Path,
) -> None:
    """Acknowledge selection before returning its correlated result."""
    operation_id = OperationId("9265897c-7881-47af-b69e-575823b33c3f")
    target = SidekickAccountId("2999e642-0299-4f73-9187-01b3d240e3d8")
    selection = _OperatorSelection()
    state = foundation_state(tmp_path)
    dispatcher = SupervisorDispatcher(
        state.queue,
        ServiceStateStore(state.paths.service_state),
        OperationEventHub(),
        RuntimeClock(),
        Event().set,
        Event().set,
        selection=selection,
        operation_id_factory=iter(
            (
                operation_id,
                OperationId("77777777-7777-4777-8777-777777777777"),
                OperationId("88888888-8888-4888-8888-888888888888"),
            )
        ).__next__,
    )
    request = ControlRequest(
        PROTOCOL_VERSION,
        new_request_id(),
        RequestKind.SELECT_ACCOUNT,
        AccountPayload(ProviderId.CLAUDE, target),
        __version__,
    )
    dispatched = tuple(dispatcher.dispatch(_verified(request)))
    accepted, completed = dispatched[0], dispatched[-1]
    assert isinstance(accepted.payload, AcceptedPayload)
    observed: list[SelectionStatus] = []
    result = consume_selection_action(
        iter(dispatched),
        provider_id=ProviderId.CLAUDE,
        account_id=target,
        status=observed.append,
    )
    assert (
        accepted.payload.operation_id,
        tuple(status.phase for status in observed),
        tuple(event.kind for event in dispatched),
    ) == (
        operation_id,
        (
            SelectionPhase.PREVALIDATING,
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.COMMITTING,
            SelectionPhase.AWAITING_READY,
        ),
        (
            EventKind.ACCEPTED,
            *((EventKind.SELECTION_STATUS,) * 4),
            EventKind.SELECTION_RESULT,
        ),
    )
    assert isinstance(completed.payload, SelectionResult)
    assert (
        result,
        completed.payload.operation_id,
        selection.operation_id,
    ) == (completed.payload, operation_id, operation_id)
    mismatched = (
        *dispatched[:-1],
        replace(
            completed,
            payload=replace(
                completed.payload,
                epoch=SelectionEpoch(2),
            ),
        ),
    )
    with pytest.raises(UnexpectedServiceEventError, match="unrelated"):
        consume_selection_action(
            iter(mismatched),
            provider_id=ProviderId.CLAUDE,
            account_id=target,
        )
    commit_status = dispatched[3].payload
    assert isinstance(commit_status, SelectionStatus)
    changed_epoch = (
        *dispatched[:3],
        replace(
            dispatched[3],
            payload=replace(
                commit_status,
                finalized_account_id=SidekickAccountId(
                    "3b094d2e-1075-4c79-b59c-4295111028ab"
                ),
                finalized_epoch=SelectionEpoch(1),
                pending_epoch=SelectionEpoch(2),
            ),
        ),
        *dispatched[4:],
    )
    with pytest.raises(UnexpectedServiceEventError, match="changed"):
        consume_selection_action(
            iter(changed_epoch),
            provider_id=ProviderId.CLAUDE,
            account_id=target,
        )
    replayed = tuple(
        dispatcher.dispatch(
            _verified(replace(request, request_id=new_request_id()))
        )
    )
    replay_accepted = replayed[0].payload
    assert isinstance(replay_accepted, AcceptedPayload)
    assert (
        replay_accepted.operation_id,
        consume_selection_action(
            iter(replayed),
            provider_id=ProviderId.CLAUDE,
            account_id=target,
        ),
    ) == (
        operation_id,
        completed.payload,
    )
    selection.unsupported_target = SidekickAccountId(
        "3b094d2e-1075-4c79-b59c-4295111028ab"
    )
    setup_request = replace(
        request,
        request_id=new_request_id(),
        payload=AccountPayload(
            ProviderId.CLAUDE, selection.unsupported_target
        ),
    )
    refused = tuple(dispatcher.dispatch(_verified(setup_request)))[-1]
    assert isinstance(refused.payload, FailedPayload)
    assert refused.payload.code == "unsupported_session_capability"
    disconnected = _OperatorSelection()
    disconnected.block_after_waiting = True
    disconnected_state = foundation_state(tmp_path / "selection-stream")
    disconnected_dispatcher = SupervisorDispatcher(
        disconnected_state.queue,
        ServiceStateStore(disconnected_state.paths.service_state),
        OperationEventHub(),
        RuntimeClock(),
        Event().set,
        Event().set,
        selection=disconnected,
    )
    server_socket, client_socket = socket.socketpair()
    server = Thread(
        target=serve_protocol_connection,
        args=(server_socket, VerifiedPeer(), disconnected_dispatcher),
    )
    server.start()
    client = ControlClient(client_socket)
    stream = client.select_account(ProviderId.CLAUDE, target)
    assert tuple(next(stream).kind for _ in range(3)) == (
        EventKind.ACCEPTED,
        EventKind.SELECTION_STATUS,
        EventKind.SELECTION_STATUS,
    )
    assert disconnected.waiting.wait(1)
    client_socket.shutdown(socket.SHUT_RDWR)
    client.close()
    disconnected.allow_completion.set()
    server.join(timeout=2)
    assert disconnected.completed.is_set()
    assert not server.is_alive()


def _dispatch_failure_code(
    root: Path,
    request: ControlRequest,
    peer: PeerIdentity,
) -> str:
    """Return one safe dispatcher refusal without a selection adapter."""
    state = foundation_state(root)
    dispatcher = foundation_dispatcher(state)
    events = tuple(dispatcher.dispatch(VerifiedControlRequest(request, peer)))
    event = events[-1]
    assert isinstance(event.payload, FailedPayload)
    return event.payload.code


@REQUIRES_MANAGED_RUNTIME
def test_participant_codec_uses_only_kernel_process_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Registration omits client process claims and kernel proof is exact."""
    peer, operator = _verified_peer_pair(monkeypatch)
    assert peer.process_identity is not None
    assert peer.process_identity.process_id == os.getpid()
    assert peer.process_identity.start_identity > 0
    assert operator.process_identity is None

    participant_id = ParticipantId("a305e0bb-69e4-42e5-a532-0ad13c5e9f78")
    turn_id = TurnId("999f42ab-a679-4a5c-889f-b39c2f478beb")
    account_id = SidekickAccountId("2999e642-0299-4f73-9187-01b3d240e3d8")
    generation = AuthorityGeneration("generation-target-8")
    epoch = SelectionEpoch(8)
    manifest = ParticipantManifest(
        participant_id=participant_id,
        provider_id=ProviderId.CLAUDE,
        client_kind=ParticipantClientKind.CLAUDE_CODE,
        capability_version=1,
        connection_generation=1,
    )
    with pytest.raises(ValueError, match="capability version"):
        replace(manifest, capability_version=2)
    ready = ParticipantReadyProof(
        account_id=account_id,
        generation=generation,
        epoch=epoch,
    )
    request_payloads: tuple[tuple[RequestKind, RequestPayload], ...] = (
        (RequestKind.PARTICIPANT_REGISTER, manifest),
        (
            RequestKind.PARTICIPANT_SUBSCRIBE,
            ParticipantConnectionRequest(participant_id, 1),
        ),
        (RequestKind.TURN_BEGIN, TurnBeginRequest(participant_id, 1, turn_id)),
        (RequestKind.TURN_END, TurnEndRequest(participant_id, 1, turn_id)),
        (
            RequestKind.PARTICIPANT_READY,
            ParticipantReadyRequest(
                participant_id=participant_id,
                connection_generation=1,
                proof=ready,
            ),
        ),
        (
            RequestKind.PARTICIPANT_ADOPT,
            ParticipantAdoptionRequest(
                participant_id=participant_id,
                connection_generation=1,
                proof=ParticipantAdoptionProof(
                    turn_id=turn_id,
                    account_id=account_id,
                    generation=generation,
                    epoch=epoch,
                ),
            ),
        ),
        (
            RequestKind.SELECT_ACCOUNT,
            AccountPayload(ProviderId.CLAUDE, account_id),
        ),
        (
            RequestKind.SELECTION_STATUS,
            ProviderPayload(ProviderId.CLAUDE),
        ),
    )
    encoded_registration = b""
    for kind, payload in request_payloads:
        request = ControlRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=new_request_id(),
            kind=kind,
            payload=payload,
            package_version=__version__,
        )
        decoder = FrameDecoder()
        (encoded,) = decoder.feed(encode_request(request))
        decoder.finish()
        assert decode_request(encoded) == request
        if kind is RequestKind.PARTICIPANT_REGISTER:
            encoded_registration = encoded
    assert b"process_id" not in encoded_registration
    assert b"start_identity" not in encoded_registration

    operation_id = OperationId("9265897c-7881-47af-b69e-575823b33c3f")
    status = SelectionStatus(
        provider_id=ProviderId.CLAUDE,
        operation_id=operation_id,
        finalized_account_id=account_id,
        finalized_epoch=epoch,
        target_account_id=account_id,
        pending_epoch=epoch,
        phase=SelectionPhase.RECOVERING,
        code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
        registered_count=3,
        reachable_count=2,
        required_count=3,
        ready_count=2,
        confirmed_dead_count=1,
        adopted_count=1,
        unreachable_count=1,
        active_turn_count=1,
        queued_turn_count=1,
    )
    participant_registration = ParticipantRegistration(
        participant_id=participant_id,
        provider_id=ProviderId.CLAUDE,
        connection_generation=1,
        registered_epoch=SelectionEpoch(7),
        pending_epoch=epoch,
    )
    event_payloads: tuple[tuple[EventKind, EventPayload], ...] = (
        (
            EventKind.PARTICIPANT_REGISTERED,
            participant_registration,
        ),
        (
            EventKind.TURN_ADMISSION,
            TurnAdmission(
                participant_id=participant_id,
                turn_id=turn_id,
                state=TurnAdmissionState.ADMITTED,
                epoch=epoch,
                account_id=account_id,
                generation=generation,
            ),
        ),
        (
            EventKind.PARTICIPANT_NOTICE,
            ParticipantNotice(
                participant_id=participant_id,
                provider_id=ProviderId.CLAUDE,
                kind=ParticipantNoticeKind.STATUS,
                epoch=epoch,
                code=SelectionCode.SELECTION_READY_ADOPTION_PENDING,
            ),
        ),
        (
            EventKind.SELECTION_RESULT,
            SelectionResult(
                operation_id=operation_id,
                provider_id=ProviderId.CLAUDE,
                target_account_id=account_id,
                target_generation=generation,
                epoch=epoch,
                outcome=SelectionOutcome.READY,
                safe_code=SelectionCode.SELECTION_SUCCEEDED,
                required_count=1,
                ready_count=1,
                adopted_count=0,
                lost_count=0,
                started_at=REFERENCE_TIME,
                completed_at=REFERENCE_TIME,
            ),
        ),
        (
            EventKind.SELECTION_STATUS,
            status,
        ),
    )
    encoded = b""
    for kind, payload in event_payloads:
        event = ControlEvent(
            protocol_version=PROTOCOL_VERSION,
            request_id=new_request_id(),
            kind=kind,
            payload=payload,
            package_version=__version__,
        )
        decoder = FrameDecoder()
        (encoded,) = decoder.feed(encode_event(event))
        decoder.finish()
        assert decode_event(encoded) == event
    assert b'"confirmed_dead_count":1' in encoded

    with pytest.raises(ValueError, match="kind and code disagree"):
        ParticipantNotice(
            participant_id=participant_id,
            provider_id=ProviderId.CLAUDE,
            kind=ParticipantNoticeKind.OPEN,
            epoch=epoch,
            code=SelectionCode.SELECTION_SUCCEEDED,
        )
    bad_status = encoded.replace(b'"pending_epoch":8', b'"pending_epoch":10')
    invalid_contracts = (
        lambda: replace(status, phase=None),
        lambda: replace(status, registered_count=17),
        lambda: replace(
            participant_registration,
            pending_epoch=SelectionEpoch(10),
        ),
        lambda: decode_event(bad_status),
    )
    for invalid_contract in invalid_contracts:
        with pytest.raises(
            ValueError,
            match=r"status|registration|malformed_frame",
        ):
            invalid_contract()

    forged = encoded_registration.replace(
        b'"participant_id"',
        b'"process_id":123,"participant_id"',
        1,
    )
    with pytest.raises(ValueError, match="malformed_frame"):
        decode_request(forged)
    registration = ControlRequest(
        PROTOCOL_VERSION,
        new_request_id(),
        RequestKind.PARTICIPANT_REGISTER,
        manifest,
        __version__,
    )
    status_request = ControlRequest(
        PROTOCOL_VERSION,
        new_request_id(),
        RequestKind.SELECTION_STATUS,
        ProviderPayload(ProviderId.CLAUDE),
        __version__,
    )
    assert (
        _dispatch_failure_code(
            tmp_path / "participant",
            registration,
            operator,
        )
        == SelectionCode.UNSUPPORTED_SESSION_CAPABILITY.value
    )
    assert (
        _dispatch_failure_code(
            tmp_path / "operator",
            status_request,
            operator,
        )
        == ProtocolErrorCode.FEATURE_DISABLED.value
    )
