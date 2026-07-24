"""Closed scalar and message types for the Codex app server."""

from enum import StrEnum

from sidekick_usages.providers.codex.models.app_server import (
    JsonRpcErrorResponse,
    JsonRpcNotification,
    JsonRpcResponse,
    JsonRpcServerRequest,
)

__all__ = ["CodexAppServerFailure", "JsonRpcMessage"]

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
