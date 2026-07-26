"""Bounded subprocess primitives for Codex app-server operations."""

import os
import selectors
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from pathlib import Path

from sidekick_usages.platform.environment import (
    SAFE_PROVIDER_ENVIRONMENT_KEYS,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.auth.home import (
    CODEX_HOME_ENVIRONMENT_KEY,
)

CODEX_PROCESS_GRACE_SECONDS = 0.25
CODEX_PROCESS_KILL_SECONDS = 1.0
_MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024
_PROCESS_POLL_SECONDS = 0.1
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
        if key in SAFE_PROVIDER_ENVIRONMENT_KEYS
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
    process_group: CodexProcessGroupPolicy = (
        CodexProcessGroupPolicy.ISOLATED
    ),
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
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
        process_group=process_group,
    )
    stream = process.stdout
    if stream is None:
        terminate_and_reap_codex_process(process, process_group)
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
            cancelled,
        )
        return_code = _wait_for_codex_process(
            process,
            deadline,
            monotonic,
            cancelled,
        )
        terminate_and_reap_codex_process(process, process_group)
        if return_code != 0:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        return output
    except CodexAppServerError:
        terminate_and_reap_codex_process(process, process_group)
        raise
    except OSError:
        terminate_and_reap_codex_process(process, process_group)
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
    process_group: CodexProcessGroupPolicy = (
        CodexProcessGroupPolicy.ISOLATED
    ),
    monotonic: Callable[[], float] = time.monotonic,
    cancelled: Callable[[], bool] | None = None,
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
        process_group=process_group,
    )
    try:
        return_code = _wait_for_codex_process(
            process,
            monotonic() + timeout_seconds,
            monotonic,
            cancelled,
        )
    except CodexAppServerError:
        terminate_and_reap_codex_process(process, process_group)
        raise
    terminate_and_reap_codex_process(process, process_group)
    if return_code != 0:
        raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)


def start_codex_json_lines(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    working_directory: Path,
    process_group: CodexProcessGroupPolicy,
) -> subprocess.Popen[bytes]:
    """Start one Codex JSON-lines child with explicit group ownership."""
    return _start_codex_process(
        argv,
        environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        working_directory=working_directory,
        process_group=process_group,
    )


def terminate_and_reap_codex_process(
    process: subprocess.Popen[bytes],
    process_group: CodexProcessGroupPolicy,
) -> None:
    """Terminate one Codex child under its declared group policy."""
    if process_group is CodexProcessGroupPolicy.INHERITED:
        _terminate_inherited_codex_process(process)
        return
    _signal_codex_process(
        process,
        signal.SIGTERM,
        CodexProcessGroupPolicy.ISOLATED,
    )
    if process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=CODEX_PROCESS_GRACE_SECONDS)
    if _wait_codex_group_exit(process.pid, CODEX_PROCESS_GRACE_SECONDS):
        return
    _signal_codex_process(
        process,
        signal.SIGKILL,
        CodexProcessGroupPolicy.ISOLATED,
    )
    if process.poll() is None:
        try:
            process.wait(timeout=CODEX_PROCESS_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None
    if not _wait_codex_group_exit(
        process.pid,
        CODEX_PROCESS_KILL_SECONDS,
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)


def _start_codex_process(
    argv: tuple[str, ...],
    environment: Mapping[str, str],
    *,
    stdin: int,
    stdout: int,
    stderr: int,
    working_directory: Path | None,
    process_group: CodexProcessGroupPolicy,
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
            start_new_session=(
                process_group is CodexProcessGroupPolicy.ISOLATED
            ),
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
    cancelled: Callable[[], bool] | None,
) -> bytes:
    output = bytearray()
    while True:
        if cancelled is not None and cancelled():
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        if not selector.select(min(remaining, _PROCESS_POLL_SECONDS)):
            continue
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


def _wait_for_codex_process(
    process: subprocess.Popen[bytes],
    deadline: float,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> int:
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            return exit_code
        if cancelled is not None and cancelled():
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_TIMEOUT)
        try:
            return process.wait(timeout=min(remaining, _PROCESS_POLL_SECONDS))
        except subprocess.TimeoutExpired:
            continue


def _signal_codex_process(
    process: subprocess.Popen[bytes],
    requested_signal: signal.Signals,
    process_group: CodexProcessGroupPolicy,
) -> None:
    if process_group is CodexProcessGroupPolicy.INHERITED:
        try:
            process.send_signal(requested_signal)
        except ProcessLookupError:
            return
        except OSError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None
        return
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


def _terminate_inherited_codex_process(
    process: subprocess.Popen[bytes],
) -> None:
    if process.poll() is not None:
        process.wait()
        return
    _signal_codex_process(
        process,
        signal.SIGTERM,
        CodexProcessGroupPolicy.INHERITED,
    )
    try:
        process.wait(timeout=CODEX_PROCESS_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        _signal_codex_process(
            process,
            signal.SIGKILL,
            CodexProcessGroupPolicy.INHERITED,
        )
    try:
        process.wait(timeout=CODEX_PROCESS_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        raise CodexAppServerError(
            CodexAppServerFailure.PROCESS_FAILED
        ) from None


def _wait_codex_group_exit(
    process_group_id: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
