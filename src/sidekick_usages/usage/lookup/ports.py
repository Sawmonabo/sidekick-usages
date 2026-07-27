"""Concurrency capabilities required by account lookup."""

from contextlib import AbstractContextManager
from typing import Protocol

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.usage.lookup.models import (
    MetricsRefreshCode,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
    MetricsRefreshWriteState,
)


class AccountOperationLocks(Protocol):
    """Serialize provider work by stable saved-account identity."""

    def hold(
        self,
        account_id: SidekickAccountId,
    ) -> AbstractContextManager[OperationAuthority]:
        """Hold one account operation lock for the complete lookup."""


class MetricsRefreshObservationSink(Protocol):
    """Persist one sanitized dashboard metrics-refresh outcome."""

    def record(
        self,
        outcome: MetricsRefreshOutcome,
        *,
        attempts: int,
        stage: MetricsRefreshStage | None = None,
        code: MetricsRefreshCode | None = None,
    ) -> MetricsRefreshWriteState:
        """Persist without raising when the artifact is unavailable."""
        ...
