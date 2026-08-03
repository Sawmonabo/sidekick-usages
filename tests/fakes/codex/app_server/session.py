"""Effective session configuration for the synthetic Codex daemon."""

from pathlib import Path
from threading import RLock

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization.json import (
    JsonObject,
    JsonValue,
    decode_json_object,
)
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
        user_config: JsonObject | None = None,
        project_config: JsonObject | None = None,
    ) -> None:
        self._codex_home = codex_home
        self._model_provider = model_provider
        self._base_url = base_url
        self._requires_openai_auth = requires_openai_auth
        self._supports_websockets = supports_websockets
        self._user_config = user_config
        self._project_config = project_config
        self._lock = RLock()
        self._config_read_count = 0

    @property
    def config_read_count(self) -> int:
        """Return effective resident-config readbacks."""
        with self._lock:
            return self._config_read_count

    def read_config(self) -> JsonObject:
        """Return exact resident config layers and record their read."""
        session_config = self._resident_config()
        with self._lock:
            self._config_read_count += 1
        layers: list[JsonValue] = []
        if self._user_config is not None:
            layers.append(
                self._layer(
                    {
                        "type": "user",
                        "file": str(self._codex_home / "config.toml"),
                    },
                    self._user_config,
                )
            )
        if self._project_config is not None:
            layers.append(
                self._layer(
                    {
                        "type": "project",
                        "dotCodexFolder": str(
                            self._codex_home / "project" / ".codex"
                        ),
                    },
                    self._project_config,
                )
            )
        session_source: JsonObject = {"type": "sessionFlags"}
        layers.append(self._layer(session_source, session_config))
        origin: JsonObject = {
            "name": session_source,
            "version": "0.146.0",
        }
        return {
            "config": {"model_provider": session_config.get("model_provider")},
            "layers": layers,
            "origins": {
                "model_provider": origin,
                "model_providers.sidekick-chatgpt-http": origin,
            },
        }

    def read_model_capabilities(self) -> JsonObject:
        """Return exact release-shaped provider feature capabilities."""
        return {
            "imageGeneration": True,
            "namespaceTools": True,
            "webSearch": True,
        }

    def _resident_config(self) -> JsonObject:
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
            "sidekick-chatgpt-http"
        )
        if not isinstance(configured_definition, dict):
            raise AssertionError("Codex fake provider config is malformed.")
        definition: JsonObject = {
            "name": configured_definition.get("name"),
            "base_url": (
                self._base_url
                if self._base_url is not None
                else configured_definition.get("base_url")
            ),
            "wire_api": configured_definition.get("wire_api"),
            "requires_openai_auth": (
                self._requires_openai_auth
                if self._requires_openai_auth is not None
                else configured_definition.get("requires_openai_auth")
            ),
            "supports_websockets": (
                self._supports_websockets
                if self._supports_websockets is not None
                else configured_definition.get("supports_websockets")
            ),
        }
        return {
            "model_provider": provider,
            "model_providers": {"sidekick-chatgpt-http": definition},
        }

    @staticmethod
    def _layer(name: JsonObject, config: JsonObject) -> JsonObject:
        return {
            "config": config,
            "name": name,
            "version": "0.146.0",
        }
