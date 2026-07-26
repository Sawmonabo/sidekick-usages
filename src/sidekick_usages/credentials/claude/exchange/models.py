"""Secret-safe models for official Claude credential exchanges."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.credentials.claude.exchange.types import (
    ClaudeExchangeFailureKind,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)

type ClaudeExchangeResult = ClaudeExchangeSuccess | ClaudeExchangeFailure


@dataclass(frozen=True, slots=True)
class ClaudeAuthorityExpectation:
    """Secret-free authority state that an exchange must advance."""

    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    scopes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaudeExchangeSuccess:
    """One verified official Claude credential generation."""

    snapshot: ClaudeAuthoritySnapshot


@dataclass(frozen=True, slots=True)
class ClaudeExchangeFailure:
    """One secret-safe official Claude exchange failure."""

    kind: ClaudeExchangeFailureKind


def authority_expectation(
    snapshot: ClaudeAuthoritySnapshot,
) -> ClaudeAuthorityExpectation:
    """Project one protected snapshot into exchange invariants."""
    return ClaudeAuthorityExpectation(
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        access_expires_at=snapshot.access_expires_at,
        refresh_expires_at=snapshot.refresh_expires_at,
        scopes=snapshot.scopes,
    )
