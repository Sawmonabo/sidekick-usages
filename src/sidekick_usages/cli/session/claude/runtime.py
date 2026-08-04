"""Retained engine and participant runtime for coordinated Claude."""

import socket
from collections import deque
from collections.abc import Callable, Iterator
from pathlib import Path
from queue import Queue
from threading import Condition, Event, RLock, Thread
from typing import NoReturn, Protocol
from uuid import uuid4

from sidekick_usages.cli.session.control import ParticipantControl
from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantRegistration,
    TurnAdmission,
    TurnAdmissionState,
)
from sidekick_usages.providers.claude.structured.codec import (
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


class ClaudeParticipantControl(Protocol):
    """Participant operations consumed by one Claude runtime."""

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register one exact participant and endpoint."""

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield authenticated admission notices."""

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Admit or queue one exact turn."""

    def end(self, turn_id: TurnId) -> None:
        """End one naturally terminal turn."""

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Publish exact provider-local readiness."""

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Publish first-real-turn adoption."""

    def close(self) -> None:
        """Close control resources owned by this participant."""


class _ClaudeControlAdapter:
    """Bind the provider-neutral control client to Claude's manifest."""

    def __init__(self, control: ParticipantControl) -> None:
        self._control = control

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register only the manifest composed with this control."""
        del manifest
        return self._control.register(protected_endpoint)

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield exact decoded Claude notices."""
        return self._control.notices()

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Return one exact turn boundary."""
        return self._control.begin(turn_id)

    def end(self, turn_id: TurnId) -> None:
        """Close one exact turn boundary."""
        self._control.end(turn_id)

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Publish one exact readiness proof."""
        self._control.ready(proof)

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Publish one exact adoption proof."""
        self._control.adopted(proof)

    def close(self) -> None:
        """Close both supervisor clients."""
        self._control.close()


class ClaudeSessionRuntime:
    """Own one unchanged engine and protected participant lifetime."""

    def __init__(
        self,
        engine: ClaudeStructuredEngine,
        control: ClaudeParticipantControl,
        host_endpoint: socket.socket,
        registration_endpoint: socket.socket,
        *,
        participant_id: ParticipantId,
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
        self._turn_id_factory = turn_id_factory
        self._request_id_factory = request_id_factory
        self._condition = Condition(RLock())
        self._engine_lock = RLock()
        self._session_lock = RLock()
        self._closing = Event()
        self._session: ClaudeStructuredSession | None = None
        self._channel: ClaudeProtectedHostChannel | None = None
        self._notice_thread: Thread | None = None
        self._protected_thread: Thread | None = None
        self._event_thread: Thread | None = None
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
        self._control_available = True
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
        manifest = ParticipantManifest(
            participant_id=participant_id,
            provider_id=ProviderId.CLAUDE,
            client_kind=ParticipantClientKind.CLAUDE_CODE,
            capability_version=1,
            connection_generation=_CONNECTION_GENERATION,
        )
        action = ControlClient.connect(supervisor_socket)
        subscription: ControlClient | None = None
        host_endpoint: socket.socket | None = None
        registration_endpoint: socket.socket | None = None
        try:
            subscription = ControlClient.connect(supervisor_socket)
            host_endpoint, registration_endpoint = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM,
            )
            control = _ClaudeControlAdapter(
                ParticipantControl(action, subscription, manifest)
            )
            runtime = cls(
                engine,
                control,
                host_endpoint,
                registration_endpoint,
                participant_id=participant_id,
            )
            host_endpoint = None
            registration_endpoint = None
            subscription = None
            return runtime
        except BaseException:
            if registration_endpoint is not None:
                registration_endpoint.close()
            if host_endpoint is not None:
                host_endpoint.close()
            if subscription is not None:
                subscription.close()
            action.close()
            raise

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
        manifest = ParticipantManifest(
            participant_id=self._participant_id,
            provider_id=ProviderId.CLAUDE,
            client_kind=ParticipantClientKind.CLAUDE_CODE,
            capability_version=1,
            connection_generation=_CONNECTION_GENERATION,
        )
        channel = ClaudeProtectedHostChannel(
            host_endpoint,
            self._participant_id,
            _CONNECTION_GENERATION,
        )
        self._channel = channel
        try:
            self._control.register(manifest, registration_endpoint)
            initial = channel.receive()
            with self._session_lock:
                session, receipt = self._bootstrap(initial)
                self._session = session
            self._initialize_engine()
            channel.acknowledge(receipt)
            notices = self._control.notices()
            try:
                first = next(notices)
            except StopIteration:
                self._fail(ClaudeStructuredFailure.PROTOCOL_EOF)
            self._apply_notice(first)
            self._start_threads(notices)
            self._enrolled = True
        except BaseException:
            channel.close()
            self._channel = None
            raise

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
                except BaseException:
                    self._control_lost()
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
            except BaseException:
                self._control_lost()

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

    def _start_threads(self, notices: Iterator[ParticipantNotice]) -> None:
        protected = Thread(
            target=self._consume_protected,
            daemon=True,
            name="claude-protected-installs",
        )
        notice = Thread(
            target=self._consume_notices,
            args=(notices,),
            daemon=True,
            name="claude-participant-notices",
        )
        events = Thread(
            target=self._consume_events,
            daemon=True,
            name="claude-structured-events",
        )
        self._protected_thread = protected
        self._notice_thread = notice
        self._event_thread = events
        protected.start()
        notice.start()
        events.start()

    def _consume_protected(self) -> None:
        channel = self._channel
        if channel is None:
            return
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
                    self._channel = None
                    self._control_lost()
                    return
                channel.acknowledge(receipt)
        except BaseException:
            if not self._closing.is_set():
                self._control_lost()

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
                self._record_failure(error)
                self._events.put(error)

    def _observe_terminal_event(
        self,
        event: ClaudeStructuredTerminalEvent,
    ) -> None:
        with self._session_lock:
            session = self._require_session()
            if event.conversation_id is not None:
                session.observe_conversation(event.conversation_id)
            for activity in event.activities:
                session.observe_event(activity)
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
    ) -> None:
        try:
            for notice in notices:
                self._apply_notice(notice)
            if not self._closing.is_set():
                self._control_lost()
        except BaseException:
            if not self._closing.is_set():
                self._control_lost()

    def _apply_notice(self, notice: ParticipantNotice) -> None:
        if (
            notice.participant_id != self._participant_id
            or notice.provider_id is not ProviderId.CLAUDE
        ):
            self._fail(ClaudeStructuredFailure.AUTHORITY_MISMATCH)
        if notice.kind is ParticipantNoticeKind.PREPARE:
            with self._condition:
                self._gate_code = None
                self._condition.notify_all()
            return
        if notice.kind is ParticipantNoticeKind.OPEN:
            with self._condition:
                self._gate_code = None
                self._open_revision += 1
                self._condition.notify_all()
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
                self._raise_failure()
                self._raise_gate()
                revision = self._open_revision
            try:
                admission = self._control.begin(turn_id)
            except BaseException:
                self._control_lost()
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

    def _record_failure(self, error: BaseException) -> None:
        with self._condition:
            if self._failure is None:
                self._failure = error
            self._condition.notify_all()

    def _control_lost(self) -> None:
        with self._condition:
            self._control_available = False
            self._gate_code = SelectionCode.SELECTION_RECOVERY_REQUIRED
            self._condition.notify_all()
        self._events.put(
            ClaudeStructuredTerminalEvent(
                conversation_id=None,
                text=(),
                status="Sidekick: selection_recovery_required",
            )
        )

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
