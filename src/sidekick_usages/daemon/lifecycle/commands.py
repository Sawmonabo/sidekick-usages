"""Bounded native command execution for service integrations."""

import locale
import os
import selectors
import subprocess
import time
from threading import Event, Lock
from typing import IO

from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import (
    MAX_COMMAND_OUTPUT_BYTES,
    CommandResult,
)
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode
from sidekick_usages.platform.process import (
    SubprocessProcessGroup,
    clear_process_group,
    terminate_process_group,
)

_COMMAND_TIMEOUT_SECONDS = 20.0
_COMMAND_TERMINATION_GRACE_SECONDS = 0.5
_COMMAND_READ_BYTES = 8192


class SystemCommandRunner:
    """Run one exact native argv without shell or inherited input."""

    def __init__(self) -> None:
        self._cancelled = Event()
        self._process_lock = Lock()
        self._active_process: SubprocessProcessGroup | None = None

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        """Run a bounded command and capture its text result."""
        if not argv or any(
            not argument or "\0" in argument for argument in argv
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        with self._process_lock:
            self._raise_if_cancelled()
            try:
                process = subprocess.Popen(
                    list(argv),
                    close_fds=True,
                    shell=False,
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError, subprocess.SubprocessError:
                raise ServiceLifecycleError(
                    ServiceFailureCode.COMMAND_FAILED
                ) from None
            handle = SubprocessProcessGroup(process)
            self._active_process = handle
        try:
            stdout, stderr = self._read_output(
                process,
                handle,
                time.monotonic() + _COMMAND_TIMEOUT_SECONDS,
            )
        finally:
            with self._process_lock:
                if self._active_process is handle:
                    self._active_process = None
        self._raise_if_cancelled()
        returncode = process.returncode
        if returncode is None:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        try:
            return CommandResult(
                returncode,
                stdout.decode(locale.getencoding()),
                stderr.decode(locale.getencoding()),
            )
        except UnicodeError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.COMMAND_FAILED
            ) from None

    def cancel(self) -> None:
        """Cancel the exact group; its runner remains the sole reaper."""
        self._cancelled.set()
        with self._process_lock:
            handle = self._active_process
        if handle is not None:
            handle.kill_group()

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ServiceLifecycleError(ServiceFailureCode.CANCELLED)

    def _read_output(
        self,
        process: subprocess.Popen[bytes],
        handle: SubprocessProcessGroup,
        deadline: float,
    ) -> tuple[bytes, bytes]:
        stdout = self._stream(process.stdout)
        stderr = self._stream(process.stderr)
        output = {
            stdout.fileno(): bytearray(),
            stderr.fileno(): bytearray(),
        }
        selector = selectors.DefaultSelector()
        try:
            selector.register(stdout, selectors.EVENT_READ)
            selector.register(stderr, selectors.EVENT_READ)
            self._drain_output(selector, output, deadline)
            self._finish_process(handle, deadline)
            return (
                bytes(output[stdout.fileno()]),
                bytes(output[stderr.fileno()]),
            )
        except OSError, ValueError:
            self._raise_if_cancelled()
            raise ServiceLifecycleError(
                ServiceFailureCode.COMMAND_FAILED
            ) from None
        finally:
            selector.close()
            stdout.close()
            stderr.close()
            if handle.poll() is None or handle.group_alive():
                terminate_process_group(
                    handle,
                    _COMMAND_TERMINATION_GRACE_SECONDS,
                )

    def _drain_output(
        self,
        selector: selectors.BaseSelector,
        output: dict[int, bytearray],
        deadline: float,
    ) -> None:
        while selector.get_map():
            self._raise_if_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
            events = selector.select(remaining)
            if not events:
                raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
            for key, _mask in events:
                self._read_chunk(selector, key, output)

    @staticmethod
    def _read_chunk(
        selector: selectors.BaseSelector,
        key: selectors.SelectorKey,
        output: dict[int, bytearray],
    ) -> None:
        chunk = os.read(key.fd, _COMMAND_READ_BYTES)
        if not chunk:
            selector.unregister(key.fileobj)
            return
        target = output[key.fd]
        if len(target) + len(chunk) > MAX_COMMAND_OUTPUT_BYTES:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        target.extend(chunk)

    @staticmethod
    def _finish_process(
        handle: SubprocessProcessGroup,
        deadline: float,
    ) -> None:
        remaining = max(0.0, deadline - time.monotonic())
        if handle.wait(remaining) is None or not clear_process_group(
            handle,
            _COMMAND_TERMINATION_GRACE_SECONDS,
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)

    @staticmethod
    def _stream(stream: IO[bytes] | None) -> IO[bytes]:
        if stream is None:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        return stream
