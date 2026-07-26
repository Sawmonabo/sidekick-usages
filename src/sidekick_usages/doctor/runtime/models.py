"""Secret-safe account runtime diagnostics."""

from dataclasses import dataclass, field
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
    AuthorityGenerationRelation,
    OperationKind,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.doctor.runtime.types import NativeAccountRelation


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRuntimeDiagnostic:
    """Native relation and passive metrics state for one saved account."""

    account_id: SidekickAccountId = field(repr=False)
    native_relation: NativeAccountRelation
    selected_generation_relation: AuthorityGenerationRelation
    metrics_freshness: MetricsFreshness
    metrics_observed_at: datetime | None

    def __post_init__(self) -> None:
        """Require unavailable metrics to have no observation timestamp."""
        unavailable = self.metrics_freshness is MetricsFreshness.UNAVAILABLE
        if unavailable != (self.metrics_observed_at is None):
            raise ValueError("Doctor metrics state is inconsistent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduledOperationDiagnostic:
    """Secret-free durable due and retry state."""

    provider_id: ProviderId
    account_label: AccountLabel | None
    kind: OperationKind
    state: OperationState
    due_at: datetime
    updated_at: datetime
    attempts: int
    failure_code: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class UnfinishedActivationDiagnostic:
    """Secret-free unfinished provider activation state."""

    provider_id: ProviderId
    target_label: AccountLabel
    phase: ActivationPhase
    started_at: datetime
    updated_at: datetime
    failure_code: str | None
