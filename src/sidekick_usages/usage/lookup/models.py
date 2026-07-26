"""Immutable results for concurrent account lookup."""

from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import (
    TokenActivityReading,
    TokenActivitySummary,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.refresh import CredentialRefreshReason
from sidekick_usages.usage.models import (
    AccountUsage,
    FetchFailure,
    TokenActivityIssue,
)

type ActivityObservation = TokenActivityReading | TokenActivityIssue
type LookupWaveReading = AccountLookupReading | LocalActivityReading
type AccountLookupObserver = Callable[[AccountLookupCompletion], None]
type AccountMutationIntent = CredentialRefreshIntent | ProviderStateIntent
type AccountMutationResult = (
    AccountRefreshResult | ProviderStateResult
)
type AccountMutationExchange = Callable[
    [AccountMutationIntent],
    AccountMutationResult,
]
type LookupWaveEvent = (
    Future[LookupWaveReading] | OwnerMutationRequest
)


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


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountActivityContribution:
    """One owner-finalized account contribution to provider activity."""

    account_id: SidekickAccountId
    summary: TokenActivitySummary | None
    issues: tuple[TokenActivityIssue, ...] = ()
