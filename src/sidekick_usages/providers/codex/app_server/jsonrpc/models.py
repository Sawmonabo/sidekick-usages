"""Immutable Codex app-server JSON-RPC messages."""

from dataclasses import dataclass, field

from sidekick_usages.serialization.json import JsonObject


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """One successful response correlated to a client request."""

    request_id: int
    result: JsonObject = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonRpcErrorResponse:
    """One redacted server error correlated to a client request."""

    request_id: int
    code: int


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """One server notification with protected parameters."""

    method: str
    params: JsonObject = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonRpcServerRequest:
    """One server-initiated request with protected parameters."""

    request_id: int | str
    method: str
    params: JsonObject = field(repr=False)
