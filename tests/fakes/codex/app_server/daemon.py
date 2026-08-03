"""Real Unix-WebSocket fake for the official shared Codex daemon."""

import base64
import binascii
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from threading import Event, RLock, Thread
from types import TracebackType
from typing import Self

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, unix_connect
from websockets.sync.server import (
    Server,
    ServerConnection,
    unix_serve,
)

from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.account.types import CodexAuthMode
from sidekick_usages.providers.codex.broker.responder import (
    CODEX_CALLBACK_RESPONSE_SECONDS,
)
from sidekick_usages.serialization.json import JsonObject, decode_json_object
from tests.fakes.codex.app_server.models import FakeCodexRefreshResponse
from tests.fakes.codex.app_server.session import FakeCodexSession
from tests.fakes.codex.auth import managed_auth

_CLIENT_TIMEOUT_SECONDS = 5.0
_INSTALL_HANDSHAKE_TIMEOUT_SECONDS = 30.0
_REFRESH_RESPONSE_TIMEOUT_SECONDS = CODEX_CALLBACK_RESPONSE_SECONDS
_EXTERNAL_REFRESH_ERROR_CODE = -32000
_EXTERNAL_REFRESH_ERROR_MESSAGE = "external auth refresh unavailable"
_EXTERNAL_REFRESH_METHOD = "account/chatgptAuthTokens/refresh"
_JWT_PARTS = 3
_CONTROL_DIRECTORY_NAME = "app-server-control"
_CONTROL_SOCKET_NAME = "app-server-control.sock"
_DAEMON_WEBSOCKET_URI = "ws://localhost/rpc"
_EMITTED_AT_MILLISECONDS = 1_750_000_000_000


class FakeCodexDaemon:
    """Official-shaped shared daemon retaining only safe observations."""

    def __init__(
        self,
        codex_home: Path,
        *,
        app_server_version: str = "0.145.0",
        model_provider: str | None = None,
        base_url: str | None = None,
        requires_openai_auth: bool | None = None,
        supports_websockets: bool | None = None,
        user_config: JsonObject | None = None,
        project_config: JsonObject | None = None,
    ) -> None:
        self._codex_home = codex_home
        self._version = app_server_version
        self._session = FakeCodexSession(
            codex_home,
            model_provider=model_provider,
            base_url=base_url,
            requires_openai_auth=requires_openai_auth,
            supports_websockets=supports_websockets,
            user_config=user_config,
            project_config=project_config,
        )
        self._lock = RLock()
        self._server: Server | None = None
        self._thread: Thread | None = None
        self._connections: set[ServerConnection] = set()
        self._initialized: set[ServerConnection] = set()
        self._client_names: dict[ServerConnection, str] = {}
        self._installed_account_ids: list[str] = []
        self._external_logins: list[tuple[str, str]] = []
        self._active_account_id: str | None = None
        self._active_access_token: str | None = None
        self._originator: str | None = None
        self._ready_account_read_count = 0
        self._auth_status_read_count = 0
        self._model_auth_read_count = 0
        self._next_server_request_id = 0
        self._refresh_event: Event | None = None
        self._refresh_request_id: int | None = None
        self._refresh_response: FakeCodexRefreshResponse | None = None
        self._pause_install = False
        self._install_paused = Event()
        self._resume_install = Event()
        self._install_resumed = Event()
        self._failures: list[BaseException] = []

    @property
    def socket_path(self) -> Path:
        """Return the official default control socket path."""
        return (
            self._codex_home / _CONTROL_DIRECTORY_NAME / _CONTROL_SOCKET_NAME
        )

    @property
    def installed_account_ids(self) -> tuple[str, ...]:
        """Return safe account identities installed across daemon processes."""
        with self._lock:
            return tuple(self._installed_account_ids)

    @property
    def ready_account_read_count(self) -> int:
        """Return successful post-install account-read observations."""
        with self._lock:
            return self._ready_account_read_count

    @property
    def auth_status_read_count(self) -> int:
        """Return effective native-auth observations."""
        with self._lock:
            return self._auth_status_read_count

    @property
    def model_auth_read_count(self) -> int:
        """Return synthetic model reads of current external auth."""
        with self._lock:
            return self._model_auth_read_count

    @property
    def config_read_count(self) -> int:
        """Return effective resident-config readbacks."""
        return self._session.config_read_count

    @property
    def external_logins(self) -> tuple[tuple[str, str], ...]:
        """Return deliberate native-login observations."""
        with self._lock:
            return tuple(self._external_logins)

    def pause_next_install(self) -> None:
        """Pause once after official mutation and before account read."""
        with self._lock:
            if self._pause_install:
                raise AssertionError("Fake Codex install is already paused.")
            self._pause_install = True
            self._install_paused.clear()
            self._resume_install.clear()
            self._install_resumed.clear()

    def wait_for_paused_install(self) -> None:
        """Wait until the one-shot install boundary is reached."""
        if not self._install_paused.wait(_INSTALL_HANDSHAKE_TIMEOUT_SECONDS):
            raise AssertionError("Fake Codex install did not pause.")

    def resume_install(self) -> None:
        """Release the one-shot install boundary."""
        self._resume_install.set()
        if not self._install_resumed.wait(_INSTALL_HANDSHAKE_TIMEOUT_SECONDS):
            raise AssertionError("Fake Codex install did not resume.")

    def perform_external_login(
        self,
        provider_identity: str,
        generation: str,
    ) -> None:
        """Write native login state without mutating the running daemon."""
        if not provider_identity or not generation:
            raise ValueError("Fake Codex external login is invalid.")
        auth_path = self._codex_home / "auth.json"
        auth_path.write_bytes(managed_auth(provider_identity, generation))
        os.chmod(auth_path, 0o600)
        with self._lock:
            self._external_logins.append((provider_identity, generation))

    def perform_external_runtime_login(
        self,
        provider_identity: str,
        generation: str,
    ) -> None:
        """Change effective daemon auth and emit its official update signal."""
        self.perform_external_login(provider_identity, generation)
        access_token = _auth_access_token(
            managed_auth(provider_identity, generation)
        )
        with self._lock:
            self._active_account_id = provider_identity
            self._active_access_token = access_token
        self._broadcast(
            {
                "method": "account/updated",
                "params": {
                    "authMode": "chatgpt",
                    "planType": "pro",
                },
            }
        )

    def install_external_auth(
        self,
        provider_identity: ProviderIdentity,
        generation: str,
    ) -> None:
        """Install synthetic auth through the daemon mutation boundary."""
        account_id = str(provider_identity)
        access_token = _auth_access_token(managed_auth(account_id, generation))
        self._record_external_auth(account_id, access_token)

    def read_current_external_auth(self) -> ProviderIdentity:
        """Read the daemon's actual current external-auth identity."""
        with self._lock:
            active = self._active_account_id
            self._model_auth_read_count += 1
        if active is None:
            raise AssertionError("Fake Codex daemon has no current auth.")
        return ProviderIdentity(active)

    def connect_tui(
        self,
        socket_path: Path | None = None,
    ) -> FakeCodexTuiObserver:
        """Connect one initialized official-shaped TUI observer."""
        observer = FakeCodexTuiObserver(
            self.socket_path if socket_path is None else socket_path
        )
        observer.open()
        return observer

    def request_refresh(
        self,
        previous_account_id: str,
    ) -> FakeCodexRefreshResponse:
        """Broadcast an official-shaped refresh and return its first answer."""
        event = Event()
        with self._lock:
            if self._refresh_event is not None:
                raise AssertionError("Fake Codex refresh is already active.")
            request_id = self._next_server_request_id
            self._next_server_request_id += 1
            self._refresh_event = event
            self._refresh_request_id = request_id
            self._refresh_response = None
            recipients = tuple(self._initialized)
        message: JsonObject = {
            "id": request_id,
            "method": _EXTERNAL_REFRESH_METHOD,
            "params": {
                "previousAccountId": previous_account_id,
                "reason": "unauthorized",
            },
        }
        for recipient in recipients:
            _send(recipient, message)
        if not event.wait(_REFRESH_RESPONSE_TIMEOUT_SECONDS):
            raise AssertionError("Fake Codex refresh received no response.")
        with self._lock:
            response = self._refresh_response
            self._refresh_event = None
            self._refresh_request_id = None
            self._refresh_response = None
        if response is None:
            raise AssertionError("Fake Codex refresh response disappeared.")
        return response

    def replace(self) -> None:
        """Replace the daemon and discard process-local external auth."""
        self._stop()
        with self._lock:
            self._active_account_id = None
            self._active_access_token = None
        self._start()

    def __enter__(self) -> Self:
        """Start the Unix-WebSocket fake."""
        self._start()
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the fake and surface background handler failures."""
        del exception_type, exception, traceback
        self._stop()
        if self._failures:
            raise AssertionError(
                "Fake Codex daemon handler failed."
            ) from self._failures[0]

    def _start(self) -> None:
        with self._lock:
            self._originator = None
        control_directory = self.socket_path.parent
        control_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(control_directory, 0o700)
        server = unix_serve(
            self._handle,
            path=str(self.socket_path),
            compression=None,
            max_size=1024 * 1024,
            max_queue=16,
        )
        os.chmod(self.socket_path, 0o600)
        thread = Thread(
            target=server.serve_forever,
            daemon=True,
            name="fake-codex-daemon",
        )
        self._server = server
        self._thread = thread
        thread.start()

    def _stop(self) -> None:
        server = self._server
        thread = self._thread
        if server is None:
            return
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()
        server.shutdown()
        if thread is not None:
            thread.join(timeout=_CLIENT_TIMEOUT_SECONDS)
            if thread.is_alive():
                raise AssertionError("Fake Codex daemon did not stop.")
        self._server = None
        self._thread = None
        with self._lock:
            self._connections.clear()
            self._initialized.clear()
            self._client_names.clear()
        with suppress(FileNotFoundError):
            self.socket_path.unlink()

    def _handle(self, connection: ServerConnection) -> None:
        try:
            request = connection.request
            if request is None or request.path != "/rpc":
                connection.close()
                return
            with self._lock:
                self._connections.add(connection)
            for message in connection:
                self._dispatch(connection, message)
        except ConnectionClosed:
            return
        except BaseException as error:
            with self._lock:
                self._failures.append(error)
        finally:
            with self._lock:
                self._connections.discard(connection)
                self._initialized.discard(connection)
                self._client_names.pop(connection, None)

    def _dispatch(
        self,
        connection: ServerConnection,
        message: str | bytes,
    ) -> None:
        if not isinstance(message, str):
            raise AssertionError("Codex fake received a binary message.")
        try:
            request = decode_json_object(message.encode())
        except InvalidPayloadError:
            raise AssertionError(
                "Codex fake received a non-object message."
            ) from None
        method = request.get("method")
        if method is None:
            self._accept_refresh_response(connection, request)
        elif method == "initialize":
            self._initialize(connection, request)
        elif method == "initialized":
            with self._lock:
                self._initialized.add(connection)
        elif method == "account/login/start":
            self._install(connection, request)
        elif method == "account/read":
            self._read_account(connection, request)
        elif method == "getAuthStatus":
            self._get_auth_status(connection, request)
        elif not self._dispatch_session_qualification(
            connection,
            request,
            method,
        ):
            raise AssertionError("Codex fake received an unsupported method.")

    def _dispatch_session_qualification(
        self,
        connection: ServerConnection,
        request: JsonObject,
        method: object,
    ) -> bool:
        if method == "config/read":
            self._read_config(connection, request)
            return True
        if method == "modelProvider/capabilities/read":
            self._read_model_capabilities(connection, request)
            return True
        return False

    def _read_config(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        if request.get("params") != {"includeLayers": True}:
            raise AssertionError("Codex fake config read is invalid.")
        result = self._session.read_config()
        _send(
            connection,
            {
                "id": _request_id(request),
                "result": result,
            },
        )

    def _read_model_capabilities(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        if request.get("params") != {}:
            raise AssertionError(
                "Codex fake model-capability read is invalid."
            )
        result = self._session.read_model_capabilities()
        _send(
            connection,
            {
                "id": _request_id(request),
                "result": result,
            },
        )

    def _initialize(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        params = request.get("params")
        request_id = _request_id(request)
        client_info = (
            None if not isinstance(params, dict) else params.get("clientInfo")
        )
        name = (
            None
            if not isinstance(client_info, dict)
            else client_info.get("name")
        )
        if (
            not isinstance(params, dict)
            or params.get("capabilities") != {"experimentalApi": True}
            or not isinstance(name, str)
            or not name
        ):
            raise AssertionError("Codex fake initialization is invalid.")
        with self._lock:
            if self._originator is None:
                self._originator = name
            originator = self._originator
            self._client_names[connection] = name
        _send(
            connection,
            {
                "id": request_id,
                "result": {
                    "codexHome": str(self._codex_home),
                    "platformFamily": "unix",
                    "platformOs": (
                        "macos" if sys.platform == "darwin" else "linux"
                    ),
                    "userAgent": (
                        f"{originator}/{self._version} (fake 1; x86_64)"
                    ),
                },
            },
        )

    def _accept_refresh_response(
        self,
        connection: ServerConnection,
        response: JsonObject,
    ) -> None:
        with self._lock:
            event = self._refresh_event
            request_id = self._refresh_request_id
            responder = self._client_names.get(connection)
            if (
                event is None
                or request_id is None
                or event.is_set()
                or response.get("id") != request_id
                or responder is None
            ):
                return
            result = response.get("result")
            error = response.get("error")
            if set(response) == {"id", "result"} and isinstance(result, dict):
                account_id, access_token = self._refresh_authority(result)
                self._active_account_id = account_id
                self._active_access_token = access_token
                observed = FakeCodexRefreshResponse(
                    responder,
                    account_id,
                    None,
                )
            elif (
                set(response) == {"id", "error"}
                and isinstance(error, dict)
                and error
                == {
                    "code": _EXTERNAL_REFRESH_ERROR_CODE,
                    "message": _EXTERNAL_REFRESH_ERROR_MESSAGE,
                }
            ):
                observed = FakeCodexRefreshResponse(
                    responder,
                    None,
                    _EXTERNAL_REFRESH_ERROR_CODE,
                )
            else:
                raise AssertionError(
                    "Fake Codex refresh response is malformed."
                )
            self._refresh_response = observed
            event.set()

    @staticmethod
    def _refresh_authority(result: JsonObject) -> tuple[str, str]:
        access_token = result.get("accessToken")
        account_id = result.get("chatgptAccountId")
        if (
            set(result)
            != {"accessToken", "chatgptAccountId", "chatgptPlanType"}
            or not isinstance(access_token, str)
            or not isinstance(account_id, str)
            or result.get("chatgptPlanType") != "pro"
            or _token_account_id(access_token) != account_id
        ):
            raise AssertionError("Fake Codex refresh result is inconsistent.")
        return account_id, access_token

    def _install(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        params = request.get("params")
        if not isinstance(params, dict):
            raise AssertionError("Codex fake projection is malformed.")
        access_token = params.get("accessToken")
        account_id = params.get("chatgptAccountId")
        plan = params.get("chatgptPlanType")
        if (
            params.get("type") != "chatgptAuthTokens"
            or not isinstance(access_token, str)
            or not isinstance(account_id, str)
            or not isinstance(plan, str)
            or _token_account_id(access_token) != account_id
        ):
            raise AssertionError("Codex fake projection is inconsistent.")
        self._record_external_auth(account_id, access_token)
        _send(
            connection,
            {
                "id": _request_id(request),
                "result": {"type": "chatgptAuthTokens"},
            },
        )
        self._broadcast(
            {
                "method": "account/login/completed",
                "params": {
                    "error": None,
                    "loginId": None,
                    "success": True,
                },
            }
        )
        self._broadcast(
            {
                "method": "account/updated",
                "params": {
                    "authMode": "chatgptAuthTokens",
                    "planType": plan,
                },
            }
        )
        with self._lock:
            pause_install = self._pause_install
            self._pause_install = False
        if pause_install:
            self._install_paused.set()
            if not self._resume_install.wait(
                _INSTALL_HANDSHAKE_TIMEOUT_SECONDS
            ):
                raise AssertionError("Fake Codex install was not resumed.")
            self._install_resumed.set()

    def _record_external_auth(
        self,
        account_id: str,
        access_token: str,
    ) -> None:
        """Record one external-auth installation used by all fake paths."""
        with self._lock:
            self._active_account_id = account_id
            self._active_access_token = access_token
            self._installed_account_ids.append(account_id)

    def _read_account(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        params = request.get("params")
        if params != {"refreshToken": False}:
            raise AssertionError("Codex fake account read is invalid.")
        with self._lock:
            active = self._active_account_id
            if active is not None:
                self._ready_account_read_count += 1
        account: JsonObject | None = (
            None
            if active is None
            else {
                "email": None,
                "planType": "pro",
                "type": "chatgpt",
            }
        )
        _send(
            connection,
            {
                "id": _request_id(request),
                "result": {
                    "account": account,
                    "requiresOpenaiAuth": True,
                },
            },
        )

    def _get_auth_status(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        if request.get("params") != {
            "includeToken": True,
            "refreshToken": False,
        }:
            raise AssertionError("Codex fake auth-status request is invalid.")
        with self._lock:
            self._auth_status_read_count += 1
            access_token = self._active_access_token
            external_auth = access_token is not None
        if access_token is None:
            auth_path = self._codex_home / "auth.json"
            access_token = (
                None
                if not auth_path.is_file()
                else _auth_access_token(auth_path.read_bytes())
            )
        _send(
            connection,
            {
                "id": _request_id(request),
                "result": {
                    "authMethod": (
                        None
                        if access_token is None
                        else (
                            CodexAuthMode.CHATGPT_AUTH_TOKENS.value
                            if external_auth
                            else CodexAuthMode.CHATGPT.value
                        )
                    ),
                    "authToken": access_token,
                    "requiresOpenaiAuth": True,
                },
            },
        )

    def _broadcast(self, message: JsonObject) -> None:
        message["emittedAtMs"] = _EMITTED_AT_MILLISECONDS
        with self._lock:
            recipients = tuple(self._initialized)
        for recipient in recipients:
            _send(recipient, message)


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
        _send(
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
        _send(connection, {"method": "initialized"})

    def wait_for_account_update(self) -> None:
        """Wait for one external-auth account update."""
        connection = self._connection
        if connection is None:
            raise AssertionError("Fake Codex observer is not open.")
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
        connection = self._connection
        if connection is None:
            raise AssertionError("Fake Codex observer is not open.")
        _send(
            connection,
            {"id": request_id, "method": method, "params": params},
        )

    def receive(self) -> JsonObject:
        """Receive one complete fake TUI frame."""
        connection = self._connection
        if connection is None:
            raise AssertionError("Fake Codex observer is not open.")
        return _receive(connection)

    def receive_optional(self, timeout_seconds: float) -> JsonObject | None:
        """Return a frame when available within one bounded wait."""
        connection = self._connection
        if connection is None:
            raise AssertionError("Fake Codex observer is not open.")
        try:
            return _receive(connection, timeout_seconds=timeout_seconds)
        except TimeoutError:
            return None

    def close(self) -> None:
        """Close this observer."""
        connection = self._connection
        self._connection = None
        if connection is not None:
            connection.close()


def _send(
    connection: ServerConnection | ClientConnection,
    message: JsonObject,
) -> None:
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


def _request_id(request: JsonObject) -> int:
    request_id = request.get("id")
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id < 1
    ):
        raise AssertionError("Codex fake request ID is invalid.")
    return request_id


def _auth_access_token(payload: bytes) -> str:
    try:
        auth = decode_json_object(payload)
    except InvalidPayloadError:
        raise AssertionError("Fake Codex auth payload is malformed.") from None
    tokens = auth.get("tokens")
    access_token = (
        None if not isinstance(tokens, dict) else tokens.get("access_token")
    )
    if not isinstance(access_token, str):
        raise AssertionError("Fake Codex access token is unavailable.")
    return access_token


def _token_account_id(token: str) -> str | None:
    parts = token.split(".")
    if len(parts) != _JWT_PARTS:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = decode_json_object(base64.urlsafe_b64decode(payload))
    except binascii.Error, InvalidPayloadError, ValueError:
        return None
    auth = decoded.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) else None
