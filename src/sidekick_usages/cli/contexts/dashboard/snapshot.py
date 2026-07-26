"""Passive cached dashboard source without provider imports."""

from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.usage.dashboard.models import DashboardSnapshot
from sidekick_usages.usage.dashboard.service import CachedDashboardService


class CachedDashboardSnapshotSource:
    """Read one current secret-free dashboard projection."""

    def __init__(
        self,
        paths: ApplicationPaths,
        clock: Clock,
    ) -> None:
        self._dashboard = CachedDashboardService(paths)
        self._clock = clock

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Load cached state and hide providers outside the requested scope."""
        snapshot = self._dashboard.load(self._clock.now())
        if only is None:
            return snapshot
        return replace(
            snapshot,
            providers=tuple(
                (
                    provider
                    if provider.provider_id is only
                    else replace(
                        provider,
                        active_account_id=None,
                        actions_enabled=False,
                        rows=(),
                    )
                )
                for provider in snapshot.providers
            ),
        )
