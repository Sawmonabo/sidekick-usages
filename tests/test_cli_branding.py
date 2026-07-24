"""Boundary tests for branding on interactive command output."""

import io
from pathlib import Path

from rich.console import Console

from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.cli.context import DaemonContext
from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.models.lifecycle import DaemonOperationResult
from sidekick_usages.daemon.types.lifecycle import (
    DaemonOperation,
    ServiceBackendId,
    ServiceLifecycleState,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.registry import build_provider_registry
from tests.test_support import (
    CliHarness,
    FixedClock,
    make_account_store_with_private,
    make_app_context,
)


def _install_context(
    tmp_path: Path,
    accounts: list[Account],
) -> tuple[CliHarness, io.StringIO, io.StringIO]:
    store, private_credentials = make_account_store_with_private(
        tmp_path,
        accounts,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    clock = FixedClock()
    http = HttpClient()
    providers = build_provider_registry(clock)
    harness = CliHarness(
        console=Console(file=stdout, width=85, force_terminal=False),
        err_console=Console(file=stderr, width=85, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private_credentials,
            clock,
            heartbeat_providers={},
        ),
    )
    return harness, stdout, stderr


def test_list_uses_one_shared_header_for_rows_and_empty_state(
    tmp_path: Path,
) -> None:
    account = Account(
        label=AccountLabel("personal"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-test-token"
        ),
        plan="max",
    )
    populated_cli, populated, _ = _install_context(
        tmp_path / "populated", [account]
    )
    result = populated_cli.invoke(["list"])
    assert result.exit_code == 0
    output = populated.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert output.lower().count("saved accounts") == 1
    assert "personal" in output

    empty_cli, empty, _ = _install_context(tmp_path / "empty", [])
    result = empty_cli.invoke(["list"])
    assert result.exit_code == 0
    output = empty.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert "(no accounts saved)" in output


def test_no_account_check_is_branded_but_quiet_maintenance_is_not(
    tmp_path: Path,
) -> None:
    check_cli, _, check_stderr = _install_context(tmp_path / "check", [])
    result = check_cli.invoke([])
    assert result.exit_code == 1
    assert check_stderr.getvalue().count(ROBOT_LINES[2]) == 1

    maintain_cli, maintain_stdout, maintain_stderr = _install_context(
        tmp_path / "maintain",
        [],
    )
    result = maintain_cli.invoke(["maintain", "--quiet"])
    assert result.exit_code == 1
    output = maintain_stdout.getvalue() + maintain_stderr.getvalue()
    assert ROBOT_LINES[2] not in output


def test_daemon_header_is_limited_to_successful_status(
    tmp_path: Path,
) -> None:
    class FakeDaemonManager(DaemonManager):
        status_exit_code = ExitCode.SUCCESS

        def __init__(self) -> None:
            pass

        def run(
            self,
            operation: DaemonOperation | str,
        ) -> DaemonOperationResult:
            operation_id = DaemonOperation(operation)
            if operation_id is DaemonOperation.STATUS:
                message = (
                    "healthy"
                    if self.status_exit_code is ExitCode.SUCCESS
                    else "missing"
                )
                return DaemonOperationResult(
                    ServiceBackendId.SYSTEMD,
                    (
                        ServiceLifecycleState.READY
                        if self.status_exit_code is ExitCode.SUCCESS
                        else ServiceLifecycleState.ABSENT
                    ),
                    message,
                    self.status_exit_code,
                )
            if operation_id is DaemonOperation.INSTALL:
                return DaemonOperationResult(
                    ServiceBackendId.SYSTEMD,
                    ServiceLifecycleState.READY,
                    "installed",
                )
            raise AssertionError(
                f"Unexpected daemon operation: {operation_id.value}"
            )

    status_cli, status_stdout, _ = _install_context(tmp_path / "status", [])
    status_cli.daemon = DaemonContext(FakeDaemonManager())
    result = status_cli.invoke(["daemon", "status"])
    assert result.exit_code == 0
    output = status_stdout.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert "daemon status" in output
    assert "systemd: healthy" in output

    install_cli, install_stdout, _ = _install_context(tmp_path / "install", [])
    install_cli.daemon = DaemonContext(FakeDaemonManager())
    result = install_cli.invoke(["daemon", "install"])
    assert result.exit_code == 0
    output = install_stdout.getvalue()
    assert ROBOT_LINES[2] not in output
    assert output.strip() == "systemd: installed"

    FakeDaemonManager.status_exit_code = ExitCode.MANUAL_ACTION
    failed_cli, failed_stdout, _ = _install_context(
        tmp_path / "failed-status", []
    )
    failed_cli.daemon = DaemonContext(FakeDaemonManager())
    result = failed_cli.invoke(["daemon", "status"])
    assert result.exit_code == 1
    output = failed_stdout.getvalue()
    assert ROBOT_LINES[2] not in output
    assert output.strip() == "systemd: missing"
