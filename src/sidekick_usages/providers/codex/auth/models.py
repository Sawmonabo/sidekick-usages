"""Validated Codex authentication models."""

from dataclasses import dataclass, field

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.auth.generation import (
    CodexGenerationOrder,
)


@dataclass(frozen=True, slots=True)
class CodexAuthSnapshot:
    """Validated identity and generation from one protected Codex home."""

    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    generation_order: CodexGenerationOrder = field(repr=False)
    plan: str

    def __post_init__(self) -> None:
        """Validate safe metadata and the provider generation ordering key."""
        if any(value < 0 for value in self.generation_order):
            raise ValueError("Codex generation order is invalid.")
        require_bounded_text(
            self.plan,
            name="Codex plan",
            maximum=MAX_METADATA_BYTES,
        )

    def advanced_from(self, previous: CodexAuthSnapshot) -> bool:
        """Return whether this same-account generation is newer."""
        return (
            self.provider_identity == previous.provider_identity
            and self.generation_order > previous.generation_order
        )

    def not_older_than(self, generation: CodexAuthSnapshot) -> bool:
        """Return whether this same-account generation did not regress."""
        return (
            self.provider_identity == generation.provider_identity
            and self.generation_order >= generation.generation_order
        )


@dataclass(frozen=True, slots=True)
class CodexTokenClaims:
    """Validated identity-bearing claims from one Codex access token."""

    expiry_seconds: int | None
    provider_identity: ProviderIdentity | None
    plan: str | None
