"""Immutable secret-free cached dashboard models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.models import TokenActivitySummary, UsageReport
from sidekick_usages.core.selection.models import (
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.daemon.selection.models import SelectionStatus
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    UsageSnapshotFailureKind,
)

MAX_DASHBOARD_STATUS_MESSAGE_CHARACTERS = 512
_DASHBOARD_ACTIVITY_CACHE_ISSUES = frozenset(
    {
        ActivitySnapshotFailureKind.READ,
        ActivitySnapshotFailureKind.MALFORMED,
    }
)
_DASHBOARD_USAGE_CACHE_ISSUES = frozenset(
    {
        UsageSnapshotFailureKind.READ,
        UsageSnapshotFailureKind.MALFORMED,
    }
)

type DashboardRow = DashboardAccount


class DashboardActionState(StrEnum):
    """Closed account states that can affect dashboard interaction."""

    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    SWITCH_SETUP_REQUIRED = "switch_setup_required"
    REPAIR_REQUIRED = "repair_required"
    SETUP_REGENERATION_REQUIRED = "setup_regeneration_required"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    PROVIDER_UNSUPPORTED = "provider_unsupported"


class DashboardNavigationKind(StrEnum):
    """Closed keyboard navigation presentations."""

    KEYS = "keys"
    HELP = "help"


class DashboardStatusKind(StrEnum):
    """Closed transient dashboard status presentations."""

    PROGRESS = "progress"
    CONFIRMATION = "confirmation"
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardCursor:
    """One provider focus with an optional saved row."""

    focused_provider: ProviderId | None
    account_id: SidekickAccountId | None

    def __post_init__(self) -> None:
        """Require an empty dashboard to remain unfocused."""
        if self.focused_provider is None and self.account_id is not None:
            raise ValueError("An empty dashboard cursor cannot select a row.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardStatus:
    """One bounded transient dashboard status."""

    kind: DashboardStatusKind
    message: str

    def __post_init__(self) -> None:
        """Require non-empty bounded status copy."""
        if (
            not self.message.strip()
            or len(self.message) > MAX_DASHBOARD_STATUS_MESSAGE_CHARACTERS
        ):
            raise ValueError("Dashboard status requires a bounded message.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardFooter:
    """Independent navigation and optional transient dashboard status."""

    navigation: DashboardNavigationKind = DashboardNavigationKind.KEYS
    status: DashboardStatus | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardUsage:
    """One retained usage observation projected without provider identity."""

    plan: str
    report: UsageReport
    observed_at: datetime

    def __post_init__(self) -> None:
        """Normalize the observation time."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardActivity:
    """One retained activity observation without its provider identity."""

    summary: TokenActivitySummary
    observed_at: datetime

    def __post_init__(self) -> None:
        """Normalize the observation time."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardAccount:
    """One saved account joined to its passive dashboard observations."""

    account_id: SidekickAccountId
    label: AccountLabel
    provider_id: ProviderId
    plan: str
    credential_health: CredentialHealth
    active: bool
    states: tuple[DashboardActionState, ...]
    metrics_freshness: MetricsFreshness | None = None
    usage: DashboardUsage | None = None
    activity: DashboardActivity | None = None

    def __post_init__(self) -> None:
        """Validate states, metrics truth, and account-scoped activity."""
        if len(self.states) != len(set(self.states)):
            raise ValueError("Dashboard account states must be unique.")
        has_observation = self.usage is not None or self.activity is not None
        if (
            self.metrics_freshness is MetricsFreshness.FRESH
            or self.metrics_freshness is MetricsFreshness.STALE
        ) and not has_observation:
            raise ValueError(
                "Fresh or stale dashboard metrics require an observation."
            )
        if (
            self.metrics_freshness is MetricsFreshness.UNAVAILABLE
            and has_observation
        ):
            raise ValueError(
                "Unavailable dashboard metrics cannot retain an observation."
            )
        if (
            self.activity is not None
            and self.activity.summary.scope is not TokenActivityScope.ACCOUNT
        ):
            raise ValueError(
                "Dashboard account activity must be account-scoped."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProviderStatus:
    """One nonfocusable provider-runtime observation."""

    runtime_state: ProviderRuntimeState | None
    observed_at: datetime | None
    finalized_epoch: SelectionEpoch | None = None
    selection: SelectionStatus | SelectionResult | None = field(
        default=None,
        repr=False,
    )
    unmanaged_sessions: int | None = None

    def __post_init__(self) -> None:
        """Normalize observation time and validate the session count."""
        if self.observed_at is not None:
            object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if self.unmanaged_sessions is not None and self.unmanaged_sessions < 0:
            raise ValueError(
                "Dashboard unmanaged sessions cannot be negative."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProvider:
    """One provider's saved rows and last verified runtime relation."""

    provider_id: ProviderId
    runtime_state: ProviderRuntimeState | None
    active_account_id: SidekickAccountId | None
    verified_at: datetime | None
    actions_enabled: bool
    rows: tuple[DashboardRow, ...]
    finalized_epoch: SelectionEpoch | None = None
    selection: SelectionStatus | SelectionResult | None = field(
        default=None,
        repr=False,
    )
    activity: DashboardActivity | None = None
    status: DashboardProviderStatus = field(init=False)

    def __post_init__(self) -> None:
        """Validate row ownership and normalize provider observation time."""
        if self.verified_at is not None:
            object.__setattr__(
                self,
                "verified_at",
                as_utc(self.verified_at),
            )
        if any(row.provider_id is not self.provider_id for row in self.rows):
            raise ValueError("Dashboard row provider does not match.")
        account_ids = tuple(row.account_id for row in self.rows)
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("Dashboard provider has duplicate accounts.")
        if (
            self.selection is not None
            and self.selection.provider_id is not self.provider_id
        ):
            raise ValueError("Dashboard selection provider does not match.")
        object.__setattr__(
            self,
            "status",
            DashboardProviderStatus(
                runtime_state=self.runtime_state,
                observed_at=self.verified_at,
                finalized_epoch=self.finalized_epoch,
                selection=self.selection,
                unmanaged_sessions=None,
            ),
        )
        if (
            self.activity is not None
            and self.activity.summary.scope
            is not TokenActivityScope.LOCAL_INSTALLATION
        ):
            raise ValueError(
                "Dashboard provider activity must be installation-scoped."
            )
        if self.activity is not None and any(
            row.activity is not None for row in self.rows
        ):
            raise ValueError(
                "Dashboard provider and account activity cannot coexist."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardService:
    """Latest passive resident-service observation."""

    ready: bool
    compatible: bool
    phase: ServicePhase | None
    observed_at: datetime | None
    failure_code: str | None

    def __post_init__(self) -> None:
        """Normalize time and require readiness to match service phase."""
        if self.observed_at is not None:
            object.__setattr__(
                self,
                "observed_at",
                as_utc(self.observed_at),
            )
        if self.ready and (
            not self.compatible or self.phase is not ServicePhase.READY
        ):
            raise ValueError("Dashboard service readiness is inconsistent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardSnapshot:
    """Complete secret-free cached state for one first paint."""

    providers: tuple[DashboardProvider, ...]
    service: DashboardService
    reference_time: datetime
    activity_cache_issue: ActivitySnapshotFailureKind | None = None
    usage_cache_issue: UsageSnapshotFailureKind | None = None

    def __post_init__(self) -> None:
        """Require deterministic provider order and normalize render time."""
        object.__setattr__(
            self,
            "reference_time",
            as_utc(self.reference_time),
        )
        if tuple(provider.provider_id for provider in self.providers) != tuple(
            ProviderId
        ):
            raise ValueError("Dashboard providers must use canonical order.")
        if (
            self.activity_cache_issue is not None
            and self.activity_cache_issue
            not in _DASHBOARD_ACTIVITY_CACHE_ISSUES
        ):
            raise ValueError("Dashboard activity cache issue is not readable.")
        if (
            self.usage_cache_issue is not None
            and self.usage_cache_issue not in _DASHBOARD_USAGE_CACHE_ISSUES
        ):
            raise ValueError("Dashboard usage cache issue is not readable.")

    @property
    def all_saved_metrics_unavailable(self) -> bool:
        """Return whether saved rows exist without any retained metrics."""
        has_saved_account = False
        for provider in self.providers:
            if provider.activity is not None:
                return False
            for row in provider.rows:
                if not isinstance(row, DashboardAccount):
                    continue
                has_saved_account = True
                if row.usage is not None or row.activity is not None:
                    return False
        return has_saved_account
