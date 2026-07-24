"""Closed HTTP retry values."""

from enum import Enum, StrEnum


class HttpOperation(StrEnum):
    """Closed operation classes with reviewed retry safety."""

    SAFE_READ = "safe_read"
    CLAUDE_PROBE = "claude_probe"
    CLAUDE_REFRESH = "claude_refresh"
    CODEX_REFRESH = "codex_refresh"
    CLAUDE_HEARTBEAT = "claude_heartbeat"
    CODEX_HEARTBEAT = "codex_heartbeat"


class TransportFailure(Enum):
    """Safety classification for a failed transport attempt."""

    PROVEN_CONNECT = "proven_connect"
    AMBIGUOUS = "ambiguous"
    TERMINAL = "terminal"


class TerminalOutcome(Enum):
    """Typed terminal outcome retained outside exception handlers."""

    TRANSPORT = "transport"
    SERVER = "server"
    RATE_LIMIT = "rate_limit"
