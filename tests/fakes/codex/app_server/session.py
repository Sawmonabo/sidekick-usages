"""Effective session configuration for the synthetic Codex daemon."""

from pathlib import Path
from threading import RLock

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization.json import JsonObject, decode_json_object
from tests.fakes.codex.app_server.executable import SESSION_CONFIG_FILE


class FakeCodexSession:
    """Read the launched overlay and retain safe transport observations."""

    def __init__(
        self,
        codex_home: Path,
        *,
        model_provider: str | None,
        base_url: str | None,
        requires_openai_auth: bool | None,
        supports_websockets: bool | None,
        model_transport: str | None,
        auth_resolution: str | None,
    ) -> None:
        self._codex_home = codex_home
        self._model_provider = model_provider
        self._base_url = base_url
        self._requires_openai_auth = requires_openai_auth
        self._supports_websockets = supports_websockets
        self._model_transport = model_transport
        self._auth_resolution = auth_resolution
        self._lock = RLock()
        self._config_read_count = 0
        self._model_transport_attempts: list[
            tuple[str, str | None, str]
        ] = []

    @property
    def config_read_count(self) -> int:
        """Return effective resident-config readbacks."""
        with self._lock:
            return self._config_read_count

    @property
    def model_transport_attempts(
        self,
    ) -> tuple[tuple[str, str | None, str], ...]:
        """Return safe model transport and current-auth observations."""
        with self._lock:
            return tuple(self._model_transport_attempts)

    def read_config(self) -> tuple[str, JsonObject]:
        """Return the effective provider definition and record its read."""
        provider, definition = self._effective_provider()
        with self._lock:
            self._config_read_count += 1
        return provider, definition

    def read_model_capabilities(
        self,
        active_account_id: str | None,
    ) -> tuple[str, JsonObject]:
        """Return and record the attempted model transport contract."""
        provider, definition = self._effective_provider()
        supports = definition["supportsWebsockets"]
        requires_auth = definition["requiresOpenaiAuth"]
        if not isinstance(supports, bool) or not isinstance(
            requires_auth,
            bool,
        ):
            raise AssertionError("Codex fake provider config is malformed.")
        transport = self._model_transport
        if transport is None:
            transport = "websocket" if supports else "http"
        auth_resolution = self._auth_resolution
        if auth_resolution is None:
            auth_resolution = "perAttempt" if requires_auth else "none"
        with self._lock:
            self._model_transport_attempts.append(
                (transport, active_account_id, auth_resolution)
            )
        return provider, {
            "authResolution": auth_resolution,
            "modelTransport": transport,
            "supportsWebsockets": supports,
        }

    def _effective_provider(self) -> tuple[str, JsonObject]:
        path = self._codex_home / SESSION_CONFIG_FILE
        try:
            root = decode_json_object(path.read_bytes())
        except InvalidPayloadError, OSError:
            raise AssertionError(
                "Codex fake effective session config is unavailable."
            ) from None
        configured_provider = root.get("model_provider")
        configured_providers = root.get("model_providers")
        provider = (
            self._model_provider
            if self._model_provider is not None
            else configured_provider
        )
        if not isinstance(provider, str) or not isinstance(
            configured_providers,
            dict,
        ):
            raise AssertionError("Codex fake provider config is malformed.")
        configured_definition = configured_providers.get(
            configured_provider
        )
        if not isinstance(configured_definition, dict):
            raise AssertionError("Codex fake provider config is malformed.")
        definition: JsonObject = {
            "name": configured_definition.get("name"),
            "baseUrl": (
                self._base_url
                if self._base_url is not None
                else configured_definition.get("base_url")
            ),
            "wireApi": configured_definition.get("wire_api"),
            "requiresOpenaiAuth": (
                self._requires_openai_auth
                if self._requires_openai_auth is not None
                else configured_definition.get("requires_openai_auth")
            ),
            "supportsWebsockets": (
                self._supports_websockets
                if self._supports_websockets is not None
                else configured_definition.get("supports_websockets")
            ),
        }
        return provider, definition
