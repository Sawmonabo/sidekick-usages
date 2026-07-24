"""Initialized private Codex app-server session."""

from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from sidekick_usages import __version__
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    verify_codex_executable,
)
from sidekick_usages.providers.codex.app_server.jsonrpc import (
    JsonRpcConnection,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    JsonRpcMessage,
)
from sidekick_usages.serialization.json import JsonObject

CLIENT_NAME = "sidekick_usages"
CLIENT_TITLE = "Sidekick Usages"
INITIALIZE_METHOD = "initialize"
INITIALIZED_METHOD = "initialized"
_SUPPORTED_PLATFORM_FAMILIES = frozenset({"unix"})
_SUPPORTED_PLATFORM_SYSTEMS = frozenset({"linux", "macos"})


class CodexAppServerSession:
    """One initialized bounded app server in a private Codex home."""

    def __init__(
        self,
        connection: JsonRpcConnection,
        codex_home: Path,
    ) -> None:
        self._connection = connection
        self._codex_home = codex_home

    @classmethod
    def open(
        cls,
        capabilities: CodexAppServerCapabilities,
        codex_home: Path,
        environment: Mapping[str, str] | None = None,
    ) -> CodexAppServerSession:
        """Start, validate, and initialize one private app server."""
        verify_codex_executable(capabilities.executable)
        try:
            resolved_home = codex_home.resolve(strict=True)
        except OSError, ValueError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None
        if not codex_home.is_absolute() or not resolved_home.is_dir():
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        connection = JsonRpcConnection.open(
            (str(capabilities.executable.path), "app-server"),
            minimal_codex_environment(
                environment,
                codex_home=resolved_home,
            ),
        )
        try:
            result = connection.request(
                INITIALIZE_METHOD,
                {
                    "capabilities": {"experimentalApi": True},
                    "clientInfo": {
                        "name": CLIENT_NAME,
                        "title": CLIENT_TITLE,
                        "version": __version__,
                    },
                },
            )
            _validate_initialize_response(result, resolved_home)
            connection.notify(INITIALIZED_METHOD)
        except CodexAppServerError:
            connection.close()
            raise
        return cls(connection, resolved_home)

    @property
    def codex_home(self) -> Path:
        """Return the exact private home reported by the server."""
        return self._codex_home

    @property
    def process_id(self) -> int:
        """Return the private app-server process identifier."""
        return self._connection.process_id

    @property
    def next_request_id(self) -> int:
        """Return the next monotonic client request ID."""
        return self._connection.next_request_id

    @property
    def closed(self) -> bool:
        """Return whether the app-server child has been reaped."""
        return self._connection.closed

    def request(
        self,
        method: str,
        params: JsonObject,
    ) -> JsonObject:
        """Send one correlated app-server request."""
        return self._connection.request(method, params)

    def receive(self) -> JsonRpcMessage:
        """Receive one queued notification or server request."""
        return self._connection.receive()

    def respond(
        self,
        request_id: int | str,
        result: JsonObject,
    ) -> None:
        """Answer one validated server-initiated request."""
        self._connection.respond(request_id, result)

    def close(self) -> None:
        """Close and reap the private app-server child."""
        self._connection.close()

    def __enter__(self) -> CodexAppServerSession:
        """Enter the bounded session context."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Always close and reap the private child."""
        del exception_type, exception, traceback
        self.close()


def _validate_initialize_response(
    result: JsonObject,
    codex_home: Path,
) -> None:
    reported_home = result.get("codexHome")
    platform_family = result.get("platformFamily")
    platform_system = result.get("platformOs")
    user_agent = result.get("userAgent")
    if (
        not isinstance(reported_home, str)
        or not isinstance(platform_family, str)
        or not isinstance(platform_system, str)
        or not isinstance(user_agent, str)
        or not user_agent
        or platform_family not in _SUPPORTED_PLATFORM_FAMILIES
        or platform_system not in _SUPPORTED_PLATFORM_SYSTEMS
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    try:
        resolved_reported_home = Path(reported_home).resolve(strict=True)
    except OSError, ValueError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if resolved_reported_home != codex_home:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
