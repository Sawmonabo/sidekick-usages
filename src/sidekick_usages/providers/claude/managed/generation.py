"""Managed Claude credential-generation identity."""

from sidekick_usages.core.accounts.generation import (
    hashed_authority_generation,
)
from sidekick_usages.core.accounts.types import AuthorityGeneration

_CLAUDE_GENERATION_PREFIX = "claude-access-token-sha256:"


def claude_access_token_generation(token: str) -> AuthorityGeneration:
    """Return the one-way generation for one Claude access credential."""
    return hashed_authority_generation(
        token,
        prefix=_CLAUDE_GENERATION_PREFIX,
    )
