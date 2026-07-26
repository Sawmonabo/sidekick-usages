"""Secret-safe resident-service result for managed-auth migration."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.validation import require_bounded_text
from sidekick_usages.core.types import ExitCode
from sidekick_usages.credentials.migration.types.service import (
    MANAGED_AUTH_MESSAGE_MAX_BYTES,
    ManagedAuthServiceState,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ManagedAuthServiceResult:
    """One provider-neutral service-readiness observation."""

    state: ManagedAuthServiceState
    message: str
    exit_code: ExitCode

    def __post_init__(self) -> None:
        """Require closed state, exit code, and bounded safe copy."""
        if not isinstance(self.state, ManagedAuthServiceState):
            raise ValueError("Managed-auth service state is invalid.")
        if not isinstance(self.exit_code, ExitCode):
            raise ValueError("Managed-auth service exit code is invalid.")
        require_bounded_text(
            self.message,
            name="Managed-auth service message",
            maximum=MANAGED_AUTH_MESSAGE_MAX_BYTES,
        )
