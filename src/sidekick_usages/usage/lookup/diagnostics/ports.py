"""Ports for dashboard metrics-refresh diagnostics."""

from typing import Protocol

from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshCause,
    MetricsRefreshOutcome,
    MetricsRefreshWriteState,
)


class MetricsRefreshObservationSink(Protocol):
    """Persist one sanitized dashboard metrics-refresh outcome."""

    def record(
        self,
        outcome: MetricsRefreshOutcome,
        *,
        attempts: int,
        retry_causes: tuple[MetricsRefreshCause, ...] = (),
        causes: tuple[MetricsRefreshCause, ...] = (),
    ) -> MetricsRefreshWriteState:
        """Persist without raising when the artifact is unavailable."""
        ...
