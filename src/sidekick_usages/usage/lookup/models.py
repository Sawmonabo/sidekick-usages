"""Immutable results for concurrent account lookup."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    TokenActivityReading,
    TokenActivitySummary,
    UsageReport,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.usage.lookup.worker.models import UsageLookupFailure
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    TokenActivityIssue,
)

MAX_METRICS_REFRESH_ATTEMPTS = 2

type ActivityObservation = TokenActivityReading | TokenActivityIssue
type LookupWaveReading = AccountLookupReading | LocalActivityReading
type LookupTaskFuture = (
    Future[AccountLookupReading] | Future[LocalActivityReading]
)
type AccountLookupObserver = Callable[[AccountLookupCompletion], None]
type AccountMutationIntent = CredentialRefreshIntent | ProviderStateIntent
type AccountMutationResult = AccountRefreshResult | ProviderStateResult
type AccountMutationExchange = Callable[
    [AccountMutationIntent],
    AccountMutationResult,
]
type LookupWaveEvent = LookupTaskFuture | OwnerMutationRequest
type MetricsRefreshCode = UsageLookupFailure | MetricsRefreshFailureCode


class MetricsRefreshOutcome(StrEnum):
    """Closed outcomes for one dashboard metrics refresh."""

    SUCCEEDED = "succeeded"
    RECOVERED = "recovered"
    PARTIAL = "partial"
    FAILED = "failed"


class MetricsRefreshStage(StrEnum):
    """Closed terminal or recovered metrics-refresh stages."""

    WORKER = "worker"
    PROVIDER = "provider"
    SNAPSHOT_RELOAD = "snapshot_reload"
    CACHE_READ = "cache_read"


class MetricsRefreshFailureCode(StrEnum):
    """Safe non-worker reasons for an incomplete metrics refresh."""

    PROVIDER_FAILURE = "provider_failure"
    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    USAGE_READ = "usage_read"
    USAGE_MALFORMED = "usage_malformed"
    ACTIVITY_READ = "activity_read"
    ACTIVITY_MALFORMED = "activity_malformed"


class MetricsRefreshWriteState(StrEnum):
    """Outcome from the no-throw diagnostic sink."""

    SAVED = "saved"
    UNAVAILABLE = "unavailable"


class MetricsRefreshDiagnosticState(StrEnum):
    """Passive availability of the latest diagnostic artifact."""

    AVAILABLE = "available"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRefreshObservation:
    """Latest sanitized dashboard metrics-refresh observation."""

    observed_at: datetime
    outcome: MetricsRefreshOutcome
    attempts: int
    stage: MetricsRefreshStage | None = None
    code: MetricsRefreshCode | None = None

    def __post_init__(self) -> None:
        """Normalize time and require one unambiguous outcome."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if not 1 <= self.attempts <= MAX_METRICS_REFRESH_ATTEMPTS:
            raise ValueError("Metrics refresh attempts are outside the limit.")
        _require_metrics_refresh_contract(
            self.outcome,
            self.attempts,
            self.stage,
            self.code,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRefreshDiagnostic:
    """Passive latest observation or its safe availability state."""

    state: MetricsRefreshDiagnosticState
    observation: MetricsRefreshObservation | None = None

    def __post_init__(self) -> None:
        """Require an observation exactly when it is available."""
        available = self.state is MetricsRefreshDiagnosticState.AVAILABLE
        if (self.observation is not None) is not available:
            raise ValueError("Metrics refresh diagnostic is inconsistent.")


def _require_metrics_refresh_contract(
    outcome: MetricsRefreshOutcome,
    attempts: int,
    stage: MetricsRefreshStage | None,
    code: MetricsRefreshCode | None,
) -> None:
    has_detail = stage is not None and code is not None
    if (stage is None) != (code is None):
        raise ValueError("Metrics refresh failure detail is incomplete.")
    if outcome is MetricsRefreshOutcome.SUCCEEDED:
        if attempts != 1 or has_detail:
            raise ValueError("Successful metrics refresh is inconsistent.")
        return
    if stage is None or code is None:
        raise ValueError("Metrics refresh failure detail is required.")
    if not _metrics_refresh_detail_is_valid(stage, code):
        raise ValueError("Metrics refresh failure detail is invalid.")
    _require_metrics_refresh_outcome(outcome, attempts, stage)


def _metrics_refresh_detail_is_valid(
    stage: MetricsRefreshStage,
    code: MetricsRefreshCode,
) -> bool:
    return (
        (
            stage is MetricsRefreshStage.WORKER
            and isinstance(code, UsageLookupFailure)
        )
        or (
            stage is MetricsRefreshStage.PROVIDER
            and code is MetricsRefreshFailureCode.PROVIDER_FAILURE
        )
        or (
            stage is MetricsRefreshStage.SNAPSHOT_RELOAD
            and code is MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
        )
        or (
            stage is MetricsRefreshStage.CACHE_READ
            and code
            in {
                MetricsRefreshFailureCode.USAGE_READ,
                MetricsRefreshFailureCode.USAGE_MALFORMED,
                MetricsRefreshFailureCode.ACTIVITY_READ,
                MetricsRefreshFailureCode.ACTIVITY_MALFORMED,
            }
        )
    )


def _require_metrics_refresh_outcome(
    outcome: MetricsRefreshOutcome,
    attempts: int,
    stage: MetricsRefreshStage,
) -> None:
    if outcome is MetricsRefreshOutcome.PARTIAL and stage not in {
        MetricsRefreshStage.PROVIDER,
        MetricsRefreshStage.CACHE_READ,
    }:
        raise ValueError("Partial metrics refresh stage is invalid.")
    if outcome is MetricsRefreshOutcome.FAILED and stage not in {
        MetricsRefreshStage.WORKER,
        MetricsRefreshStage.SNAPSHOT_RELOAD,
    }:
        raise ValueError("Failed metrics refresh stage is invalid.")
    if outcome is MetricsRefreshOutcome.RECOVERED and (
        attempts != MAX_METRICS_REFRESH_ATTEMPTS
        or stage
        not in {
            MetricsRefreshStage.WORKER,
            MetricsRefreshStage.SNAPSHOT_RELOAD,
        }
    ):
        raise ValueError("Recovered metrics refresh is inconsistent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CredentialRefreshIntent:
    """Request owner-thread refresh without credential material."""

    account: SavedAccount
    reason: CredentialRefreshReason


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderStateIntent:
    """Request owner-thread persistence of provider-observed safe state."""

    account: SavedAccount
    plan: str


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRefreshResult:
    """Owner-thread result of one requested credential refresh."""

    account: SavedAccount | None = None
    failure: FetchFailure | None = None

    def __post_init__(self) -> None:
        """Require exactly one refreshed account or terminal failure."""
        if (self.account is None) == (self.failure is None):
            raise ValueError("Credential refresh result is ambiguous.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderStateResult:
    """Owner-thread result of one provider-state persistence intent."""

    failure: FetchFailure | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class OwnerMutationRequest:
    """One internal wave request awaiting owner-thread completion."""

    intent: AccountMutationIntent
    response: Future[AccountMutationResult] = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True)
class CurrentUsageReading:
    """One current provider response before owner-thread persistence."""

    plan: str
    report: UsageReport


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountLookupReading:
    """One secret-free account result produced by a lookup thread."""

    ordinal: int
    account_id: SidekickAccountId
    label: AccountLabel
    provider_id: ProviderId
    usage: CurrentUsageReading | None
    failure: FetchFailure | None
    activity: ActivityObservation | None
    activity_eligible: bool

    def __post_init__(self) -> None:
        """Require one usage outcome and a valid fixed ordinal."""
        if self.ordinal < 0:
            raise ValueError("Account lookup ordinal cannot be negative.")
        if (self.usage is None) == (self.failure is None):
            raise ValueError("Account lookup requires exactly one outcome.")
        if not self.activity_eligible and self.activity is not None:
            raise ValueError(
                "Ineligible account lookup cannot expose activity."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountLookupCompletion:
    """One owner-finalized account result ready for publication."""

    ordinal: int
    account_id: SidekickAccountId
    label: AccountLabel
    provider_id: ProviderId
    usage: AccountUsage | None
    failure: FetchFailure | None

    def __post_init__(self) -> None:
        """Require current or retained usage, a failure, or both."""
        if self.ordinal < 0:
            raise ValueError("Account lookup ordinal cannot be negative.")
        if self.usage is None and self.failure is None:
            raise ValueError("Account completion requires an outcome.")


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalActivityReading:
    """One provider-local activity result from the shared lookup wave."""

    provider_id: ProviderId
    observation: ActivityObservation
    issues: tuple[TokenActivityIssue, ...] = ()

    def __post_init__(self) -> None:
        """Keep provider-level persistence issues account-neutral."""
        if any(issue.label is not None for issue in self.issues):
            raise ValueError("Local activity issues cannot name an account.")


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountActivityContribution:
    """One owner-finalized account contribution to provider activity."""

    account_id: SidekickAccountId
    summary: TokenActivitySummary | None
    issues: tuple[TokenActivityIssue, ...] = ()
