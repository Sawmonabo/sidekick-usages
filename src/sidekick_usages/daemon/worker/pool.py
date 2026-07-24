"""Isolated account-worker launch and pool lifecycle."""

import os
import signal
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from threading import Thread

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.daemon.models.worker import (
    ALLOWED_WORKER_ENVIRONMENT_KEYS,
    ActiveWorker,
    WorkerExit,
    WorkerLaunchSpec,
)
from sidekick_usages.daemon.types.ports import WorkerHandle, WorkerLauncher
from sidekick_usages.daemon.types.worker import (
    ExitNotifier,
    WorkerLaunchFailureCode,
)

__all__ = [
    "CODEX_CALLBACK_TIMEOUT_SECONDS",
    "GENERAL_WORKER_TIMEOUT_SECONDS",
    "MAX_GENERAL_WORKERS",
    "WORKER_TERMINATION_GRACE_SECONDS",
    "SubprocessWorkerLauncher",
    "WorkerLaunchError",
    "WorkerLaunchPlanner",
    "WorkerPool",
    "resolve_worker_executable",
]

GENERAL_WORKER_TIMEOUT_SECONDS = 120.0
CODEX_CALLBACK_TIMEOUT_SECONDS = 8.0
WORKER_TERMINATION_GRACE_SECONDS = 0.5
MAX_GENERAL_WORKERS = 2

_WORKER_ENTRY_POINT = "sidekick-usages-worker"


class WorkerLaunchError(RuntimeError):
    """An isolated worker could not be launched safely."""

    def __init__(self, code: WorkerLaunchFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class WorkerLaunchPlanner:
    """Build exact worker process arguments from a trusted executable."""

    def __init__(
        self,
        executable: Path,
        source_environment: Mapping[str, str],
    ) -> None:
        if not executable.is_absolute():
            raise ValueError("Worker executable must be absolute.")
        self._executable = executable
        self._environment = tuple(
            sorted(
                (key, value)
                for key, value in source_environment.items()
                if key in ALLOWED_WORKER_ENVIRONMENT_KEYS
            )
        )

    def plan(self, operation_id: OperationId) -> WorkerLaunchSpec:
        """Build one immutable operation-ID-only launch specification."""
        return WorkerLaunchSpec(
            operation_id=operation_id,
            argv=(str(self._executable), str(operation_id)),
            environment=self._environment,
        )


class SubprocessWorkerHandle:
    """Killable process-group wrapper around ``subprocess.Popen``."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

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

    def terminate_group(self) -> None:
        """Request termination of the isolated process group."""
        _signal_process_group(self._process, signal.SIGTERM)

    def kill_group(self) -> None:
        """Force termination of the isolated process group."""
        _signal_process_group(self._process, signal.SIGKILL)


class SubprocessWorkerLauncher:
    """Production launcher with no shell or inherited descriptors."""

    def launch(
        self,
        spec: WorkerLaunchSpec,
        notify_exit: ExitNotifier,
    ) -> WorkerHandle:
        """Start one exact process and a bounded completion notifier."""
        if sys.platform == "win32":
            raise WorkerLaunchError(WorkerLaunchFailureCode.FEATURE_DISABLED)
        try:
            process = subprocess.Popen(
                list(spec.argv),
                close_fds=True,
                env=spec.environment_map(),
                shell=False,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.EXECUTABLE_UNSAFE
            ) from None
        handle = SubprocessWorkerHandle(process)
        Thread(
            target=_wait_and_notify,
            args=(handle, notify_exit),
            daemon=True,
            name=f"sidekick-worker-{spec.operation_id}",
        ).start()
        return handle


def resolve_worker_executable(
    supervisor_executable: Path | None = None,
) -> Path:
    """Resolve the worker beside the exact running supervisor entry point."""
    try:
        running = (
            Path(sys.argv[0])
            if supervisor_executable is None
            else supervisor_executable
        ).resolve(strict=True)
        executable = running.with_name(_WORKER_ENTRY_POINT).resolve(
            strict=True
        )
    except OSError:
        raise WorkerLaunchError(
            WorkerLaunchFailureCode.EXECUTABLE_MISSING
        ) from None
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise WorkerLaunchError(WorkerLaunchFailureCode.EXECUTABLE_UNSAFE)
    return executable


class WorkerPool:
    """Bound general workers plus one reserved Codex callback slot."""

    def __init__(
        self,
        launcher: WorkerLauncher,
        planner: WorkerLaunchPlanner,
        notify_exit: ExitNotifier,
        *,
        general_limit: int = MAX_GENERAL_WORKERS,
        general_timeout_seconds: float = GENERAL_WORKER_TIMEOUT_SECONDS,
        callback_timeout_seconds: float = CODEX_CALLBACK_TIMEOUT_SECONDS,
        termination_grace_seconds: float = (WORKER_TERMINATION_GRACE_SECONDS),
    ) -> None:
        if general_limit < 1:
            raise ValueError("General worker limit must be positive.")
        if (
            min(
                general_timeout_seconds,
                callback_timeout_seconds,
                termination_grace_seconds,
            )
            <= 0
        ):
            raise ValueError("Worker deadlines must be positive.")
        self._launcher = launcher
        self._planner = planner
        self._notify_exit = notify_exit
        self._general_limit = general_limit
        self._general_timeout = general_timeout_seconds
        self._callback_timeout = callback_timeout_seconds
        self._termination_grace = termination_grace_seconds
        self._active: dict[OperationId, ActiveWorker[WorkerHandle]] = {}

    @property
    def active_count(self) -> int:
        """Return the bounded number of active workers."""
        return len(self._active)

    def can_start(self, operation: DueOperation) -> bool:
        """Return whether capacity and account authority are available."""
        if any(
            active.operation.account_id == operation.account_id
            for active in self._active.values()
        ):
            return False
        if operation.priority is OperationPriority.CODEX_CALLBACK:
            return not any(
                active.operation.priority is OperationPriority.CODEX_CALLBACK
                for active in self._active.values()
            )
        general = sum(
            active.operation.priority is not OperationPriority.CODEX_CALLBACK
            for active in self._active.values()
        )
        return general < self._general_limit

    def start(
        self,
        operation: DueOperation,
        *,
        monotonic_now: float,
    ) -> None:
        """Launch one already-durable running operation."""
        if operation.state is not OperationState.RUNNING:
            raise ValueError("Only durable running work can be launched.")
        if not self.can_start(operation):
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.CAPACITY_UNAVAILABLE
            )
        timeout = (
            self._callback_timeout
            if operation.priority is OperationPriority.CODEX_CALLBACK
            else self._general_timeout
        )
        spec = self._planner.plan(operation.operation_id)
        handle = self._launcher.launch(spec, self._notify_exit)
        self._active[operation.operation_id] = ActiveWorker(
            operation,
            handle,
            monotonic_now + timeout,
        )

    def preempt_for_callback(
        self,
        callback: DueOperation,
    ) -> WorkerExit | None:
        """Reap lower-priority same-authority work before a callback."""
        if callback.priority is not OperationPriority.CODEX_CALLBACK:
            raise ValueError("Only the reserved callback lane can preempt.")
        owner = next(
            (
                active
                for active in self._active.values()
                if active.operation.account_id == callback.account_id
            ),
            None,
        )
        if owner is None:
            return None
        if (
            owner.operation.priority is OperationPriority.CODEX_CALLBACK
            or owner.operation.kind is OperationKind.ACTIVATE
        ):
            raise WorkerLaunchError(WorkerLaunchFailureCode.AUTHORITY_BUSY)
        return self._stop(owner, preempted=True, timed_out=False)

    def reap_completed(self) -> tuple[WorkerExit, ...]:
        """Remove every naturally exited worker without polling idle loops."""
        completed: list[WorkerExit] = []
        for active in tuple(self._active.values()):
            exit_code = active.handle.poll()
            if exit_code is None:
                continue
            del self._active[active.operation.operation_id]
            completed.append(WorkerExit(active.operation, exit_code))
        return tuple(completed)

    def expire(self, monotonic_now: float) -> tuple[WorkerExit, ...]:
        """Terminate and reap every worker whose hard deadline elapsed."""
        expired = tuple(
            active
            for active in self._active.values()
            if active.deadline <= monotonic_now
        )
        return tuple(
            self._stop(active, preempted=False, timed_out=True)
            for active in expired
        )

    def next_deadline(self) -> float | None:
        """Return the nearest active monotonic deadline."""
        if not self._active:
            return None
        return min(active.deadline for active in self._active.values())

    def shutdown(self) -> tuple[WorkerExit, ...]:
        """Terminate and reap all remaining workers."""
        return tuple(
            self._stop(active, preempted=True, timed_out=False)
            for active in tuple(self._active.values())
        )

    def _stop(
        self,
        active: ActiveWorker[WorkerHandle],
        *,
        preempted: bool,
        timed_out: bool,
    ) -> WorkerExit:
        active.handle.terminate_group()
        exit_code = active.handle.wait(self._termination_grace)
        if exit_code is None:
            active.handle.kill_group()
            exit_code = active.handle.wait(self._termination_grace)
        if exit_code is None:
            raise WorkerLaunchError(WorkerLaunchFailureCode.TERMINATION_FAILED)
        self._active.pop(active.operation.operation_id, None)
        return WorkerExit(
            active.operation,
            exit_code,
            timed_out=timed_out,
            preempted=preempted,
        )


def _wait_and_notify(
    handle: WorkerHandle,
    notify_exit: ExitNotifier,
) -> None:
    handle.wait(None)
    notify_exit()


def _signal_process_group(
    process: subprocess.Popen[bytes],
    requested_signal: signal.Signals,
) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, requested_signal)
    except ProcessLookupError:
        return
    except OSError:
        if requested_signal is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()
