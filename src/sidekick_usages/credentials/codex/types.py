"""Closed outcomes for managed Codex authority operations."""

from enum import StrEnum


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
