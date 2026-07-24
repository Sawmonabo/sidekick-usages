"""Secret-free results for managed Codex authority operations."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import AuthorityId, ProviderIdentity
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.providers.codex.models import CodexAuthSnapshot


@dataclass(frozen=True, slots=True)
class CodexAuthorityExpectation:
    """Saved identity, authority, and optional generation baseline."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity
    baseline: CodexAuthSnapshot | None

    def __post_init__(self) -> None:
        """Reject a baseline that belongs to another provider identity."""
        if (
            self.baseline is not None
            and self.baseline.provider_identity != self.provider_identity
        ):
            raise ValueError("Codex authority baseline identity is invalid.")


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
