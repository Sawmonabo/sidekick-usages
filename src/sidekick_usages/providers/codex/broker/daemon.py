"""Official shared Codex daemon lifecycle and socket qualification."""

import os
import socket
import stat
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.platform.peer import (
    OperatingSystemPeerVerifier,
    PeerVerificationError,
)
from sidekick_usages.platform.types import PeerVerifier
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    verify_codex_executable,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexVersion,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
    run_bounded_codex_command,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import (
    CodexDaemonAuthority,
    CodexDaemonLifecycle,
    CodexFilesystemIdentity,
)
from sidekick_usages.providers.codex.broker.types import (
    CodexBrokerFailure,
    CodexDaemonStatus,
)
from sidekick_usages.serialization.json import (
    JsonObject,
    decode_json_object,
)

CONTROL_DIRECTORY_NAME = "app-server-control"
CONTROL_SOCKET_NAME = "app-server-control.sock"
MANAGED_CODEX_COMPONENTS = ("packages", "standalone", "current", "codex")
_CONTROL_DIRECTORY_MODE = 0o700
_CONTROL_SOCKET_MODE = 0o600
_DAEMON_START_TIMEOUT_SECONDS = 100.0
_DAEMON_RESTART_TIMEOUT_SECONDS = 100.0
_DAEMON_VERSION_TIMEOUT_SECONDS = 10.0
_DAEMON_CONNECT_TIMEOUT_SECONDS = 5.0
_MAXIMUM_LIFECYCLE_OUTPUT_BYTES = 4096
_LIFECYCLE_BASE_FIELDS = frozenset(
    {
        "appServerVersion",
        "backend",
        "cliVersion",
        "managedCodexPath",
        "managedCodexVersion",
        "socketPath",
        "status",
    }
)
_LIFECYCLE_FIELDS = {
    CodexDaemonStatus.STARTED: _LIFECYCLE_BASE_FIELDS | {"pid"},
    CodexDaemonStatus.ALREADY_RUNNING: _LIFECYCLE_BASE_FIELDS,
    CodexDaemonStatus.RESTARTED: _LIFECYCLE_BASE_FIELDS | {"pid"},
    CodexDaemonStatus.RUNNING: _LIFECYCLE_BASE_FIELDS,
}
_ATTACHABLE_UNMANAGED_STATUSES = frozenset(
    {
        CodexDaemonStatus.ALREADY_RUNNING,
        CodexDaemonStatus.RUNNING,
    }
)


class CodexDaemonManager:
    """Start and qualify one official daemon for an exact native home."""

    def __init__(
        self,
        capabilities: CodexAppServerCapabilities,
        native_home: Path,
        *,
        environment: Mapping[str, str] | None = None,
        expected_user_id: int | None = None,
        peer_verifier: PeerVerifier | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if (
            sys.platform == "win32"
            or not hasattr(socket, "AF_UNIX")
            or not hasattr(socket, "MSG_DONTWAIT")
            or not native_home.is_absolute()
        ):
            raise CodexBrokerError(CodexBrokerFailure.PLATFORM_UNSUPPORTED)
        try:
            resolved_home = native_home.resolve(strict=True)
        except OSError, ValueError:
            raise CodexBrokerError(
                CodexBrokerFailure.INSTALLATION_UNSUPPORTED
            ) from None
        if not resolved_home.is_dir():
            raise CodexBrokerError(CodexBrokerFailure.INSTALLATION_UNSUPPORTED)
        self._capabilities = capabilities
        self._native_home = resolved_home
        self._environment = None if environment is None else dict(environment)
        self._expected_user_id = (
            os.geteuid() if expected_user_id is None else expected_user_id
        )
        if self._expected_user_id < 0:
            raise ValueError("Expected Codex daemon user ID is invalid.")
        self._peer_verifier = (
            OperatingSystemPeerVerifier(self._expected_user_id)
            if peer_verifier is None
            else peer_verifier
        )
        self._cancelled = cancelled

    @property
    def native_home(self) -> Path:
        """Return the exact native home owned by the shared daemon."""
        return self._native_home

    @property
    def socket_path(self) -> Path:
        """Return the only accepted official daemon socket."""
        return self._native_home / CONTROL_DIRECTORY_NAME / CONTROL_SOCKET_NAME

    def cancellation_requested(self) -> bool:
        """Return whether the owning broker requested shutdown."""
        return self._cancelled is not None and self._cancelled()

    def verify_executable(self) -> None:
        """Require the stable launcher to retain this runtime target."""
        verify_codex_executable(self._capabilities.executable)

    def ensure_running(self) -> CodexDaemonAuthority:
        """Idempotently start, inspect, and qualify the official daemon."""
        self.verify_executable()
        started = self._run_lifecycle(
            "start",
            timeout_seconds=_DAEMON_START_TIMEOUT_SECONDS,
        )
        if started.status not in {
            CodexDaemonStatus.STARTED,
            CodexDaemonStatus.ALREADY_RUNNING,
        }:
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        self._require_current_installation(started)
        current_version = self._capabilities.executable.version
        if started.app_server_version != current_version:
            if not started.managed:
                raise CodexBrokerError(CodexBrokerFailure.DAEMON_UNMANAGED)
            if (
                started.status is not CodexDaemonStatus.ALREADY_RUNNING
                or started.app_server_version > current_version
            ):
                raise CodexBrokerError(CodexBrokerFailure.VERSION_UNSUPPORTED)
            restarted = self._run_lifecycle(
                "restart",
                timeout_seconds=_DAEMON_RESTART_TIMEOUT_SECONDS,
            )
            if (
                restarted.status is not CodexDaemonStatus.RESTARTED
                or not restarted.managed
            ):
                raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
            self._require_current_version(restarted)
        running = self._run_lifecycle(
            "version",
            timeout_seconds=_DAEMON_VERSION_TIMEOUT_SECONDS,
        )
        if running.status is not CodexDaemonStatus.RUNNING:
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        self._require_current_version(running)
        self.verify_executable()
        control_directory, control_socket = self._qualify_socket()
        return CodexDaemonAuthority(
            lifecycle=running,
            executable=self._capabilities.executable,
            control_directory=control_directory,
            control_socket=control_socket,
        )

    def connect(self, authority: CodexDaemonAuthority) -> socket.socket:
        """Connect only while the qualified socket identity is unchanged."""
        self.revalidate(authority)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(_DAEMON_CONNECT_TIMEOUT_SECONDS)
            connection.connect(str(self.socket_path))
            self._peer_verifier.verify(connection)
            connection.settimeout(None)
        except PeerVerificationError:
            connection.close()
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_UNSAFE) from None
        except OSError:
            connection.close()
            raise CodexBrokerError(
                CodexBrokerFailure.CONNECTION_FAILED
            ) from None
        return connection

    def revalidate(self, authority: CodexDaemonAuthority) -> None:
        """Require the exact qualified directory and socket to remain."""
        control_directory, control_socket = self._qualify_socket()
        if (
            authority.lifecycle.socket_path != self.socket_path
            or authority.control_directory != control_directory
            or authority.control_socket != control_socket
        ):
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)

    def _run_lifecycle(
        self,
        operation: str,
        *,
        timeout_seconds: float,
    ) -> CodexDaemonLifecycle:
        self.verify_executable()
        try:
            output = run_bounded_codex_command(
                (
                    str(self._capabilities.executable.provenance.path),
                    "app-server",
                    "daemon",
                    operation,
                ),
                minimal_codex_environment(
                    self._environment,
                    codex_home=self._native_home,
                ),
                timeout_seconds=timeout_seconds,
                maximum_output_bytes=_MAXIMUM_LIFECYCLE_OUTPUT_BYTES,
                working_directory=self._native_home,
                cancelled=self._cancelled,
            )
        except CodexAppServerError:
            raise CodexBrokerError(
                CodexBrokerFailure.LIFECYCLE_FAILED
            ) from None
        self.verify_executable()
        return self._decode_lifecycle(output)

    def _decode_lifecycle(self, output: bytes) -> CodexDaemonLifecycle:
        if (
            not output.endswith(b"\n")
            or b"\n" in output[:-1]
            or not output[:-1]
        ):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        try:
            payload = decode_json_object(output[:-1])
        except InvalidPayloadError:
            raise CodexBrokerError(
                CodexBrokerFailure.LIFECYCLE_MALFORMED
            ) from None
        status = self._status(payload)
        expected_fields = _LIFECYCLE_FIELDS[status]
        actual_fields = set(payload)
        backend_missing = actual_fields == expected_fields - {"backend"}
        if backend_missing and status not in _ATTACHABLE_UNMANAGED_STATUSES:
            raise CodexBrokerError(CodexBrokerFailure.DAEMON_UNMANAGED)
        if not backend_missing and actual_fields != expected_fields:
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        if not backend_missing and payload.get("backend") != "pid":
            raise CodexBrokerError(CodexBrokerFailure.DAEMON_UNMANAGED)
        if status in {
            CodexDaemonStatus.STARTED,
            CodexDaemonStatus.RESTARTED,
        } and not _valid_process_id(payload.get("pid")):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        managed_path = self._managed_path(payload)
        socket_path = self._reported_socket(payload)
        return CodexDaemonLifecycle(
            status=status,
            app_server_version=self._reported_version(
                payload,
                "appServerVersion",
            ),
            cli_version=self._reported_version(payload, "cliVersion"),
            managed_version=self._reported_version(
                payload,
                "managedCodexVersion",
            ),
            managed=not backend_missing,
            managed_executable=managed_path,
            socket_path=socket_path,
        )

    def _reported_version(
        self,
        payload: JsonObject,
        name: str,
    ) -> CodexVersion:
        value = payload.get(name)
        if not isinstance(value, str):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        try:
            return CodexVersion.parse(value)
        except ValueError:
            raise CodexBrokerError(
                CodexBrokerFailure.LIFECYCLE_MALFORMED
            ) from None

    def _require_current_version(
        self,
        lifecycle: CodexDaemonLifecycle,
    ) -> None:
        self._require_current_installation(lifecycle)
        current = self._capabilities.executable.version
        if lifecycle.app_server_version != current:
            raise CodexBrokerError(CodexBrokerFailure.VERSION_UNSUPPORTED)

    def _require_current_installation(
        self,
        lifecycle: CodexDaemonLifecycle,
    ) -> None:
        current = self._capabilities.executable.version
        if (
            lifecycle.cli_version != current
            or lifecycle.managed_version != current
        ):
            raise CodexBrokerError(CodexBrokerFailure.VERSION_UNSUPPORTED)

    def _status(self, payload: JsonObject) -> CodexDaemonStatus:
        value = payload.get("status")
        if not isinstance(value, str):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        try:
            return CodexDaemonStatus(value)
        except ValueError:
            raise CodexBrokerError(
                CodexBrokerFailure.LIFECYCLE_MALFORMED
            ) from None

    def _managed_path(self, payload: JsonObject) -> Path:
        value = payload.get("managedCodexPath")
        if not isinstance(value, str):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        managed_path = Path(value)
        expected_path = self._native_home.joinpath(*MANAGED_CODEX_COMPONENTS)
        if managed_path != expected_path:
            raise CodexBrokerError(CodexBrokerFailure.INSTALLATION_UNSUPPORTED)
        try:
            resolved = managed_path.resolve(strict=True)
        except OSError, ValueError:
            raise CodexBrokerError(
                CodexBrokerFailure.INSTALLATION_UNSUPPORTED
            ) from None
        if resolved != self._capabilities.executable.provenance.path:
            raise CodexBrokerError(CodexBrokerFailure.INSTALLATION_UNSUPPORTED)
        return managed_path

    def _reported_socket(self, payload: JsonObject) -> Path:
        value = payload.get("socketPath")
        if not isinstance(value, str):
            raise CodexBrokerError(CodexBrokerFailure.LIFECYCLE_MALFORMED)
        socket_path = Path(value)
        if socket_path != self.socket_path:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_UNSAFE)
        return socket_path

    def _qualify_socket(
        self,
    ) -> tuple[CodexFilesystemIdentity, CodexFilesystemIdentity]:
        control_directory_path = self.socket_path.parent
        try:
            directory_status = control_directory_path.lstat()
            socket_status = self.socket_path.lstat()
        except OSError:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_UNSAFE) from None
        if (
            not stat.S_ISDIR(directory_status.st_mode)
            or stat.S_ISLNK(directory_status.st_mode)
            or directory_status.st_uid != self._expected_user_id
            or stat.S_IMODE(directory_status.st_mode)
            != _CONTROL_DIRECTORY_MODE
            or not stat.S_ISSOCK(socket_status.st_mode)
            or stat.S_ISLNK(socket_status.st_mode)
            or socket_status.st_uid != self._expected_user_id
            or stat.S_IMODE(socket_status.st_mode) != _CONTROL_SOCKET_MODE
        ):
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_UNSAFE)
        return (
            _filesystem_identity(directory_status),
            _filesystem_identity(socket_status),
        )


def _filesystem_identity(
    file_status: os.stat_result,
) -> CodexFilesystemIdentity:
    return CodexFilesystemIdentity(
        device=file_status.st_dev,
        inode=file_status.st_ino,
        owner_user_id=file_status.st_uid,
        mode=stat.S_IMODE(file_status.st_mode),
    )


def _valid_process_id(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value > 0
