"""Real Unix-WebSocket fake for the official shared Codex daemon."""

import base64
import binascii
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from threading import RLock, Thread
from types import TracebackType
from typing import Self

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, unix_connect
from websockets.sync.server import (
    Server,
    ServerConnection,
    unix_serve,
)

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization.json import JsonObject, decode_json_object

_CLIENT_TIMEOUT_SECONDS = 5.0
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
    ) -> None:
        self._codex_home = codex_home
        self._version = app_server_version
        self._lock = RLock()
        self._server: Server | None = None
        self._thread: Thread | None = None
        self._connections: set[ServerConnection] = set()
        self._initialized: set[ServerConnection] = set()
        self._installed_account_ids: list[str] = []
        self._active_account_id: str | None = None
        self._originator: str | None = None
        self._ready_account_read_count = 0
        self._failures: list[BaseException] = []

    @property
    def socket_path(self) -> Path:
        """Return the official default control socket path."""
        return (
            self._codex_home
            / _CONTROL_DIRECTORY_NAME
            / _CONTROL_SOCKET_NAME
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

    def connect_tui(self) -> FakeCodexTuiObserver:
        """Connect one initialized official-shaped TUI observer."""
        observer = FakeCodexTuiObserver(self.socket_path)
        observer.open()
        return observer

    def replace(self) -> None:
        """Replace the daemon and discard process-local external auth."""
        self._stop()
        with self._lock:
            self._active_account_id = None
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
        if method == "initialize":
            self._initialize(connection, request)
            return
        if method == "initialized":
            with self._lock:
                self._initialized.add(connection)
            return
        if method == "account/login/start":
            self._install(connection, request)
            return
        if method == "account/read":
            self._read_account(connection, request)
            return
        raise AssertionError("Codex fake received an unsupported method.")

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
            or params.get("capabilities")
            != {"experimentalApi": True}
            or not isinstance(name, str)
            or not name
        ):
            raise AssertionError("Codex fake initialization is invalid.")
        with self._lock:
            if self._originator is None:
                self._originator = name
            originator = self._originator
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
        del access_token
        with self._lock:
            self._active_account_id = account_id
            self._installed_account_ids.append(account_id)
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
            if (
                message.get("method") == "account/updated"
                and message.get("params")
                == {
                    "authMode": "chatgptAuthTokens",
                    "planType": "pro",
                }
            ):
                return
        raise AssertionError("Fake Codex observer saw no account update.")

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


def _receive(connection: ClientConnection) -> JsonObject:
    message = connection.recv(timeout=_CLIENT_TIMEOUT_SECONDS)
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
