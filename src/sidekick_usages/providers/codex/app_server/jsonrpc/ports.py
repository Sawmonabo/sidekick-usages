"""Transport port for framed Codex app-server JSON-RPC."""

from typing import Protocol

from sidekick_usages.serialization.json import JsonObject

DEFAULT_JSON_RPC_TIMEOUT_SECONDS = 5.0


class JsonRpcRequester(Protocol):
    """Send one correlated Codex JSON-RPC request."""

    def request(
        self,
        method: str,
        params: JsonObject,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonObject:
        """Return one validated object result."""


class JsonRpcTransport(Protocol):
    """Exchange complete bounded JSON payloads over one framed transport."""

    @property
    def closed(self) -> bool:
        """Return whether the transport is closed."""

    def send(self, payload: bytes, deadline: float) -> None:
        """Send one complete encoded payload before ``deadline``."""

    def receive(self, deadline: float) -> bytes:
        """Receive one complete encoded payload before ``deadline``."""

    def close(self) -> None:
        """Close the transport and release its resources."""
