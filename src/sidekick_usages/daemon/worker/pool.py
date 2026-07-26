"""Isolated account-worker launch and pool lifecycle."""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Thread

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import (
    ActiveWorker,
    QuarantinedWorker,
    WorkerExit,
    WorkerLaunchSpec,
)
from sidekick_usages.daemon.types.ports import WorkerHandle, WorkerLauncher
from sidekick_usages.daemon.types.worker import (
    ExitNotifier,
    WorkerLaunchFailureCode,
    WorkerOutcome,
)
from sidekick_usages.daemon.worker.exchange import (
    SupervisorWorkerExchange,
    WorkerExchangeError,
    WorkerExchangeRegistry,
    operation_requires_worker_exchange,
)
from sidekick_usages.platform.environment import (
    SAFE_WORKER_ENVIRONMENT_KEYS,
)

GENERAL_WORKER_TIMEOUT_SECONDS = 120.0
WORKER_TERMINATION_GRACE_SECONDS = 0.5
MAX_GENERAL_WORKERS = 2
QUARANTINE_INITIAL_RETRY_SECONDS = 1.0
QUARANTINE_MAX_RETRY_SECONDS = 30.0

_WORKER_ENTRY_POINT = "sidekick-usages-worker"
_SELECTION_OPERATION_KINDS = frozenset(
    {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }
)


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
                if key in SAFE_WORKER_ENVIRONMENT_KEYS
            )
        )

    def plan(
        self,
        operation_id: OperationId,
        *,
        exchange_descriptor: int | None = None,
    ) -> WorkerLaunchSpec:
        """Build one immutable operation-ID-only launch specification."""
        return WorkerLaunchSpec(
            operation_id=operation_id,
            argv=(str(self._executable), str(operation_id)),
            environment=self._environment,
            exchange_descriptor=exchange_descriptor,
        )


class SubprocessWorkerHandle:
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


class SubprocessWorkerLauncher:
    """Production launcher with one worker-exchange descriptor."""

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
                pass_fds=spec.inherited_descriptors(),
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
        exchanges: WorkerExchangeRegistry | None = None,
        general_limit: int = MAX_GENERAL_WORKERS,
        general_timeout_seconds: float = GENERAL_WORKER_TIMEOUT_SECONDS,
        termination_grace_seconds: float = (WORKER_TERMINATION_GRACE_SECONDS),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if general_limit < 1:
            raise ValueError("General worker limit must be positive.")
        if (
            min(
                general_timeout_seconds,
                termination_grace_seconds,
            )
            <= 0
        ):
            raise ValueError("Worker deadlines must be positive.")
        self._launcher = launcher
        self._planner = planner
        self._notify_exit = notify_exit
        self._exchanges = exchanges
        self._general_limit = general_limit
        self._general_timeout = general_timeout_seconds
        self._termination_grace = termination_grace_seconds
        self._monotonic = monotonic
        self._active: dict[OperationId, ActiveWorker[WorkerHandle]] = {}
        self._quarantine: dict[
            OperationId,
            QuarantinedWorker[WorkerHandle],
        ] = {}

    @property
    def active_count(self) -> int:
        """Return the bounded number of active workers."""
        return len(self._active) + len(self._quarantine)

    def has_capacity_for(self, operation: DueOperation) -> bool:
        """Return whether worker and account capacity allow an operation."""
        owned = self._owned_operations()
        callback = operation.priority is OperationPriority.CODEX_CALLBACK
        if callback and (
            any(
                current.provider_id is ProviderId.CODEX
                and current.kind in _SELECTION_OPERATION_KINDS
                for current in owned
            )
        ):
            return False
        if (
            operation.provider_id is ProviderId.CODEX
            and operation.kind in _SELECTION_OPERATION_KINDS
            and any(
                current.provider_id is ProviderId.CODEX
                and (
                    current.priority is OperationPriority.CODEX_CALLBACK
                    or current.kind in _SELECTION_OPERATION_KINDS
                )
                for current in owned
            )
        ):
            return False
        if any(
            operation.account_id is not None
            and current.account_id == operation.account_id
            for current in owned
        ):
            return False
        if callback:
            return not any(
                current.priority is OperationPriority.CODEX_CALLBACK
                for current in owned
            )
        general = sum(
            current.priority is not OperationPriority.CODEX_CALLBACK
            for current in owned
        )
        return general < self._general_limit

    def can_start(self, operation: DueOperation) -> bool:
        """Return whether capacity and required exchange are available."""
        if not self.has_capacity_for(operation):
            return False
        return not operation_requires_worker_exchange(operation) or (
            self._exchanges is not None
            and self._exchanges.available(operation.operation_id)
        )

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
        exchange = self._operation_exchange(operation)
        spec = self._planner.plan(
            operation.operation_id,
            exchange_descriptor=(
                None if exchange is None else exchange.child_descriptor
            ),
        )
        handle = self._launch(operation, spec, exchange)
        deadline = (
            exchange.completion_deadline
            if exchange is not None
            else monotonic_now + self._general_timeout
        )
        self._active[operation.operation_id] = ActiveWorker(
            operation,
            handle,
            deadline,
        )

    def _operation_exchange(
        self,
        operation: DueOperation,
    ) -> SupervisorWorkerExchange | None:
        if not operation_requires_worker_exchange(operation):
            return None
        if self._exchanges is None:
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.CAPACITY_UNAVAILABLE
            )
        exchange = self._exchanges.claim(operation.operation_id)
        if exchange is None:
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.CAPACITY_UNAVAILABLE
            )
        return exchange

    def _launch(
        self,
        operation: DueOperation,
        spec: WorkerLaunchSpec,
        exchange: SupervisorWorkerExchange | None,
    ) -> WorkerHandle:
        try:
            handle = self._launcher.launch(spec, self._notify_exit)
        except WorkerLaunchError:
            self._abort_exchange(operation.operation_id)
            raise
        except Exception:
            self._abort_exchange(operation.operation_id)
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.EXECUTABLE_UNSAFE
            ) from None
        try:
            if exchange is not None:
                if (
                    self._exchanges is None
                    or not self._exchanges.finish_launch(
                        operation.operation_id
                    )
                ):
                    raise WorkerExchangeError
                exchange.worker_started()
        except Exception:
            if not self._terminate_launched(handle):
                self._quarantine_worker(
                    operation,
                    handle,
                    completion_pending=False,
                )
            self._abort_exchange(operation.operation_id)
            raise WorkerLaunchError(
                WorkerLaunchFailureCode.EXECUTABLE_UNSAFE
            ) from None
        return handle

    def _terminate_launched(self, handle: WorkerHandle) -> bool:
        return self._terminate_handle(handle) is not None

    def preempt_for_callback(
        self,
        callback: DueOperation,
        *,
        monotonic_now: float,
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
            quarantined = next(
                (
                    current
                    for current in self._quarantine.values()
                    if current.operation.account_id == callback.account_id
                ),
                None,
            )
            if quarantined is None:
                return None
            if (
                quarantined.operation.priority
                is OperationPriority.CODEX_CALLBACK
                or quarantined.operation.kind in _SELECTION_OPERATION_KINDS
            ):
                raise WorkerLaunchError(WorkerLaunchFailureCode.AUTHORITY_BUSY)
            cleaned, completed = self._cleanup_quarantined(
                quarantined,
                monotonic_now,
                force=True,
            )
            if not cleaned:
                raise WorkerLaunchError(WorkerLaunchFailureCode.AUTHORITY_BUSY)
            return completed
        if (
            owner.operation.priority is OperationPriority.CODEX_CALLBACK
            or owner.operation.kind in _SELECTION_OPERATION_KINDS
        ):
            raise WorkerLaunchError(WorkerLaunchFailureCode.AUTHORITY_BUSY)
        return self._stop(owner, preempted=True, timed_out=False)

    def codex_transition_active(self) -> bool:
        """Return whether a non-preemptible Codex transition is running."""
        return any(
            operation.provider_id is ProviderId.CODEX
            and operation.kind in _SELECTION_OPERATION_KINDS
            for operation in self._owned_operations()
        )

    def cancel_exchange(self, operation_id: OperationId) -> None:
        """Close one worker exchange that will never launch."""
        if self._exchanges is not None:
            self._exchanges.cancel(operation_id)

    def complete_exchange(
        self,
        operation_id: OperationId,
        outcome: WorkerOutcome,
    ) -> None:
        """Publish exchange completion after durable scheduler cleanup."""
        if self._exchanges is not None:
            self._exchanges.complete(
                operation_id,
                outcome is WorkerOutcome.SUCCEEDED,
            )

    def close_exchanges(self) -> None:
        """Cancel exchanges that cannot survive supervisor exit."""
        if self._exchanges is not None:
            self._exchanges.close()

    def reap_completed(
        self,
        monotonic_now: float,
    ) -> tuple[WorkerExit, ...]:
        """Remove every naturally exited worker without polling idle loops."""
        completed = list(self._reap_quarantine(monotonic_now))
        for active in tuple(self._active.values()):
            exit_code = active.handle.poll()
            if exit_code is None:
                continue
            worker_exit = self._finish_natural_exit(active, exit_code)
            if worker_exit is not None:
                completed.append(worker_exit)
        return tuple(completed)

    def expire(self, monotonic_now: float) -> tuple[WorkerExit, ...]:
        """Terminate and reap every worker whose hard deadline elapsed."""
        expired = tuple(
            active
            for active in self._active.values()
            if active.deadline <= monotonic_now
        )
        exits: list[WorkerExit] = []
        for active in expired:
            try:
                exits.append(
                    self._stop(
                        active,
                        preempted=False,
                        timed_out=True,
                    )
                )
            except WorkerLaunchError:
                continue
        return tuple(exits)

    def next_deadline(self) -> float | None:
        """Return the nearest active monotonic deadline."""
        deadlines = [active.deadline for active in self._active.values()]
        deadlines.extend(
            quarantined.retry_at for quarantined in self._quarantine.values()
        )
        return min(deadlines) if deadlines else None

    def shutdown(self) -> tuple[WorkerExit, ...]:
        """Terminate and reap all remaining workers."""
        exits: list[WorkerExit] = []
        for active in tuple(self._active.values()):
            try:
                exits.append(
                    self._stop(
                        active,
                        preempted=True,
                        timed_out=False,
                    )
                )
            except WorkerLaunchError:
                continue
        for quarantined in tuple(self._quarantine.values()):
            _cleaned, completed = self._cleanup_quarantined(
                quarantined,
                self._monotonic(),
                force=True,
            )
            if completed is not None:
                exits.append(completed)
        if self._quarantine:
            raise WorkerLaunchError(WorkerLaunchFailureCode.TERMINATION_FAILED)
        return tuple(exits)

    def _stop(
        self,
        active: ActiveWorker[WorkerHandle],
        *,
        preempted: bool,
        timed_out: bool,
    ) -> WorkerExit:
        natural_exit_code = active.handle.poll()
        if natural_exit_code is not None:
            natural_exit = self._finish_natural_exit(
                active,
                natural_exit_code,
            )
            if natural_exit is None:
                raise WorkerLaunchError(
                    WorkerLaunchFailureCode.TERMINATION_FAILED
                )
            return natural_exit
        exit_code = self._terminate_handle(active.handle)
        if exit_code is None:
            self._active.pop(active.operation.operation_id, None)
            self._quarantine_worker(
                active.operation,
                active.handle,
                completion_pending=True,
                timed_out=timed_out,
                preempted=preempted,
            )
            raise WorkerLaunchError(WorkerLaunchFailureCode.TERMINATION_FAILED)
        self._active.pop(active.operation.operation_id, None)
        return WorkerExit(
            active.operation,
            exit_code,
            timed_out=timed_out,
            preempted=preempted,
        )

    def _finish_natural_exit(
        self,
        active: ActiveWorker[WorkerHandle],
        exit_code: int,
    ) -> WorkerExit | None:
        self._active.pop(active.operation.operation_id, None)
        if not self._clear_residual_group(active.handle):
            self._quarantine_worker(
                active.operation,
                active.handle,
                completion_pending=True,
            )
            return None
        return WorkerExit(active.operation, exit_code)

    def _abort_exchange(self, operation_id: OperationId) -> None:
        if self._exchanges is not None:
            self._exchanges.abort_launch(operation_id)

    def _terminate_handle(self, handle: WorkerHandle) -> int | None:
        handle.terminate_group()
        exit_code = handle.wait(self._termination_grace)
        if exit_code is None or handle.group_alive():
            handle.kill_group()
            if exit_code is None:
                exit_code = handle.wait(self._termination_grace)
        if exit_code is None or not _wait_for_group_exit(
            handle, self._termination_grace
        ):
            return None
        return exit_code

    def _clear_residual_group(self, handle: WorkerHandle) -> bool:
        if not handle.group_alive():
            return True
        handle.terminate_group()
        if _wait_for_group_exit(handle, self._termination_grace):
            return True
        handle.kill_group()
        return _wait_for_group_exit(handle, self._termination_grace)

    def _owned_operations(self) -> tuple[DueOperation, ...]:
        return (
            *(active.operation for active in self._active.values()),
            *(
                quarantined.operation
                for quarantined in self._quarantine.values()
            ),
        )

    def _quarantine_worker(
        self,
        operation: DueOperation,
        handle: WorkerHandle,
        *,
        completion_pending: bool,
        timed_out: bool = False,
        preempted: bool = False,
    ) -> None:
        self._quarantine[operation.operation_id] = QuarantinedWorker(
            operation=operation,
            handle=handle,
            attempts=1,
            retry_at=(self._monotonic() + QUARANTINE_INITIAL_RETRY_SECONDS),
            completion_pending=completion_pending,
            timed_out=timed_out,
            preempted=preempted,
        )

    def _reap_quarantine(
        self,
        monotonic_now: float,
    ) -> tuple[WorkerExit, ...]:
        completed: list[WorkerExit] = []
        for quarantined in tuple(self._quarantine.values()):
            cleaned, worker_exit = self._cleanup_quarantined(
                quarantined,
                monotonic_now,
                force=False,
            )
            if cleaned and worker_exit is not None:
                completed.append(worker_exit)
        return tuple(completed)

    def _cleanup_quarantined(
        self,
        quarantined: QuarantinedWorker[WorkerHandle],
        monotonic_now: float,
        *,
        force: bool,
    ) -> tuple[bool, WorkerExit | None]:
        if not force and quarantined.retry_at > monotonic_now:
            return False, None
        exit_code = self._terminate_handle(quarantined.handle)
        if exit_code is None:
            quarantined.attempts += 1
            quarantined.retry_at = monotonic_now + _quarantine_retry_seconds(
                quarantined.attempts
            )
            return False, None
        self._quarantine.pop(
            quarantined.operation.operation_id,
            None,
        )
        if not quarantined.completion_pending:
            return True, None
        return True, WorkerExit(
            quarantined.operation,
            exit_code,
            timed_out=quarantined.timed_out,
            preempted=quarantined.preempted,
        )


def _wait_and_notify(
    handle: WorkerHandle,
    notify_exit: ExitNotifier,
) -> None:
    handle.wait(None)
    notify_exit()


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


def _wait_for_group_exit(
    handle: WorkerHandle,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while handle.group_alive():
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    return True


def _quarantine_retry_seconds(attempts: int) -> float:
    exponent = max(0, min(attempts - 1, 30))
    return min(
        QUARANTINE_INITIAL_RETRY_SECONDS * (2**exponent),
        QUARANTINE_MAX_RETRY_SECONDS,
    )
