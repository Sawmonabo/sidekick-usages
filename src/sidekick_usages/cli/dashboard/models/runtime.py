"""Immutable launcher dependencies for the interactive dashboard."""

from dataclasses import dataclass
from typing import Protocol

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


@dataclass(frozen=True, slots=True)
class DashboardRuntime:
    """Secret-free cache and process boundaries for default invocation."""

    snapshots: DashboardSnapshotSource
    process: DashboardProcess
