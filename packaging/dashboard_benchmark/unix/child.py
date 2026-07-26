"""Fresh-process cached dashboard first-paint trace."""

import os
import sys
from pathlib import Path

from dashboard_benchmark.cache.paths import benchmark_application_paths
from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.models import FirstPaintSignal
from dashboard_benchmark.render import render_snapshot
from sidekick_usages.cli.contexts.dashboard import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.clock import SystemClock
from sidekick_usages.usage.dashboard.models import DashboardAccount

TRACE_MODULE = "dashboard_benchmark.unix.trace"
FIRST_PAINT_ARGUMENT_COUNT = 2


def _cache_root() -> Path:
    if len(sys.argv) != FIRST_PAINT_ARGUMENT_COUNT:
        raise DashboardBenchmarkError(
            "Dashboard first-paint trace requires one cache root."
        )
    try:
        root = Path(sys.argv[1]).resolve(strict=True)
    except OSError:
        raise DashboardBenchmarkError(
            "Dashboard first-paint cache root is unavailable."
        ) from None
    if not root.is_dir():
        raise DashboardBenchmarkError(
            "Dashboard first-paint cache root is not a directory."
        )
    return root


def main() -> int:
    """Paint cached state, then replace this process with the lookup trace."""
    snapshot = CachedDashboardSnapshotSource(
        benchmark_application_paths(_cache_root()),
        SystemClock(),
    ).load(None)
    account_count = sum(
        isinstance(row, DashboardAccount)
        for provider in snapshot.providers
        for row in provider.rows
    )
    rendered_bytes = render_snapshot(snapshot)
    sys.stdout.write(
        FirstPaintSignal(
            account_count=account_count,
            rendered_bytes=rendered_bytes,
        ).encode()
        + "\n"
    )
    sys.stdout.flush()
    os.execv(
        sys.executable,
        (sys.executable, "-m", TRACE_MODULE),
    )


if __name__ == "__main__":
    sys.exit(main())
