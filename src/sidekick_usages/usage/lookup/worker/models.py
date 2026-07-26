"""Secret-free global lookup-worker process contracts."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.platform.environment import require_worker_environment
from sidekick_usages.platform.types import WorkerEnvironment

type UsageLookupEventObserver = Callable[[UsageLookupWorkerEvent], None]

USAGE_LOOKUP_MODULE = "sidekick_usages.entrypoints.usage_lookup"


class UsageLookupEventKind(StrEnum):
    """Closed global lookup-worker event kinds."""

    ACCOUNT_COMPLETED = "account_completed"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class UsageLookupFailure(StrEnum):
    """Safe global lookup-worker failure categories."""

    FEATURE_DISABLED = "feature_disabled"
    INTERPRETER_UNSAFE = "interpreter_unsafe"
    LAUNCH_FAILED = "launch_failed"
    TIMED_OUT = "timed_out"
    MALFORMED_PROTOCOL = "malformed_protocol"
    INTERNAL = "internal"
    TERMINATION_FAILED = "termination_failed"


@dataclass(frozen=True, slots=True)
class UsageLookupWorkerEvent:
    """One immutable event keyed by stable account identity."""

    kind: UsageLookupEventKind
    account_id: SidekickAccountId | None = None
    failure: UsageLookupFailure | None = None

    def __post_init__(self) -> None:
        """Require exactly the fields owned by the selected event kind."""
        account_event = self.kind is UsageLookupEventKind.ACCOUNT_COMPLETED
        failure_event = self.kind is UsageLookupEventKind.FAILED
        if (self.account_id is not None) is not account_event:
            raise ValueError("Lookup event account identity is invalid.")
        if (self.failure is not None) is not failure_event:
            raise ValueError("Lookup event failure is invalid.")


@dataclass(frozen=True, slots=True)
class UsageLookupWorkerResult:
    """Completed stable IDs plus one optional terminal worker failure."""

    completed_account_ids: tuple[SidekickAccountId, ...]
    failure: UsageLookupFailure | None = None

    def __post_init__(self) -> None:
        """Reject duplicate account completions."""
        if len(self.completed_account_ids) != len(
            set(self.completed_account_ids)
        ):
            raise ValueError("Lookup worker completed an account twice.")

    @property
    def succeeded(self) -> bool:
        """Return whether the worker reached its successful terminal event."""
        return self.failure is None


@dataclass(frozen=True, slots=True)
class UsageLookupModuleLaunchSpec:
    """Exact Python module process launch contract."""

    argv: tuple[str, str, str]
    environment: WorkerEnvironment = field(repr=False)

    def __post_init__(self) -> None:
        """Reject module expansion and unsafe process environments."""
        interpreter, module_flag, module = self.argv
        if (
            not Path(interpreter).is_absolute()
            or module_flag != "-m"
            or module != USAGE_LOOKUP_MODULE
        ):
            raise ValueError("Usage lookup module launch is invalid.")
        require_worker_environment(self.environment)

    def environment_map(self) -> dict[str, str]:
        """Return a fresh minimal subprocess environment."""
        return dict(self.environment)
