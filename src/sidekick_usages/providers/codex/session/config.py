"""Protected direct-HTTP configuration for neutral Codex sessions."""

from typing import NoReturn, Protocol

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.methods import (
    CONFIG_READ_METHOD,
    MODEL_PROVIDER_CAPABILITIES_READ_METHOD,
)
from sidekick_usages.providers.codex.app_server.models import CodexVersion
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_BASE_URL,
    CODEX_SESSION_MODEL_PROVIDER,
    CODEX_SESSION_PROVIDER_NAME,
    CODEX_SESSION_WIRE_API,
    CodexSessionCapability,
)
from sidekick_usages.serialization.json import JsonObject

_PROTECTED_ARGUMENTS = (
    "-c",
    f'model_provider="{CODEX_SESSION_MODEL_PROVIDER}"',
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.name="
        f'"{CODEX_SESSION_PROVIDER_NAME}"'
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.base_url="
        f'"{CODEX_SESSION_BASE_URL}"'
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.wire_api="
        f'"{CODEX_SESSION_WIRE_API}"'
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http."
        "requires_openai_auth=true"
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http."
        "supports_websockets=false"
    ),
)


class CodexSessionReader(Protocol):
    """Read correlated resident app-server objects."""

    def request(self, method: str, params: JsonObject) -> JsonObject:
        """Return one strict object response."""


class CodexSessionConfig:
    """Own the immutable provider overlay and effective readback."""

    @property
    def arguments(self) -> tuple[str, ...]:
        """Return the exact global CLI override tuple."""
        return _PROTECTED_ARGUMENTS

    def command(self, subcommand: tuple[str, ...]) -> tuple[str, ...]:
        """Prefix one nonempty Codex subcommand with protected overrides."""
        if not subcommand or any(not argument for argument in subcommand):
            raise ValueError("Codex session subcommand is invalid.")
        return (*self.arguments, *subcommand)

    def qualify(
        self,
        reader: CodexSessionReader,
        version: CodexVersion,
        *,
        session_schema_supported: bool,
    ) -> CodexSessionCapability:
        """Read and validate the resident effective model transport."""
        result = reader.request(
            CONFIG_READ_METHOD,
            {"includeLayers": True},
        )
        provider, definition = _effective_provider(result)
        transport = reader.request(
            MODEL_PROVIDER_CAPABILITIES_READ_METHOD,
            {"modelProvider": provider},
        )
        model_transport = transport.get("modelTransport")
        auth_resolution = transport.get("authResolution")
        transport_websockets = transport.get("supportsWebsockets")
        name = definition.get("name")
        base_url = definition.get("baseUrl")
        wire_api = definition.get("wireApi")
        requires_openai_auth = definition.get("requiresOpenaiAuth")
        supports_websockets = definition.get("supportsWebsockets")
        if (
            set(transport)
            != {
                "authResolution",
                "modelTransport",
                "supportsWebsockets",
            }
            or not isinstance(name, str)
            or not isinstance(base_url, str)
            or not isinstance(wire_api, str)
            or not isinstance(requires_openai_auth, bool)
            or not isinstance(supports_websockets, bool)
            or not isinstance(model_transport, str)
            or not isinstance(auth_resolution, str)
            or not isinstance(transport_websockets, bool)
            or transport_websockets is not supports_websockets
        ):
            _malformed()
        return CodexSessionCapability(
            version=version,
            session_schema_supported=session_schema_supported,
            model_provider=provider,
            provider_name=name,
            base_url=base_url,
            wire_api=wire_api,
            requires_openai_auth=requires_openai_auth,
            supports_websockets=supports_websockets,
            model_transport=model_transport,
            auth_resolution=auth_resolution,
        )


def _effective_provider(result: JsonObject) -> tuple[str, JsonObject]:
    config = result.get("config")
    layers = result.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        _malformed()
    provider = config.get("modelProvider")
    providers = config.get("modelProviders")
    if not isinstance(provider, str) or not isinstance(providers, dict):
        _malformed()
    definition = providers.get(provider)
    if not isinstance(definition, dict):
        _malformed()
    return provider, definition


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
