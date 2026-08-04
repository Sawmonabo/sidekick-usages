"""Public ownership of one qualified coordinated Claude session."""

from collections.abc import Callable

from sidekick_usages.cli.session.claude.runtime import (
    ClaudeProviderTerminatedError,
    ClaudeSessionRuntime,
)
from sidekick_usages.cli.session.claude.terminal import ClaudeTerminal
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredDialogRequest,
    ClaudeStructuredElicitationRequest,
    ClaudeStructuredHookCallbackRequest,
    ClaudeStructuredMcpMessageRequest,
    ClaudeStructuredPermissionDecision,
    ClaudeStructuredPermissionRequest,
    ClaudeStructuredQuestionAnswer,
    ClaudeStructuredQuestionRequest,
    ClaudeStructuredTerminalEvent,
)


class ClaudeCliSession:
    """Run one retained engine through coordinated turn boundaries."""

    def __init__(
        self,
        runtime: ClaudeSessionRuntime,
        terminal_factory: Callable[[], ClaudeTerminal],
    ) -> None:
        self._runtime = runtime
        self._terminal_factory = terminal_factory

    def run(self, arguments: tuple[str, ...]) -> int:
        """Run one application and return the engine's natural status."""
        del arguments
        try:
            try:
                self._runtime.open()
            except ClaudeProviderTerminatedError:
                return self._runtime.finish_engine()
            except BaseException:
                self._runtime.finish_unattached_engine()
                raise
            while True:
                try:
                    self._terminal_factory().run(self)
                    break
                except ClaudeProviderTerminatedError:
                    return self._runtime.finish_engine()
                except BaseException:
                    self._runtime.report_terminal_failure()
            return self._runtime.finish_engine()
        finally:
            self._runtime.close()

    def start_turn(self, prompt: str) -> TurnId:
        """Queue, admit, and transmit one real provider turn."""
        return self._runtime.start_turn(prompt)

    def receive_event(self) -> ClaudeStructuredTerminalEvent:
        """Return one continuously decoded provider event."""
        return self._runtime.receive_event()

    def stop_terminal_events(self) -> None:
        """Release only the terminal-facing event consumer."""
        self._runtime.stop_terminal_events()

    def respond_permission(
        self,
        request: ClaudeStructuredPermissionRequest,
        decision: ClaudeStructuredPermissionDecision,
    ) -> None:
        """Return one correlated permission decision."""
        self._runtime.respond_permission(request, decision)

    def respond_question(
        self,
        request: ClaudeStructuredQuestionRequest,
        answers: tuple[ClaudeStructuredQuestionAnswer, ...],
    ) -> None:
        """Return one correlated question answer set."""
        self._runtime.respond_question(request, answers)

    def decline_elicitation(
        self,
        request: ClaudeStructuredElicitationRequest,
    ) -> None:
        """Decline one provider elicitation."""
        self._runtime.decline_elicitation(request)

    def refuse_dialog(self, request: ClaudeStructuredDialogRequest) -> None:
        """Refuse one undeclared private dialog kind."""
        self._runtime.refuse_dialog(request)

    def refuse_unsupported_control(
        self,
        request: (
            ClaudeStructuredHookCallbackRequest
            | ClaudeStructuredMcpMessageRequest
        ),
    ) -> None:
        """Refuse one undeclared hook or SDK MCP capability."""
        self._runtime.refuse_unsupported_control(request)

    def interrupt(self) -> None:
        """Interrupt only the current retained-engine response."""
        self._runtime.interrupt()

    def end_turn(self, turn_id: TurnId) -> None:
        """Close one naturally completed turn."""
        self._runtime.end_turn(turn_id)
