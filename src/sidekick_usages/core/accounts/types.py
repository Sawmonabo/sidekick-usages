"""Identifiers and closed values for secret-free saved accounts."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from sidekick_usages.core.accounts.validation import (
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)

type AccountIdFactory = Callable[[], SidekickAccountId]
type AuthorityIdFactory = Callable[[], AuthorityId]


class _CanonicalUuid(str):
    """Canonical lower-case UUID identifier."""

    _name: ClassVar[str]

    def __new__(cls, value: str) -> Self:
        """Validate and construct one canonical UUID string."""
        if not isinstance(value, str):
            raise TypeError(f"{cls._name} must be a string.")
        try:
            parsed = UUID(value)
        except ValueError, AttributeError, TypeError:
            raise ValueError(
                f"{cls._name} must be a canonical UUID."
            ) from None
        if str(parsed) != value:
            raise ValueError(f"{cls._name} must be a canonical UUID.")
        return super().__new__(cls, value)


class SidekickAccountId(_CanonicalUuid):
    """Stable Sidekick-owned account identifier."""

    _name = "Sidekick account ID"


class AuthorityId(_CanonicalUuid):
    """Stable Sidekick-owned credential-authority identifier."""

    _name = "Authority ID"


class OperationId(_CanonicalUuid):
    """Stable identifier for one durable Sidekick operation."""

    _name = "Operation ID"


class RequestId(_CanonicalUuid):
    """Correlation identifier for one local control request."""

    _name = "Request ID"


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Bounded provider identity hidden from representations."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require one complete bounded identity value."""
        require_bounded_text(
            self.value,
            name="Provider identity",
            maximum=MAX_OPAQUE_BYTES,
        )

    def __str__(self) -> str:
        """Return the opaque value only at a qualified boundary."""
        return self.value


@dataclass(frozen=True, slots=True)
class AuthorityGeneration:
    """Bounded provider generation hidden from representations."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require one complete bounded generation value."""
        require_bounded_text(
            self.value,
            name="Authority generation",
            maximum=MAX_OPAQUE_BYTES,
        )

    def __str__(self) -> str:
        """Return the opaque value only at a qualified boundary."""
        return self.value


class CredentialHealth(StrEnum):
    """Closed credential authority health states."""

    HEALTHY = "healthy"
    REFRESH_DUE = "refresh_due"
    LOGIN_REQUIRED = "login_required"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNKNOWN = "unknown"


class CredentialAction(StrEnum):
    """Safe next action for one credential authority."""

    NONE = "none"
    REFRESH = "refresh"
    LOGIN = "login"


class MetricsFreshness(StrEnum):
    """Closed account metrics freshness states."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
