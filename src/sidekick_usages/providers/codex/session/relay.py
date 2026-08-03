"""Bounded control relay for one stock Codex TUI participant."""

import socket
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock, Thread, current_thread
from types import TracebackType
from typing import Protocol, Self

from sidekick_usages.core.selection.types import SelectionCode, TurnId
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    JsonRpcRouting,
    decode_json_rpc_routing,
    encode_account_mutation_refusal,
    encode_relay_backpressure_refusal,
)
from sidekick_usages.providers.codex.app_server.methods import (
    ACCOUNT_LOGIN_CANCEL_METHOD,
    ACCOUNT_LOGIN_START_METHOD,
    ACCOUNT_LOGOUT_METHOD,
    THREAD_REALTIME_CLOSED_METHOD,
    THREAD_REALTIME_START_METHOD,
    THREAD_REALTIME_STARTED_METHOD,
    TURN_COMPLETED_METHOD,
    TURN_START_METHOD,
    TURN_STARTED_METHOD,
)
from sidekick_usages.providers.codex.broker.wire import (
    CodexRelayFrameConnection,
    CodexRelayServer,
)
from sidekick_usages.providers.codex.session.models import (
    CodexRelayAdmission,
    CodexRelayAdmissionState,
    CodexRelayAuthority,
    CodexRelayLease,
    CodexRelayLeaseKind,
)

MAX_CODEX_RELAY_QUEUED_FRAMES = 16
MAX_CODEX_RELAY_LOADED_THREADS = 256
_RELAY_POLL_SECONDS = 0.1
_ACCOUNT_MUTATION_METHODS = frozenset(
    {
        ACCOUNT_LOGIN_CANCEL_METHOD,
        ACCOUNT_LOGIN_START_METHOD,
        ACCOUNT_LOGOUT_METHOD,
    }
)


class CodexRelayAdmissionPort(Protocol):
    """Bridge provider-local frames to participant turn admission."""

    def begin(self, turn_id: TurnId) -> CodexRelayAdmission:
        """Admit or queue one stable provider operation."""

    def recheck(self, admission: CodexRelayAdmission) -> None:
        """Recheck finalized authority before the first upstream byte."""

    def end(self, turn_id: TurnId) -> None:
        """End one exact naturally terminal admitted operation."""


class CodexRelayReadinessPort(Protocol):
    """Publish participant readiness and first-real-turn adoption."""

    def ready(self, target: CodexRelayAuthority) -> None:
        """Acknowledge an idle relay ready for one target."""

    def adopted(
        self,
        turn_id: TurnId,
        target: CodexRelayAuthority,
    ) -> None:
        """Publish first provider transmission under one target."""


class CodexRelayError(RuntimeError):
    """Typed relay failure containing only one safe selection code."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True, kw_only=True)
class _QueuedStart:
    raw: bytes = field(repr=False)
    lease: CodexRelayLease


class CodexAdmissionRelay:
    """Own one stable TUI-to-resident admission relay."""

    def __init__(
        self,
        socket_path: Path,
        upstream: CodexRelayFrameConnection,
        admission: CodexRelayAdmissionPort,
        readiness: CodexRelayReadinessPort,
        turn_id_factory: Callable[[], TurnId],
    ) -> None:
        self._socket_path = socket_path
        self._upstream = upstream
        self._admission = admission
        self._readiness = readiness
        self._turn_id_factory = turn_id_factory
        self._lock = RLock()
        self._server: CodexRelayServer | None = None
        self._downstream: CodexRelayFrameConnection | None = None
        self._upstream_thread: Thread | None = None
        self._queue: deque[_QueuedStart] = deque()
        self._pending_requests: dict[int | str, CodexRelayLease] = {}
        self._turns: dict[tuple[str, str], TurnId] = {}
        self._realtime: dict[str, TurnId] = {}
        self._loaded_threads: set[str] = set()
        self._active: set[TurnId] = set()
        self._ready_target: CodexRelayAuthority | None = None
        self._opened_target: CodexRelayAuthority | None = None
        self._adopted_target: CodexRelayAuthority | None = None
        self._failure: CodexRelayError | None = None
        self._session_started = False
        self._closed = False

    @classmethod
    def open(
        cls,
        socket_path: Path,
        upstream_socket: socket.socket,
        admission: CodexRelayAdmissionPort,
        readiness: CodexRelayReadinessPort,
        *,
        turn_id_factory: Callable[[], TurnId],
    ) -> Self:
        """Upgrade upstream, bind owner-only socket, and start serving."""
        upstream = CodexRelayFrameConnection.open_upstream(upstream_socket)
        relay = cls(
            socket_path,
            upstream,
            admission,
            readiness,
            turn_id_factory,
        )
        try:
            relay._server = CodexRelayServer.start(
                socket_path,
                relay._serve_downstream,
            )
        except BaseException:
            upstream.close()
            raise
        return relay

    @property
    def socket_path(self) -> Path:
        """Return the ready owner-only participant socket."""
        return self._socket_path

    @property
    def loaded_thread_ids(self) -> tuple[str, ...]:
        """Return bounded ephemeral thread IDs for readiness readback."""
        with self._lock:
            return tuple(sorted(self._loaded_threads))

    def mark_ready(self, target: CodexRelayAuthority) -> None:
        """Acknowledge target readiness only after every lease drained."""
        with self._lock:
            self._raise_if_unusable()
            if self._active:
                code = (
                    SelectionCode.REALTIME_SESSION_ACTIVE
                    if self._realtime
                    else SelectionCode.ACTIVE_OPERATION_TIMEOUT
                )
                raise CodexRelayError(code)
            if self._ready_target == target:
                return
            if (
                self._ready_target is not None
                and self._opened_target != self._ready_target
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
        self._readiness.ready(target)
        with self._lock:
            self._ready_target = target
            self._adopted_target = None

    def open_admission(self, target: CodexRelayAuthority) -> None:
        """Release queued raw frames once under one finalized target."""
        with self._lock:
            self._raise_if_unusable()
            if self._ready_target != target:
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            self._opened_target = target
        while True:
            with self._lock:
                if not self._queue:
                    return
                queued = self._queue[0]
            admission = self._admission.begin(queued.lease.turn_id)
            if admission.state is CodexRelayAdmissionState.QUEUED:
                return
            if admission.authority != target:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            with self._lock:
                if not self._queue or self._queue[0] is not queued:
                    raise CodexRelayError(
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                    )
                self._queue.popleft()
            self._transmit_start(queued, admission)

    def close(self) -> None:
        """Close only session-owned relay resources and unlink its socket."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            downstream = self._downstream
            upstream_thread = self._upstream_thread
            server = self._server
        if downstream is not None:
            downstream.close()
        self._upstream.close()
        if server is not None:
            server.close()
        if (
            upstream_thread is not None
            and upstream_thread is not current_thread()
        ):
            upstream_thread.join(timeout=1.0)

    def __enter__(self) -> Self:
        """Enter this already-started relay lifetime."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close session-owned resources after the participant exits."""
        del exception_type, exception, traceback
        self.close()

    def _serve_downstream(
        self,
        downstream: CodexRelayFrameConnection,
    ) -> None:
        with self._lock:
            if self._session_started or self._closed:
                downstream.close()
                return
            self._session_started = True
            self._downstream = downstream
            upstream_thread = Thread(
                target=self._pump_upstream,
                daemon=True,
                name="codex-relay-upstream",
            )
            self._upstream_thread = upstream_thread
            upstream_thread.start()
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                payload = downstream.receive(_RELAY_POLL_SECONDS)
                if payload is not None:
                    self._route_downstream(payload)
        except CodexAppServerError, CodexRelayError:
            self._record_failure()
        finally:
            downstream.close()
            self._upstream.close()

    def _pump_upstream(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed:
                        return
                    downstream = self._downstream
                if downstream is None:
                    return
                payload = self._upstream.receive(_RELAY_POLL_SECONDS)
                if payload is None:
                    continue
                routing = decode_json_rpc_routing(
                    payload,
                    from_client=False,
                )
                self._record_loaded_thread(routing)
                self._observe_upstream(routing)
                downstream.send(routing.raw)
        except CodexAppServerError, CodexRelayError:
            self._record_failure()

    def _route_downstream(self, payload: bytes) -> None:
        routing = decode_json_rpc_routing(payload, from_client=True)
        self._record_loaded_thread(routing)
        method = routing.method
        if method in _ACCOUNT_MUTATION_METHODS:
            if routing.request_id is None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._require_downstream().send(
                encode_account_mutation_refusal(routing.request_id)
            )
            return
        if method not in {TURN_START_METHOD, THREAD_REALTIME_START_METHOD}:
            self._upstream.send(routing.raw)
            return
        if routing.request_id is None or routing.thread_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        with self._lock:
            if len(self._queue) >= MAX_CODEX_RELAY_QUEUED_FRAMES:
                self._require_downstream().send(
                    encode_relay_backpressure_refusal(routing.request_id)
                )
                return
            turn_id = self._turn_id_factory()
            if turn_id in self._active or any(
                queued.lease.turn_id == turn_id for queued in self._queue
            ):
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        lease = CodexRelayLease(
            turn_id=turn_id,
            kind=(
                CodexRelayLeaseKind.TURN
                if method == TURN_START_METHOD
                else CodexRelayLeaseKind.REALTIME
            ),
            request_id=routing.request_id,
            thread_id=routing.thread_id,
        )
        queued = _QueuedStart(raw=routing.raw, lease=lease)
        admission = self._admission.begin(turn_id)
        if admission.state is CodexRelayAdmissionState.QUEUED:
            with self._lock:
                self._queue.append(queued)
            return
        self._transmit_start(queued, admission)

    def _transmit_start(
        self,
        queued: _QueuedStart,
        admission: CodexRelayAdmission,
    ) -> None:
        authority = admission.authority
        if authority is None or admission.turn_id != queued.lease.turn_id:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        self._admission.recheck(admission)
        with self._lock:
            if queued.lease.request_id in self._pending_requests:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            if (
                queued.lease.kind is CodexRelayLeaseKind.REALTIME
                and queued.lease.thread_id in self._realtime
            ):
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._pending_requests[queued.lease.request_id] = queued.lease
            self._active.add(queued.lease.turn_id)
            if queued.lease.kind is CodexRelayLeaseKind.REALTIME:
                self._realtime[queued.lease.thread_id] = queued.lease.turn_id
        self._upstream.send(queued.raw)
        with self._lock:
            adopt = (
                self._opened_target == authority
                and self._adopted_target != authority
            )
        if adopt:
            self._readiness.adopted(queued.lease.turn_id, authority)
            with self._lock:
                self._adopted_target = authority

    def _observe_upstream(self, routing: JsonRpcRouting) -> None:
        if routing.method is None:
            self._observe_response(routing)
            return
        if routing.method == TURN_STARTED_METHOD:
            self._require_turn_route(routing)
        elif routing.method == TURN_COMPLETED_METHOD:
            self._complete_turn(routing)
        elif routing.method == THREAD_REALTIME_STARTED_METHOD:
            self._require_realtime_route(routing)
        elif routing.method == THREAD_REALTIME_CLOSED_METHOD:
            self._complete_realtime(routing)

    def _observe_response(self, routing: JsonRpcRouting) -> None:
        request_id = routing.request_id
        if request_id is None:
            return
        with self._lock:
            lease = self._pending_requests.get(request_id)
        if lease is None:
            return
        if routing.error_response:
            with self._lock:
                self._pending_requests.pop(request_id, None)
                if lease.kind is CodexRelayLeaseKind.REALTIME:
                    self._realtime.pop(lease.thread_id, None)
            self._end(lease.turn_id)
            return
        if lease.kind is CodexRelayLeaseKind.REALTIME:
            with self._lock:
                self._pending_requests.pop(request_id, None)
            return
        if routing.turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        route = (lease.thread_id, routing.turn_id)
        with self._lock:
            if route in self._turns:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._pending_requests.pop(request_id, None)
            self._turns[route] = lease.turn_id

    def _require_turn_route(self, routing: JsonRpcRouting) -> TurnId:
        route = self._turn_route(routing)
        with self._lock:
            turn_id = self._turns.get(route)
        if turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return turn_id

    def _complete_turn(self, routing: JsonRpcRouting) -> None:
        route = self._turn_route(routing)
        with self._lock:
            turn_id = self._turns.pop(route, None)
        if turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        self._end(turn_id)

    def _require_realtime_route(self, routing: JsonRpcRouting) -> TurnId:
        thread_id = self._thread_id(routing)
        with self._lock:
            turn_id = self._realtime.get(thread_id)
        if turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return turn_id

    def _complete_realtime(self, routing: JsonRpcRouting) -> None:
        thread_id = self._thread_id(routing)
        with self._lock:
            turn_id = self._realtime.pop(thread_id, None)
        if turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        self._end(turn_id)

    def _end(self, turn_id: TurnId) -> None:
        with self._lock:
            if turn_id not in self._active:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        self._admission.end(turn_id)
        with self._lock:
            self._active.remove(turn_id)

    def _turn_route(self, routing: JsonRpcRouting) -> tuple[str, str]:
        if routing.thread_id is None or routing.turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return routing.thread_id, routing.turn_id

    def _thread_id(self, routing: JsonRpcRouting) -> str:
        if routing.thread_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return routing.thread_id

    def _record_loaded_thread(self, routing: JsonRpcRouting) -> None:
        thread_id = routing.thread_id
        if thread_id is None:
            return
        with self._lock:
            if thread_id in self._loaded_threads:
                return
            if len(self._loaded_threads) >= MAX_CODEX_RELAY_LOADED_THREADS:
                raise CodexRelayError(
                    SelectionCode.UNSUPPORTED_SESSION_CAPABILITY
                )
            self._loaded_threads.add(thread_id)

    def _require_downstream(self) -> CodexRelayFrameConnection:
        with self._lock:
            downstream = self._downstream
        if downstream is None:
            raise CodexRelayError(SelectionCode.PARTICIPANT_UNREACHABLE)
        return downstream

    def _raise_if_unusable(self) -> None:
        if self._closed:
            raise CodexRelayError(SelectionCode.PARTICIPANT_UNREACHABLE)
        if self._failure is not None:
            raise self._failure

    def _record_failure(self) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            downstream = self._downstream
        if downstream is not None:
            downstream.close()
