"""Bounded Rich rendering used by the dashboard release trace."""

import io
import time

from rich.console import Console

from dashboard_benchmark.errors import DashboardBenchmarkError
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
    DashboardSnapshot,
)
from sidekick_usages.usage.presentation.dashboard.overview import (
    dashboard_overview,
)

CURSOR_SAMPLE_COUNT = 40
OUTPUT_WIDTH = 200


def _console(output: io.StringIO) -> Console:
    return Console(
        width=OUTPUT_WIDTH,
        file=output,
        color_system=None,
        force_terminal=False,
        legacy_windows=False,
    )


def _cursor(controller: DashboardController) -> DashboardCursor:
    return DashboardCursor(
        focused_provider=controller.state.focused_provider,
        account_id=controller.state.account_id,
        external=controller.state.external,
    )


def _render(
    snapshot: DashboardSnapshot,
    controller: DashboardController,
    console: Console,
    output: io.StringIO,
) -> int:
    output.seek(0)
    output.truncate(0)
    console.print(
        dashboard_overview(
            snapshot,
            width=OUTPUT_WIDTH,
            cursor=_cursor(controller),
            footer=DashboardFooter(),
        )
    )
    rendered = output.getvalue()
    if not rendered:
        raise DashboardBenchmarkError("Dashboard render produced no output.")
    return len(rendered.encode("utf-8"))


def render_snapshot(snapshot: DashboardSnapshot) -> int:
    """Render one cached snapshot and return its encoded size."""
    output = io.StringIO()
    return _render(
        snapshot,
        DashboardController.start(snapshot),
        _console(output),
        output,
    )


def cursor_render_p95(snapshot: DashboardSnapshot) -> int:
    """Measure one bounded cursor-to-render trace in nanoseconds."""
    controller = DashboardController.start(snapshot)
    output = io.StringIO()
    console = _console(output)
    durations: list[int] = []
    for sample in range(CURSOR_SAMPLE_COUNT):
        direction = DashboardMove.DOWN if sample % 2 == 0 else DashboardMove.UP
        started_at = time.perf_counter_ns()
        controller = controller.move(direction)
        _render(snapshot, controller, console, output)
        durations.append(time.perf_counter_ns() - started_at)
    durations.sort()
    percentile_index = (len(durations) * 95 + 99) // 100 - 1
    return durations[percentile_index]
