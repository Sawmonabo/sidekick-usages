"""Provider-neutral access-token expiry values and classification."""

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, assert_never

from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ExpiryState


@dataclass(frozen=True, slots=True)
class KnownExpiry:
    """An authoritative expiry instant awaiting classification."""

    at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", as_utc(self.at))


@dataclass(frozen=True, slots=True)
class UnknownExpiry:
    """Expiry metadata was not supplied by its owning boundary."""

    state: ClassVar[ExpiryState] = ExpiryState.UNKNOWN


@dataclass(frozen=True, slots=True)
class InvalidExpiry:
    """Expiry metadata was present but invalid at its boundary."""

    state: ClassVar[ExpiryState] = ExpiryState.INVALID


type Expiry = KnownExpiry | UnknownExpiry | InvalidExpiry


@dataclass(frozen=True, slots=True)
class ValidExpiry:
    """An authoritative expiry strictly later than the reference time."""

    at: datetime
    state: ClassVar[ExpiryState] = ExpiryState.VALID

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", as_utc(self.at))


@dataclass(frozen=True, slots=True)
class ExpiredExpiry:
    """An authoritative expiry at or before the reference time."""

    at: datetime
    state: ClassVar[ExpiryState] = ExpiryState.EXPIRED

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", as_utc(self.at))


type ClassifiedExpiry = (
    ValidExpiry | ExpiredExpiry | UnknownExpiry | InvalidExpiry
)


def classify_expiry(
    expiry: Expiry,
    *,
    now: datetime,
) -> ClassifiedExpiry:
    """Classify expiry against one explicit aware wall time.

    :param expiry: Stored provider-neutral expiry value.
    :param now: Aware reference time for this decision.
    :returns: A discriminated classified expiry.
    :raises ValueError: If ``now`` is naive.
    """
    reference_time = as_utc(now)
    if isinstance(expiry, KnownExpiry):
        if expiry.at <= reference_time:
            return ExpiredExpiry(expiry.at)
        return ValidExpiry(expiry.at)
    if isinstance(expiry, UnknownExpiry | InvalidExpiry):
        return expiry
    assert_never(expiry)
