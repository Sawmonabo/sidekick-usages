"""Shared Codex app-server initialization and release validation."""

from pathlib import Path

from sidekick_usages import __version__
from sidekick_usages.core.accounts.validation import (
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.client import (
    JsonRpcClient,
)
from sidekick_usages.providers.codex.app_server.methods import (
    INITIALIZE_METHOD,
    INITIALIZED_METHOD,
)
from sidekick_usages.providers.codex.app_server.models import CodexVersion
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.serialization.json import JsonObject

CLIENT_NAME = "sidekick_usages"
CLIENT_TITLE = "Sidekick Usages"
_SUPPORTED_PLATFORM_FAMILIES = frozenset({"unix"})
_SUPPORTED_PLATFORM_SYSTEMS = frozenset({"linux", "macos"})


def initialize_codex_app_server(
    connection: JsonRpcClient,
    codex_home: Path,
    version: CodexVersion,
) -> None:
    """Initialize one exact release-matched Codex app server."""
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
    _validate_initialize_response(result, codex_home, version)
    connection.notify(INITIALIZED_METHOD)


def _validate_initialize_response(
    result: JsonObject,
    codex_home: Path,
    version: CodexVersion,
) -> None:
    reported_home = result.get("codexHome")
    platform_family = result.get("platformFamily")
    platform_system = result.get("platformOs")
    user_agent = result.get("userAgent")
    if (
        set(result)
        != {"codexHome", "platformFamily", "platformOs", "userAgent"}
        or not isinstance(reported_home, str)
        or not isinstance(platform_family, str)
        or not isinstance(platform_system, str)
        or not isinstance(user_agent, str)
        or platform_family not in _SUPPORTED_PLATFORM_FAMILIES
        or platform_system not in _SUPPORTED_PLATFORM_SYSTEMS
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    try:
        require_bounded_text(
            user_agent,
            name="Codex app-server user agent",
            maximum=MAX_OPAQUE_BYTES,
        )
    except TypeError, ValueError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if user_agent.find(f"/{version} (") < 1:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    try:
        resolved_reported_home = Path(reported_home).resolve(strict=True)
    except OSError, ValueError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None
    if resolved_reported_home != codex_home:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
