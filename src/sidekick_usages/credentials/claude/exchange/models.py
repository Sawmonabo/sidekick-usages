"""Secret-safe models for official Claude credential exchanges."""

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

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
    plan: str
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    scopes: tuple[str, ...]
    modified_milliseconds: Decimal | None


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
        plan=snapshot.plan,
        access_expires_at=snapshot.access_expires_at,
        refresh_expires_at=snapshot.refresh_expires_at,
        scopes=snapshot.scopes,
        modified_milliseconds=snapshot.modified_milliseconds,
    )


def native_authority_expectation(
    target: ClaudeAuthoritySnapshot,
    modified_milliseconds: Decimal | None,
) -> ClaudeAuthorityExpectation:
    """Bind target semantics to the current native ``mtimeMs`` baseline."""
    return replace(
        authority_expectation(target),
        modified_milliseconds=modified_milliseconds,
    )
