"""Protected Claude projection exchange boundaries."""

from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import OperationKind


class ClaudeProtectedSupervisorExchange(Protocol):
    """Resident half of one existing bounded worker exchange."""

    def receive_response(self) -> bytearray:
        """Receive the sole worker response."""

    def acknowledge(self, payload: bytes | bytearray) -> None:
        """Acknowledge receipt without returning a credential."""


class ClaudeProtectedExchangeRegistry(Protocol):
    """Create and cancel exact child exchanges owned by the scheduler."""

    def create(
        self,
        operation_id: OperationId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> ClaudeProtectedSupervisorExchange:
        """Create one exchange before the child is enqueued."""

    def cancel(self, operation_id: OperationId) -> None:
        """Cancel the exact child exchange."""


class ClaudeProtectedWorkerSubmission(Protocol):
    """Worker response awaiting one safe supervisor receipt."""

    def receive_acknowledgement(self) -> bytearray:
        """Return the sole safe acknowledgement."""


class ClaudeProtectedWorkerExchange(Protocol):
    """Worker half of one existing bounded exchange."""

    def receive_instruction(self) -> bytearray:
        """Receive one safe exact-child instruction."""

    def submit(
        self,
        payload: bytearray,
        response_deadline: float,
        completion_deadline: float,
    ) -> ClaudeProtectedWorkerSubmission:
        """Submit one mutable projection and clear the payload."""


class ClaudeProtectedCompletion(Protocol):
    """Safe selection metadata required after scheduler completion."""

    @property
    def kind(self) -> OperationKind:
        """Return the exact selection child kind."""

    @property
    def observed_account_id(self) -> SidekickAccountId | None:
        """Return the worker-observed exact account."""

    @property
    def observed_generation(self) -> AuthorityGeneration | None:
        """Return the worker-observed exact generation."""

    @property
    def pending_epoch(self) -> SelectionEpoch:
        """Return the pending selection epoch."""
