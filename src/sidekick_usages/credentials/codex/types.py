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
