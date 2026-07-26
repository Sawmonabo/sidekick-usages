"""Hard-deadline client for one global usage lookup process."""

import os
import selectors
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.limits import MAX_ACCOUNTS
from sidekick_usages.platform.environment import minimal_worker_environment
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.process import (
    SubprocessProcessGroup,
    clear_process_group,
    terminate_process_group,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    FramingError,
    clear_mutable_buffer,
)
from sidekick_usages.usage.lookup.worker.models import (
    USAGE_LOOKUP_MODULE,
    UsageLookupEventKind,
    UsageLookupEventObserver,
    UsageLookupFailure,
    UsageLookupModuleLaunchSpec,
    UsageLookupWorkerEvent,
    UsageLookupWorkerResult,
)
from sidekick_usages.usage.lookup.worker.protocol import (
    MAX_USAGE_LOOKUP_FRAME_BYTES,
    UsageLookupProtocolError,
    decode_usage_lookup_event,
)

USAGE_LOOKUP_TIMEOUT_SECONDS = 120.0
USAGE_LOOKUP_TERMINATION_GRACE_SECONDS = 0.5
USAGE_LOOKUP_READ_BYTES = 4096


class UsageLookupLaunchError(RuntimeError):
    """The exact global lookup worker could not be resolved safely."""

    def __init__(self, failure: UsageLookupFailure) -> None:
        self.failure = failure
        super().__init__(failure.value)


class UsageLookupModuleLaunchPlanner:
    """Build an exact lookup-module launch from a trusted interpreter."""

    def __init__(
        self,
        interpreter: Path,
        source_environment: Mapping[str, str],
    ) -> None:
        if not interpreter.is_absolute():
            raise ValueError("Usage lookup interpreter must be absolute.")
        self._interpreter = interpreter
        self._environment = minimal_worker_environment(source_environment)

    def plan(self) -> UsageLookupModuleLaunchSpec:
        """Return one exact Python module launch specification."""
        return UsageLookupModuleLaunchSpec(
            argv=(
                str(self._interpreter),
                "-m",
                USAGE_LOOKUP_MODULE,
            ),
            environment=self._environment,
        )


class UsageLookupWorkerClient:
    """Launch, stream, and fully reap one global lookup worker."""

    def __init__(
        self,
        planner: UsageLookupModuleLaunchPlanner,
        *,
        timeout_seconds: float = USAGE_LOOKUP_TIMEOUT_SECONDS,
        termination_grace_seconds: float = (
            USAGE_LOOKUP_TERMINATION_GRACE_SECONDS
        ),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if min(timeout_seconds, termination_grace_seconds) <= 0:
            raise ValueError("Usage lookup worker deadlines must be positive.")
        self._planner = planner
        self._timeout = timeout_seconds
        self._termination_grace = termination_grace_seconds
        self._monotonic = monotonic

    def run(
        self,
        observe: UsageLookupEventObserver | None = None,
    ) -> UsageLookupWorkerResult:
        """Stream stable-ID completions and return one terminal outcome."""
        if sys.platform == "win32":
            return UsageLookupWorkerResult(
                (),
                UsageLookupFailure.FEATURE_DISABLED,
            )
        try:
            spec = self._planner.plan()
            process = subprocess.Popen(
                list(spec.argv),
                close_fds=True,
                env=spec.environment_map(),
                shell=False,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return UsageLookupWorkerResult(
                (),
                UsageLookupFailure.LAUNCH_FAILED,
            )
        return self._receive(
            process,
            self._monotonic() + self._timeout,
            observe,
        )

    def _receive(
        self,
        process: subprocess.Popen[bytes],
        deadline: float,
        observe: UsageLookupEventObserver | None,
    ) -> UsageLookupWorkerResult:
        stream = process.stdout
        if stream is None:
            raise AssertionError("Lookup worker stdout pipe is unavailable.")
        handle = SubprocessProcessGroup(process)
        completed: list[SidekickAccountId] = []
        terminal: UsageLookupWorkerEvent | None = None
        decoder = BoundedFrameDecoder(MAX_USAGE_LOOKUP_FRAME_BYTES)
        selector = selectors.DefaultSelector()
        reaped = False
        try:
            selector.register(stream, selectors.EVENT_READ)
            while True:
                remaining = deadline - self._monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return self._failed(
                        handle,
                        completed,
                        UsageLookupFailure.TIMED_OUT,
                    )
                chunk = os.read(stream.fileno(), USAGE_LOOKUP_READ_BYTES)
                if not chunk:
                    decoder.finish()
                    break
                for payload in decoder.feed(chunk):
                    try:
                        event = decode_usage_lookup_event(payload)
                    finally:
                        clear_mutable_buffer(payload)
                    terminal = self._accept(
                        event,
                        completed,
                        terminal,
                        observe,
                    )
            exit_code = handle.wait(max(0.0, deadline - self._monotonic()))
            if exit_code is None:
                return self._failed(
                    handle,
                    completed,
                    UsageLookupFailure.TIMED_OUT,
                )
            reaped = clear_process_group(handle, self._termination_grace)
            if not reaped:
                return UsageLookupWorkerResult(
                    tuple(completed),
                    UsageLookupFailure.TERMINATION_FAILED,
                )
            return self._terminal_result(completed, terminal, exit_code)
        except FramingError, OSError, UsageLookupProtocolError:
            return self._failed(
                handle,
                completed,
                UsageLookupFailure.MALFORMED_PROTOCOL,
            )
        finally:
            selector.close()
            decoder.clear()
            stream.close()
            if not reaped and handle.poll() is None:
                terminate_process_group(handle, self._termination_grace)

    @staticmethod
    def _accept(
        event: UsageLookupWorkerEvent,
        completed: list[SidekickAccountId],
        terminal: UsageLookupWorkerEvent | None,
        observe: UsageLookupEventObserver | None,
    ) -> UsageLookupWorkerEvent | None:
        if terminal is not None:
            raise UsageLookupProtocolError
        if event.kind is UsageLookupEventKind.ACCOUNT_COMPLETED:
            account_id = event.account_id
            if (
                account_id is None
                or account_id in completed
                or len(completed) >= MAX_ACCOUNTS
            ):
                raise UsageLookupProtocolError
            completed.append(account_id)
        else:
            terminal = event
        if observe is not None:
            observe(event)
        return terminal

    def _failed(
        self,
        handle: SubprocessProcessGroup,
        completed: list[SidekickAccountId],
        failure: UsageLookupFailure,
    ) -> UsageLookupWorkerResult:
        reaped = terminate_process_group(handle, self._termination_grace)
        if reaped is None:
            failure = UsageLookupFailure.TERMINATION_FAILED
        return UsageLookupWorkerResult(tuple(completed), failure)

    @staticmethod
    def _terminal_result(
        completed: list[SidekickAccountId],
        terminal: UsageLookupWorkerEvent | None,
        exit_code: int,
    ) -> UsageLookupWorkerResult:
        if (
            terminal is not None
            and terminal.kind is UsageLookupEventKind.SUCCEEDED
            and exit_code == 0
        ):
            return UsageLookupWorkerResult(tuple(completed))
        failure = (
            terminal.failure
            if terminal is not None
            and terminal.kind is UsageLookupEventKind.FAILED
            else UsageLookupFailure.INTERNAL
        )
        return UsageLookupWorkerResult(tuple(completed), failure)


def resolve_usage_lookup_interpreter(
    python_executable: Path | None = None,
) -> Path:
    """Resolve and qualify the current absolute Python interpreter."""
    candidate = (
        Path(sys.executable)
        if python_executable is None
        else python_executable
    )
    try:
        qualify_executable(candidate)
    except ExecutableQualificationError:
        raise UsageLookupLaunchError(
            UsageLookupFailure.INTERPRETER_UNSAFE
        ) from None
    return candidate
