"""Immutable application results for account usage checks."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from typing import ClassVar

from sidekick_usages.core.models import TokenActivitySummary, UsageReport
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.providers.base import ProviderFailure


class FetchFailureKind(StrEnum):
    """Closed terminal outcomes for one account usage request."""

    UNKNOWN_PROVIDER = "unknown_provider"
    INVALID_EXPIRY = "invalid_expiry"
    AUTHENTICATION = "authentication"
    REFRESH_REJECTED = "refresh_rejected"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    PROVIDER = "provider"
    PERSISTENCE = "persistence"


class CredentialRecoveryKind(StrEnum):
    """Credential modes that select presentation-owned recovery copy."""

    CLAUDE_SETUP_TOKEN = auto()
    CLAUDE_SUBSCRIPTION_LOGIN = auto()
    CODEX_LOGIN = auto()


class TokenActivityFailureKind(StrEnum):
    """Closed failures from an attempted token-activity read."""

    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_MALFORMED = "source_malformed"
    AUTHENTICATION = "authentication"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    PROVIDER = "provider"
    PERSISTENCE = "persistence"


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountUsage:
    """One immutable account identity paired with normalized usage."""

    label: AccountLabel
    provider_id: ProviderId
    plan: str
    report: UsageReport


@dataclass(frozen=True, slots=True, kw_only=True)
class FetchFailure:
    """One safe provider failure without presentation behavior."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.PROVIDER

    label: AccountLabel
    provider_id: ProviderId
    plan: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownProviderFailure(FetchFailure):
    """No provider adapter is registered for the saved account."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.UNKNOWN_PROVIDER


@dataclass(frozen=True, slots=True, kw_only=True)
class InvalidExpiryFailure(FetchFailure):
    """Invalid expiry metadata blocked all provider traffic."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.INVALID_EXPIRY


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthenticationFailure(FetchFailure):
    """A provider rejected the account after allowed recovery."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.AUTHENTICATION

    credential_kind: CredentialRecoveryKind


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshRejectedFailure(FetchFailure):
    """The shared refresh workflow could not refresh the account."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.REFRESH_REJECTED

    credential_kind: CredentialRecoveryKind
    provider_failure: ProviderFailure | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderPayloadFailure(FetchFailure):
    """A provider boundary rejected malformed external data."""

    provider_failure: ProviderFailure


@dataclass(frozen=True, slots=True, kw_only=True)
class ForbiddenFailure(FetchFailure):
    """A valid credential lacks permission for the usage request."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.FORBIDDEN

    required_scope: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimitFailure(FetchFailure):
    """Provider rate limiting remained after HTTP-layer retries."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.RATE_LIMITED

    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TransientFailure(FetchFailure):
    """A transient provider or network failure exhausted retries."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.TRANSIENT


@dataclass(frozen=True, slots=True, kw_only=True)
class PersistenceFailure(FetchFailure):
    """Usage was not successful because account state was not durable."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.PERSISTENCE

    persistence_code: PersistenceCode


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenActivityIssue:
    """One secret-safe failure from an attempted activity read."""

    kind: TokenActivityFailureKind
    message: str
    label: AccountLabel | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class CompleteTokenActivity:
    """Every selected authoritative source contributed to the total."""

    provider_id: ProviderId
    summary: TokenActivitySummary
    issues: tuple[TokenActivityIssue, ...] = ()

    def __post_init__(self) -> None:
        """Require issue identities compatible with the summary scope."""
        if self.summary.scope is TokenActivityScope.ACCOUNT:
            valid = all(issue.label is not None for issue in self.issues)
        else:
            valid = all(issue.label is None for issue in self.issues)
        if not valid:
            raise ValueError("Activity issue labels must match their scope.")


@dataclass(frozen=True, slots=True, kw_only=True)
class PartialTokenActivity:
    """An exact known account sum with incomplete selected coverage."""

    provider_id: ProviderId
    summary: TokenActivitySummary
    covered_accounts: int
    selected_accounts: int
    issues: tuple[TokenActivityIssue, ...] = ()

    def __post_init__(self) -> None:
        """Reject partial states that claim invalid or complete coverage."""
        if self.summary.scope is not TokenActivityScope.ACCOUNT:
            raise ValueError("Partial token activity must be account-scoped.")
        if (
            isinstance(self.covered_accounts, bool)
            or not isinstance(self.covered_accounts, int)
            or self.covered_accounts <= 0
            or isinstance(self.selected_accounts, bool)
            or not isinstance(self.selected_accounts, int)
            or self.selected_accounts <= self.covered_accounts
        ):
            raise ValueError(
                "Partial token activity requires incomplete positive coverage."
            )
        if any(issue.label is None for issue in self.issues):
            raise ValueError("Account activity issues require account labels.")


@dataclass(frozen=True, slots=True, kw_only=True)
class UnavailableTokenActivity:
    """No selected source exposed an authoritative token total."""

    provider_id: ProviderId
    scope: TokenActivityScope


@dataclass(frozen=True, slots=True, kw_only=True)
class FailedTokenActivity:
    """All attempted activity reads failed without a numeric total."""

    provider_id: ProviderId
    scope: TokenActivityScope
    issues: tuple[TokenActivityIssue, ...]

    def __post_init__(self) -> None:
        """Require failures compatible with the declared activity scope."""
        if not self.issues:
            raise ValueError("Failed token activity requires an issue.")
        if self.scope is TokenActivityScope.ACCOUNT:
            valid = all(issue.label is not None for issue in self.issues)
        else:
            valid = all(issue.label is None for issue in self.issues)
        if not valid:
            raise ValueError("Activity issue labels must match their scope.")


type ProviderTokenActivity = (
    CompleteTokenActivity
    | PartialTokenActivity
    | UnavailableTokenActivity
    | FailedTokenActivity
)


def activity_has_failure(activity: ProviderTokenActivity) -> bool:
    """Return whether an activity outcome has an attempted-read failure."""
    if isinstance(activity, FailedTokenActivity):
        return True
    if isinstance(activity, CompleteTokenActivity | PartialTokenActivity):
        return bool(activity.issues)
    return False


@dataclass(frozen=True, slots=True)
class UsageCheckResult:
    """Complete successes and failures from one usage check."""

    usages: tuple[AccountUsage, ...]
    failures: tuple[FetchFailure, ...]
    reference_time: datetime
    activities: tuple[ProviderTokenActivity, ...] = ()

    def __post_init__(self) -> None:
        """Normalize the shared render reference to aware UTC."""
        object.__setattr__(
            self,
            "reference_time",
            as_utc(self.reference_time),
        )
        provider_ids = tuple(
            activity.provider_id for activity in self.activities
        )
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError(
                "Usage results contain duplicate provider activity."
            )


__all__ = [
    "AccountUsage",
    "AuthenticationFailure",
    "CompleteTokenActivity",
    "FailedTokenActivity",
    "FetchFailure",
    "FetchFailureKind",
    "ForbiddenFailure",
    "InvalidExpiryFailure",
    "PartialTokenActivity",
    "PersistenceFailure",
    "ProviderPayloadFailure",
    "ProviderTokenActivity",
    "RateLimitFailure",
    "RefreshRejectedFailure",
    "TokenActivityFailureKind",
    "TokenActivityIssue",
    "TransientFailure",
    "UnavailableTokenActivity",
    "UnknownProviderFailure",
    "UsageCheckResult",
    "activity_has_failure",
]
