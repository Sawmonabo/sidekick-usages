"""Immutable application results for account usage checks."""

from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from sidekick_usages.core.models import UsageReport
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.errors import PersistenceCode
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


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshRejectedFailure(FetchFailure):
    """The shared refresh workflow could not refresh the account."""

    kind: ClassVar[FetchFailureKind] = FetchFailureKind.REFRESH_REJECTED

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


@dataclass(frozen=True, slots=True)
class UsageCheckResult:
    """Complete successes and failures from one usage check."""

    usages: tuple[AccountUsage, ...] = ()
    failures: tuple[FetchFailure, ...] = ()


__all__ = [
    "AccountUsage",
    "AuthenticationFailure",
    "FetchFailure",
    "FetchFailureKind",
    "ForbiddenFailure",
    "InvalidExpiryFailure",
    "PersistenceFailure",
    "ProviderPayloadFailure",
    "RateLimitFailure",
    "RefreshRejectedFailure",
    "TransientFailure",
    "UnknownProviderFailure",
    "UsageCheckResult",
]
