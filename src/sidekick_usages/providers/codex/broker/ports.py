"""Structural ports for the resident Codex refresh broker."""

from typing import Protocol

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectedAccountState


class CodexProjection(Protocol):
    """Expose one short-lived locally proven account projection."""

    @property
    def account_id(self) -> SidekickAccountId:
        """Return the stable Sidekick account identifier."""

    @property
    def provider_identity(self) -> ProviderIdentity:
        """Return the locally proven ChatGPT account identifier."""

    @property
    def generation(self) -> AuthorityGeneration:
        """Return the protected managed-home generation."""

    @property
    def plan(self) -> str:
        """Return the validated plan supplied by managed Codex."""

    @property
    def access_token(self) -> str:
        """Return the credential only while the projection is active."""


class CodexCallbackExchange(Protocol):
    """One live isolated-worker response and acknowledgement channel."""

    def receive_response(self) -> bytearray:
        """Return one bounded worker response."""

    def acknowledge(self, payload: bytes | bytearray) -> None:
        """Send one secret-free response-dispatch acknowledgement."""

    def wait_for_completion(self) -> bool:
        """Wait for successful durable callback completion."""


class CodexCallbackDispatcher(Protocol):
    """Dispatch one correlated Codex operation through the reserved lane."""

    def dispatch(
        self,
        operation_id: OperationId,
        account_id: SidekickAccountId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> CodexCallbackExchange:
        """Persist and wake one callback operation."""

    def cancel(self, operation_id: OperationId) -> None:
        """Cancel one callback whose daemon recipient disappeared."""


class CodexSelectionReader(Protocol):
    """Read the currently verified Codex projection without credentials."""

    def current(self) -> SelectedAccountState | None:
        """Return the selected saved authority when callback-safe."""
