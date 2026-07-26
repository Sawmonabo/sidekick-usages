"""Secret-safe doctor result models."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto

from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
    MetricsFreshness,
)
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExpiryState,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
)
from sidekick_usages.credentials.claude.lifetime import (
    ClaudeLoginRenewalState,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.runtime.models import (
    ScheduledOperationDiagnostic,
    UnfinishedActivationDiagnostic,
)
from sidekick_usages.doctor.runtime.types import (
    DoctorAccountWarning,
    NativeAccountRelation,
)
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)

type DoctorResult = DoctorReadyResult | DoctorFailedResult


class DoctorCredentialKind(StrEnum):
    """Stable authority-kind values exposed by doctor JSON."""

    SETUP_TOKEN = auto()
    SUBSCRIPTION_LOGIN = auto()
    CODEX_LOGIN = auto()


class DoctorAuthorityManagement(StrEnum):
    """Owner of one credential authority's durable credential state."""

    SIDEKICK_STORED = "sidekick_stored"
    PROVIDER_MANAGED = "provider_managed"


class IdentityState(StrEnum):
    """Secret-safe stable-identity association state."""

    KNOWN = "known"
    UNAVAILABLE = "unavailable"
    ASSOCIATION_REQUIRED = "association_required"


class HeartbeatSupport(StrEnum):
    """Truthful heartbeat capability known from saved metadata."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AuthorityDiagnostic:
    """Classified state for one independent credential authority."""

    kind: DoctorCredentialKind
    management: DoctorAuthorityManagement
    health: CredentialHealth
    usage_route: str
    access_expires_at: datetime | None
    access_expiry_state: ExpiryState
    access_expiry_display: str
    refresh_expires_at: datetime | None
    refresh_expiry_state: ExpiryState
    refresh_expiry_display: str
    login_renewal_state: ClaudeLoginRenewalState
    provider_action: CredentialAction | None
    can_auto_refresh: bool
    manual_action_required: bool


@dataclass(frozen=True, slots=True)
class AccountDiagnostic:
    """Public doctor data for one logical saved account."""

    label: AccountLabel
    provider: ProviderId
    provider_available: bool
    plan: str
    credential_health: CredentialHealth
    identity_state: IdentityState
    setup_token: AuthorityDiagnostic | None
    subscription: AuthorityDiagnostic | None
    last_refresh_at: datetime | None
    last_refresh_status: RefreshStatus | None
    last_refresh_error: str | None
    heartbeat_support: HeartbeatSupport
    heartbeat_enabled: bool
    heartbeat: str
    heartbeat_window_resets: tuple[tuple[str, datetime], ...] | None
    heartbeat_targets: tuple[str, ...] | None
    last_heartbeat_at: datetime | None
    last_heartbeat_status: HeartbeatStatus | None
    last_heartbeat_error: str | None
    native_relation: NativeAccountRelation
    selected_generation_relation: AuthorityGenerationRelation
    metrics_freshness: MetricsFreshness
    metrics_observed_at: datetime | None
    warning: DoctorAccountWarning | None
    manual_action: tuple[str, ...] | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorReadyResult:
    """Completed diagnostics for the current persistence store."""

    diagnostics: tuple[AccountDiagnostic, ...]
    scheduled_operations: tuple[ScheduledOperationDiagnostic, ...]
    unfinished_activations: tuple[UnfinishedActivationDiagnostic, ...]
    persistence: PersistenceStatus
    refresh_state: CredentialRefreshState
    supervisor: SupervisorHealth
    capabilities: ProviderCapabilityReport


@dataclass(frozen=True, slots=True, kw_only=True)
class DoctorFailedResult:
    """Completed bounded failure from doctor composition."""

    failure: PersistenceFailure
    supervisor: SupervisorHealth
    capabilities: ProviderCapabilityReport
