"""Secret-safe account runtime diagnostics."""

from dataclasses import dataclass, field
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.doctor.runtime.types import NativeAccountRelation


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRuntimeDiagnostic:
    """Native relation and passive metrics state for one saved account."""

    account_id: SidekickAccountId = field(repr=False)
    native_relation: NativeAccountRelation
    metrics_freshness: MetricsFreshness
    metrics_observed_at: datetime | None

    def __post_init__(self) -> None:
        """Require unavailable metrics to have no observation timestamp."""
        unavailable = self.metrics_freshness is MetricsFreshness.UNAVAILABLE
        if unavailable != (self.metrics_observed_at is None):
            raise ValueError("Doctor metrics state is inconsistent.")
