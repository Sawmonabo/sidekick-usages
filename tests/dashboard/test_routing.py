"""Cached dashboard routing behavior."""

import io
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.commands import usage
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.dashboard import launch
from sidekick_usages.cli.dashboard.models.runtime import DashboardRuntime
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.types import ExecutableFailure
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupFailure,
    UsageLookupWorkerResult,
)
from tests.fakes.dashboard.lookup_worker import (
    LookupCancellationProof,
    exercise_lookup_worker_cancellation,
)
from tests.fakes.dashboard.runtime import (
    OneShotRecorder,
    RoutingDashboardProcess,
    RoutingSnapshotSource,
    interactive_terminal,
    redirected_terminal,
)

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
ONE_SHOT_ROUTE_COUNT = 3


def _assert_execve_process_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the isolated handoff uses one qualified no-shell replacement."""
    replacements: list[tuple[Path, list[str], dict[str, str]]] = []

    def record_execve(
        executable: Path,
        arguments: list[str],
        environment: dict[str, str],
    ) -> Never:
        replacements.append((executable, arguments, environment))
        raise OSError("Synthetic no-shell replacement failure.")

    with monkeypatch.context() as replacement_boundary:
        replacement_boundary.setattr(launch.os, "execve", record_execve)
        with pytest.raises(
            launch.InteractiveDashboardLaunchError,
        ) as replacement_failure:
            launch.ExecveDashboardProcess().replace(ProviderId.CODEX)

    executable = Path(sys.executable)
    assert isinstance(replacement_failure.value.__cause__, OSError)
    assert replacements == [
        (
            executable,
            [
                sys.executable,
                "-m",
                launch.DASHBOARD_ENTRYPOINT_MODULE,
                "--only",
                ProviderId.CODEX.value,
            ],
            os.environ.copy(),
        )
    ]
    assert replacements[0][2] is not os.environ


def test_cli_routes_one_cached_frame_before_isolated_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One routing journey preserves TTY and one-shot process boundaries."""
    output = io.StringIO()
    events: list[str] = []
    process = RoutingDashboardProcess(events, output)
    runtime = DashboardRuntime(
        RoutingSnapshotSource(events, REFERENCE_TIME),
        process,
    )
    application = create_app()
    runner = CliRunner()
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        interactive_terminal,
    )

    interactive = runner.invoke(
        application,
        ["--only", "codex"],
        obj=InvocationContext(
            console=Console(file=output, width=100, force_terminal=True),
            dashboard_composer=lambda: runtime,
        ),
    )

    assert interactive.exit_code == 0
    assert events == ["load:codex", "replace:codex"]
    assert "CODEX" in process.frame_at_replace
    assert "CLAUDE" not in process.frame_at_replace

    _assert_execve_process_boundary(monkeypatch)

    def reject_executable(_path: Path) -> Never:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)

    monkeypatch.setattr(launch, "qualify_executable", reject_executable)
    qualification_output = io.StringIO()
    qualification_events: list[str] = []
    qualification = runner.invoke(
        application,
        [],
        obj=InvocationContext(
            console=Console(
                file=qualification_output,
                width=100,
                force_terminal=True,
            ),
            dashboard_composer=lambda: DashboardRuntime(
                RoutingSnapshotSource(
                    qualification_events,
                    REFERENCE_TIME,
                ),
                launch.ExecveDashboardProcess(),
            ),
        ),
    )
    qualification_frame = qualification_output.getvalue()

    assert qualification.exit_code == 1
    assert isinstance(
        qualification.exception,
        launch.InteractiveDashboardLaunchError,
    )
    assert isinstance(
        qualification.exception.__cause__,
        ExecutableQualificationError,
    )
    assert qualification_events == ["load:None"]
    assert qualification_frame.endswith(
        f"\x1b[{qualification_frame.count('\n')}B\r"
    )

    monkeypatch.setattr(launch, "qualify_executable", qualify_executable)

    cancellation = exercise_lookup_worker_cancellation(monkeypatch)
    canceled = UsageLookupWorkerResult((), UsageLookupFailure.CANCELED)
    assert cancellation == LookupCancellationProof(
        before_start_joined=True,
        before_start_results=(canceled,),
        before_start_process_count=0,
        worker_started=True,
        active_joined=True,
        active_results=(canceled,),
        active_process_count=1,
        active_reaped=True,
    )

    one_shot = OneShotRecorder()
    monkeypatch.setattr(usage, "run", one_shot)
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        redirected_terminal,
    )
    redirected = runner.invoke(application, [], obj=InvocationContext())
    check = runner.invoke(application, ["check"], obj=InvocationContext())
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        interactive_terminal,
    )
    disabled = runner.invoke(
        application,
        ["--no-interactive"],
        obj=InvocationContext(),
    )
    help_result = runner.invoke(
        application,
        ["--help"],
        obj=InvocationContext(),
    )
    version = runner.invoke(
        application,
        ["--version"],
        obj=InvocationContext(),
    )

    assert (
        redirected.exit_code,
        check.exit_code,
        disabled.exit_code,
        help_result.exit_code,
        version.exit_code,
    ) == (0, 0, 0, 0, 0)
    assert one_shot.calls == ONE_SHOT_ROUTE_COUNT
    assert events == ["load:codex", "replace:codex"]
