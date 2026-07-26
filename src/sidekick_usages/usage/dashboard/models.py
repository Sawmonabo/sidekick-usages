"""Immutable secret-free cached dashboard models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.models import TokenActivitySummary, UsageReport
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.daemon.types.service import ServicePhase

type DashboardRow = DashboardAccount | DashboardExternalRow

MAX_DASHBOARD_FOOTER_MESSAGE_CHARACTERS = 512


class DashboardActionState(StrEnum):
    """Closed account states that can affect dashboard interaction."""

    HEALTHY = "healthy"
    LOGIN_REQUIRED = "login_required"
    REPAIR_REQUIRED = "repair_required"
    SETUP_REGENERATION_REQUIRED = "setup_regeneration_required"
    METRICS_STALE = "metrics_stale"
    EXTERNAL_ACTIVE = "external_active"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    SERVICE_UNAVAILABLE = "service_unavailable"


class DashboardFooterKind(StrEnum):
    """Closed footer presentations for the interactive dashboard."""

    KEYS = "keys"
    HELP = "help"
    PROGRESS = "progress"
    CONFIRMATION = "confirmation"
    ERROR = "error"


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardCursor:
    """One focused provider row without provider-owned identity."""

    focused_provider: ProviderId | None
    account_id: SidekickAccountId | None
    external: bool = False

    def __post_init__(self) -> None:
        """Require external focus to remain distinct from saved accounts."""
        if self.focused_provider is None and (
            self.account_id is not None or self.external
        ):
            raise ValueError("An empty dashboard cursor cannot select a row.")
        if self.external and self.account_id is not None:
            raise ValueError("External dashboard focus cannot use an account.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardFooter:
    """One bounded transient or keyboard-help footer."""

    kind: DashboardFooterKind = DashboardFooterKind.KEYS
    message: str | None = None

    def __post_init__(self) -> None:
        """Require bounded copy only for transient dashboard notices."""
        if self.kind in {
            DashboardFooterKind.PROGRESS,
            DashboardFooterKind.CONFIRMATION,
            DashboardFooterKind.ERROR,
        }:
            valid = self.message is not None and bool(self.message.strip())
        else:
            valid = self.message is None
        if self.message is not None:
            valid = (
                valid
                and len(self.message)
                <= MAX_DASHBOARD_FOOTER_MESSAGE_CHARACTERS
            )
        if not valid:
            raise ValueError(
                "Transient dashboard footers require a bounded message."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardUsage:
    """One retained usage observation projected without provider identity."""

    plan: str
    report: UsageReport
    observed_at: datetime
    freshness: MetricsFreshness = MetricsFreshness.STALE

    def __post_init__(self) -> None:
        """Normalize observation time and require retained freshness."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if self.freshness is not MetricsFreshness.STALE:
            raise ValueError("Cached dashboard usage must remain stale.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardActivity:
    """One retained activity observation without its provider identity."""

    summary: TokenActivitySummary
    observed_at: datetime
    freshness: MetricsFreshness = MetricsFreshness.STALE

    def __post_init__(self) -> None:
        """Normalize observation time and require retained freshness."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if self.freshness is not MetricsFreshness.STALE:
            raise ValueError("Cached dashboard activity must remain stale.")


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
    usage: DashboardUsage | None = None
    activity: DashboardActivity | None = None

    def __post_init__(self) -> None:
        """Reject duplicate account states."""
        if len(self.states) != len(set(self.states)):
            raise ValueError("Dashboard account states must be unique.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardExternalRow:
    """One anonymous provider login not owned by a saved account."""

    provider_id: ProviderId
    observed_at: datetime
    states: tuple[DashboardActionState, ...]

    def __post_init__(self) -> None:
        """Normalize observation time and require external truth."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if (
            not self.states
            or self.states[0] is not DashboardActionState.EXTERNAL_ACTIVE
            or len(self.states) != len(set(self.states))
        ):
            raise ValueError("External rows require unique external state.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProvider:
    """One provider's saved rows and last verified runtime relation."""

    provider_id: ProviderId
    runtime_state: ProviderRuntimeState | None
    active_account_id: SidekickAccountId | None
    verified_at: datetime | None
    actions_enabled: bool
    rows: tuple[DashboardRow, ...]

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
        account_ids = tuple(
            row.account_id
            for row in self.rows
            if isinstance(row, DashboardAccount)
        )
        if len(account_ids) != len(set(account_ids)):
            raise ValueError("Dashboard provider has duplicate accounts.")
        external_count = sum(
            isinstance(row, DashboardExternalRow) for row in self.rows
        )
        if external_count > 1:
            raise ValueError("Dashboard provider has multiple external rows.")


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
