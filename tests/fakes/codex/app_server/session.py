"""Effective session configuration for the synthetic Codex daemon."""

import json
from pathlib import Path
from threading import RLock

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, unix_connect
from websockets.sync.server import ServerConnection

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization.json import (
    JsonObject,
    JsonValue,
    decode_json_object,
)
from tests.fakes.codex.app_server.executable import SESSION_CONFIG_FILE

_CLIENT_TIMEOUT_SECONDS = 5.0
_DAEMON_WEBSOCKET_URI = "ws://localhost/rpc"


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
        mcp_server_names: tuple[str, ...] = (),
    ) -> None:
        self._codex_home = codex_home
        self._model_provider = model_provider
        self._base_url = base_url
        self._requires_openai_auth = requires_openai_auth
        self._supports_websockets = supports_websockets
        self._user_config = user_config
        self._project_config = project_config
        self._mcp_server_names = mcp_server_names
        self._lock = RLock()
        self._config_read_count = 0
        self._mcp_status_thread_ids: list[str] = []

    @property
    def config_read_count(self) -> int:
        """Return effective resident-config readbacks."""
        with self._lock:
            return self._config_read_count

    @property
    def mcp_status_thread_ids(self) -> tuple[str, ...]:
        """Return threads subjected to resident MCP readback."""
        with self._lock:
            return tuple(self._mcp_status_thread_ids)

    @property
    def mcp_server_names(self) -> tuple[str, ...]:
        """Return the configured inventory names."""
        return self._mcp_server_names

    def read_mcp_status(self, params: JsonObject) -> JsonObject:
        """Validate one thread-scoped request and return quiescence."""
        if set(params) != {"threadId"}:
            raise AssertionError("Fake Codex MCP status read is invalid.")
        thread_id = params.get("threadId")
        if not isinstance(thread_id, str):
            raise AssertionError("Fake Codex MCP status read is invalid.")
        with self._lock:
            self._mcp_status_thread_ids.append(thread_id)
        return {
            "data": [
                {"name": name} for name in self._mcp_server_names
            ]
        }

    def read_config(self) -> JsonObject:
        """Return exact resident config layers and record their read."""
        session_config = self._resident_config()
        with self._lock:
            self._config_read_count += 1
        layers: list[JsonValue] = []
        session_source: JsonObject = {
            "type": "user",
            "file": str(self._codex_home / "config.toml"),
        }
        layers.append(self._layer(session_source, session_config))
        if self._user_config is not None:
            layers.append(
                self._layer(
                    {
                        "type": "user",
                        "file": str(self._codex_home / "external-config.toml"),
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


class FakeCodexTuiObserver:
    """One initialized fake TUI observing shared account updates."""

    def __init__(self, socket_path: Path) -> None:
        self._socket_path = socket_path
        self._connection: ClientConnection | None = None

    def open(self) -> None:
        """Connect and initialize this observer."""
        connection = unix_connect(
            str(self._socket_path),
            uri=_DAEMON_WEBSOCKET_URI,
            compression=None,
            proxy=None,
            open_timeout=_CLIENT_TIMEOUT_SECONDS,
            close_timeout=1.0,
        )
        self._connection = connection
        send_fake_codex_message(
            connection,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "capabilities": {"experimentalApi": True},
                    "clientInfo": {
                        "name": "codex-tui",
                        "title": "Codex TUI",
                        "version": "0.145.0",
                    },
                },
            },
        )
        response = _receive(connection)
        if response.get("id") != 1 or not isinstance(
            response.get("result"),
            dict,
        ):
            raise AssertionError("Fake Codex observer failed to initialize.")
        send_fake_codex_message(connection, {"method": "initialized"})

    def wait_for_account_update(self) -> None:
        """Wait for one external-auth account update."""
        connection = self._require_connection()
        for _message_index in range(4):
            message = _receive(connection)
            if message.get("method") == "account/updated" and message.get(
                "params"
            ) == {
                "authMode": "chatgptAuthTokens",
                "planType": "pro",
            }:
                return
        raise AssertionError("Fake Codex observer saw no account update.")

    def send_request(
        self,
        request_id: int,
        method: str,
        params: JsonObject,
    ) -> None:
        """Send one official-shaped request through the active connection."""
        send_fake_codex_message(
            self._require_connection(),
            {"id": request_id, "method": method, "params": params},
        )

    def receive(self) -> JsonObject:
        """Receive one complete fake TUI frame."""
        return _receive(self._require_connection())

    def receive_optional(self, timeout_seconds: float) -> JsonObject | None:
        """Return a frame when available within one bounded wait."""
        try:
            return _receive(
                self._require_connection(),
                timeout_seconds=timeout_seconds,
            )
        except TimeoutError:
            return None

    def assert_turn_completed(
        self,
        request_id: int,
        thread_id: str,
    ) -> None:
        """Require one correlated turn response and completion."""
        self.send_request(
            request_id,
            "turn/start",
            {"input": [], "threadId": thread_id},
        )
        response_seen = False
        completion_seen = False
        for _message_index in range(8):
            message = self.receive()
            response_seen = response_seen or message.get("id") == request_id
            completion_seen = completion_seen or (
                message.get("method") == "turn/completed"
            )
            if response_seen and completion_seen:
                return
        raise AssertionError("Fake Codex TUI turn did not complete.")

    def wait_closed(self) -> None:
        """Require the relay to close this observer connection."""
        try:
            self._require_connection().recv(
                timeout=_CLIENT_TIMEOUT_SECONDS
            )
        except ConnectionClosed:
            return
        raise AssertionError("Fake Codex observer remained open.")

    def close(self) -> None:
        """Close this observer."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()

    def _require_connection(self) -> ClientConnection:
        connection = self._connection
        if connection is None:
            raise AssertionError("Fake Codex observer is not open.")
        return connection


def send_fake_codex_message(
    connection: ServerConnection | ClientConnection,
    message: JsonObject,
) -> None:
    """Send one complete JSON object through a fake daemon connection."""
    connection.send(json.dumps(message))


def _receive(
    connection: ClientConnection,
    *,
    timeout_seconds: float = _CLIENT_TIMEOUT_SECONDS,
) -> JsonObject:
    message = connection.recv(timeout=timeout_seconds)
    if not isinstance(message, str):
        raise AssertionError("Fake Codex observer received binary data.")
    try:
        return decode_json_object(message.encode())
    except InvalidPayloadError:
        raise AssertionError(
            "Fake Codex observer received invalid JSON."
        ) from None
