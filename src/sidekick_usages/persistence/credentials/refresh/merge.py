"""Typed account fields allowed during credential-refresh merge."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.models import Credentials
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)

type CredentialRefreshMerge = (
    CredentialRefreshSuccessMerge | CredentialRefreshFailureMerge
)


@dataclass(frozen=True, slots=True)
class CredentialRefreshSuccessMerge:
    """Validated successful fields allowed in one targeted refresh merge."""

    credentials: Credentials
    plan: str | None
    completed_at: datetime
    private_bundle: PreparedPrivateBundleWrite | None = None


@dataclass(frozen=True, slots=True)
class CredentialRefreshFailureMerge:
    """Safe failed fields allowed in one targeted refresh merge."""

    message: str
    completed_at: datetime
