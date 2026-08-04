"""Pipe lifecycle for one qualified official structured Claude engine."""

import os
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from itertools import pairwise
from pathlib import Path
from threading import Thread
from typing import NoReturn
from uuid import UUID, uuid4

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import (
    qualify_executable,
    verify_executable,
)
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    inspect_claude_executable_artifact,
    verify_claude_executable,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeVersion,
)
from sidekick_usages.providers.claude.process import (
    MAX_CLAUDE_CONTROL_FRAME_BYTES,
    launch_piped_claude_command,
)
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
    decode_control_response_request_id,
    decode_oauth_update_rejection,
    decode_oauth_update_success,
    encode_invalid_oauth_probe,
    encode_oauth_update,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredCapability,
    ClaudeStructuredEngine,
    ClaudeStructuredEngineFactory,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
)

_READ_CHUNK_BYTES = 8192
CLAUDE_STRUCTURED_ARGUMENTS = (
    "--print",
    "--verbose",
    "--input-format",
    "stream-json",
    "--output-format",
    "stream-json",
    "--include-partial-messages",
    "--include-hook-events",
    "--permission-prompt-tool",
    "stdio",
)
_CONTINUE_ARGUMENTS = frozenset({("-c",), ("--continue",)})
_RESUME_OPTIONS = frozenset({"-r", "--resume"})
_AUTH_ARGUMENTS = frozenset({"/login", "/logout", "login", "logout"})
_RESUME_FORK_ARGUMENT_COUNT = 3
CLAUDE_STRUCTURED_VERSION = ClaudeVersion(2, 1, 221)
CLAUDE_STRUCTURED_ARTIFACT_SIZE = 288_705_544
CLAUDE_STRUCTURED_ARTIFACT_SHA256 = (
    "60db8e88d42c24b5199c92cfd56ec88370c510c3789c6f364af748354f087ada"
)
CLAUDE_STRUCTURED_EMBEDDED_BUILD_TIME = "2026-08-03T03:19:26Z"
CLAUDE_STRUCTURED_EMBEDDED_GIT_SHA = "6efaf12e8b43dc7dbe50e0955c76dc4174a15876"
CLAUDE_STRUCTURED_PROBE_CANARY = "sidekick-invalid-oauth-capability-canary"
_CLAUDE_STRUCTURED_VARIABLE_ALLOWLIST = (
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
_CLAUDE_STRUCTURED_ARTIFACT_MARKERS = (
    CLAUDE_STRUCTURED_EMBEDDED_BUILD_TIME.encode(),
    CLAUDE_STRUCTURED_EMBEDDED_GIT_SHA.encode(),
    *(variable.encode() for variable in _CLAUDE_STRUCTURED_VARIABLE_ALLOWLIST),
)
_PROBE_TIMEOUT_SECONDS = 5.0
_NETWORK_NAMESPACE_EXECUTABLE = Path("/usr/bin/unshare")
_NETWORK_NAMESPACE_ARGUMENTS = (
    "--user",
    "--map-current-user",
    "--net",
    "--",
)

type ClaudeStructuredArtifactReader = Callable[
    [ClaudeExecutable, tuple[bytes, ...]],
    tuple[str, frozenset[bytes]],
]


def _new_request_id() -> RequestId:
    return RequestId(str(uuid4()))


def qualify_claude_structured_capability(
    executable: ClaudeExecutable,
    host: HostPlatform,
    environment: Mapping[str, str],
    *,
    working_directory: Path,
    engine_factory: ClaudeStructuredEngineFactory | None = None,
    artifact_reader: ClaudeStructuredArtifactReader = (
        inspect_claude_executable_artifact
    ),
    request_id_factory: Callable[[], RequestId] = _new_request_id,
) -> ClaudeStructuredCapability:
    """Prove the exact artifact and no-network private control behavior."""
    if (
        host not in {HostPlatform.LINUX, HostPlatform.WSL}
        or executable.version != CLAUDE_STRUCTURED_VERSION
        or executable.provenance.size != CLAUDE_STRUCTURED_ARTIFACT_SIZE
    ):
        _unsupported()
    try:
        verify_claude_executable(executable)
        digest, markers = artifact_reader(
            executable,
            _CLAUDE_STRUCTURED_ARTIFACT_MARKERS,
        )
        verify_claude_executable(executable)
    except ClaudeManagedError:
        _unsupported()
    if digest != CLAUDE_STRUCTURED_ARTIFACT_SHA256 or markers != frozenset(
        _CLAUDE_STRUCTURED_ARTIFACT_MARKERS
    ):
        _unsupported()
    factory = _open_probe_engine if engine_factory is None else engine_factory
    try:
        engine = factory(
            executable,
            environment,
            working_directory=working_directory,
        )
    except (
        ClaudeManagedError,
        ClaudeProcessError,
        ClaudeStructuredError,
        ExecutableQualificationError,
    ):
        _unsupported()
    failed = False
    try:
        _probe_structured_control(engine, request_id_factory)
    except ClaudeStructuredError:
        failed = True
    try:
        engine.close_input()
    except ClaudeStructuredError:
        failed = True
    try:
        exit_status = engine.wait(_PROBE_TIMEOUT_SECONDS)
    except ClaudeStructuredError:
        failed = True
        exit_status = -1
    if failed or exit_status != 0:
        _unsupported()
    return ClaudeStructuredCapability(
        executable=executable,
        host=host,
        artifact_sha256=digest,
        embedded_build_time=CLAUDE_STRUCTURED_EMBEDDED_BUILD_TIME,
        embedded_git_sha=CLAUDE_STRUCTURED_EMBEDDED_GIT_SHA,
        variable_allowlist=_CLAUDE_STRUCTURED_VARIABLE_ALLOWLIST,
    )


def _probe_structured_control(
    engine: ClaudeStructuredEngine,
    request_id_factory: Callable[[], RequestId],
) -> None:
    positive_id = request_id_factory()
    oauth = bytearray(CLAUDE_STRUCTURED_PROBE_CANARY, "utf-8")
    try:
        positive = encode_oauth_update(positive_id, oauth)
    finally:
        clear_secret_buffer(oauth)
    try:
        response = engine.exchange(
            positive,
            positive_id,
            _PROBE_TIMEOUT_SECONDS,
        )
    finally:
        clear_secret_buffer(positive)
    decode_oauth_update_success(response, positive_id, frozenset())
    negative_id = request_id_factory()
    if negative_id == positive_id:
        raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_MALFORMED)
    negative = encode_invalid_oauth_probe(negative_id)
    try:
        response = engine.exchange(
            negative,
            negative_id,
            _PROBE_TIMEOUT_SECONDS,
        )
    finally:
        clear_secret_buffer(negative)
    decode_oauth_update_rejection(response, negative_id)


def _unsupported() -> NoReturn:
    raise ClaudeStructuredError(ClaudeStructuredFailure.VERSION_UNSUPPORTED)


class ClaudeStructuredProcess:
    """Exchange bounded JSON lines with one unchanged official child."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        stdin = process.stdin
        stdout = process.stdout
        stderr = process.stderr
        if stdin is None or stdout is None or stderr is None:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            )
        self._process = process
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._monotonic = monotonic
        self._selector = selectors.DefaultSelector()
        self._buffer = bytearray()
        self._event_frames: list[bytes] = []
        self._event_bytes = 0
        self._stderr_buffer = bytearray()
        self._stderr_overflow = False
        self._input_closed = False
        try:
            os.set_blocking(stdin.fileno(), False)
            os.set_blocking(stdout.fileno(), False)
            self._selector.register(stdout, selectors.EVENT_READ)
            self._stderr_reader = Thread(
                target=self._drain_stderr,
                daemon=True,
            )
            self._stderr_reader.start()
        except OSError, RuntimeError:
            self._selector.close()
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            ) from None

    @property
    def process_id(self) -> int:
        """Return the exact unchanged official child PID."""
        return self._process.pid

    @classmethod
    def open(
        cls,
        capability: ClaudeStructuredCapability,
        environment: Mapping[str, str],
        *,
        working_directory: Path,
        user_arguments: tuple[str, ...] = (),
    ) -> ClaudeStructuredProcess:
        """Launch one qualified official engine with structured pipes."""
        return cls._open_executable(
            capability.executable,
            environment,
            working_directory=working_directory,
            user_arguments=user_arguments,
        )

    @classmethod
    def _open_executable(
        cls,
        executable: ClaudeExecutable,
        environment: Mapping[str, str],
        *,
        working_directory: Path,
        user_arguments: tuple[str, ...] = (),
    ) -> ClaudeStructuredProcess:
        verify_claude_executable(executable)
        _require_safe_user_arguments(user_arguments)
        process = launch_piped_claude_command(
            (
                str(executable.provenance.path),
                *CLAUDE_STRUCTURED_ARGUMENTS,
                *user_arguments,
            ),
            environment=environment,
            working_directory=working_directory,
        )
        verify_claude_executable(executable)
        return cls(process)

    def exchange(
        self,
        request: bytearray,
        request_id: RequestId,
        timeout_seconds: float,
    ) -> bytes:
        """Exchange one control line and wipe it before response wait."""
        try:
            if timeout_seconds <= 0 or self._process.poll() is not None:
                raise ClaudeStructuredError(
                    ClaudeStructuredFailure.PROCESS_EXITED
                )
            deadline = self._monotonic() + timeout_seconds
            self._prepare_exchange(deadline)
            self._send(request, deadline)
        finally:
            clear_secret_buffer(request)
        return self._receive(request_id, deadline)

    def take_events(self) -> tuple[bytes, ...]:
        """Take bounded non-control frames retained for the terminal host."""
        events = tuple(self._event_frames)
        self._event_frames.clear()
        self._event_bytes = 0
        return events

    def send_interactive(
        self,
        frame: bytearray,
        timeout_seconds: float,
    ) -> None:
        """Send one bounded interactive frame on the owned input pipe."""
        try:
            if (
                not frame
                or len(frame) > MAX_CLAUDE_CONTROL_FRAME_BYTES
                or timeout_seconds <= 0
                or self._process.poll() is not None
            ):
                self._malformed()
            deadline = self._monotonic() + timeout_seconds
            self._prepare_exchange(deadline)
            self._send(frame, deadline)
        finally:
            clear_secret_buffer(frame)

    def receive_event(self, timeout_seconds: float) -> bytes:
        """Receive one bounded non-control event from the owned output pipe."""
        if timeout_seconds <= 0 or self._process.poll() is not None:
            raise ClaudeStructuredError(ClaudeStructuredFailure.PROCESS_EXITED)
        deadline = self._monotonic() + timeout_seconds
        while not self._event_frames:
            self._consume_pending_frames(None)
            if not self._event_frames:
                self._read(deadline)
        frame = self._event_frames.pop(0)
        self._event_bytes -= len(frame) + 1
        return frame

    def close_input(self) -> None:
        """Close input once and allow the child to exit naturally."""
        if self._input_closed:
            return
        self._input_closed = True
        try:
            self._stdin.close()
        except OSError:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            ) from None

    def wait(self, timeout_seconds: float) -> int:
        """Return the ordinary child status without sending a signal."""
        try:
            status = self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROTOCOL_TIMEOUT
            ) from None
        except OSError, subprocess.SubprocessError:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROCESS_UNAVAILABLE
            ) from None
        self._selector.close()
        self._stdout.close()
        self._stderr_reader.join(_PROBE_TIMEOUT_SECONDS)
        self._stderr.close()
        return status

    def _send(self, request: bytearray, deadline: float) -> None:
        selector = selectors.DefaultSelector()
        view = memoryview(request)
        offset = 0
        try:
            selector.register(self._stdin, selectors.EVENT_WRITE)
            while offset < len(view):
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ClaudeStructuredError(
                        ClaudeStructuredFailure.PROTOCOL_TIMEOUT
                    )
                try:
                    written = os.write(
                        self._stdin.fileno(),
                        view[offset:],
                    )
                except BrokenPipeError:
                    raise ClaudeStructuredError(
                        ClaudeStructuredFailure.PROTOCOL_EOF
                    ) from None
                if written == 0:
                    raise ClaudeStructuredError(
                        ClaudeStructuredFailure.PROTOCOL_EOF
                    )
                offset += written
        except OSError:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROTOCOL_EOF
            ) from None
        finally:
            view.release()
            selector.close()

    def _prepare_exchange(self, deadline: float) -> None:
        while True:
            self._consume_pending_frames(None)
            if self._buffer:
                self._read(deadline)
                continue
            if not self._selector.select(0):
                return
            self._read(deadline)

    def _receive(
        self,
        request_id: RequestId,
        deadline: float,
    ) -> bytes:
        response: bytes | None = None
        while True:
            response = self._consume_pending_frames(request_id, response)
            if response is not None:
                return response
            self._read(deadline)

    def _consume_pending_frames(
        self,
        request_id: RequestId | None,
        response: bytes | None = None,
    ) -> bytes | None:
        while (line_end := self._buffer.find(b"\n")) >= 0:
            if line_end > MAX_CLAUDE_CONTROL_FRAME_BYTES:
                self._malformed()
            frame = bytes(self._buffer[:line_end])
            del self._buffer[: line_end + 1]
            if not frame:
                self._malformed()
            correlated = decode_control_response_request_id(frame)
            if correlated is None:
                self._retain_event(frame)
                continue
            if (
                request_id is None
                or correlated != request_id
                or response is not None
            ):
                self._malformed()
            response = frame
        if len(self._buffer) > MAX_CLAUDE_CONTROL_FRAME_BYTES:
            self._malformed()
        return response

    def _read(self, deadline: float) -> None:
        remaining = deadline - self._monotonic()
        if remaining <= 0 or not self._selector.select(remaining):
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROTOCOL_TIMEOUT
            )
        maximum = MAX_CLAUDE_CONTROL_FRAME_BYTES + 1 - len(self._buffer)
        if maximum <= 0:
            self._malformed()
        try:
            chunk = os.read(
                self._stdout.fileno(),
                min(_READ_CHUNK_BYTES, maximum),
            )
        except OSError:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.PROTOCOL_EOF
            ) from None
        if not chunk:
            raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_EOF)
        self._buffer.extend(chunk)

    def _retain_event(self, frame: bytes) -> None:
        retained = len(frame) + 1
        if self._event_bytes + retained > MAX_CLAUDE_CONTROL_FRAME_BYTES:
            self._malformed()
        self._event_frames.append(frame)
        self._event_bytes += retained

    def _drain_stderr(self) -> None:
        try:
            while chunk := self._stderr.read(_READ_CHUNK_BYTES):
                remaining = MAX_CLAUDE_CONTROL_FRAME_BYTES - len(
                    self._stderr_buffer
                )
                self._stderr_buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self._stderr_overflow = True
        except OSError:
            self._stderr_overflow = True

    @staticmethod
    def _malformed() -> NoReturn:
        raise ClaudeStructuredError(ClaudeStructuredFailure.PROTOCOL_MALFORMED)


def _require_safe_user_arguments(arguments: tuple[str, ...]) -> None:
    if not claude_structured_arguments_supported(arguments):
        raise ClaudeStructuredError(
            ClaudeStructuredFailure.PROCESS_UNAVAILABLE
        )


def claude_structured_arguments_supported(arguments: tuple[str, ...]) -> bool:
    """Return whether exact continuation arguments preserve integration."""
    if not arguments or arguments in _CONTINUE_ARGUMENTS:
        return True
    if len(arguments) not in {2, 3} or arguments[0] not in _RESUME_OPTIONS:
        return False
    if (
        len(arguments) == _RESUME_FORK_ARGUMENT_COUNT
        and arguments[2] != "--fork-session"
    ):
        return False
    try:
        return str(UUID(arguments[1])) == arguments[1].casefold()
    except ValueError:
        return False


def claude_arguments_mutate_auth(arguments: tuple[str, ...]) -> bool:
    """Return whether argv attempts to bypass coordinated Claude auth."""
    normalized = tuple(argument.casefold() for argument in arguments)
    if any(argument in _AUTH_ARGUMENTS for argument in normalized):
        return True
    return any(
        left == "auth" and right in {"login", "logout"}
        for left, right in pairwise(normalized)
    )


def _open_probe_engine(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    *,
    working_directory: Path,
    user_arguments: tuple[str, ...] = (),
) -> ClaudeStructuredEngine:
    verify_claude_executable(executable)
    _require_safe_user_arguments(user_arguments)
    network_isolator = qualify_executable(_NETWORK_NAMESPACE_EXECUTABLE)
    verify_executable(network_isolator)
    process = launch_piped_claude_command(
        (
            str(network_isolator.path),
            *_NETWORK_NAMESPACE_ARGUMENTS,
            str(executable.provenance.path),
            *CLAUDE_STRUCTURED_ARGUMENTS,
            *user_arguments,
        ),
        environment=environment,
        working_directory=working_directory,
    )
    verify_executable(network_isolator)
    verify_claude_executable(executable)
    return ClaudeStructuredProcess(process)
