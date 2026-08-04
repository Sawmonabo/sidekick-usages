"""Real Unix-WebSocket fake for the official shared Codex daemon."""

import os
import sys
from contextlib import suppress
from pathlib import Path
from threading import Event, RLock, Thread
from types import TracebackType
from typing import Self

from websockets.exceptions import ConnectionClosed
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
from tests.fakes.codex.app_server.session import (
    FakeCodexSession,
    FakeCodexTuiObserver,
    send_fake_codex_message,
)
from tests.fakes.codex.auth import codex_token_account_id, managed_auth

_CLIENT_TIMEOUT_SECONDS = 5.0
_INSTALL_HANDSHAKE_TIMEOUT_SECONDS = 30.0
_REFRESH_RESPONSE_TIMEOUT_SECONDS = CODEX_CALLBACK_RESPONSE_SECONDS + 2.0
_EXTERNAL_REFRESH_ERROR_CODE = -32000
_EXTERNAL_REFRESH_ERROR_MESSAGE = "external auth refresh unavailable"
_EXTERNAL_REFRESH_METHOD = "account/chatgptAuthTokens/refresh"
_CONTROL_DIRECTORY_NAME = "app-server-control"
_CONTROL_SOCKET_NAME = "app-server-control.sock"
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
        mcp_server_names: tuple[str, ...] = (),
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
            mcp_server_names=mcp_server_names,
        )
        self._lock = RLock()
        self._server: Server | None = None
        self._thread: Thread | None = None
        self._retired_listeners: list[tuple[Server, Thread]] = []
        self._connections: set[ServerConnection] = set()
        self._initialized: set[ServerConnection] = set()
        self._client_names: dict[ServerConnection, str] = {}
        self._loaded_threads: dict[ServerConnection, set[str]] = {}
        self._relay_start_request_ids: list[int] = []
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
        self._mcp_reload_count = 0
        self._suppress_next_mcp_reload_status = False
        self._mutation_events: list[str] = []

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

    def emit_mcp_status(
        self,
        thread_id: str,
        name: str,
        status: str,
    ) -> None:
        """Send one lifecycle event only to loaded-thread subscribers."""
        with self._lock:
            subscribers = tuple(
                connection
                for connection, thread_ids in self._loaded_threads.items()
                if thread_id in thread_ids
            )
        notification: JsonObject = {
            "emittedAtMs": _EMITTED_AT_MILLISECONDS,
            "method": "mcpServer/startupStatus/updated",
            "params": {
                "threadId": thread_id,
                "name": name,
                "status": status,
            },
        }
        for connection in subscribers:
            send_fake_codex_message(connection, notification)

    def emit_mcp_statuses(
        self,
        observer: FakeCodexTuiObserver,
        thread_ids: tuple[str, ...],
        statuses: tuple[str, ...],
    ) -> None:
        """Send and receive exact subscriber-scoped lifecycle events."""
        for status in statuses:
            for thread_id in thread_ids:
                self.emit_mcp_status(thread_id, "synthetic", status)
                for _message_index in range(16):
                    if observer.receive().get("method") == (
                        "mcpServer/startupStatus/updated"
                    ):
                        break
                else:
                    raise AssertionError("Fake Codex MCP event was not seen.")

    @property
    def model_auth_read_count(self) -> int:
        """Return synthetic model reads of current external auth."""
        with self._lock:
            return self._model_auth_read_count

    @property
    def mcp_status_thread_ids(self) -> tuple[str, ...]:
        return self._session.mcp_status_thread_ids

    @property
    def mcp_reload_count(self) -> int:
        """Return correlated resident MCP reload requests."""
        with self._lock:
            return self._mcp_reload_count

    @property
    def resident_connection_ids(self) -> tuple[int, ...]:
        """Return exact initialized resident broker connection identities."""
        with self._lock:
            return tuple(
                sorted(
                    id(connection)
                    for connection, name in self._client_names.items()
                    if name == "sidekick_usages"
                )
            )

    @property
    def mutation_events(self) -> tuple[str, ...]:
        """Return reload and auth-mutation requests in received order."""
        with self._lock:
            return tuple(self._mutation_events)

    def suppress_next_mcp_reload_status(self) -> None:
        """Suppress one reload's lifecycle notifications."""
        with self._lock:
            self._suppress_next_mcp_reload_status = True

    @property
    def config_read_count(self) -> int:
        """Return effective resident-config readbacks."""
        return self._session.config_read_count

    @property
    def external_logins(self) -> tuple[tuple[str, str], ...]:
        """Return deliberate native-login observations."""
        with self._lock:
            return tuple(self._external_logins)

    @property
    def relay_start_request_ids(self) -> tuple[int, ...]:
        """Return provider starts in their exact received order."""
        with self._lock:
            return tuple(self._relay_start_request_ids)

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
            send_fake_codex_message(recipient, message)
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

    def replace_socket_listener(self) -> None:
        server = self._server
        thread = self._thread
        if server is None or thread is None:
            raise AssertionError("Fake Codex daemon is not running.")
        self.socket_path.unlink()
        self._retired_listeners.append((server, thread))
        self._start_listener()

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
        self._start_listener()

    def _start_listener(self) -> None:
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
        if thread is None:
            raise AssertionError("Fake Codex listener thread disappeared.")
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            connection.close()
        listeners = [(server, thread)]
        listeners.extend(self._retired_listeners)
        for listener, _listener_thread in listeners:
            listener.shutdown()
        for _listener, listener_thread in listeners:
            listener_thread.join(timeout=_CLIENT_TIMEOUT_SECONDS)
            if listener_thread.is_alive():
                raise AssertionError("Fake Codex daemon did not stop.")
        self._server = None
        self._thread = None
        self._retired_listeners.clear()
        with self._lock:
            self._connections.clear()
            self._initialized.clear()
            self._client_names.clear()
            self._loaded_threads.clear()
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
                self._loaded_threads.pop(connection, None)

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
        elif self._dispatch_account(
            connection, request, method
        ) or self._dispatch_relay(connection, request, method):
            return
        elif not self._dispatch_session_qualification(
            connection,
            request,
            method,
        ):
            raise AssertionError("Codex fake received an unsupported method.")

    def _dispatch_account(
        self,
        connection: ServerConnection,
        request: JsonObject,
        method: object,
    ) -> bool:
        if method == "account/login/start":
            self._install(connection, request)
        elif method == "account/read":
            self._read_account(connection, request)
        elif method == "getAuthStatus":
            self._get_auth_status(connection, request)
        else:
            return False
        return True

    def _dispatch_relay(
        self,
        connection: ServerConnection,
        request: JsonObject,
        method: object,
    ) -> bool:
        if method == "turn/start":
            self._complete_relay_start(connection, request, realtime=False)
            return True
        if method == "thread/realtime/start":
            self._complete_relay_start(connection, request, realtime=True)
            return True
        if method == "thread/resume":
            self._resume_relay_thread(connection, request)
            return True
        return False

    def _complete_relay_start(
        self,
        connection: ServerConnection,
        request: JsonObject,
        *,
        realtime: bool,
    ) -> None:
        request_id = _request_id(request)
        params = request.get("params")
        thread_id = (
            None if not isinstance(params, dict) else params.get("threadId")
        )
        if not isinstance(thread_id, str) or not thread_id:
            raise AssertionError("Codex fake relay thread is invalid.")
        with self._lock:
            self._relay_start_request_ids.append(request_id)
            self._loaded_threads.setdefault(connection, set()).add(thread_id)
        if realtime:
            send_fake_codex_message(
                connection,
                {"id": request_id, "result": {}},
            )
            methods = ("thread/realtime/started", "thread/realtime/closed")
            turn = None
        else:
            turn: JsonObject = {"id": f"turn-{request_id}"}
            response: JsonObject = {
                "id": request_id,
                "result": {"turn": turn},
            }
            send_fake_codex_message(connection, response)
            methods = ("turn/started", "turn/completed")
        for method in methods:
            if turn is None:
                notification: JsonObject = {
                    "emittedAtMs": _EMITTED_AT_MILLISECONDS,
                    "method": method,
                    "params": {"threadId": thread_id},
                }
                send_fake_codex_message(connection, notification)
            else:
                turn_notification: JsonObject = {
                    "emittedAtMs": _EMITTED_AT_MILLISECONDS,
                    "method": method,
                    "params": {"threadId": thread_id, "turn": turn},
                }
                send_fake_codex_message(connection, turn_notification)

    def _resume_relay_thread(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        params = request.get("params")
        thread_id = (
            None if not isinstance(params, dict) else params.get("threadId")
        )
        if not isinstance(thread_id, str) or not thread_id:
            raise AssertionError("Codex fake resumed thread is invalid.")
        with self._lock:
            self._loaded_threads.setdefault(connection, set()).add(thread_id)
        send_fake_codex_message(
            connection,
            {
                "id": _request_id(request),
                "result": {"thread": {"id": thread_id}},
            },
        )

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
        if method == "mcpServerStatus/list":
            params = request.get("params")
            if not isinstance(params, dict):
                raise AssertionError("Fake Codex MCP status read is invalid.")
            send_fake_codex_message(
                connection,
                {
                    "id": _request_id(request),
                    "result": self._session.read_mcp_status(params),
                },
            )
            return True
        if method == "config/mcpServer/reload":
            self._reload_mcp_servers(connection, request)
            return True
        return False

    def _reload_mcp_servers(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        if "params" in request:
            raise AssertionError("Fake Codex MCP reload params were present.")
        with self._lock:
            self._mcp_reload_count += 1
            self._mutation_events.append("reload")
            suppress_status = self._suppress_next_mcp_reload_status
            self._suppress_next_mcp_reload_status = False
        send_fake_codex_message(
            connection,
            {"id": _request_id(request), "result": {}},
        )
        if not suppress_status:
            self._emit_mcp_lifecycle("ready")

    def _read_config(
        self,
        connection: ServerConnection,
        request: JsonObject,
    ) -> None:
        if request.get("params") != {"includeLayers": True}:
            raise AssertionError("Codex fake config read is invalid.")
        result = self._session.read_config()
        send_fake_codex_message(
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
        send_fake_codex_message(
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
        send_fake_codex_message(
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
            or codex_token_account_id(access_token) != account_id
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
            or codex_token_account_id(access_token) != account_id
        ):
            raise AssertionError("Codex fake projection is inconsistent.")
        self._record_external_auth(account_id, access_token)
        send_fake_codex_message(
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
            self._mutation_events.append("install")
        self._emit_mcp_lifecycle("ready")

    def _emit_mcp_lifecycle(self, status: str) -> None:
        with self._lock:
            routes = tuple(
                (connection, tuple(sorted(thread_ids)))
                for connection, thread_ids in self._loaded_threads.items()
            )
            names = self._session.mcp_server_names
        for connection, thread_ids in routes:
            for thread_id in thread_ids:
                for name in names:
                    send_fake_codex_message(
                        connection,
                        {
                            "emittedAtMs": _EMITTED_AT_MILLISECONDS,
                            "method": "mcpServer/startupStatus/updated",
                            "params": {
                                "threadId": thread_id,
                                "name": name,
                                "status": status,
                            },
                        },
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
        send_fake_codex_message(
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
        send_fake_codex_message(
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
            send_fake_codex_message(recipient, message)


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
