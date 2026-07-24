"""Stable Sidekick-owned identifier generation."""

from uuid import uuid4

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)


def new_sidekick_account_id() -> SidekickAccountId:
    """Generate one random stable account ID."""
    return SidekickAccountId(str(uuid4()))


def new_authority_id() -> AuthorityId:
    """Generate one random stable credential-authority ID."""
    return AuthorityId(str(uuid4()))
