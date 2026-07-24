"""Bounded JSON-lines transport for one Codex app-server child."""

import os
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    MAX_JSON_RPC_MESSAGE_BYTES,
)
from sidekick_usages.providers.codex.app_server.process import (
    CODEX_PROCESS_GRACE_SECONDS,
    start_codex_json_lines,
    terminate_and_reap_codex_process,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

_READ_CHUNK_BYTES = 64 * 1024


class JsonLinesTransport:
    """Exchange complete JSON objects with one JSON-lines child."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        stdout = process.stdout
        stdin = process.stdin
        if stdout is None or stdin is None:
            terminate_and_reap_codex_process(process)
            raise CodexAppServerError(CodexAppServerFailure.PROCESS_FAILED)
        self._process = process
        self._monotonic = monotonic
        self._stdout = stdout
        self._stdin = stdin
        self._selector = selectors.DefaultSelector()
        self._buffer = bytearray()
        self._closed = False
        try:
            os.set_blocking(stdout.fileno(), False)
            os.set_blocking(stdin.fileno(), False)
            self._selector.register(stdout, selectors.EVENT_READ)
        except OSError:
            self._selector.close()
            terminate_and_reap_codex_process(process)
            raise CodexAppServerError(
                CodexAppServerFailure.PROCESS_FAILED
            ) from None

    @classmethod
    def open(
        cls,
        argv: tuple[str, ...],
        environment: Mapping[str, str],
        *,
        working_directory: Path,
    ) -> JsonLinesTransport:
        """Start one exact JSON-lines subprocess transport."""
        return cls(
            start_codex_json_lines(
                argv,
                environment,
                working_directory=working_directory,
            )
        )

    @property
    def process_id(self) -> int:
        """Return the exact child process identifier."""
        return self._process.pid

    @property
    def closed(self) -> bool:
        """Return whether the child has been closed and reaped."""
        return self._closed

    def send(self, payload: bytes, deadline: float) -> None:
        """Write one complete JSON-line payload before ``deadline``."""
        if self._closed:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_CLOSED)
        view = memoryview(payload + b"\n")
        selector = selectors.DefaultSelector()
        try:
            selector.register(self._stdin, selectors.EVENT_WRITE)
            while view:
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_TIMEOUT
                    )
                try:
                    written = os.write(self._stdin.fileno(), view)
                except BrokenPipeError:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_CLOSED
                    ) from None
                if written == 0:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_CLOSED
                    )
                view = view[written:]
        except OSError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_CLOSED
            ) from None
        finally:
            selector.close()

    def receive(self, deadline: float) -> bytes:
        """Read one complete bounded JSON-line payload before ``deadline``."""
        while True:
            line_end = self._buffer.find(b"\n")
            if line_end >= 0:
                line = bytes(self._buffer[:line_end])
                del self._buffer[: line_end + 1]
                if not line or len(line) > MAX_JSON_RPC_MESSAGE_BYTES:
                    raise CodexAppServerError(
                        CodexAppServerFailure.PROTOCOL_MALFORMED
                    )
                return line
            if len(self._buffer) > MAX_JSON_RPC_MESSAGE_BYTES:
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_MALFORMED
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_TIMEOUT
                )
            try:
                chunk = os.read(
                    self._stdout.fileno(),
                    min(
                        _READ_CHUNK_BYTES,
                        MAX_JSON_RPC_MESSAGE_BYTES + 1 - len(self._buffer),
                    ),
                )
            except OSError:
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_CLOSED
                ) from None
            if not chunk:
                failure = (
                    CodexAppServerFailure.PROTOCOL_MALFORMED
                    if self._buffer
                    else CodexAppServerFailure.PROTOCOL_CLOSED
                )
                raise CodexAppServerError(failure)
            self._buffer.extend(chunk)

    def close(self) -> None:
        """Close input, terminate if needed, and reap the child."""
        if self._closed:
            return
        self._closed = True
        self._selector.close()
        try:
            self._stdin.close()
        except OSError:
            terminate_and_reap_codex_process(self._process)
            return
        try:
            self._process.wait(timeout=CODEX_PROCESS_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            terminate_and_reap_codex_process(self._process)
