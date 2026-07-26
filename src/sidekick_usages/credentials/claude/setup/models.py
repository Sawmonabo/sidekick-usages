"""Validated Claude setup-token persistence inputs."""

from dataclasses import dataclass, field

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.models import ClaudeSetupTokenCredentials
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.types.credential import StoredCredentialKind


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeSetupTokenUpdate:
    """Pair one no-secret account update with its protected token."""

    account: SavedAccount
    authority: StoredCredentialAuthority = field(repr=False)

    def __post_init__(self) -> None:
        """Require one internally consistent Claude setup-token update."""
        account_authority = self.account.authority
        setup_token = (
            account_authority.setup_token
            if isinstance(account_authority, ClaudeAccountAuthority)
            else None
        )
        if (
            self.account.provider_id is not ProviderId.CLAUDE
            or setup_token is None
            or self.authority.account_id != self.account.account_id
            or self.authority.authority_id != setup_token.authority_id
            or self.authority.provider_id is not ProviderId.CLAUDE
            or self.authority.kind is not StoredCredentialKind.CLAUDE_SETUP
            or not isinstance(
                self.authority.credentials,
                ClaudeSetupTokenCredentials,
            )
        ):
            raise ValueError("Claude setup-token update is inconsistent.")
