"""Boundary tests for branding on interactive command output."""

import io
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.core.models import Account, ClaudeCredentials
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.credentials import CredentialService
from sidekick_usages.daemon import DaemonOperation, DaemonOperationResult
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.registry import build_provider_registry
from tests.test_support import (
    FixedClock,
    make_account_store_with_private,
    make_application_paths,
)


def _install_context(
    tmp_path: Path,
    accounts: list[Account],
) -> tuple[io.StringIO, io.StringIO]:
    paths = make_application_paths(tmp_path)
    store, private_credentials = make_account_store_with_private(
        tmp_path,
        accounts,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    clock = FixedClock()
    http = HttpClient()
    providers = build_provider_registry(clock)
    cli.set_context(
        cli.AppContext(
            store=store,
            http=http,
            providers=providers,
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, width=85, force_terminal=False),
            err_console=Console(file=stderr, width=85, force_terminal=False),
            clock=clock,
            credentials=CredentialService(
                store,
                http,
                providers,
                private_credentials,
                clock=clock,
            ),
        )
    )
    return stdout, stderr


def test_list_uses_one_shared_header_for_rows_and_empty_state(
    tmp_path: Path,
) -> None:
    account = Account(
        label=AccountLabel("personal"),
        credentials=ClaudeCredentials(access_token="sk-ant-oat01-test-token"),
        plan="max",
    )
    populated, _ = _install_context(tmp_path / "populated", [account])
    result = CliRunner().invoke(cli.app, ["list"])
    assert result.exit_code == 0
    output = populated.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert output.lower().count("saved accounts") == 1
    assert "personal" in output

    empty, _ = _install_context(tmp_path / "empty", [])
    result = CliRunner().invoke(cli.app, ["list"])
    assert result.exit_code == 0
    output = empty.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert "(no accounts saved)" in output


def test_no_account_check_is_branded_but_quiet_maintenance_is_not(
    tmp_path: Path,
) -> None:
    _, check_stderr = _install_context(tmp_path / "check", [])
    result = CliRunner().invoke(cli.app, [])
    assert result.exit_code == 1
    assert check_stderr.getvalue().count(ROBOT_LINES[2]) == 1

    maintain_stdout, maintain_stderr = _install_context(
        tmp_path / "maintain",
        [],
    )
    result = CliRunner().invoke(cli.app, ["maintain", "--quiet"])
    assert result.exit_code == 1
    output = maintain_stdout.getvalue() + maintain_stderr.getvalue()
    assert ROBOT_LINES[2] not in output


def test_daemon_header_is_limited_to_successful_status(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeDaemonManager:
        status_exit_code = ExitCode.SUCCESS

        def run(
            self,
            operation: DaemonOperation,
            backend: str,
        ) -> DaemonOperationResult:
            if operation is DaemonOperation.STATUS:
                message = (
                    "healthy"
                    if self.status_exit_code is ExitCode.SUCCESS
                    else "missing"
                )
                return DaemonOperationResult(
                    backend,
                    message,
                    self.status_exit_code,
                )
            if operation is DaemonOperation.INSTALL:
                return DaemonOperationResult(backend, "installed")
            raise AssertionError(
                f"Unexpected daemon operation: {operation.value}"
            )

    monkeypatch.setattr(cli, "DaemonManager", FakeDaemonManager)

    status_stdout, _ = _install_context(tmp_path / "status", [])
    result = CliRunner().invoke(cli.app, ["daemon", "status"])
    assert result.exit_code == 0
    output = status_stdout.getvalue()
    assert output.count(ROBOT_LINES[2]) == 1
    assert "daemon status" in output
    assert "auto: healthy" in output

    install_stdout, _ = _install_context(tmp_path / "install", [])
    result = CliRunner().invoke(cli.app, ["daemon", "install"])
    assert result.exit_code == 0
    output = install_stdout.getvalue()
    assert ROBOT_LINES[2] not in output
    assert output.strip() == "auto: installed"

    FakeDaemonManager.status_exit_code = ExitCode.MANUAL_ACTION
    failed_stdout, _ = _install_context(tmp_path / "failed-status", [])
    result = CliRunner().invoke(cli.app, ["daemon", "status"])
    assert result.exit_code == 1
    output = failed_stdout.getvalue()
    assert ROBOT_LINES[2] not in output
    assert output.strip() == "auto: missing"
