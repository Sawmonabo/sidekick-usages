"""Closed Codex app-server types."""

from enum import StrEnum

from sidekick_usages.providers.codex.app_server.models import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcServerRequest,
)

type JsonRpcMessage = (
    JsonRpcResponse
    | JsonRpcErrorResponse
    | JsonRpcNotification
    | JsonRpcServerRequest
)


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
