"""Durable scheduler and operation update models."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import SelectionWorkerMetadata
from sidekick_usages.daemon.types.protocol import ProgressPhase
from sidekick_usages.daemon.types.worker import WorkerOutcome


@dataclass(frozen=True, slots=True)
class SchedulerCompletion:
    """One durable post-worker queue outcome."""

    provider_id: ProviderId
    operation_id: OperationId
    operation_kind: OperationKind
    state: OperationState | None
    outcome: WorkerOutcome
    failure_code: str | None
    selection: SelectionWorkerMetadata | None = None

    def __post_init__(self) -> None:
        """Require selection metadata to match its exact phase."""
        if self.selection is not None and (
            self.selection.operation_id != self.operation_id
            or self.selection.provider_id is not self.provider_id
            or self.selection.kind is not self.operation_kind
        ):
            raise ValueError("Scheduler selection completion is unrelated.")


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
