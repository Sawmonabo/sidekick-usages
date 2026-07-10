"""Application wall-clock boundary."""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Provide aware UTC wall time."""

    def now(self) -> datetime:
        """Return the current aware UTC time."""
        ...


class SystemClock:
    """Read wall time from the operating system."""

    def now(self) -> datetime:
        """Return the current aware UTC time."""
        return datetime.now(UTC)
