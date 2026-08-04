"""Retained engine and participant runtime for coordinated Claude."""

import socket
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import suppress
from pathlib import Path
from queue import Queue
from threading import Condition, Event, RLock, Thread
from typing import NoReturn
from uuid import uuid4

from sidekick_usages.cli.session.claude.coordination import (
    ClaudeControl,
    ClaudeCoordination,
    ClaudeCoordinationFactory,
    ClaudeSupervisorCoordinationFactory,
    claude_participant_manifest,
    require_first_claude_notice,
)
from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.protocol import ConnectionClosedError
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    TurnAdmission,
    TurnAdmissionState,
)
from sidekick_usages.providers.claude.structured.codec import (
    ClaudeProtectedChannelClosedError,
    clear_secret_buffer,
    decode_control_success,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedHostChannel,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredActivityKind,
    ClaudeStructuredActivityState,
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredBinding,
    ClaudeStructuredControlRequest,
    ClaudeStructuredConversationId,
    ClaudeStructuredDialogRequest,
    ClaudeStructuredElicitationRequest,
    ClaudeStructuredEngine,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredHookCallbackRequest,
    ClaudeStructuredInstallReceipt,
    ClaudeStructuredMcpMessageRequest,
    ClaudeStructuredPermissionDecision,
    ClaudeStructuredPermissionRequest,
    ClaudeStructuredProtectedFrame,
    ClaudeStructuredQuestionAnswer,
    ClaudeStructuredQuestionRequest,
    ClaudeStructuredTerminalEvent,
)
from sidekick_usages.providers.claude.structured.session import (
    ClaudeStructuredSession,
)
from sidekick_usages.providers.claude.structured.stream import (
    ClaudeStructuredStreamDecoder,
    encode_claude_dialog_unsupported,
    encode_claude_elicitation_decline,
    encode_claude_initialize,
    encode_claude_interrupt,
    encode_claude_permission_response,
    encode_claude_question_response,
    encode_claude_unsupported_control,
    encode_claude_user_prompt,
)

_CONNECTION_GENERATION = 1
_ENGINE_EVENT_TIMEOUT_SECONDS = 60.0
_ENGINE_POLL_SECONDS = 0.1
_ENGINE_EXIT_TIMEOUT_SECONDS = 30.0
_THREAD_JOIN_SECONDS = 2.0
_COMPLETED_CONTROL_LIMIT = 256
_REATTACH_DELAYS_SECONDS = (0.0, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
_ATTACHMENT_RETRY_ERRORS = (
    BrokenPipeError, ClaudeProtectedChannelClosedError,
    ConnectionAbortedError, ConnectionClosedError,
    ConnectionRefusedError, ConnectionResetError,
    FileNotFoundError, TimeoutError,
)


def _new_turn_id() -> TurnId:
    return TurnId(str(uuid4()))


def _new_request_id() -> RequestId:
    return RequestId(str(uuid4()))


def _retain_turn(
    receipt: ClaudeStructuredAdoptionReceipt,
) -> None:
    del receipt


class ClaudeSessionGateError(RuntimeError):
    """One recoverable supervisor gate status for a live engine."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class ClaudeTerminalEventsClosedError(RuntimeError):
    """Stop only the terminal-facing event consumer at ordinary exit."""


class ClaudeProviderTerminatedError(RuntimeError):
    """Report the official engine's natural termination to its host."""


class ClaudeSessionRuntime:
    """Own one unchanged engine and protected participant lifetime."""

    def __init__(
        self,
        engine: ClaudeStructuredEngine,
        control: ClaudeControl,
        host_endpoint: socket.socket,
        registration_endpoint: socket.socket,
        *,
        participant_id: ParticipantId,
        coordination_factory: ClaudeCoordinationFactory | None = None,
        turn_id_factory: Callable[[], TurnId] = _new_turn_id,
        request_id_factory: Callable[[], RequestId] | None = None,
    ) -> None:
        self._require_endpoint(host_endpoint)
        self._require_endpoint(registration_endpoint)
        self._engine = engine
        self._control = control
        self._host_endpoint: socket.socket | None = host_endpoint
        self._registration_endpoint: socket.socket | None = (
            registration_endpoint
        )
        self._participant_id = participant_id
        self._coordination_factory = coordination_factory
        self._connection_generation = _CONNECTION_GENERATION
        self._turn_id_factory = turn_id_factory
        self._request_id_factory = request_id_factory
        self._condition = Condition(RLock())
        self._engine_lock = RLock()
        self._session_lock = RLock()
        self._closing = Event()
        self._coordination_changed = Event()
        self._session: ClaudeStructuredSession | None = None
        self._channel: ClaudeProtectedHostChannel | None = None
        self._notice_thread: Thread | None = None
        self._protected_thread: Thread | None = None
        self._event_thread: Thread | None = None
        self._reattach_thread: Thread | None = None
        self._events: Queue[ClaudeStructuredTerminalEvent | BaseException] = (
            Queue()
        )
        self._stream_decoder = ClaudeStructuredStreamDecoder()
        self._pending_controls: dict[
            str,
            ClaudeStructuredActivityKind,
        ] = {}
        self._completed_control_ids: set[str] = set()
        self._completed_control_order: deque[str] = deque()
        self._open_revision = 0
        self._gate_code: SelectionCode | None = None
        self._failure: BaseException | None = None
        self._control_available = False
        self._reattaching = False
        self._opened = False
        self._enrolled = False
        self._finished = False
        self._provider_terminated = False
        self._terminal_recovering = False

    @classmethod
    def create(
        cls,
        engine: ClaudeStructuredEngine,
        supervisor_socket: Path,
    ) -> ClaudeSessionRuntime:
        """Compose one AF_UNIX participant against the supervisor."""
        participant_id = ParticipantId(str(uuid4()))
        factory = ClaudeSupervisorCoordinationFactory(supervisor_socket)
        coordination = factory(participant_id, _CONNECTION_GENERATION)
        return cls(
            engine,
            coordination.control,
            coordination.host_endpoint,
            coordination.registration_endpoint,
            participant_id=participant_id,
            coordination_factory=factory,
        )

    @property
    def process_id(self) -> int:
        """Return the unchanged official engine PID."""
        return self._engine.process_id

    @property
    def conversation_id(self) -> ClaudeStructuredConversationId | None:
        """Return the stable provider conversation after initialization."""
        with self._session_lock:
            session = self._session
            return None if session is None else session.conversation_id

    def open(self) -> None:
        """Register, install the initial binding, and subscribe."""
        if self._opened:
            raise RuntimeError("Claude session runtime is already open.")
        self._opened = True
        registration_endpoint = self._registration_endpoint
        host_endpoint = self._host_endpoint
        if registration_endpoint is None or host_endpoint is None:
            self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
        self._registration_endpoint = None
        self._host_endpoint = None
        coordination = ClaudeCoordination(
            self._control,
            host_endpoint,
            registration_endpoint,
        )
        try:
            channel, initial = self._initial_attachment(coordination)
            self._channel = channel
            with self._session_lock:
                session, receipt = self._bootstrap(initial)
                self._session = session
            self._initialize_engine()
            try:
                channel.acknowledge(receipt)
                notices = self._control.notices()
                first = require_first_claude_notice(notices)
                self._apply_notice(first)
            except _ATTACHMENT_RETRY_ERRORS:
                with self._condition:
                    self._gate_code = SelectionCode.SELECTION_RECOVERY_REQUIRED
                    self._reattaching = True
                self._reattach()
                self._raise_failure()
            else:
                self._start_threads(notices, self._connection_generation)
            events = Thread(
                target=self._consume_events,
                daemon=True, name="claude-structured-events",
            )
            events.start()
            self._event_thread = events
            self._enrolled = True
        except BaseException:
            if self._channel is not None:
                self._channel.close()
            self._channel = None
            raise

    def _initial_attachment(
        self,
        coordination: ClaudeCoordination,
    ) -> tuple[ClaudeProtectedHostChannel, ClaudeStructuredProtectedFrame]:
        """Retain the engine while awaiting its first protected binding."""
        factory = self._coordination_factory
        attempt = 0
        current: ClaudeCoordination | None = coordination
        while True:
            channel: ClaudeProtectedHostChannel | None = None
            try:
                if current is None:
                    if factory is None:
                        self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
                    current = factory(
                        self._participant_id,
                        self._connection_generation,
                    )
                channel, _registration = current.register(
                    claude_participant_manifest(
                        self._participant_id,
                        self._connection_generation,
                    ),
                    None,
                )
                initial = channel.receive()
            except _ATTACHMENT_RETRY_ERRORS:
                self._close_coordination(current, channel)
                current = None
                if factory is None:
                    raise
                delay = _REATTACH_DELAYS_SECONDS[
                    min(attempt, len(_REATTACH_DELAYS_SECONDS) - 1)
                ]
                if self._closing.wait(delay):
                    self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
                with self._condition:
                    self._connection_generation += 1
                attempt += 1
                continue
            except BaseException:
                self._close_coordination(current, channel)
                raise
            self._control = current.control
            return channel, initial

    def dispose_unenrolled_engine(self) -> None:
        """Dispose the child only before participant enrollment finishes."""
        if self._enrolled:
            raise RuntimeError("An enrolled Claude engine cannot be disposed.")
        self._engine.dispose_unenrolled()

    def _bootstrap(
        self,
        frame: ClaudeStructuredProtectedFrame,
    ) -> tuple[ClaudeStructuredSession, ClaudeStructuredInstallReceipt]:
        request_id_factory = self._request_id_factory
        if request_id_factory is None:
            return ClaudeStructuredSession.bootstrap(self._engine, frame)
        return ClaudeStructuredSession.bootstrap(
            self._engine,
            frame,
            request_id_factory=request_id_factory,
        )

    def _initialize_engine(self) -> None:
        factory = self._request_id_factory
        request_id = _new_request_id() if factory is None else factory()
        frame = encode_claude_initialize(request_id)
        try:
            with self._engine_lock:
                response = self._engine.exchange(
                    frame,
                    request_id,
                    _ENGINE_EVENT_TIMEOUT_SECONDS,
                )
        finally:
            clear_secret_buffer(frame)
        decode_control_success(response, request_id)

    def start_turn(self, prompt: str) -> TurnId:
        """Queue or admit one prompt and transmit it exactly once."""
        turn_id = self._turn_id_factory()
        admission = self._await_admission(turn_id)
        binding = self._require_admitted_binding(admission)
        frame = encode_claude_user_prompt(prompt)
        routed = False

        try:
            with self._session_lock:
                self._require_session().route_turn(
                    turn_id,
                    binding,
                    _retain_turn,
                )
                routed = True
            proof = ParticipantAdoptionProof(
                turn_id=turn_id,
                account_id=binding.account_id,
                generation=binding.generation,
                epoch=binding.epoch,
            )
            if self._control_is_available():
                try:
                    self._control.adopted(proof)
                except _ATTACHMENT_RETRY_ERRORS:
                    self._control_lost(self._connection_generation, True)
                    self._raise_gate()
            with self._engine_lock:
                self._engine.send_interactive(
                    frame,
                    _ENGINE_EVENT_TIMEOUT_SECONDS,
                )
        except BaseException as error:
            if not routed:
                raise
            failures: list[BaseException] = [error]
            try:
                with self._session_lock:
                    self._require_session().end_turn(turn_id)
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
            try:
                self._control.end(turn_id)
            except BaseException as cleanup_error:
                failures.append(cleanup_error)
            if len(failures) > 1:
                raise BaseExceptionGroup(
                    "Claude turn transmission cleanup failed.",
                    failures,
                ) from None
            raise
        finally:
            clear_secret_buffer(frame)
        return turn_id

    def receive_event(self) -> ClaudeStructuredTerminalEvent:
        """Return the next continuously observed provider event."""
        while True:
            event = self._events.get()
            with self._condition:
                recovering = self._terminal_recovering
                if not isinstance(event, ClaudeTerminalEventsClosedError):
                    self._terminal_recovering = False
            if (
                isinstance(event, ClaudeTerminalEventsClosedError)
                and recovering
            ):
                continue
            if isinstance(event, BaseException):
                raise event
            return event

    def stop_terminal_events(self) -> None:
        """Release the terminal consumer without stopping provider reads."""
        self._events.put(ClaudeTerminalEventsClosedError())

    def report_terminal_failure(self) -> None:
        """Keep the engine live while a failed terminal owner is recreated."""
        with self._condition:
            self._terminal_recovering = True
        self._events.put(
            ClaudeStructuredTerminalEvent(
                conversation_id=None,
                text=(),
                status=(
                    "Sidekick recovered the terminal; Claude remained active."
                ),
            )
        )

    def respond_permission(
        self,
        request: ClaudeStructuredPermissionRequest,
        decision: ClaudeStructuredPermissionDecision,
    ) -> None:
        """Send one correlated decision and close its activity afterward."""
        self._respond_control(
            request.request_id,
            encode_claude_permission_response(request, decision),
        )

    def respond_question(
        self,
        request: ClaudeStructuredQuestionRequest,
        answers: tuple[ClaudeStructuredQuestionAnswer, ...],
    ) -> None:
        """Return one validated ``AskUserQuestion`` answer set."""
        self._respond_control(
            request.permission.request_id,
            encode_claude_question_response(request, answers),
        )

    def decline_elicitation(
        self,
        request: ClaudeStructuredElicitationRequest,
    ) -> None:
        """Decline one provider elicitation without generic JSON input."""
        self._respond_control(
            request.request_id,
            encode_claude_elicitation_decline(request),
        )

    def refuse_dialog(self, request: ClaudeStructuredDialogRequest) -> None:
        """Return a typed error for an undeclared private dialog kind."""
        self._respond_control(
            request.request_id,
            encode_claude_dialog_unsupported(request),
        )

    def refuse_unsupported_control(
        self,
        request: (
            ClaudeStructuredHookCallbackRequest
            | ClaudeStructuredMcpMessageRequest
        ),
    ) -> None:
        """Return a correlated error for one undeclared host capability."""
        self._respond_control(
            request.request_id,
            encode_claude_unsupported_control(request),
        )

    def _respond_control(self, request_id: str, frame: bytearray) -> None:
        try:
            with self._engine_lock, self._session_lock:
                kind = self._pending_controls.get(request_id)
                if kind is None:
                    if request_id in self._completed_control_ids:
                        return
                    self._fail(ClaudeStructuredFailure.ACTIVITY_INVALID)
                self._engine.send_interactive(
                    frame,
                    _ENGINE_EVENT_TIMEOUT_SECONDS,
                )
                del self._pending_controls[request_id]
                self._require_session().observe_activity(
                    kind,
                    request_id,
                    ClaudeStructuredActivityState.FINISHED,
                )
                self._remember_completed_control(request_id)
        finally:
            clear_secret_buffer(frame)

    def interrupt(self) -> None:
        """Interrupt the active response inside the retained engine."""
        factory = self._request_id_factory
        request_id = _new_request_id() if factory is None else factory()
        frame = encode_claude_interrupt(request_id)
        try:
            with self._engine_lock:
                response = self._engine.exchange(
                    frame,
                    request_id,
                    _ENGINE_EVENT_TIMEOUT_SECONDS,
                )
        finally:
            clear_secret_buffer(frame)
        decode_control_success(response, request_id)

    def end_turn(self, turn_id: TurnId) -> None:
        """Close one naturally terminal turn in both local owners."""
        with self._session_lock:
            self._require_session().end_turn(turn_id)
        if self._control_is_available():
            try:
                self._control.end(turn_id)
            except _ATTACHMENT_RETRY_ERRORS:
                self._control_lost(self._connection_generation, True)

    def finish_engine(self) -> int:
        """Close engine input and await only its ordinary exit."""
        if self._finished:
            raise RuntimeError("Claude engine is already finished.")
        self._finished = True
        with self._engine_lock:
            if not self._provider_terminated:
                self._engine.close_input()
            return self._engine.wait(_ENGINE_EXIT_TIMEOUT_SECONDS)

    def close(self) -> None:
        """Close only host and supervisor resources owned by this runtime."""
        self._closing.set()
        self._coordination_changed.set()
        channel = self._channel
        if channel is not None:
            channel.close()
        registration_endpoint = self._registration_endpoint
        if registration_endpoint is not None:
            registration_endpoint.close()
        host_endpoint = self._host_endpoint
        if host_endpoint is not None:
            host_endpoint.close()
        self._control.close()
        failures: list[BaseException] = []
        for thread in (
            self._protected_thread,
            self._notice_thread,
            self._event_thread,
            self._reattach_thread,
        ):
            if thread is None:
                continue
            thread.join(timeout=_THREAD_JOIN_SECONDS)
            if thread.is_alive():
                failures.append(
                    RuntimeError("Claude participant thread did not close.")
                )
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup(
                "Claude participant resources did not all close.",
                failures,
            )

    def _start_threads(
        self,
        notices: Iterator[ParticipantNotice],
        connection_generation: int,
    ) -> None:
        channel = self._channel
        if channel is None:
            self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
        protected = Thread(
            target=self._consume_protected,
            args=(channel, connection_generation),
            daemon=True,
            name="claude-protected-installs",
        )
        notice = Thread(
            target=self._consume_notices,
            args=(notices, connection_generation),
            daemon=True,
            name="claude-participant-notices",
        )
        protected.start()
        notice.start()
        self._protected_thread = protected
        self._notice_thread = notice

    def _consume_protected(
        self,
        channel: ClaudeProtectedHostChannel,
        connection_generation: int,
    ) -> None:
        try:
            while not self._closing.is_set():
                frame = channel.receive()
                try:
                    with self._engine_lock, self._session_lock:
                        session = self._require_session()
                        session.prepare_target(frame.protected_binding)
                        receipt = session.update_oauth(frame)
                except BaseException:
                    frame.close_protected_frame()
                    with self._session_lock:
                        self._require_session().discard_uninstalled_target()
                    channel.close()
                    self._control_lost(connection_generation, False)
                    return
                channel.acknowledge(receipt)
        except _ATTACHMENT_RETRY_ERRORS:
            if not self._closing.is_set():
                self._control_lost(connection_generation, True)
        except BaseException as error:
            if not self._closing.is_set():
                self._publish_failure(error)

    def _consume_events(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    with self._engine_lock:
                        payload = self._engine.receive_event(
                            _ENGINE_POLL_SECONDS
                        )
                        event = self._stream_decoder.decode(payload)
                        self._observe_terminal_event(event)
                except ClaudeStructuredError as error:
                    if error.code is ClaudeStructuredFailure.PROTOCOL_TIMEOUT:
                        continue
                    if self._finished and error.code in {
                        ClaudeStructuredFailure.PROCESS_EXITED,
                        ClaudeStructuredFailure.PROTOCOL_EOF,
                    }:
                        return
                    if error.code in {
                        ClaudeStructuredFailure.PROCESS_EXITED,
                        ClaudeStructuredFailure.PROTOCOL_EOF,
                    }:
                        self._provider_terminated = True
                        self._events.put(ClaudeProviderTerminatedError())
                        return
                    raise
                self._events.put(event)
        except BaseException as error:
            if not self._closing.is_set() and not self._finished:
                self._publish_failure(error)

    def _observe_terminal_event(
        self,
        event: ClaudeStructuredTerminalEvent,
    ) -> None:
        with self._session_lock:
            session = self._require_session()
            session.observe_terminal_event(event)
            control = event.control
            if control is not None:
                request_id, kind = self._control_activity(control)
                if request_id in self._pending_controls:
                    self._fail(ClaudeStructuredFailure.ACTIVITY_INVALID)
                self._pending_controls[request_id] = kind
                session.observe_activity(
                    kind,
                    request_id,
                    ClaudeStructuredActivityState.STARTED,
                )
            cancelled = event.cancelled_request_id
            if cancelled is not None:
                kind = self._pending_controls.pop(cancelled, None)
                if kind is None:
                    self._remember_completed_control(cancelled)
                    return
                session.observe_activity(
                    kind,
                    cancelled,
                    ClaudeStructuredActivityState.FINISHED,
                )
                self._remember_completed_control(cancelled)

    def _remember_completed_control(self, request_id: str) -> None:
        if request_id in self._completed_control_ids:
            return
        self._completed_control_ids.add(request_id)
        self._completed_control_order.append(request_id)
        if len(self._completed_control_order) > _COMPLETED_CONTROL_LIMIT:
            expired = self._completed_control_order.popleft()
            self._completed_control_ids.remove(expired)

    @staticmethod
    def _control_activity(
        control: ClaudeStructuredControlRequest,
    ) -> tuple[str, ClaudeStructuredActivityKind]:
        if isinstance(control, ClaudeStructuredQuestionRequest):
            return (
                control.permission.request_id,
                ClaudeStructuredActivityKind.PERMISSION,
            )
        if isinstance(control, ClaudeStructuredPermissionRequest):
            return control.request_id, ClaudeStructuredActivityKind.PERMISSION
        if isinstance(control, ClaudeStructuredDialogRequest):
            return control.request_id, ClaudeStructuredActivityKind.DIALOG
        if isinstance(control, ClaudeStructuredHookCallbackRequest):
            return control.request_id, ClaudeStructuredActivityKind.HOOK
        return control.request_id, ClaudeStructuredActivityKind.MCP

    def _consume_notices(
        self,
        notices: Iterator[ParticipantNotice],
        connection_generation: int,
    ) -> None:
        try:
            for notice in notices:
                self._apply_notice(notice)
            if not self._closing.is_set():
                raise ConnectionClosedError("Claude notices closed.")
        except _ATTACHMENT_RETRY_ERRORS:
            if not self._closing.is_set():
                self._control_lost(connection_generation, True)
        except BaseException as error:
            if not self._closing.is_set():
                self._publish_failure(error)

    def _apply_notice(self, notice: ParticipantNotice) -> None:
        if (
            notice.participant_id != self._participant_id
            or notice.provider_id is not ProviderId.CLAUDE
        ):
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        if notice.kind is ParticipantNoticeKind.PREPARE:
            with self._condition:
                if self._control_available:
                    self._gate_code = None
                self._condition.notify_all()
            return
        if notice.kind is ParticipantNoticeKind.OPEN:
            with self._condition:
                self._control_available = True
                self._reattaching = False
                self._gate_code = None
                self._open_revision += 1
                self._condition.notify_all()
            self._coordination_changed.set()
            return
        if notice.kind is ParticipantNoticeKind.STATUS:
            with self._condition:
                self._gate_code = (
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                    if notice.code is None
                    else notice.code
                )
                self._condition.notify_all()
            return
        if notice.kind is not ParticipantNoticeKind.READY:
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        with self._session_lock:
            binding = self._require_session().binding
        if (
            notice.operation_id != binding.operation_id
            or notice.target_account_id != binding.account_id
            or notice.target_generation != binding.generation
            or notice.epoch != binding.epoch
        ):
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        self._control.ready(
            ParticipantReadyProof(
                account_id=binding.account_id,
                generation=binding.generation,
                epoch=binding.epoch,
            )
        )

    def _await_admission(self, turn_id: TurnId) -> TurnAdmission:
        while True:
            with self._condition:
                self._condition.wait_for(
                    lambda: not self._reattaching or self._closing.is_set()
                )
                self._raise_failure()
                self._raise_gate()
                revision = self._open_revision
            try:
                admission = self._control.begin(turn_id)
            except _ATTACHMENT_RETRY_ERRORS:
                self._control_lost(self._connection_generation, True)
                continue
            if admission.state is TurnAdmissionState.ADMITTED:
                return admission
            with self._condition:
                self._condition.wait_for(
                    lambda expected=revision: (
                        self._open_revision != expected
                        or self._failure is not None
                        or self._gate_code is not None
                    )
                )
                self._raise_failure()
                self._raise_gate()

    def _require_admitted_binding(
        self,
        admission: TurnAdmission,
    ) -> ClaudeStructuredBinding:
        if (
            admission.state is not TurnAdmissionState.ADMITTED
            or admission.epoch is None
            or admission.account_id is None
            or admission.generation is None
        ):
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        with self._session_lock:
            binding = self._require_session().binding
        if (
            admission.epoch != binding.epoch
            or admission.account_id != binding.account_id
            or admission.generation != binding.generation
        ):
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        return binding

    def _require_session(self) -> ClaudeStructuredSession:
        session = self._session
        if session is None:
            self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
        return session

    def _publish_failure(self, error: BaseException) -> None:
        with self._condition:
            publish = self._failure is None
            if publish:
                self._failure = error
            self._condition.notify_all()
        self._coordination_changed.set()
        if publish:
            self._events.put(error)

    def _control_lost(self, connection_generation: int, retry: bool) -> None:
        report_unavailable = False
        with self._condition:
            if (
                connection_generation != self._connection_generation
                or self._closing.is_set()
            ):
                return
            if not self._control_available and self._reattaching:
                self._coordination_changed.set()
                return
            self._control_available = False
            self._gate_code = SelectionCode.SELECTION_RECOVERY_REQUIRED
            factory = self._coordination_factory
            if factory is not None and retry and not self._reattaching:
                self._reattaching = True
                thread = Thread(
                    target=self._reattach,
                    daemon=True,
                    name="claude-participant-reattach",
                )
                thread.start()
                self._reattach_thread = thread
            elif factory is None or not retry:
                report_unavailable = True
            self._condition.notify_all()
        if report_unavailable:
            self._events.put(
                ClaudeStructuredTerminalEvent(
                    conversation_id=None,
                    text=(),
                    status="Sidekick: selection_recovery_required",
                )
            )

    def _reattach(self) -> None:
        factory = self._coordination_factory
        if factory is None:
            self._fail(ClaudeStructuredFailure.PROCESS_UNAVAILABLE)
        old_channel = self._channel
        if old_channel is not None:
            old_channel.close()
        with suppress(BaseException):
            self._control.close()
        attempt = 0
        reported = False
        while True:
            delay = _REATTACH_DELAYS_SECONDS[
                min(attempt, len(_REATTACH_DELAYS_SECONDS) - 1)
            ]
            if self._closing.wait(delay):
                return
            try:
                if self._reattach_once(factory):
                    return
            except BaseException as error:
                self._publish_failure(error)
                return
            if not reported and self._enrolled:
                reported = True
                self._events.put(
                    ClaudeStructuredTerminalEvent(
                        conversation_id=None,
                        text=(),
                        status="Sidekick: selection_recovery_required",
                    )
                )
            attempt += 1

    def _reattach_once(
        self,
        factory: ClaudeCoordinationFactory,
    ) -> bool:
        with self._condition:
            self._connection_generation += 1
            generation = self._connection_generation
            self._coordination_changed.clear()
        coordination: ClaudeCoordination | None = None
        channel: ClaudeProtectedHostChannel | None = None
        try:
            coordination = factory(self._participant_id, generation)
            with self._session_lock:
                binding = self._require_session().binding
            manifest = claude_participant_manifest(
                self._participant_id,
                generation,
            )
            channel, _registration = coordination.register(manifest, binding)
            notices = coordination.control.notices()
            first = require_first_claude_notice(notices)
            with self._condition:
                self._control = coordination.control
                self._channel = channel
            self._apply_notice(first)
            self._start_threads(notices, generation)
            if not self._control_is_available():
                self._coordination_changed.wait()
            self._raise_failure()
            if self._control_is_available() or self._closing.is_set():
                return True
        except _ATTACHMENT_RETRY_ERRORS:
            self._close_coordination(coordination, channel)
            return False
        except BaseException:
            self._close_coordination(coordination, channel)
            raise
        self._close_coordination(coordination, channel)
        return False

    @staticmethod
    def _close_coordination(
        coordination: ClaudeCoordination | None,
        channel: ClaudeProtectedHostChannel | None,
    ) -> None:
        if channel is not None:
            channel.close()
        if coordination is not None:
            coordination.host_endpoint.close()
            coordination.registration_endpoint.close()
            with suppress(BaseException):
                coordination.control.close()

    def _control_is_available(self) -> bool:
        with self._condition:
            return self._control_available

    def _raise_failure(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _raise_gate(self) -> None:
        if self._gate_code is not None:
            raise ClaudeSessionGateError(self._gate_code)

    @staticmethod
    def _require_endpoint(endpoint: socket.socket) -> None:
        if (
            endpoint.family is not socket.AF_UNIX
            or endpoint.type & socket.SOCK_STREAM != socket.SOCK_STREAM
            or endpoint.fileno() < 0
        ):
            raise ValueError("Claude participant endpoint is invalid.")

    @staticmethod
    def _fail(code: ClaudeStructuredFailure) -> NoReturn:
        raise ClaudeStructuredError(code)
