"""Claude credential-generation identity."""

from sidekick_usages.core.accounts.generation import (
    hashed_authority_generation,
)
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
)

_CLAUDE_GENERATION_PREFIX = "claude-access-token-sha256:"


def claude_access_token_generation(token: str) -> AuthorityGeneration:
    """Return the one-way generation for one Claude access credential."""
    return hashed_authority_generation(
        token,
        prefix=_CLAUDE_GENERATION_PREFIX,
    )


def claude_access_token_buffer_generation(
    token: bytearray,
) -> AuthorityGeneration:
    """Return the one-way generation without materializing token text."""
    return hashed_authority_generation(
        token,
        prefix=_CLAUDE_GENERATION_PREFIX,
    )


def claude_generation_relation(
    saved: AuthorityGeneration,
    selected: AuthorityGeneration,
) -> AuthorityGenerationRelation:
    """Compare opaque Claude generations without claiming an order."""
    if selected == saved:
        return AuthorityGenerationRelation.CURRENT
    return AuthorityGenerationRelation.NOT_SAFELY_COMPARABLE
