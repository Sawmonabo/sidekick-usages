"""Secret-free isolated worker process and result models."""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import (
    DueOperation,
    safe_outcome_code,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.daemon.types.worker import (
    WorkerExchangePhase,
    WorkerOutcome,
)
from sidekick_usages.platform.environment import (
    require_worker_environment,
)

WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY = (
    "SIDEKICK_WORKER_EXCHANGE_DESCRIPTOR"
)
MINIMUM_WORKER_EXCHANGE_DESCRIPTOR = 3


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerResult:
    """One bounded worker result containing no provider response."""

    operation_id: OperationId
    outcome: WorkerOutcome
    finished_at: datetime
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize time and require truthful safe failure metadata."""
        object.__setattr__(self, "finished_at", as_utc(self.finished_at))
        code = safe_outcome_code(self.failure_code)
        if self.outcome is WorkerOutcome.SUCCEEDED and code is not None:
            raise ValueError("Successful worker results cannot carry errors.")
        if self.outcome is not WorkerOutcome.SUCCEEDED and code is None:
            raise ValueError("Failed worker results require a safe code.")
        object.__setattr__(self, "failure_code", code)


@dataclass(frozen=True, slots=True)
class WorkerLaunchSpec:
    """Exact operation-ID-only process contract."""

    operation_id: OperationId
    argv: tuple[str, str]
    environment: tuple[tuple[str, str], ...] = field(repr=False)
    exchange_descriptor: int | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Reject argument or environment expansion."""
        executable, argument = self.argv
        if not Path(executable).is_absolute():
            raise ValueError("Worker executable must be absolute.")
        if argument != str(self.operation_id):
            raise ValueError(
                "Worker arguments must contain only operation ID."
            )
        require_worker_environment(self.environment)
        descriptor = self.exchange_descriptor
        if descriptor is not None and (
            type(descriptor) is not int
            or descriptor < MINIMUM_WORKER_EXCHANGE_DESCRIPTOR
        ):
            raise ValueError("Worker exchange descriptor is invalid.")

    def environment_map(self) -> dict[str, str]:
        """Return a fresh minimal subprocess environment."""
        environment = dict(self.environment)
        if self.exchange_descriptor is not None:
            environment[WORKER_EXCHANGE_DESCRIPTOR_ENVIRONMENT_KEY] = str(
                self.exchange_descriptor
            )
        return environment

    def inherited_descriptors(self) -> tuple[int, ...]:
        """Return the sole worker exchange descriptor when required."""
        return (
            ()
            if self.exchange_descriptor is None
            else (self.exchange_descriptor,)
        )


@dataclass(frozen=True, slots=True)
class WorkerExit:
    """One reaped worker and its supervisor-classified exit."""

    operation: DueOperation
    exit_code: int | None
    timed_out: bool = False
    preempted: bool = False


@dataclass(slots=True)
class ActiveWorker[HandleT]:
    """One live worker process and its monotonic deadline."""

    operation: DueOperation
    handle: HandleT
    deadline: float


@dataclass(slots=True)
class QuarantinedWorker[HandleT]:
    """One residual worker group awaiting bounded cleanup."""

    operation: DueOperation
    handle: HandleT
    attempts: int
    retry_at: float
    completion_pending: bool
    timed_out: bool = False
    preempted: bool = False

    def __post_init__(self) -> None:
        """Require explicit positive cleanup scheduling state."""
        if self.attempts < 1 or self.retry_at < 0:
            raise ValueError("Worker quarantine state is invalid.")


@dataclass(slots=True)
class WorkerExchangeRegistration[ExchangeT]:
    """One registry-owned exchange during worker descriptor inheritance."""

    exchange: ExchangeT
    phase: WorkerExchangePhase = WorkerExchangePhase.READY
    cancellation_requested: bool = False
