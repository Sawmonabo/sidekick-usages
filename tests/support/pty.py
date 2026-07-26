"""Bounded Unix pseudoterminal support for process-level CLI tests."""

import errno
import fcntl
import os
import pty
import selectors
import signal
import struct
import subprocess
import termios
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from types import TracebackType

DEFAULT_COLUMNS = 100
DEFAULT_ROWS = 30
DEFAULT_OUTPUT_LIMIT_BYTES = 1_048_576
DEFAULT_PROCESS_TIMEOUT_SECONDS = 5.0
PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
PROCESS_GROUP_POLL_SECONDS = 0.01
READ_POLL_SECONDS = 0.05
TERMINAL_LOCAL_FLAGS_INDEX = 3
type TerminalAttributes = list[int | list[bytes]]


class PtyOutputTimeoutError(TimeoutError):
    """Raised when expected pseudoterminal output misses its deadline."""


class PtyProcessExitedError(RuntimeError):
    """Raised when a child exits before emitting expected output."""


class PtySession:
    """Own one bounded child process attached to a Unix pseudoterminal."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        master_fd: int,
        slave_fd: int,
        output_limit_bytes: int,
        initial_terminal_attributes: TerminalAttributes,
    ) -> None:
        self.process = process
        self._master_fd = master_fd
        self._slave_fd = slave_fd
        self._output_limit_bytes = output_limit_bytes
        self._initial_terminal_attributes = initial_terminal_attributes
        self._output = bytearray()
        self._selector = selectors.DefaultSelector()
        self._selector.register(master_fd, selectors.EVENT_READ)
        self._closed = False

    @classmethod
    def start(
        cls,
        arguments: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        columns: int = DEFAULT_COLUMNS,
        rows: int = DEFAULT_ROWS,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    ) -> PtySession:
        """Start one isolated process group on a new pseudoterminal."""
        if not arguments:
            raise ValueError("A pseudoterminal command is required.")
        if output_limit_bytes < 1:
            raise ValueError("The output limit must be positive.")
        master_fd, slave_fd = pty.openpty()
        try:
            _set_terminal_size(slave_fd, columns, rows)
            initial_terminal_attributes = termios.tcgetattr(slave_fd)
            os.set_blocking(master_fd, False)
            process = subprocess.Popen(
                tuple(arguments),
                cwd=cwd,
                env=dict(environment),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        return cls(
            process,
            master_fd,
            slave_fd,
            output_limit_bytes,
            initial_terminal_attributes,
        )

    def __enter__(self) -> PtySession:
        """Return this active pseudoterminal session."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close descriptors and reap the complete child process group."""
        self.close()

    @property
    def output(self) -> str:
        """Return the bounded UTF-8 terminal transcript."""
        return self._output.decode("utf-8", errors="replace")

    @property
    def terminal_restored(self) -> bool:
        """Return whether the child restored the original terminal modes."""
        return (
            termios.tcgetattr(self._slave_fd)
            == self._initial_terminal_attributes
        )

    @property
    def echo_enabled(self) -> bool:
        """Return whether terminal echo is currently enabled."""
        local_flags = termios.tcgetattr(self._slave_fd)[
            TERMINAL_LOCAL_FLAGS_INDEX
        ]
        return bool(local_flags & termios.ECHO)

    @property
    def canonical_mode_enabled(self) -> bool:
        """Return whether canonical terminal input is currently enabled."""
        local_flags = termios.tcgetattr(self._slave_fd)[
            TERMINAL_LOCAL_FLAGS_INDEX
        ]
        return bool(local_flags & termios.ICANON)

    def clear_output(self) -> None:
        """Discard the accumulated transcript without touching the child."""
        self._output.clear()

    def send(self, data: bytes) -> None:
        """Write exact input bytes to the child terminal."""
        if self._closed:
            raise RuntimeError("The pseudoterminal session is closed.")
        view = memoryview(data)
        while view:
            written = os.write(self._master_fd, view)
            view = view[written:]

    def resize(self, columns: int, rows: int) -> None:
        """Resize the terminal and notify the child process group."""
        _set_terminal_size(self._slave_fd, columns, rows)
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGWINCH)

    def read_until(
        self,
        expected: str,
        *,
        timeout: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> str:
        """Read until one exact text fragment appears or the child exits."""
        if not expected:
            raise ValueError("Expected terminal output must not be empty.")
        deadline = time.monotonic() + timeout
        while expected not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PtyOutputTimeoutError(
                    f"Terminal output did not contain {expected!r}."
                )
            self._read_once(min(remaining, READ_POLL_SECONDS))
            if self.process.poll() is not None:
                self._drain_ready()
                if expected not in self.output:
                    raise PtyProcessExitedError(
                        "The pseudoterminal child exited with status "
                        f"{self.process.returncode} before emitting "
                        f"{expected!r}."
                    )
        return self.output

    def wait(
        self,
        *,
        timeout: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> int:
        """Drain terminal output while waiting to reap the child."""
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            self._read_once(min(remaining, READ_POLL_SECONDS))
        self._drain_ready()
        return self.process.wait()

    def process_group_exists(self) -> bool:
        """Return whether any process remains in the child's process group."""
        try:
            os.killpg(self.process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def wait_for_process_group_exit(
        self,
        *,
        timeout: float = DEFAULT_PROCESS_TIMEOUT_SECONDS,
    ) -> bool:
        """Wait a bounded interval for every child-group process to exit."""
        deadline = time.monotonic() + timeout
        while self.process_group_exists():
            self.process.poll()
            if time.monotonic() >= deadline:
                return False
            time.sleep(PROCESS_GROUP_POLL_SECONDS)
        return True

    def close(self) -> None:
        """Terminate if necessary, reap the child, and close descriptors."""
        if self._closed:
            return
        try:
            self._terminate_and_reap()
        finally:
            self._selector.close()
            os.close(self._master_fd)
            os.close(self._slave_fd)
            self._closed = True

    def _read_once(self, timeout: float) -> None:
        if not self._selector.select(timeout):
            return
        try:
            chunk = os.read(self._master_fd, 65_536)
        except BlockingIOError:
            return
        except OSError as error:
            if error.errno == errno.EIO:
                return
            raise
        if not chunk:
            return
        self._output.extend(chunk)
        overflow = len(self._output) - self._output_limit_bytes
        if overflow > 0:
            del self._output[:overflow]

    def _drain_ready(self) -> None:
        while self._selector.select(0):
            previous_size = len(self._output)
            self._read_once(0)
            if len(self._output) == previous_size:
                return

    def _terminate_and_reap(self) -> None:
        if self.process.poll() is not None:
            self.process.wait()
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            self.process.wait()
            return
        try:
            self.process.wait(timeout=PROCESS_CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()


def _set_terminal_size(file_descriptor: int, columns: int, rows: int) -> None:
    if columns < 1 or rows < 1:
        raise ValueError("Terminal dimensions must be positive.")
    packed_size = struct.pack("HHHH", rows, columns, 0, 0)
    fcntl.ioctl(file_descriptor, termios.TIOCSWINSZ, packed_size)
