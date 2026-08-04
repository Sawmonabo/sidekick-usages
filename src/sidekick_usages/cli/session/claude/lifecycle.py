"""Runtime lifecycle signals for one retained Claude engine."""

from uuid import uuid4

from sidekick_usages.core.selection.types import SelectionCode, TurnId
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredTerminalEvent,
)


class ClaudeSessionGateError(RuntimeError):
    """One recoverable supervisor gate status for a live engine."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)


class ClaudeTerminalEventsClosedError(RuntimeError):
    """Stop only the terminal-facing event consumer at ordinary exit."""


class ClaudeProviderTerminatedError(RuntimeError):
    """Report the official engine's natural termination to its host."""


def claude_recovery_event() -> ClaudeStructuredTerminalEvent:
    """Return the stable terminal status for a gated live engine."""
    return ClaudeStructuredTerminalEvent(
        conversation_id=None,
        text=(),
        status="Sidekick: selection_recovery_required",
    )


def new_claude_turn_id() -> TurnId:
    """Return one unique retained-session turn identifier."""
    return TurnId(str(uuid4()))


def retain_claude_turn(receipt: ClaudeStructuredAdoptionReceipt) -> None:
    """Retain only the existence of one adoption proof callback."""
    del receipt
