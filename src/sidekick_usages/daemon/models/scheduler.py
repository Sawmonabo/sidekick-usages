"""Durable scheduler and operation update models."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.types import OperationState
from sidekick_usages.daemon.types.protocol import ProgressPhase
from sidekick_usages.daemon.types.worker import WorkerOutcome


@dataclass(frozen=True, slots=True)
class SchedulerCompletion:
    """One durable post-worker queue outcome."""

    operation_id: OperationId
    state: OperationState | None
    outcome: WorkerOutcome
    failure_code: str | None


@dataclass(frozen=True, slots=True)
class OperationUpdate:
    """One progress or terminal operation notification."""

    sequence: int
    operation_id: OperationId
    phase: ProgressPhase | None = None
    completion: SchedulerCompletion | None = None

    def __post_init__(self) -> None:
        """Require exactly one progress or terminal value."""
        if (self.phase is None) == (self.completion is None):
            raise ValueError("Operation update must be progress or terminal.")
