"""Closed managed-Codex outcomes and presentation port."""

from collections.abc import Callable
from enum import StrEnum

from sidekick_usages.providers.codex.models import CodexLoginEvent

type CodexLoginEventSink = Callable[[CodexLoginEvent], None]


class CodexManagedOutcome(StrEnum):
    """Secret-safe outcomes from official managed-home operations."""

    HEALTHY = "healthy"
    UNCHANGED = "unchanged"
    REJECTED = "rejected"
    LOGGED_OUT = "logged_out"
    INCOMPATIBLE = "incompatible"
    MALFORMED = "malformed"
    TIMED_OUT = "timed_out"
    TRANSIENT = "transient"

    @property
    def action_required(self) -> bool:
        """Return whether the user must repair this credential authority."""
        return self in {
            CodexManagedOutcome.INCOMPATIBLE,
            CodexManagedOutcome.LOGGED_OUT,
            CodexManagedOutcome.MALFORMED,
            CodexManagedOutcome.REJECTED,
        }


class CodexActivationFailure(StrEnum):
    """Secret-safe failures from Codex activation and recovery."""

    TARGET_UNAVAILABLE = "codex_activation_target_unavailable"
    NATIVE_UNREADABLE = "codex_activation_native_unreadable"
    NATIVE_CHANGED = "codex_activation_native_changed"
    DAEMON_UNAVAILABLE = "codex_activation_daemon_unavailable"
    RECEIPT_MISMATCH = "codex_activation_receipt_mismatch"
    STATE_CHANGED = "codex_activation_state_changed"
