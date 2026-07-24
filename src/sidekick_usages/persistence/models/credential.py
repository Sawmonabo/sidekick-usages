"""Private credential persistence result models."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.types.credential import StoredCredentialKind


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredCredentialAuthority:
    """One protected credential authority with a redacted representation."""

    authority_id: AuthorityId
    account_id: SidekickAccountId
    provider_id: ProviderId
    kind: StoredCredentialKind
    credentials: Credentials = field(repr=False)

    def __post_init__(self) -> None:
        if self.credentials.provider_id is not self.provider_id:
            raise ValueError("Credential authority provider does not match.")
        if stored_credential_kind(self.credentials) is not self.kind:
            raise ValueError("Credential authority kind does not match.")


def stored_credential_kind(
    credentials: Credentials,
) -> StoredCredentialKind:
    """Return the protected persistence variant for validated credentials."""
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return StoredCredentialKind.CLAUDE_SETUP
    if isinstance(credentials, ClaudeLoginCredentials):
        return StoredCredentialKind.CLAUDE_LOGIN
    if isinstance(credentials, CodexCredentials):
        return StoredCredentialKind.CODEX_LOGIN
    raise TypeError("Unsupported credential variant.")


@dataclass(frozen=True, slots=True)
class PrivateCredentialRepairResult:
    """Verified outcome of one explicit private-permission repair."""

    root: Path
    account_parent_repaired: bool
    directories_repaired: int
    files_repaired: int
    artifacts_present: bool

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError(
                "Private credential repair root must be absolute."
            )
        if type(self.account_parent_repaired) is not bool:
            raise TypeError("account_parent_repaired must be Boolean.")
        if self.directories_repaired < 0 or self.files_repaired < 0:
            raise ValueError(
                "Private credential repair counts cannot be negative."
            )
        if type(self.artifacts_present) is not bool:
            raise TypeError("artifacts_present must be Boolean.")
