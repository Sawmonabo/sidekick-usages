"""Secret-safe managed Claude maintenance models."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.providers.claude.managed.storage.models import (
    ClaudeAuthoritySnapshot,
)


@dataclass(frozen=True, slots=True)
class ClaudeManagedAuthorityResult:
    """One secret-free managed Claude maintenance result."""

    outcome: ClaudeManagedOutcome
    account: SavedAccount


@dataclass(frozen=True, slots=True)
class ClaudeVerifiedAuthorityExchange:
    """One verified secret-free Claude generation transition."""

    source: SavedAccount
    before: ClaudeAuthoritySnapshot
    after: ClaudeAuthoritySnapshot


def require_managed_claude_authority(
    account: SavedAccount,
) -> ClaudeManagedLoginAuthority:
    """Return one validated managed Claude subscription authority."""
    authority = account.authority
    if not isinstance(authority, ClaudeAccountAuthority):
        raise ValueError("Account is not managed by Claude.")
    subscription = authority.subscription
    if not isinstance(subscription, ClaudeManagedLoginAuthority):
        raise ValueError("Claude account is not a managed authority.")
    return subscription
