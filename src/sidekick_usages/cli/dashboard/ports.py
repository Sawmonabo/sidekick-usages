"""Typed boundaries for the isolated interactive dashboard."""

from typing import Protocol

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import DashboardSnapshot


class DashboardSnapshotSource(Protocol):
    """Load one secret-free cached dashboard projection."""

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return cached state constrained to one optional provider."""
        ...


class DashboardProcess(Protocol):
    """Replace the launcher with the dedicated interactive process."""

    def replace(self, only: ProviderId | None) -> None:
        """Replace the current process using safe routing state only."""
        ...


class DashboardLookupPort(Protocol):
    """Enqueue refresh work outside the input and render loop."""

    def enqueue(
        self,
        intent: RefreshAccountIntent | RefreshDueAccountsIntent,
    ) -> None:
        """Submit one refresh intent without running provider work."""
        ...


class DashboardSupervisorPort(Protocol):
    """Enqueue account activation outside the input and render loop."""

    def enqueue(self, intent: ActivateOrRepairIntent) -> None:
        """Submit one activation intent without running provider work."""
        ...
