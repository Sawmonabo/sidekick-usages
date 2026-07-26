"""Canonical aware-UTC timestamp serialization for persisted state."""

import re
from datetime import UTC, datetime

from sidekick_usages.persistence.errors import InvalidSchemaError

_CANONICAL_TIMESTAMP = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-"
    r"(?P<day>[0-9]{2})T(?P<hour>[0-9]{2}):"
    r"(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})\."
    r"(?P<fraction>[0-9]{6})Z\Z",
    re.ASCII,
)


def parse_canonical_timestamp(value: str) -> datetime:
    """Parse one exact microsecond-resolution UTC timestamp."""
    if not isinstance(value, str) or not value.isascii():
        raise InvalidSchemaError
    match = _CANONICAL_TIMESTAMP.fullmatch(value)
    if match is None:
        raise InvalidSchemaError
    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            int(match.group("fraction")),
            tzinfo=UTC,
        )
    except ValueError:
        raise InvalidSchemaError from None


def canonical_timestamp_text(value: str) -> str:
    """Require canonical timestamp text for a schema validator."""
    try:
        parse_canonical_timestamp(value)
    except InvalidSchemaError:
        raise ValueError from None
    return value


def canonical_timestamp(value: datetime) -> str:
    """Encode one aware timestamp as exact microsecond-resolution UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidSchemaError
    utc_value = value.astimezone(UTC)
    return (
        f"{utc_value.year:04d}-{utc_value.month:02d}-"
        f"{utc_value.day:02d}T{utc_value.hour:02d}:"
        f"{utc_value.minute:02d}:{utc_value.second:02d}."
        f"{utc_value.microsecond:06d}Z"
    )
