"""Bounded subprocess primitives for Codex app-server operations."""

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

CODEX_HOME_ENVIRONMENT_KEY = "CODEX_HOME"
CODEX_PROCESS_GRACE_SECONDS = 0.25
CODEX_PROCESS_KILL_SECONDS = 1.0
SAFE_CODEX_ENVIRONMENT_KEYS = frozenset(
    {
        "ALL_PROXY",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
        "USER",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)
_MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024
_READ_CHUNK_BYTES = 64 * 1024


def minimal_codex_environment(
    source_environment: Mapping[str, str] | None,
    *,
    codex_home: Path | None = None,
) -> dict[str, str]:
    """Build a credential-free environment for one Codex child."""
    source = os.environ if source_environment is None else source_environment
    environment = {
        key: value
        for key, value in source.items()
        if key in SAFE_CODEX_ENVIRONMENT_KEYS
    }
    environment.setdefault("PATH", os.defpath)
    if codex_home is not None:
        if not codex_home.is_absolute():
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        environment[CODEX_HOME_ENVIRONMENT_KEY] = str(codex_home)
        environment.setdefault("HOME", str(codex_home))
    for value in environment.values():
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None
        if "\0" in value or len(encoded) > _MAX_ENVIRONMENT_VALUE_BYTES:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
    return environment


def run_bounded_codex_command(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    maximum_output_bytes: int,
    working_directory: Path | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bytes:
    """Run one command and return strictly bounded stdout."""
    if timeout_seconds <= 0 or maximum_output_bytes < 1:
        raise ValueError("Codex command bounds must be positive.")
    process = _start_codex_process(
        argv,
        environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        working_directory=working_directory,
    )
    stream = process.stdout
    if stream is None:
        terminate_and_reap_codex_process(process)
        raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
    deadline = monotonic() + timeout_seconds
    selector = selectors.DefaultSelector()
    try:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
        output = _read_bounded_output(
            stream.fileno(),
            selector,
            deadline,
            maximum_output_bytes,
            monotonic,
        )
        remaining = max(0.0, deadline - monotonic())
        try:
            return_code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_TIMEOUT
            ) from None
        if return_code != 0:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        return output
    except CodexAppServerError:
        terminate_and_reap_codex_process(process)
        raise
    except OSError:
        terminate_and_reap_codex_process(process)
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_FAILED
        ) from None
    finally:
        selector.close()


def run_quiet_codex_command(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    timeout_seconds: float,
    working_directory: Path | None = None,
) -> None:
    """Run one bounded command while discarding provider output."""
    if timeout_seconds <= 0:
        raise ValueError("Codex command timeout must be positive.")
    process = _start_codex_process(
        argv,
        environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        working_directory=working_directory,
    )
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        terminate_and_reap_codex_process(process)
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_TIMEOUT
        ) from None
    if return_code != 0:
        raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)


def start_codex_json_lines(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    working_directory: Path,
) -> subprocess.Popen[bytes]:
    """Start one isolated Codex child with JSON-lines stdio."""
    return _start_codex_process(
        argv,
        environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        working_directory=working_directory,
    )


def terminate_and_reap_codex_process(
    process: subprocess.Popen[bytes],
) -> None:
    """Terminate one isolated process group and reap its leader."""
    if process.poll() is not None:
        process.wait()
        return
    _signal_codex_process(process, signal.SIGTERM)
    try:
        process.wait(timeout=CODEX_PROCESS_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        _signal_codex_process(process, signal.SIGKILL)
    try:
        process.wait(timeout=CODEX_PROCESS_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_FAILED
        ) from None


def _start_codex_process(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    stdin: int,
    stdout: int,
    stderr: int,
    working_directory: Path | None,
) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        raise CodexAppServerError(CodexAppServerFailure.CAPABILITY_UNSUPPORTED)
    if (
        not argv
        or not Path(argv[0]).is_absolute()
        or any(not argument or "\0" in argument for argument in argv)
        or (
            working_directory is not None
            and (
                not working_directory.is_absolute()
                or not working_directory.is_dir()
            )
        )
    ):
        raise CodexAppServerError(CodexAppServerFailure.EXECUTABLE_UNSAFE)
    try:
        return subprocess.Popen(
            list(argv),
            close_fds=True,
            env=dict(environment),
            cwd=working_directory,
            shell=False,
            start_new_session=True,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
    except OSError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_FAILED
        ) from None


def _read_bounded_output(
    file_descriptor: int,
    selector: selectors.BaseSelector,
    deadline: float,
    maximum_output_bytes: int,
    monotonic: Callable[[], float],
) -> bytes:
    output = bytearray()
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        if not selector.select(remaining):
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        chunk = os.read(
            file_descriptor,
            min(
                _READ_CHUNK_BYTES,
                maximum_output_bytes + 1 - len(output),
            ),
        )
        if not chunk:
            return bytes(output)
        output.extend(chunk)
        if len(output) > maximum_output_bytes:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)


def _signal_codex_process(
    process: subprocess.Popen[bytes],
    requested_signal: signal.Signals,
) -> None:
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            process.send_signal(requested_signal)
        except ProcessLookupError:
            return
        except OSError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None
    except OSError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_FAILED
        ) from None
