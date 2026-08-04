"""Initialized private Codex app-server session."""

from collections.abc import Mapping
from pathlib import Path
from types import TracebackType

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    verify_codex_executable,
)
from sidekick_usages.providers.codex.app_server.initialization import (
    initialize_codex_app_server,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.client import (
    JsonRpcClient,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.stdio import (
    JsonLinesTransport,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.types import (
    JsonRpcMessage,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    CodexProcessGroupPolicy,
)
from sidekick_usages.serialization.json import JsonObject


class CodexAppServerSession:
    """One initialized bounded app server in a private Codex home."""

    def __init__(
        self,
        connection: JsonRpcClient,
        transport: JsonLinesTransport,
        codex_home: Path,
    ) -> None:
        self._connection = connection
        self._transport = transport
        self._codex_home = codex_home

    @classmethod
    def open(
        cls,
        capabilities: CodexAppServerCapabilities,
        codex_home: Path,
        environment: Mapping[str, str] | None = None,
        *,
        process_group: CodexProcessGroupPolicy = (
            CodexProcessGroupPolicy.ISOLATED
        ),
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
        transport = JsonLinesTransport.open(
            (
                str(capabilities.executable.provenance.path),
                "app-server",
            ),
            minimal_codex_environment(
                environment,
                codex_home=resolved_home,
            ),
            working_directory=resolved_home,
            process_group=process_group,
        )
        connection = JsonRpcClient(transport)
        try:
            initialize_codex_app_server(
                connection,
                resolved_home,
                capabilities.executable.version,
            )
        except CodexAppServerError:
            connection.close()
            raise
        return cls(connection, transport, resolved_home)

    @property
    def codex_home(self) -> Path:
        """Return the exact private home reported by the server."""
        return self._codex_home

    @property
    def process_id(self) -> int:
        """Return the private app-server process identifier."""
        return self._transport.process_id

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
        params: JsonObject | None = None,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonObject:
        """Send one correlated app-server request."""
        return self._connection.request(
            method,
            params,
            timeout_seconds=timeout_seconds,
        )

    def receive(
        self,
        *,
        timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    ) -> JsonRpcMessage:
        """Receive one queued notification or server request."""
        return self._connection.receive(timeout_seconds=timeout_seconds)

    def respond(
        self,
        request_id: int | str,
        result: JsonObject,
    ) -> None:
        """Answer one validated server-initiated request."""
        self._connection.respond(request_id, result)

    def respond_error(
        self,
        request_id: int | str,
        code: int,
        message: str,
    ) -> None:
        """Answer one server request with a bounded safe error."""
        self._connection.respond_error(request_id, code, message)

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
