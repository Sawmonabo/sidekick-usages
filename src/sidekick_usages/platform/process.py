"""Portable lifecycle control for isolated POSIX process groups."""

import os
import signal
import subprocess
import time

from sidekick_usages.platform.types import ProcessGroup

PROCESS_GROUP_POLL_SECONDS = 0.01


class SubprocessProcessGroup:
    """Killable process-group wrapper around ``subprocess.Popen``."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        self._process_group_id = process.pid

    @property
    def process_id(self) -> int:
        """Return the worker process identifier."""
        return self._process.pid

    def poll(self) -> int | None:
        """Return its exit status when available."""
        return self._process.poll()

    def wait(self, timeout_seconds: float | None) -> int | None:
        """Wait for exit and return ``None`` when the bound expires."""
        try:
            return self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return None

    def group_alive(self) -> bool:
        """Return whether any process remains in the isolated group."""
        try:
            os.killpg(self._process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def terminate_group(self) -> None:
        """Request termination of the isolated process group."""
        _signal_process_group(
            self._process_group_id,
            self._process,
            signal.SIGTERM,
        )

    def kill_group(self) -> None:
        """Force termination of the isolated process group."""
        _signal_process_group(
            self._process_group_id,
            self._process,
            signal.SIGKILL,
        )


def terminate_process_group(
    handle: ProcessGroup,
    grace_seconds: float,
) -> int | None:
    """Terminate, kill if required, and reap one complete process group."""
    if grace_seconds <= 0:
        raise ValueError("Process termination grace must be positive.")
    handle.terminate_group()
    exit_code = handle.wait(grace_seconds)
    if exit_code is None or handle.group_alive():
        handle.kill_group()
        if exit_code is None:
            exit_code = handle.wait(grace_seconds)
    if exit_code is None or not wait_for_process_group_exit(
        handle,
        grace_seconds,
    ):
        return None
    return exit_code


def clear_process_group(
    handle: ProcessGroup,
    grace_seconds: float,
) -> bool:
    """Reap any descendants left after the group leader exits."""
    if grace_seconds <= 0:
        raise ValueError("Process termination grace must be positive.")
    if not handle.group_alive():
        return True
    handle.terminate_group()
    if wait_for_process_group_exit(handle, grace_seconds):
        return True
    handle.kill_group()
    return wait_for_process_group_exit(handle, grace_seconds)


def wait_for_process_group_exit(
    handle: ProcessGroup,
    timeout_seconds: float,
) -> bool:
    """Wait briefly for every process in an isolated group to disappear."""
    if timeout_seconds <= 0:
        raise ValueError("Process group wait must be positive.")
    deadline = time.monotonic() + timeout_seconds
    while handle.group_alive():
        if time.monotonic() >= deadline:
            return False
        time.sleep(
            min(
                PROCESS_GROUP_POLL_SECONDS,
                max(0.0, deadline - time.monotonic()),
            )
        )
    return True


def _signal_process_group(
    process_group_id: int,
    process: subprocess.Popen[bytes],
    requested_signal: signal.Signals,
) -> None:
    try:
        os.killpg(process_group_id, requested_signal)
    except ProcessLookupError:
        return
    except OSError:
        if process.poll() is not None:
            return
        if requested_signal is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
