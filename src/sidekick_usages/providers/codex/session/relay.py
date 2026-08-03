"""Bounded control relay for one stock Codex TUI participant."""

import socket
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Condition, RLock, Thread, current_thread
from time import monotonic
from types import TracebackType
from typing import Protocol, Self

from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import SelectionCode, TurnId
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    JsonRpcRouting,
    decode_json_rpc_routing,
    encode_account_mutation_refusal,
    encode_relay_admission_refusal,
    encode_relay_backpressure_refusal,
)
from sidekick_usages.providers.codex.app_server.methods import (
    ACCOUNT_LOGIN_CANCEL_METHOD,
    ACCOUNT_LOGIN_START_METHOD,
    ACCOUNT_LOGOUT_METHOD,
    MCP_SERVER_STATUS_UPDATED_METHOD,
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
from sidekick_usages.providers.codex.session.config import CodexSessionReader
from sidekick_usages.providers.codex.session.errors import CodexRelayError
from sidekick_usages.providers.codex.session.mcp import (
    CodexMcpRefreshProof,
    read_codex_mcp_names,
)
from sidekick_usages.providers.codex.session.models import (
    CodexLoadedThreadSnapshot,
    CodexQueuedStart,
    CodexRelayAdmission,
    CodexRelayAdmissionState,
    CodexRelayAuthority,
    CodexRelayLease,
    CodexRelayLeaseKind,
)

MAX_CODEX_RELAY_QUEUED_FRAMES = 16
MAX_CODEX_RELAY_LOADED_THREADS = 256
_RELAY_POLL_SECONDS = 0.1
_MCP_REFRESH_PROOF_TIMEOUT_SECONDS = 6.0
_MCP_TERMINAL_STATES = frozenset({"cancelled", "failed", "ready"})
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


class CodexAdmissionRelay:
    """Own one stable TUI-to-resident admission relay."""

    def __init__(
        self,
        socket_path: Path,
        upstream: CodexRelayFrameConnection,
        admission: CodexRelayAdmissionPort,
        readiness: CodexRelayReadinessPort,
        mcp_reader: CodexSessionReader,
        turn_id_factory: Callable[[], TurnId],
    ) -> None:
        self._socket_path = socket_path
        self._upstream = upstream
        self._admission = admission
        self._readiness = readiness
        self._mcp_reader = mcp_reader
        self._turn_id_factory = turn_id_factory
        self._lock = RLock()
        self._proof_condition = Condition(self._lock)
        self._server: CodexRelayServer | None = None
        self._downstream: CodexRelayFrameConnection | None = None
        self._upstream_thread: Thread | None = None
        self._queue: deque[CodexQueuedStart] = deque()
        self._pending_requests: dict[int | str, CodexRelayLease] = {}
        self._turns: dict[tuple[str, str], TurnId] = {}
        self._realtime: dict[str, TurnId] = {}
        self._loaded_threads: set[str] = set()
        self._loaded_threads_revision = 0
        self._proof_snapshot: CodexLoadedThreadSnapshot | None = None
        self._mcp_status_revision = 0
        self._mcp_statuses: dict[tuple[str, str], tuple[str, int]] = {}
        self._mcp_proof: CodexMcpRefreshProof | None = None
        self._active: set[TurnId] = set()
        self._baseline_authority: CodexRelayAuthority | None = None
        self._ready_target: CodexRelayAuthority | None = None
        self._ready_threads_revision: int | None = None
        self._opened_target: CodexRelayAuthority | None = None
        self._adoption_target: CodexRelayAuthority | None = None
        self._adopted_target: CodexRelayAuthority | None = None
        self._admission_refusal: SelectionCode | None = None
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
        mcp_reader: CodexSessionReader,
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
            mcp_reader,
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
    def loaded_threads_snapshot(self) -> CodexLoadedThreadSnapshot:
        """Return an immutable revision-bound readiness input."""
        with self._lock:
            return self._loaded_threads_snapshot_locked()

    def arm_quiescence(
        self,
        refresh_required: bool,
    ) -> tuple[int, int, bool]:
        """Arm the relay-local barrier and prove precommit quiescence."""
        with self._proof_condition:
            self._raise_if_unusable()
            if self._proof_snapshot is not None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            snapshot = self._loaded_threads_snapshot_locked()
            self._proof_snapshot = snapshot
            if self._active:
                return snapshot.revision, len(snapshot.thread_ids), False
        try:
            names = read_codex_mcp_names(self._mcp_reader, snapshot)
        except CodexRelayError:
            return snapshot.revision, len(snapshot.thread_ids), False
        with self._lock:
            self._raise_if_unusable()
            current = self._loaded_threads_snapshot_locked()
            quiescent = (
                not self._active
                and current == snapshot
                and self._mcp_states_terminal_locked(names)
            )
            if quiescent:
                self._mcp_proof = CodexMcpRefreshProof(
                    refresh_required=refresh_required,
                    armed_revision=self._mcp_status_revision,
                    baseline_revisions={
                        key: revision
                        for key, (_status, revision) in (
                            self._mcp_statuses.items()
                        )
                        if key[0] in names and key[1] in names[key[0]]
                    },
                    names=names,
                )
            return current.revision, len(current.thread_ids), quiescent

    def confirm_quiescence(
        self,
        refresh_required: bool,
    ) -> tuple[int, int, bool]:
        """Reprove the retained precommit snapshot before barrier release."""
        with self._lock:
            self._raise_if_unusable()
            snapshot = self._proof_snapshot
            proof = self._mcp_proof
            if (
                snapshot is None
                or proof is None
                or proof.refresh_required is not refresh_required
            ):
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        if refresh_required and not self._await_mcp_refresh(
            proof,
            proof.names,
        ):
            return snapshot.revision, len(snapshot.thread_ids), False
        try:
            names = read_codex_mcp_names(self._mcp_reader, snapshot)
        except CodexRelayError:
            return snapshot.revision, len(snapshot.thread_ids), False
        if refresh_required and not self._await_mcp_refresh(proof, names):
            return snapshot.revision, len(snapshot.thread_ids), False
        with self._lock:
            self._raise_if_unusable()
            current = self._loaded_threads_snapshot_locked()
            quiescent = (
                not self._active
                and current == snapshot
                and names == proof.names
                and (
                    self._mcp_states_refreshed_locked(proof, names)
                    if refresh_required
                    else self._mcp_states_terminal_locked(names)
                )
            )
            if quiescent:
                proof.confirmed_names = names
            return current.revision, len(current.thread_ids), quiescent

    def release_quiescence(self) -> tuple[int, int, bool]:
        """Release a retained proof barrier without another provider read."""
        with self._proof_condition:
            snapshot = self._proof_snapshot
            if snapshot is None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._proof_snapshot = None
            self._mcp_proof = None
            self._proof_condition.notify_all()
            return snapshot.revision, len(snapshot.thread_ids), False

    def discard_quiescence(self) -> None:
        """Release a failed proof barrier without raising another failure."""
        with self._proof_condition:
            self._proof_snapshot = None
            self._mcp_proof = None
            self._proof_condition.notify_all()

    def seed_baseline(self, authority: CodexRelayAuthority) -> None:
        """Seed one exact finalized authority before participant traffic."""
        with self._lock:
            self._raise_if_unusable()
            if (
                self._baseline_authority is not None
                or self._session_started
                or self._queue
                or self._active
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            self._baseline_authority = authority
            self._opened_target = authority

    def mark_ready(
        self,
        target: CodexRelayAuthority,
        loaded_threads: CodexLoadedThreadSnapshot,
    ) -> None:
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
            if loaded_threads != self._loaded_threads_snapshot_locked():
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            if (
                self._ready_target == target
                and self._ready_threads_revision == loaded_threads.revision
            ):
                return
            if (
                self._ready_target is not None
                and self._opened_target != self._ready_target
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            proof = self._mcp_proof
            if proof is None or proof.confirmed_names is None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            confirmed_names = proof.confirmed_names
        current_names = read_codex_mcp_names(
            self._mcp_reader,
            loaded_threads,
        )
        with self._lock:
            self._raise_if_unusable()
            if (
                self._active
                or (loaded_threads != self._loaded_threads_snapshot_locked())
                or current_names != confirmed_names
                or not self._mcp_states_proven_locked(proof, current_names)
            ):
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._readiness.ready(target)
            self._ready_target = target
            self._ready_threads_revision = loaded_threads.revision
            self._adopted_target = None

    def open_epoch(self, epoch: SelectionEpoch) -> None:
        """Apply one exact participant OPEN notice without inventing truth."""
        with self._lock:
            self._raise_if_unusable()
            target = self._ready_target
            baseline = self._baseline_authority
            if target is not None and (
                target.epoch == epoch and target != self._opened_target
            ):
                authority = target
                commit = True
            elif (
                baseline is not None
                and self._opened_target == baseline
                and baseline.epoch == epoch
            ):
                authority = baseline
                commit = False
            elif target is not None and target.epoch == epoch:
                authority = target
                commit = False
            elif baseline is not None and baseline.epoch == epoch:
                authority = baseline
                commit = False
            elif baseline is None and target is None and not self._active:
                self._admission_refusal = None
                return
            else:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        if commit:
            self.open_admission(authority)
        else:
            self.reopen_baseline(authority)

    def refuse_admission(self, code: SelectionCode) -> None:
        """Refuse new starts while preserving the provider connection."""
        with self._lock:
            self._raise_if_unusable()
            self._admission_refusal = code

    def enter_recovery(
        self,
        code: SelectionCode,
        epoch: SelectionEpoch,
    ) -> None:
        """Retain only an exact postcommit READY proof during recovery."""
        with self._proof_condition:
            self._raise_if_unusable()
            self._admission_refusal = code
            target = self._ready_target
            proof = self._mcp_proof
            retain = (
                target is not None
                and target.epoch == epoch
                and self._ready_threads_revision
                == self._loaded_threads_revision
                and proof is not None
                and proof.confirmed_names is not None
                and self._mcp_states_proven_locked(
                    proof,
                    proof.confirmed_names,
                )
            )
            if not retain:
                self._proof_snapshot = None
                self._mcp_proof = None
                self._proof_condition.notify_all()

    def prepare_admission(self) -> None:
        """Route new starts to the coordinator's queued admission gate."""
        with self._lock:
            self._raise_if_unusable()
            self._admission_refusal = None

    def open_admission(self, target: CodexRelayAuthority) -> None:
        """Release queued raw frames once under one finalized target."""
        with self._lock:
            self._raise_if_unusable()
            proof = self._mcp_proof
            if (
                self._ready_target != target
                or self._ready_threads_revision
                != self._loaded_threads_revision
                or proof is None
                or proof.confirmed_names is None
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            snapshot = self._loaded_threads_snapshot_locked()
            confirmed_names = proof.confirmed_names
        try:
            current_names = read_codex_mcp_names(
                self._mcp_reader,
                snapshot,
            )
        except CodexRelayError:
            raise CodexRelayError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ) from None
        with self._lock:
            self._raise_if_unusable()
            if (
                self._ready_target != target
                or self._ready_threads_revision
                != self._loaded_threads_revision
                or self._mcp_proof is not proof
                or snapshot != self._loaded_threads_snapshot_locked()
                or current_names != confirmed_names
                or not self._mcp_states_proven_locked(proof, current_names)
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            changed = self._opened_target != target
            self._opened_target = target
            self._baseline_authority = target
            self._adoption_target = target
            if changed:
                self._adopted_target = None
            self._drain_queue_locked(target)
            self._admission_refusal = None

    def reopen_baseline(self, authority: CodexRelayAuthority) -> None:
        """Reopen one exact unchanged baseline without publishing ready."""
        with self._lock:
            self._raise_if_unusable()
            if self._baseline_authority != authority or (
                self._ready_target is not None
                and self._ready_target != self._opened_target
            ):
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
            self._opened_target = authority
            self._adoption_target = None
            self._drain_queue_locked(authority)
            self._admission_refusal = None

    def close(self) -> None:
        """Close only session-owned relay resources and unlink its socket."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._proof_snapshot = None
            self._mcp_proof = None
            self._proof_condition.notify_all()
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
        except (CodexAppServerError, CodexRelayError) as error:
            self._record_failure(error)
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
                with self._lock:
                    self._record_loaded_thread_locked(routing.thread_id)
                    self._observe_upstream(routing)
                    suppress = (
                        routing.method == MCP_SERVER_STATUS_UPDATED_METHOD
                        and routing.mcp_status == "cancelled"
                        and self._mcp_proof is not None
                        and self._mcp_proof.refresh_required
                    )
                if not suppress:
                    downstream.send(routing.raw)
        except (CodexAppServerError, CodexRelayError) as error:
            self._record_failure(error)

    def _route_downstream(self, payload: bytes) -> None:
        routing = decode_json_rpc_routing(payload, from_client=True)
        method = routing.method
        if method in _ACCOUNT_MUTATION_METHODS:
            if routing.request_id is None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._require_downstream().send(
                encode_account_mutation_refusal(routing.request_id)
            )
            return
        if method not in {TURN_START_METHOD, THREAD_REALTIME_START_METHOD}:
            with self._proof_condition:
                while self._proof_snapshot is not None:
                    self._proof_condition.wait()
                self._raise_if_unusable()
                self._ensure_loaded_thread_capacity_locked(routing.thread_id)
                self._upstream.send(routing.raw)
                self._record_loaded_thread_locked(routing.thread_id)
            return
        self._route_start(routing)

    def _route_start(self, routing: JsonRpcRouting) -> None:
        if routing.request_id is None or routing.thread_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        with self._lock:
            self._raise_if_unusable()
            self._ensure_loaded_thread_capacity_locked(routing.thread_id)
            if self._admission_refusal is not None:
                self._require_downstream().send(
                    encode_relay_admission_refusal(
                        routing.request_id,
                        self._admission_refusal,
                    )
                )
                return
            if len(self._queue) >= MAX_CODEX_RELAY_QUEUED_FRAMES:
                self._require_downstream().send(
                    encode_relay_backpressure_refusal(routing.request_id)
                )
                return
            turn_id = self._turn_id_factory()
            self._require_new_turn_id_locked(turn_id)
            lease = CodexRelayLease(
                turn_id=turn_id,
                kind=(
                    CodexRelayLeaseKind.TURN
                    if routing.method == TURN_START_METHOD
                    else CodexRelayLeaseKind.REALTIME
                ),
                request_id=routing.request_id,
                thread_id=routing.thread_id,
            )
            queued = CodexQueuedStart(raw=routing.raw, lease=lease)
            admission = self._begin_locked(turn_id, routing.request_id)
            if admission is None:
                return
            if self._queue:
                if admission.state is not CodexRelayAdmissionState.QUEUED:
                    raise CodexRelayError(
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                    )
                self._queue.append(queued)
                return
            if admission.state is CodexRelayAdmissionState.QUEUED:
                self._queue.append(queued)
                return
            target = self._opened_target
            if target is None:
                target = admission.authority
                if target is None:
                    raise CodexRelayError(
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                    )
                self._baseline_authority = target
                self._opened_target = target
            self._transmit_start_locked(queued, admission, target)

    def _drain_queue_locked(self, target: CodexRelayAuthority) -> None:
        while self._queue:
            queued = self._queue[0]
            admission = self._begin_locked(
                queued.lease.turn_id,
                queued.lease.request_id,
            )
            if admission is None:
                self._queue.popleft()
                return
            if admission.state is CodexRelayAdmissionState.QUEUED:
                return
            if admission.authority != target:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._queue.popleft()
            self._transmit_start_locked(queued, admission, target)

    def _begin_locked(
        self,
        turn_id: TurnId,
        request_id: int | str,
    ) -> CodexRelayAdmission | None:
        try:
            return self._admission.begin(turn_id)
        except Exception as error:
            self._refuse_control_locked(error, request_id)
            return None

    def _require_new_turn_id_locked(self, turn_id: TurnId) -> None:
        if turn_id in self._active or any(
            queued.lease.turn_id == turn_id for queued in self._queue
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)

    def _transmit_start_locked(
        self,
        queued: CodexQueuedStart,
        admission: CodexRelayAdmission,
        expected: CodexRelayAuthority,
    ) -> None:
        authority = admission.authority
        if (
            authority is None
            or authority != expected
            or admission.turn_id != queued.lease.turn_id
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        try:
            self._admission.recheck(admission)
        except Exception as error:
            self._refuse_control_locked(error, queued.lease.request_id)
            return
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
        self._record_loaded_thread_locked(queued.lease.thread_id)
        if (
            self._adoption_target == authority
            and self._adopted_target != authority
        ):
            try:
                self._readiness.adopted(queued.lease.turn_id, authority)
            except Exception as error:
                self._refuse_control_locked(error)
            else:
                self._adopted_target = authority

    def _observe_upstream(self, routing: JsonRpcRouting) -> None:
        if routing.method == MCP_SERVER_STATUS_UPDATED_METHOD:
            self._observe_mcp_status_locked(routing)
            return
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
            try:
                self._admission.end(turn_id)
            except Exception as error:
                self._refuse_control_locked(error)
            finally:
                self._active.remove(turn_id)

    def _refuse_control_locked(
        self,
        error: Exception,
        request_id: int | str | None = None,
    ) -> None:
        self._admission_refusal = (
            error.code
            if isinstance(error, CodexRelayError)
            else SelectionCode.SELECTION_RECOVERY_REQUIRED
        )
        if request_id is not None:
            self._require_downstream().send(
                encode_relay_admission_refusal(
                    request_id,
                    self._admission_refusal,
                )
            )

    def _turn_route(self, routing: JsonRpcRouting) -> tuple[str, str]:
        if routing.thread_id is None or routing.turn_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return routing.thread_id, routing.turn_id

    def _thread_id(self, routing: JsonRpcRouting) -> str:
        if routing.thread_id is None:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return routing.thread_id

    def _loaded_threads_snapshot_locked(self) -> CodexLoadedThreadSnapshot:
        return CodexLoadedThreadSnapshot(
            revision=self._loaded_threads_revision,
            thread_ids=tuple(sorted(self._loaded_threads)),
        )

    def _observe_mcp_status_locked(self, routing: JsonRpcRouting) -> None:
        thread_id = routing.thread_id
        name = routing.mcp_name
        status = routing.mcp_status
        if thread_id is None or name is None or status is None:
            return
        self._mcp_status_revision += 1
        self._mcp_statuses[(thread_id, name)] = (
            status,
            self._mcp_status_revision,
        )
        self._proof_condition.notify_all()

    def _mcp_states_terminal_locked(
        self,
        names: dict[str, frozenset[str]],
    ) -> bool:
        return all(
            self._mcp_statuses.get((thread_id, name), (None, 0))[0]
            in _MCP_TERMINAL_STATES
            for thread_id, thread_names in names.items()
            for name in thread_names
        )

    def _mcp_states_refreshed_locked(
        self,
        proof: CodexMcpRefreshProof,
        names: dict[str, frozenset[str]],
    ) -> bool:
        for thread_id, thread_names in names.items():
            for name in thread_names:
                status, revision = self._mcp_statuses.get(
                    (thread_id, name),
                    (None, 0),
                )
                baseline = proof.baseline_revisions.get(
                    (thread_id, name),
                    proof.armed_revision,
                )
                if status != "ready" or revision <= baseline:
                    return False
        return True

    def _mcp_states_proven_locked(
        self,
        proof: CodexMcpRefreshProof,
        names: dict[str, frozenset[str]],
    ) -> bool:
        if proof.refresh_required:
            return self._mcp_states_refreshed_locked(proof, names)
        return names == proof.names and self._mcp_states_terminal_locked(names)

    def _await_mcp_refresh(
        self,
        proof: CodexMcpRefreshProof,
        names: dict[str, frozenset[str]],
    ) -> bool:
        deadline = monotonic() + _MCP_REFRESH_PROOF_TIMEOUT_SECONDS
        with self._proof_condition:
            while not self._mcp_states_refreshed_locked(proof, names):
                self._raise_if_unusable()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._proof_condition.wait(remaining)
            return True

    def _ensure_loaded_thread_capacity_locked(
        self,
        thread_id: str | None,
    ) -> None:
        if (
            thread_id is not None
            and thread_id not in self._loaded_threads
            and len(self._loaded_threads) >= MAX_CODEX_RELAY_LOADED_THREADS
        ):
            raise CodexRelayError(SelectionCode.UNSUPPORTED_SESSION_CAPABILITY)

    def _record_loaded_thread_locked(self, thread_id: str | None) -> None:
        if thread_id is None:
            return
        if thread_id in self._loaded_threads:
            return
        self._ensure_loaded_thread_capacity_locked(thread_id)
        self._loaded_threads.add(thread_id)
        self._loaded_threads_revision += 1
        if self._ready_target != self._opened_target:
            self._ready_target = None
            self._ready_threads_revision = None

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

    def _record_failure(
        self,
        error: CodexAppServerError | CodexRelayError,
    ) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = (
                    error
                    if isinstance(error, CodexRelayError)
                    else CodexRelayError(
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                    )
                )
            self._proof_snapshot = None
            self._mcp_proof = None
            self._proof_condition.notify_all()
            downstream = self._downstream
        if downstream is not None:
            downstream.close()
