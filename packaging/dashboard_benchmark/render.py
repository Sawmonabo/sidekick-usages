"""Bounded canonical rendering used by the dashboard release trace."""

import time

from dashboard_benchmark.errors import DashboardBenchmarkError
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard,
)

CURSOR_SAMPLE_COUNT = 40
OUTPUT_WIDTH = 200


def _cursor(controller: DashboardController) -> DashboardCursor:
    return DashboardCursor(
        focused_provider=controller.state.focused_provider,
        account_id=controller.state.account_id,
    )


def _render(
    snapshot: DashboardSnapshot,
    controller: DashboardController,
) -> int:
    rendered = render_dashboard(
        snapshot,
        width=OUTPUT_WIDTH,
        cursor=_cursor(controller),
        footer=DashboardFooter(),
        color=False,
    )
    if not rendered:
        raise DashboardBenchmarkError("Dashboard render produced no output.")
    return len(rendered.encode("utf-8"))


def render_snapshot(snapshot: DashboardSnapshot) -> int:
    """Render one cached snapshot and return its encoded size."""
    return _render(
        snapshot,
        DashboardController.start(snapshot),
    )


def cursor_render_p95(snapshot: DashboardSnapshot) -> int:
    """Measure bounded cursor-to-render CPU time in nanoseconds."""
    controller = DashboardController.start(snapshot)
    durations: list[int] = []
    for sample in range(CURSOR_SAMPLE_COUNT):
        direction = DashboardMove.DOWN if sample % 2 == 0 else DashboardMove.UP
        started_at = time.process_time_ns()
        controller = controller.move(direction)
        _render(snapshot, controller)
        durations.append(time.process_time_ns() - started_at)
    durations.sort()
    percentile_index = (len(durations) * 95 + 99) // 100 - 1
    return durations[percentile_index]
