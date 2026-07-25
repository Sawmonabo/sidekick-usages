"""Bounded one-shot Claude process execution."""

import os
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from threading import Event, Thread
from typing import IO

from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.models import ClaudeCommandResult
from sidekick_usages.providers.claude.types import ClaudeProcessFailure

PROCESS_GRACE_SECONDS = 0.25
PROCESS_KILL_SECONDS = 1.0
_READ_CHUNK_BYTES = 8192


def run_bounded_claude_command(
    argv: tuple[str, ...],
    *,
    timeout_seconds: float,
    maximum_output_bytes: int,
    environment: Mapping[str, str] | None = None,
    working_directory: Path | None = None,
    umask: int = -1,
) -> ClaudeCommandResult:
    """Run one Claude command and capture strictly bounded merged output."""
    if timeout_seconds <= 0 or maximum_output_bytes < 1:
        raise ValueError("Claude command bounds must be positive.")
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
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)
    try:
        process = subprocess.Popen(
            list(argv),
            close_fds=True,
            cwd=working_directory,
            env=None if environment is None else dict(environment),
            shell=False,
            start_new_session=os.name == "posix",
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            umask=umask,
        )
    except OSError, subprocess.SubprocessError:
        raise ClaudeProcessError(
            ClaudeProcessFailure.PROCESS_UNAVAILABLE
        ) from None
    output = bytearray()
    overflow = Event()
    read_failed = Event()
    stdout = process.stdout
    if stdout is None:
        _terminate_and_reap(process)
        raise ClaudeProcessError(ClaudeProcessFailure.OUTPUT_UNREADABLE)
    reader = Thread(
        target=_drain_bounded_output,
        args=(
            stdout,
            process,
            output,
            overflow,
            read_failed,
            maximum_output_bytes,
        ),
        daemon=True,
    )
    try:
        reader.start()
    except RuntimeError:
        _terminate_and_reap(process)
        stdout.close()
        raise ClaudeProcessError(
            ClaudeProcessFailure.PROCESS_UNAVAILABLE
        ) from None
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_and_reap(process)
        reader.join(PROCESS_KILL_SECONDS)
        stdout.close()
        raise ClaudeProcessError(ClaudeProcessFailure.TIMED_OUT) from None
    except OSError, subprocess.SubprocessError:
        _terminate_and_reap(process)
        reader.join(PROCESS_KILL_SECONDS)
        stdout.close()
        raise ClaudeProcessError(
            ClaudeProcessFailure.PROCESS_UNAVAILABLE
        ) from None
    reader.join(PROCESS_GRACE_SECONDS)
    if reader.is_alive():
        _terminate_group(process)
        reader.join(PROCESS_KILL_SECONDS)
    stdout.close()
    if reader.is_alive() or overflow.is_set() or read_failed.is_set():
        raise ClaudeProcessError(ClaudeProcessFailure.OUTPUT_UNREADABLE)
    return ClaudeCommandResult(return_code, bytes(output))


def _drain_bounded_output(
    stdout: IO[bytes],
    process: subprocess.Popen[bytes],
    output: bytearray,
    overflow: Event,
    read_failed: Event,
    maximum_output_bytes: int,
) -> None:
    try:
        while chunk := stdout.read(_READ_CHUNK_BYTES):
            remaining = maximum_output_bytes - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                _kill_process(process)
                return
    except OSError:
        read_failed.set()
        _kill_process(process)


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        return
    with suppress(OSError):
        process.kill()


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        with suppress(OSError):
            os.killpg(process.pid, signal.SIGTERM)
        return
    if process.poll() is None:
        with suppress(OSError):
            process.terminate()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    _terminate_group(process)
    if process.poll() is None:
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=PROCESS_GRACE_SECONDS)
    if process.poll() is None:
        _kill_process(process)
        try:
            process.wait(timeout=PROCESS_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            raise ClaudeProcessError(
                ClaudeProcessFailure.PROCESS_UNAVAILABLE
            ) from None
    else:
        with suppress(OSError):
            process.wait()
