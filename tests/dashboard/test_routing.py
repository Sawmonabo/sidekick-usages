"""Cached dashboard routing behavior."""

import io
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.commands import usage
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.dashboard import application, launch, terminal
from sidekick_usages.cli.runtime import bootstrap
from sidekick_usages.cli.runtime.routing import (
    dashboard_arguments,
    dashboard_candidate,
    parse_dashboard_arguments,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupFailure,
    UsageLookupWorkerResult,
)
from sidekick_usages.usage.presentation.dashboard.selection import (
    CURSOR_GLYPH,
)
from tests.fakes.dashboard.lookup_worker import (
    LookupCancellationProof,
    exercise_lookup_worker_cancellation,
)
from tests.fakes.dashboard.runtime import (
    OneShotRecorder,
    interactive_terminal,
    redirected_terminal,
)
from tests.fakes.dashboard.state import controller_snapshot
from tests.support.platform import MANAGED_RUNTIME_SUPPORTED

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
ONE_SHOT_ROUTE_COUNT = 3
WINDOWS_CHILD_EXIT_CODE = 7
ACTUAL_TEST_TERMINAL_WIDTH = 37


def _assert_execve_process_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove closed routes use one qualified no-shell replacement."""
    replacements: list[tuple[Path, tuple[str, ...], dict[str, str]]] = []
    qualifications: list[Path] = []

    def record_qualification(path: Path) -> ExecutableProvenance:
        qualifications.append(path)
        return qualify_executable(path)

    def record_execve(
        executable: Path,
        arguments: tuple[str, ...],
        environment: dict[str, str],
    ) -> Never:
        replacements.append((executable, arguments, environment))
        raise OSError("Synthetic no-shell replacement failure.")

    def run_cached_dashboard(only: ProviderId | None) -> int:
        return bootstrap.execute_interactive_dashboard(
            dashboard_arguments(only)
        )

    with monkeypatch.context() as replacement_boundary:
        replacement_boundary.setattr(bootstrap.sys, "platform", "linux")
        replacement_boundary.setattr(bootstrap.os, "execve", record_execve)
        replacement_boundary.setattr(
            bootstrap,
            "_run_cached_dashboard",
            run_cached_dashboard,
        )
        replacement_boundary.setattr(
            bootstrap,
            "qualify_executable",
            record_qualification,
        )
        for terminal, arguments in (
            (interactive_terminal, ()),
            (interactive_terminal, ("--only", "codex")),
            (interactive_terminal, ("--only=claude",)),
            (interactive_terminal, ("--only", "unsupported")),
            (interactive_terminal, ("--help",)),
            (redirected_terminal, ()),
        ):
            replacement_boundary.setattr(
                bootstrap,
                "_interactive_terminal_supported",
                terminal,
            )
            assert (
                bootstrap.main(arguments)
                == bootstrap.PROCESS_LAUNCH_FAILURE_EXIT_CODE
            )

    executable = Path(sys.executable)
    assert qualifications == [executable] * 6
    assert [arguments[2:] for _, arguments, _ in replacements] == [
        (bootstrap.INTERACTIVE_DASHBOARD_MODULE,),
        (bootstrap.INTERACTIVE_DASHBOARD_MODULE, "--only", "codex"),
        (bootstrap.INTERACTIVE_DASHBOARD_MODULE, "--only", "claude"),
        (bootstrap.APPLICATION_MODULE, "--only", "unsupported"),
        (bootstrap.APPLICATION_MODULE, "--help"),
        (bootstrap.APPLICATION_MODULE,),
    ]
    assert all(
        path == executable and arguments[:2] == (sys.executable, "-m")
        for path, arguments, _ in replacements
    )
    assert all(
        environment is not os.environ for *_, environment in replacements
    )

    windows_calls: list[tuple[tuple[str, ...], bool, dict[str, str]]] = []

    def run_windows(
        command: tuple[str, ...],
        *,
        check: bool,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[bytes]:
        windows_calls.append((command, check, env))
        return subprocess.CompletedProcess(command, WINDOWS_CHILD_EXIT_CODE)

    expected_environment: dict[str, str]
    with monkeypatch.context() as windows_boundary:
        windows_boundary.setattr(bootstrap.sys, "platform", "win32")
        windows_boundary.setattr(
            bootstrap,
            "subprocess",
            subprocess,
            raising=False,
        )
        windows_boundary.setattr(
            subprocess,
            "run",
            run_windows,
        )
        windows_boundary.setenv(
            bootstrap.PYTHON_IO_ENCODING_ENVIRONMENT_KEY,
            "cp1252",
        )
        expected_environment = os.environ.copy()
        expected_environment[bootstrap.PYTHON_IO_ENCODING_ENVIRONMENT_KEY] = (
            bootstrap.UTF8_IO_ENCODING
        )
        assert bootstrap.main(()) == WINDOWS_CHILD_EXIT_CODE

    assert len(windows_calls) == 1
    command, check, environment = windows_calls[0]
    assert command == (
        sys.executable,
        "-m",
        bootstrap.APPLICATION_MODULE,
    )
    assert check is False
    assert environment == expected_environment
    assert environment is not os.environ


def test_cli_routes_one_cached_frame_before_isolated_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One routing journey preserves TTY and one-shot process boundaries."""
    monkeypatch.setenv("COLUMNS", "999")
    monkeypatch.setattr(
        terminal.os,
        "get_terminal_size",
        lambda _descriptor: os.terminal_size((ACTUAL_TEST_TERMINAL_WIDTH, 24)),
    )
    assert bootstrap.terminal_width is terminal.terminal_width
    assert application.terminal_width is terminal.terminal_width
    assert terminal.terminal_width(sys.stdout) == ACTUAL_TEST_TERMINAL_WIDTH

    output = io.StringIO()
    snapshot = controller_snapshot(REFERENCE_TIME)
    snapshot = replace(
        snapshot,
        providers=(
            replace(
                snapshot.providers[0],
                runtime_state=ProviderRuntimeState.UNREADABLE,
                active_account_id=None,
            ),
            snapshot.providers[1],
        ),
    )
    line_count = launch.present_cached_dashboard(
        output,
        snapshot,
        width=100,
        color=False,
    )
    cli = create_app()
    runner = CliRunner()
    frame = output.getvalue()
    cursor_lines = [
        line for line in frame.splitlines() if CURSOR_GLYPH in line
    ]

    assert line_count > 0
    assert "CLAUDE" in frame
    assert "CODEX" in frame
    assert not cursor_lines
    assert frame.endswith(f"\x1b[{line_count}A\r")
    assert parse_dashboard_arguments(()) is None
    assert parse_dashboard_arguments(("--only", "codex")) is ProviderId.CODEX
    assert parse_dashboard_arguments(("--only=claude",)) is ProviderId.CLAUDE
    assert dashboard_candidate(("--only", "codex"))
    assert not dashboard_candidate(("--only", "unsupported"))
    assert dashboard_arguments(ProviderId.CODEX) == ("--only", "codex")
    with pytest.raises(ValueError, match="not a valid ProviderId"):
        parse_dashboard_arguments(("--only", "unsupported"))

    _assert_execve_process_boundary(monkeypatch)

    cancellation = exercise_lookup_worker_cancellation(monkeypatch)
    canceled = UsageLookupWorkerResult((), UsageLookupFailure.CANCELED)
    active_result = (
        canceled
        if MANAGED_RUNTIME_SUPPORTED
        else UsageLookupWorkerResult(
            (),
            UsageLookupFailure.FEATURE_DISABLED,
        )
    )
    assert cancellation == LookupCancellationProof(
        before_start_joined=True,
        before_start_results=(canceled,),
        before_start_process_count=0,
        worker_started=MANAGED_RUNTIME_SUPPORTED,
        active_joined=True,
        active_results=(active_result,),
        active_process_count=int(MANAGED_RUNTIME_SUPPORTED),
        active_reaped=MANAGED_RUNTIME_SUPPORTED,
    )

    one_shot = OneShotRecorder()
    monkeypatch.setattr(usage, "run", one_shot)
    redirected = runner.invoke(cli, [], obj=InvocationContext())
    check = runner.invoke(cli, ["check"], obj=InvocationContext())
    disabled = runner.invoke(
        cli,
        ["--no-interactive"],
        obj=InvocationContext(),
    )
    help_result = runner.invoke(
        cli,
        ["--help"],
        obj=InvocationContext(),
    )
    version = runner.invoke(
        cli,
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
