"""Secret-free results for managed Codex authority operations."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.types import CodexManagedOutcome


@dataclass(frozen=True, slots=True)
class CodexManagedAuthorityResult:
    """One persisted managed-account outcome containing no credentials."""

    outcome: CodexManagedOutcome
    account: SavedAccount

    def __post_init__(self) -> None:
        """Require a managed Codex account result."""
        if (
            self.account.provider_id is not ProviderId.CODEX
            or not self.account.has_managed_authority
        ):
            raise ValueError("Managed Codex result account is invalid.")
