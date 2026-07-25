"""Closed Codex app-server types."""

from enum import StrEnum


class CodexProcessGroupPolicy(StrEnum):
    """Whether a Codex child owns or inherits its process group."""

    ISOLATED = "isolated"
    INHERITED = "inherited"


class CodexAppServerFailure(StrEnum):
    """Safe failure categories for the versioned app-server boundary."""

    EXECUTABLE_MISSING = "executable_missing"
    EXECUTABLE_UNSAFE = "executable_unsafe"
    VERSION_UNSUPPORTED = "version_unsupported"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    PROCESS_FAILED = "process_failed"
    PROCESS_TIMEOUT = "process_timeout"
    PROTOCOL_MALFORMED = "protocol_malformed"
    REQUEST_REJECTED = "request_rejected"
    PROTOCOL_TIMEOUT = "protocol_timeout"
    PROTOCOL_CLOSED = "protocol_closed"
