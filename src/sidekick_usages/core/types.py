"""Closed type vocabulary shared across application features."""

from enum import IntEnum, StrEnum


class ProviderId(StrEnum):
    """Supported provider identifiers at string boundaries."""

    CLAUDE = "claude"
    CODEX = "codex"


class ExitCode(IntEnum):
    """Stable application process outcomes.

    ``SYSTEM_ERROR`` covers configuration, provider, and local system
    failures. Scheduler lifecycle failures remain distinct.
    """

    SUCCESS = 0
    MANUAL_ACTION = 1
    SYSTEM_ERROR = 2
    SCHEDULER_ERROR = 3
