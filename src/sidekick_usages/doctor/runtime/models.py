"""Secret-safe account runtime diagnostics."""

from dataclasses import dataclass, field
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    SelectionEpoch,
    safe_outcome_code,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
    AuthorityGenerationRelation,
    OperationKind,
    OperationState,
    SelectionCode,
    SelectionPhase,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
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


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSessionDiagnostic:
    """Safe live selection and participant state for one provider."""

    provider_id: ProviderId
    finalized_account_id: SidekickAccountId | None = field(repr=False)
    finalized_epoch: SelectionEpoch | None
    target_account_id: SidekickAccountId | None = field(repr=False)
    pending_epoch: SelectionEpoch | None
    phase: SelectionPhase | None
    code: SelectionCode | None
    registered_count: int
    reachable_count: int
    required_count: int
    ready_count: int
    adopted_count: int
    unreachable_count: int
    confirmed_dead_after_commit_count: int
    active_turn_count: int
    queued_turn_count: int
    unmanaged_count: int | None

    @property
    def session_enrollment(self) -> str:
        """Return whether an integrated participant was observed."""
        return "observed" if self.registered_count else "not_observed"

    @property
    def protected_session_state(self) -> str:
        """Return only provider-owned protected-session qualification."""
        if self.registered_count:
            return "unavailable"
        return "not_observed"


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractiveRuntimeDiagnostic:
    """Safe shell and live provider-session diagnostic snapshot."""

    selection_status: ServiceComponentState
    shell_integration_code: str
    providers: tuple[ProviderSessionDiagnostic, ...]

    def __post_init__(self) -> None:
        """Require canonical providers only for available live state."""
        safe_outcome_code(self.shell_integration_code)
        provider_ids = tuple(item.provider_id for item in self.providers)
        if self.selection_status is ServiceComponentState.HEALTHY:
            canonical = tuple(
                provider_id
                for provider_id in ProviderId
                if provider_id in provider_ids
            )
            if not provider_ids or provider_ids != canonical:
                raise ValueError(
                    "Doctor session providers must use canonical order."
                )
        elif self.providers:
            raise ValueError(
                "Unavailable Doctor selection state cannot list providers."
            )

    def scoped(
        self,
        provider_id: ProviderId | None,
    ) -> InteractiveRuntimeDiagnostic:
        """Return canonical session diagnostics in one provider scope."""
        if provider_id is None or not self.providers:
            return self
        return InteractiveRuntimeDiagnostic(
            selection_status=self.selection_status,
            shell_integration_code=self.shell_integration_code,
            providers=tuple(
                provider
                for provider in self.providers
                if provider.provider_id is provider_id
            ),
        )

    @classmethod
    def unavailable(cls) -> InteractiveRuntimeDiagnostic:
        """Build an unavailable diagnostic for isolated presentation tests."""
        return cls(
            selection_status=ServiceComponentState.UNAVAILABLE,
            shell_integration_code="unavailable",
            providers=(),
        )
