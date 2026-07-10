"""Pure invariants for provider-neutral runtime datetimes."""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    """Return an aware datetime in UTC.

    :param value: Runtime datetime to normalize.
    :returns: Equivalent UTC datetime.
    :raises ValueError: If ``value`` is naive.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Runtime timestamps must be timezone-aware.")
    return value.astimezone(UTC)
