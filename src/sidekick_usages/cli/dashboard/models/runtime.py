"""Immutable launcher dependencies for the interactive dashboard."""

from dataclasses import dataclass

from sidekick_usages.cli.dashboard.ports import (
    DashboardProcess,
    DashboardSnapshotSource,
)


@dataclass(frozen=True, slots=True)
class DashboardRuntime:
    """Secret-free cache and process boundaries for default invocation."""

    snapshots: DashboardSnapshotSource
    process: DashboardProcess
