"""Deterministic test clocks."""

from dataclasses import dataclass
from datetime import UTC, datetime

REFERENCE_TIME = datetime(2026, 6, 12, 12, 34, 56, 789000, tzinfo=UTC)


@dataclass(slots=True)
class FixedClock:
    """Return one fixed instant while counting wall-time acquisitions."""

    value: datetime = REFERENCE_TIME
    calls: int = 0

    def now(self) -> datetime:
        """Return the fixed instant and record one acquisition."""
        self.calls += 1
        return self.value
