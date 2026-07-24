"""Credential refresh persistence models."""

from dataclasses import dataclass, field
from datetime import datetime

from sidekick_usages.core.models import Credentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)


@dataclass(frozen=True, slots=True)
class DecodedCredentialRefreshStage:
    """One validated credential replacement and optional private bundle."""

    label: AccountLabel
    credentials: Credentials = field(repr=False)
    completed_at: datetime
    plan_update: str | None
    private_bundle: PreparedPrivateBundleWrite | None = field(repr=False)
