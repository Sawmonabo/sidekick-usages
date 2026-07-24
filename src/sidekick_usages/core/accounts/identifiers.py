"""Stable Sidekick-owned identifier generation."""

from uuid import uuid4

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    OperationId,
    RequestId,
    SidekickAccountId,
)


def new_sidekick_account_id() -> SidekickAccountId:
    """Generate one random stable account ID."""
    return SidekickAccountId(str(uuid4()))


def new_authority_id() -> AuthorityId:
    """Generate one random stable credential-authority ID."""
    return AuthorityId(str(uuid4()))


def new_operation_id() -> OperationId:
    """Generate one random durable operation ID."""
    return OperationId(str(uuid4()))


def new_request_id() -> RequestId:
    """Generate one random local control request ID."""
    return RequestId(str(uuid4()))
