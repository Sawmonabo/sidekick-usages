"""Closed Codex app-server JSON-RPC types."""

from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
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
