"""Single prompt-toolkit owner for coordinated Claude sessions."""

from collections import deque
from collections.abc import Callable
from contextlib import suppress
from enum import StrEnum
from functools import partial
from queue import Queue
from threading import Condition, Event, RLock, Thread
from typing import Protocol

from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from sidekick_usages.cli.session.claude.commands import (
    ClaudeCommandKind,
    ClaudeSavedAccountCommands,
)
from sidekick_usages.cli.session.claude.runtime import ClaudeSessionGateError
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredControlRequest,
    ClaudeStructuredDialogRequest,
    ClaudeStructuredElicitationRequest,
    ClaudeStructuredHookCallbackRequest,
    ClaudeStructuredMcpMessageRequest,
    ClaudeStructuredPermissionDecision,
    ClaudeStructuredPermissionRequest,
    ClaudeStructuredQuestion,
    ClaudeStructuredQuestionAnswer,
    ClaudeStructuredQuestionOption,
    ClaudeStructuredQuestionRequest,
    ClaudeStructuredTerminalEvent,
)
from sidekick_usages.serialization.json import decode_json_object

_THREAD_JOIN_SECONDS = 2.0
type _TerminalAction = Callable[[], None]


class ClaudeTerminalSession(Protocol):
    """Coordinated operations consumed by the terminal application."""

    def start_turn(self, prompt: str) -> TurnId:
        """Queue, admit, and transmit one real provider turn."""

    def receive_event(self) -> ClaudeStructuredTerminalEvent:
        """Return the next continuously decoded provider event."""

    def stop_terminal_events(self) -> None:
        """Release only the terminal-facing event consumer."""

    def respond_permission(
        self,
        request: ClaudeStructuredPermissionRequest,
        decision: ClaudeStructuredPermissionDecision,
    ) -> None:
        """Return one correlated permission decision."""

    def respond_question(
        self,
        request: ClaudeStructuredQuestionRequest,
        answers: tuple[ClaudeStructuredQuestionAnswer, ...],
    ) -> None:
        """Return one correlated validated question answer set."""

    def decline_elicitation(
        self,
        request: ClaudeStructuredElicitationRequest,
    ) -> None:
        """Decline one elicitation request."""

    def refuse_dialog(self, request: ClaudeStructuredDialogRequest) -> None:
        """Refuse one undeclared private dialog kind."""

    def refuse_unsupported_control(
        self,
        request: (
            ClaudeStructuredHookCallbackRequest
            | ClaudeStructuredMcpMessageRequest
        ),
    ) -> None:
        """Refuse one undeclared hook or SDK MCP capability."""

    def interrupt(self) -> None:
        """Interrupt only the current retained-engine response."""

    def end_turn(self, turn_id: TurnId) -> None:
        """Close one naturally completed turn."""


class ClaudeTerminal(Protocol):
    """One terminal lifecycle consumed by the public Claude host."""

    def run(self, session: ClaudeTerminalSession) -> None:
        """Run until ordinary terminal EOF with restoration."""


class _TerminalMode(StrEnum):
    PROMPT = "prompt"
    LOGIN = "login"
    PERMISSION = "permission"
    QUESTION = "question"


class ClaudeTerminalApplication:
    """Multiplex input, events, controls, and restoration in one app."""

    def __init__(self, commands: ClaudeSavedAccountCommands) -> None:
        self._commands = commands
        self._history = InMemoryHistory()
        self._buffer = Buffer(multiline=True, history=self._history)
        self._lock = RLock()
        self._turn_condition = Condition(self._lock)
        self._closing = Event()
        self._prompts: Queue[str | None] = Queue()
        self._control_actions: Queue[_TerminalAction | None] = Queue()
        self._selection_actions: Queue[_TerminalAction | None] = Queue()
        self._output: list[str] = []
        self._output_correlations: dict[str, int] = {}
        self._status = "Enter a prompt. Ctrl-C interrupts an active turn."
        self._mode = _TerminalMode.PROMPT
        self._active_turn: TurnId | None = None
        self._turn_starting = False
        self._close_when_idle = False
        self._control: ClaudeStructuredControlRequest | None = None
        self._controls: dict[
            str,
            ClaudeStructuredPermissionRequest
            | ClaudeStructuredQuestionRequest,
        ] = {}
        self._control_order: deque[str] = deque()
        self._control_retry: _TerminalAction | None = None
        self._control_response_in_flight = False
        self._question_answers: list[ClaudeStructuredQuestionAnswer] = []
        self._question_index = 0
        self._accounts: tuple[SavedAccount, ...] = ()
        self._account_index = 0
        self._session: ClaudeTerminalSession | None = None
        self._threads: tuple[Thread, ...] = ()
        bindings = KeyBindings()
        bindings.add("enter")(self._accept)
        bindings.add("escape", "enter")(self._newline)
        bindings.add("c-c")(self._interrupt)
        bindings.add("c-d")(self._eof)
        bindings.add("up")(self._move_up)
        bindings.add("down")(self._move_down)
        output = FormattedTextControl(
            text=self._render_output,
            get_cursor_position=self._output_cursor,
        )
        status = FormattedTextControl(text=lambda: self._status)
        application: Application[int] = Application(
            layout=Layout(
                HSplit(
                    (
                        Window(
                            output,
                            wrap_lines=True,
                            always_hide_cursor=True,
                        ),
                        Window(
                            status,
                            height=Dimension.exact(1),
                            wrap_lines=False,
                        ),
                        Window(
                            BufferControl(buffer=self._buffer),
                            height=Dimension(min=1, max=5),
                        ),
                    )
                ),
                focused_element=self._buffer,
            ),
            key_bindings=bindings,
            full_screen=False,
            erase_when_done=False,
        )
        self._application = application

    def run(self, session: ClaudeTerminalSession) -> None:
        """Run one prompt-toolkit lifecycle and bounded worker set."""
        self._session = session
        self._threads = (
            Thread(target=self._consume_prompts, daemon=True),
            Thread(target=self._consume_control_actions, daemon=True),
            Thread(target=self._consume_selection_actions, daemon=True),
            Thread(target=self._consume_events, daemon=True),
        )
        for thread in self._threads:
            thread.start()
        failure: BaseException | None = None
        try:
            self._application.run()
        except BaseException as error:
            failure = error
        finally:
            self._closing.set()
            session.stop_terminal_events()
            self._prompts.put(None)
            self._control_actions.put(None)
            self._selection_actions.put(None)
            with self._turn_condition:
                self._turn_condition.notify_all()
            for thread in self._threads:
                thread.join(_THREAD_JOIN_SECONDS)
                if thread.is_alive() and failure is None:
                    failure = RuntimeError(
                        "Claude terminal worker did not close."
                    )
        if failure is not None:
            raise failure

    def _accept(self, _event: KeyPressEvent) -> None:
        value = self._buffer.text.strip()
        if self._mode is _TerminalMode.PROMPT and value:
            self._history.append_string(value)
        self._buffer.text = ""
        if self._mode is _TerminalMode.LOGIN:
            self._select_account()
        elif self._mode is _TerminalMode.PERMISSION:
            self._answer_permission(value)
        elif self._mode is _TerminalMode.QUESTION:
            self._answer_question(value)
        elif value:
            self._route_prompt(value)

    def _newline(self, _event: KeyPressEvent) -> None:
        if self._mode is _TerminalMode.PROMPT:
            self._buffer.insert_text("\n")

    def _route_prompt(self, prompt: str) -> None:
        route = self._commands.route(prompt)
        if route.kind is ClaudeCommandKind.PROVIDER:
            self._prompts.put(prompt)
            self._set_status("Prompt queued for the current account epoch.")
            return
        if route.kind is ClaudeCommandKind.REFUSED:
            self._append(route.guidance or "Credential command refused.")
            return
        accounts = self._commands.accounts
        if not accounts:
            self._append("No saved Claude accounts are available.")
            return
        with self._lock:
            self._accounts = accounts
            self._account_index = 0
            self._mode = _TerminalMode.LOGIN
            self._status = self._account_status()
        self._application.invalidate()

    def _select_account(self) -> None:
        with self._lock:
            account = self._accounts[self._account_index]
            self._mode = _TerminalMode.PROMPT
            self._status = "Selecting the saved Claude account..."
        self._selection_actions.put(
            lambda selected=account: self._append(
                self._commands.select(selected.account_id)
            )
        )

    def _answer_permission(self, value: str) -> None:
        control = self._control
        if not isinstance(control, ClaudeStructuredPermissionRequest):
            self._fail_local("Permission state is invalid.")
            return
        if self._control_retry is not None:
            self._queue_control_response(self._control_retry)
            return
        decision = (
            ClaudeStructuredPermissionDecision.ALLOW
            if value.casefold() in {"y", "yes"}
            else ClaudeStructuredPermissionDecision.DENY
        )
        self._queue_control_response(
            lambda request=control, answer=decision: (
                self._require_session().respond_permission(request, answer)
            )
        )

    def _answer_question(self, value: str) -> None:
        control = self._control
        if not isinstance(control, ClaudeStructuredQuestionRequest):
            self._fail_local("Question state is invalid.")
            return
        if self._control_retry is not None:
            self._queue_control_response(self._control_retry)
            return
        question = control.questions[self._question_index]
        selected = self._selected_options(question, value)
        if selected is None:
            self._set_status("Choose the displayed option label or number.")
            return
        self._question_answers.append(
            ClaudeStructuredQuestionAnswer(
                question=question.question,
                answer=", ".join(option.label for option in selected),
                preview=selected[0].preview if len(selected) == 1 else None,
            )
        )
        self._question_index += 1
        if self._question_index < len(control.questions):
            self._show_question(control.questions[self._question_index])
            return
        answers = tuple(self._question_answers)
        self._queue_control_response(
            lambda request=control, values=answers: (
                self._require_session().respond_question(request, values)
            )
        )

    @staticmethod
    def _selected_options(
        question: ClaudeStructuredQuestion,
        value: str,
    ) -> tuple[ClaudeStructuredQuestionOption, ...] | None:
        requested = tuple(
            part.strip() for part in value.split(",") if part.strip()
        )
        if not requested or (
            not question.multi_select and len(requested) != 1
        ):
            return None
        selected = []
        for part in requested:
            option = next(
                (
                    candidate
                    for index, candidate in enumerate(
                        question.options, start=1
                    )
                    if part == str(index)
                    or part.casefold() == candidate.label.casefold()
                ),
                None,
            )
            if option is None or option in selected:
                return None
            selected.append(option)
        return tuple(selected)

    def _interrupt(self, _event: KeyPressEvent) -> None:
        control = self._control
        if isinstance(control, ClaudeStructuredQuestionRequest):
            self._queue_control_response(
                lambda request=control.permission: (
                    self._require_session().respond_permission(
                        request,
                        ClaudeStructuredPermissionDecision.DENY,
                    )
                )
            )
            return
        if isinstance(control, ClaudeStructuredPermissionRequest):
            self._queue_control_response(
                lambda request=control: (
                    self._require_session().respond_permission(
                        request,
                        ClaudeStructuredPermissionDecision.DENY,
                    )
                )
            )
            return
        with self._lock:
            active = self._active_turn is not None
        if active:
            self._control_actions.put(self._require_session().interrupt)
            self._set_status("Interrupt requested for the active response.")
        else:
            self._buffer.text = ""

    def _eof(self, _event: KeyPressEvent) -> None:
        if self._control is not None:
            self._close_when_idle = True
            self._deny_control()
            return
        with self._lock:
            if self._turn_starting or self._active_turn is not None:
                self._close_when_idle = True
                self._status = "Waiting for the active turn to finish."
                self._application.invalidate()
                return
        self._application.exit(result=0)

    def _move_up(self, _event: KeyPressEvent) -> None:
        self._move_account(-1)

    def _move_down(self, _event: KeyPressEvent) -> None:
        self._move_account(1)

    def _move_account(self, delta: int) -> None:
        with self._lock:
            if self._mode is not _TerminalMode.LOGIN:
                return
            self._account_index = (self._account_index + delta) % len(
                self._accounts
            )
            self._status = self._account_status()
        self._application.invalidate()

    def _consume_prompts(self) -> None:
        while not self._closing.is_set():
            prompt = self._prompts.get()
            if prompt is None:
                return
            with self._turn_condition:
                self._turn_condition.wait_for(
                    lambda: self._active_turn is None or self._closing.is_set()
                )
                if self._closing.is_set():
                    return
                self._turn_starting = True
            try:
                turn_id = self._require_session().start_turn(prompt)
            except ClaudeSessionGateError as error:
                with self._turn_condition:
                    self._turn_starting = False
                    self._turn_condition.notify_all()
                self._set_status(f"Sidekick: {error.code.value}")
                continue
            except BaseException as error:
                with self._turn_condition:
                    self._turn_starting = False
                    self._turn_condition.notify_all()
                self._exit(error)
                return
            with self._turn_condition:
                self._active_turn = turn_id
                self._turn_starting = False
                self._turn_condition.notify_all()
            self._set_status("Claude is responding. New prompts will queue.")

    def _consume_control_actions(self) -> None:
        self._consume_actions(self._control_actions)

    def _consume_selection_actions(self) -> None:
        self._consume_actions(self._selection_actions)

    def _consume_actions(
        self,
        actions: Queue[_TerminalAction | None],
    ) -> None:
        while not self._closing.is_set():
            action = actions.get()
            if action is None:
                return
            try:
                action()
            except BaseException:
                self._append("Sidekick action failed; Claude remains active.")
                self._set_status("Retry the action or continue the session.")

    def _consume_events(self) -> None:
        while not self._closing.is_set():
            try:
                event = self._require_session().receive_event()
                self._present_event(event)
            except BaseException as error:
                if not self._closing.is_set():
                    self._exit(error)
                return

    def _present_event(self, event: ClaudeStructuredTerminalEvent) -> None:
        self._present_text(event)
        if event.status is not None:
            self._set_status(event.status)
        control = event.control
        if isinstance(
            control,
            ClaudeStructuredPermissionRequest
            | ClaudeStructuredQuestionRequest,
        ):
            self._enqueue_control(control)
        elif isinstance(control, ClaudeStructuredElicitationRequest):
            self._append("Sidekick safely declined an MCP elicitation.")
            self._control_actions.put(
                lambda request=control: (
                    self._require_session().decline_elicitation(request)
                )
            )
        elif isinstance(control, ClaudeStructuredDialogRequest):
            self._append("Sidekick safely refused an unsupported dialog.")
            self._control_actions.put(
                lambda request=control: self._require_session().refuse_dialog(
                    request
                )
            )
        elif isinstance(
            control,
            ClaudeStructuredHookCallbackRequest
            | ClaudeStructuredMcpMessageRequest,
        ):
            message = (
                "Sidekick cannot run an undeclared Claude hook."
                if isinstance(control, ClaudeStructuredHookCallbackRequest)
                else (
                    "Sidekick cannot route an undeclared SDK MCP server."
                )
            )
            self._append(message)
            self._control_actions.put(
                lambda request=control: (
                    self._require_session().refuse_unsupported_control(
                        request
                    )
                )
            )
        if event.cancelled_request_id is not None:
            self._cancel_control(event.cancelled_request_id)
        if event.turn_complete:
            self._finish_turn()

    def _show_permission(
        self,
        request: ClaudeStructuredPermissionRequest,
    ) -> None:
        details = (
            self._permission_action(request),
            *tuple(
                value
                for value in (
                    request.title,
                    request.display_name,
                    request.description,
                    request.decision_reason,
                    request.blocked_path,
                )
                if value is not None
            ),
        )
        for detail in details:
            self._append(detail)
        with self._lock:
            self._mode = _TerminalMode.PERMISSION
            self._status = f"Allow {request.tool_name}? Type y or n."
        if request.permission_suggestions:
            self._append(
                "Persistent permission changes require separate consent "
                "and were not applied."
            )
        self._application.invalidate()

    def _show_question_request(
        self,
        request: ClaudeStructuredQuestionRequest,
    ) -> None:
        with self._lock:
            self._question_answers.clear()
            self._question_index = 0
            self._mode = _TerminalMode.QUESTION
        self._show_question(request.questions[0])

    def _show_question(self, question: ClaudeStructuredQuestion) -> None:
        options = "  ".join(
            f"{index}. {option.label} - {option.description}"
            for index, option in enumerate(question.options, start=1)
        )
        self._append(f"{question.header}: {question.question}\n{options}")
        self._set_status("Choose an option label or number.")

    def _cancel_control(self, request_id: str) -> None:
        with self._lock:
            control = self._controls.pop(request_id, None)
            if control is None:
                return
            with suppress(ValueError):
                self._control_order.remove(request_id)
            if self._control_request_id(self._control) == request_id:
                self._control = None
                self._activate_next_control()
        self._append("The provider cancelled the pending request.")

    def _finish_turn(self) -> None:
        with self._turn_condition:
            self._turn_condition.wait_for(lambda: not self._turn_starting)
            turn_id = self._active_turn
            if turn_id is None:
                self._exit(RuntimeError("Claude completed an unknown turn."))
                return
            self._active_turn = None
            self._turn_condition.notify_all()
            close = self._close_when_idle
        self._require_session().end_turn(turn_id)
        if close:
            self._exit(None)
        else:
            self._set_status("Enter a prompt. Ctrl-D exits.")

    def _clear_control(self) -> None:
        request_id = self._control_request_id(self._control)
        with self._lock:
            self._control = None
            self._question_answers.clear()
            self._question_index = 0
            self._control_retry = None
            self._control_response_in_flight = False
            self._mode = _TerminalMode.PROMPT
            self._status = "Claude is responding."
        if request_id is not None:
            self._controls.pop(request_id, None)
        self._activate_next_control()
        self._application.invalidate()

    def _append(self, text: str) -> None:
        with self._lock:
            self._output.append(text)
        self._application.invalidate()

    def _present_text(self, event: ClaudeStructuredTerminalEvent) -> None:
        correlation = event.text_correlation
        for text in event.text:
            if not event.text_append or correlation is None:
                self._append(text)
                continue
            with self._lock:
                index = self._output_correlations.get(correlation)
                if index is None:
                    self._output_correlations[correlation] = len(self._output)
                    self._output.append(text)
                else:
                    self._output[index] += text
            self._application.invalidate()

    def _set_status(self, text: str) -> None:
        with self._lock:
            self._status = text
        self._application.invalidate()

    def _render_output(self) -> str:
        with self._lock:
            return "\n".join(self._output)

    def _output_cursor(self) -> Point:
        with self._lock:
            lines = sum(text.count("\n") + 1 for text in self._output)
            return Point(x=0, y=max(0, lines - 1))

    def _account_status(self) -> str:
        account = self._accounts[self._account_index]
        return (
            f"Saved Claude account {self._account_index + 1}/"
            f"{len(self._accounts)}: {account.label}. Enter selects."
        )

    def _exit(self, error: BaseException | None) -> None:
        loop = self._application.loop
        if loop is None:
            return
        if error is None:
            loop.call_soon_threadsafe(
                partial(self._application.exit, result=0)
            )
        else:
            loop.call_soon_threadsafe(
                partial(self._application.exit, exception=error)
            )

    def _require_session(self) -> ClaudeTerminalSession:
        session = self._session
        if session is None:
            raise RuntimeError("Claude terminal session is unavailable.")
        return session

    def _fail_local(self, text: str) -> None:
        self._clear_control()
        self._append(text)

    def _deny_control(self) -> None:
        control = self._control
        if isinstance(control, ClaudeStructuredQuestionRequest):
            request = control.permission
        elif isinstance(control, ClaudeStructuredPermissionRequest):
            request = control
        else:
            return
        self._queue_control_response(
            lambda denied=request: self._require_session().respond_permission(
                denied,
                ClaudeStructuredPermissionDecision.DENY,
            )
        )

    def _queue_control_response(self, action: _TerminalAction) -> None:
        with self._lock:
            if self._control_response_in_flight:
                return
            self._control_retry = action
            self._control_response_in_flight = True
            self._status = "Sending the correlated Claude response..."
        self._control_actions.put(
            lambda response=action: self._send_control_response(response)
        )
        self._application.invalidate()

    def _send_control_response(self, action: _TerminalAction) -> None:
        try:
            action()
        except BaseException:
            with self._lock:
                self._control_response_in_flight = False
                self._status = (
                    "Claude response was not confirmed. Press Enter to retry."
                )
            self._application.invalidate()
            return
        self._clear_control()

    def _enqueue_control(
        self,
        control: ClaudeStructuredPermissionRequest
        | ClaudeStructuredQuestionRequest,
    ) -> None:
        request_id = self._control_request_id(control)
        with self._lock:
            if request_id is None or request_id in self._controls:
                return
            self._controls[request_id] = control
            self._control_order.append(request_id)
            if self._control is None:
                self._activate_next_control()

    def _activate_next_control(self) -> None:
        with self._lock:
            while self._control_order:
                request_id = self._control_order.popleft()
                control = self._controls.get(request_id)
                if control is None:
                    continue
                self._control = control
                if isinstance(control, ClaudeStructuredQuestionRequest):
                    self._show_question_request(control)
                else:
                    self._show_permission(control)
                return
            self._mode = _TerminalMode.PROMPT

    @staticmethod
    def _permission_action(
        request: ClaudeStructuredPermissionRequest,
    ) -> str:
        payload = decode_json_object(request.tool_input)
        fields = ", ".join(tuple(payload)[:3]) or "no input fields"
        return f"Requested action: {request.tool_name} ({fields})"

    @staticmethod
    def _control_request_id(
        control: ClaudeStructuredControlRequest | None,
    ) -> str | None:
        if isinstance(control, ClaudeStructuredQuestionRequest):
            return control.permission.request_id
        if control is None:
            return None
        return control.request_id
