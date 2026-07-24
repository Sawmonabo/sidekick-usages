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
from sidekick_usages.daemon.types.worker import WorkerOutcome

ALLOWED_WORKER_ENVIRONMENT_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "TMPDIR",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)
_MAX_ENVIRONMENT_VALUE_BYTES = 16 * 1024

__all__ = [
    "ALLOWED_WORKER_ENVIRONMENT_KEYS",
    "ActiveWorker",
    "WorkerExit",
    "WorkerLaunchSpec",
    "WorkerResult",
]


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

    def __post_init__(self) -> None:
        """Reject argument or environment expansion."""
        executable, argument = self.argv
        if not Path(executable).is_absolute():
            raise ValueError("Worker executable must be absolute.")
        if argument != str(self.operation_id):
            raise ValueError(
                "Worker arguments must contain only operation ID."
            )
        keys = tuple(key for key, _value in self.environment)
        if (
            len(keys) != len(set(keys))
            or tuple(sorted(keys)) != keys
            or not set(keys) <= ALLOWED_WORKER_ENVIRONMENT_KEYS
        ):
            raise ValueError("Worker environment is not a minimal allowlist.")
        for _key, value in self.environment:
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError(
                    "Worker environment must be valid UTF-8."
                ) from None
            if len(encoded) > _MAX_ENVIRONMENT_VALUE_BYTES or "\x00" in value:
                raise ValueError("Worker environment value is unsafe.")

    def environment_map(self) -> dict[str, str]:
        """Return a fresh minimal subprocess environment."""
        return dict(self.environment)


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
