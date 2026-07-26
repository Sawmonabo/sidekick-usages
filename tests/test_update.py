"""Load-bearing tests for release checks and self-update behavior."""

import io
import subprocess
from collections.abc import Callable, Mapping

import pytest
from rich.console import Console

from sidekick_usages import __version__
from sidekick_usages.cli.context import UpdateContext
from sidekick_usages.errors import ForbiddenError
from sidekick_usages.http.client import HttpClient
from sidekick_usages.serialization.json import JsonObject
from sidekick_usages.update import (
    PACKAGE_NAME,
    InstallMethod,
    UpdateService,
    fetch_latest_release,
    is_newer,
    parse_version,
)
from tests.test_support import CliHarness

EXPECTED_RELEASES_URL = (
    "https://api.github.com/repos/Sawmonabo/sidekick-usages/releases/latest"
)
UV_EXECUTABLE = "/home/user/.local/share/uv/tools/sidekick-usages/bin/python"
PIPX_EXECUTABLE = (
    "/home/user/.local/share/pipx/venvs/sidekick-usages/bin/python"
)
MACOS_HOMEBREW_EXECUTABLE = (
    "/opt/homebrew/Cellar/sidekick-usages/0.2.0/libexec/bin/python"
)
LINUX_HOMEBREW_EXECUTABLE = (
    "/home/linuxbrew/.linuxbrew/Cellar/sidekick-usages/0.2.0/bin/python"
)
UNKNOWN_EXECUTABLE = "/usr/local/bin/python"
UV_UPGRADE = ("uv", "tool", "upgrade", PACKAGE_NAME)
PIPX_UPGRADE = ("pipx", "upgrade", PACKAGE_NAME)
HOMEBREW_UPGRADE = ("brew", "upgrade", PACKAGE_NAME)
FAILED_COMMAND_EXIT_CODE = 7


class _FakeHttp(HttpClient):
    """Record release requests and return one configured response."""

    def __init__(
        self,
        response_json: JsonObject | None = None,
        raise_on_get: Exception | None = None,
    ) -> None:
        super().__init__()
        self.response_json: JsonObject = (
            response_json if response_json is not None else {}
        )
        self.raise_on_get = raise_on_get
        self.calls: list[tuple[str, str]] = []

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        """Return the configured release response."""
        del headers
        self.calls.append(("GET", url))
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self.response_json


def _unexpected_command(command: tuple[str, ...]) -> None:
    """Fail if a command crosses a non-update test boundary."""
    raise AssertionError(f"Unexpected command execution: {command!r}")


def _cli_harness(
    http: HttpClient,
    *,
    executable: str = UNKNOWN_EXECUTABLE,
    command_executor: Callable[[tuple[str, ...]], None] = _unexpected_command,
) -> tuple[CliHarness, io.StringIO, io.StringIO]:
    """Compose the update commands with deterministic boundaries."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        update=UpdateContext(
            UpdateService(
                http,
                executable=executable,
                command_executor=command_executor,
            )
        ),
    )
    return harness, stdout, stderr


def test_version_parsing_and_comparison() -> None:
    """Stable numeric releases parse and compare without guesswork."""
    assert parse_version("v0.3.0") == (0, 3, 0)
    assert parse_version("0.3.0") == (0, 3, 0)
    assert parse_version("0.3") == (0, 3)
    assert is_newer("0.3.0", "0.2.0") is True
    assert is_newer("0.2.0", "0.2.0") is False
    assert is_newer("0.1.0", "0.2.0") is False

    with pytest.raises(ValueError, match="invalid literal"):
        parse_version("0.3.x")


def test_release_fetch_targets_normalizes_and_rejects_shape() -> None:
    """Release lookup uses the canonical endpoint and strict tag shape."""
    http = _FakeHttp(response_json={"tag_name": "v0.3.0"})

    assert UpdateService(http).latest_release() == "0.3.0"
    assert http.calls == [("GET", EXPECTED_RELEASES_URL)]

    with pytest.raises(ValueError, match="tag_name"):
        fetch_latest_release(_FakeHttp(response_json={}))
    with pytest.raises(ValueError, match="tag_name"):
        fetch_latest_release(_FakeHttp(response_json={"tag_name": 42}))


def test_update_service_owns_detection_selection_and_execution() -> None:
    """The service selects and executes every supported install path."""
    commands: list[tuple[str, ...]] = []
    cases = (
        (UV_EXECUTABLE, InstallMethod.UV, UV_UPGRADE),
        (PIPX_EXECUTABLE, InstallMethod.PIPX, PIPX_UPGRADE),
        (
            MACOS_HOMEBREW_EXECUTABLE,
            InstallMethod.HOMEBREW,
            HOMEBREW_UPGRADE,
        ),
        (
            LINUX_HOMEBREW_EXECUTABLE,
            InstallMethod.HOMEBREW,
            HOMEBREW_UPGRADE,
        ),
    )

    for executable, install_method, expected_command in cases:
        service = UpdateService(
            _FakeHttp(),
            executable=executable,
            command_executor=commands.append,
        )
        previous_commands = commands.copy()

        assert service.install_method() is install_method
        assert service.upgrade(dry_run=True) == expected_command
        assert commands == previous_commands
        assert service.upgrade() == expected_command
        assert commands == [*previous_commands, expected_command]

    unknown = UpdateService(
        _FakeHttp(),
        executable=UNKNOWN_EXECUTABLE,
        command_executor=commands.append,
    )
    assert unknown.install_method() is InstallMethod.UNKNOWN
    with pytest.raises(ValueError, match="install method"):
        unknown.upgrade()
    assert commands == [
        UV_UPGRADE,
        PIPX_UPGRADE,
        HOMEBREW_UPGRADE,
        HOMEBREW_UPGRADE,
    ]


def test_check_update_cli_reports_release_states_and_rate_limit() -> None:
    """The CLI reports newer, current, and rate-limited release states."""
    newer_http = _FakeHttp(response_json={"tag_name": "v99.0.0"})
    harness, stdout, stderr = _cli_harness(newer_http)

    result = harness.invoke(["check-update"])

    assert result.exit_code == 0
    assert "99.0.0" in stdout.getvalue()
    assert "update" in stdout.getvalue().lower()
    assert stderr.getvalue() == ""
    assert newer_http.calls == [("GET", EXPECTED_RELEASES_URL)]

    current_http = _FakeHttp(response_json={"tag_name": f"v{__version__}"})
    harness, stdout, stderr = _cli_harness(current_http)

    result = harness.invoke(["check-update"])

    assert result.exit_code == 0
    assert "up to date" in stdout.getvalue().lower()
    assert "sidekick usages · update status" in stdout.getvalue()
    assert stderr.getvalue() == ""

    limited_http = _FakeHttp(raise_on_get=ForbiddenError("API rate limit"))
    harness, stdout, stderr = _cli_harness(limited_http)

    result = harness.invoke(["check-update"])

    assert result.exit_code == 1
    assert stdout.getvalue() == ""
    assert "rate limit" in stderr.getvalue().lower()
    assert "sidekick usages · update status" not in stderr.getvalue()


def test_update_cli_dry_run_and_execution_use_exact_argv() -> None:
    """Dry-run prints once; normal execution owns the exact same argv."""
    commands: list[tuple[str, ...]] = []
    harness, stdout, stderr = _cli_harness(
        _FakeHttp(),
        executable=UV_EXECUTABLE,
        command_executor=commands.append,
    )

    dry_run = harness.invoke(["update", "--dry-run"])

    assert dry_run.exit_code == 0
    assert "$ uv tool upgrade sidekick-usages" in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert commands == []

    stdout.seek(0)
    stdout.truncate()

    update = harness.invoke(["update"])

    assert update.exit_code == 0
    assert stdout.getvalue().strip() == "$ uv tool upgrade sidekick-usages"
    assert stderr.getvalue() == ""
    assert commands == [UV_UPGRADE]


def test_update_cli_maps_typed_execution_failures() -> None:
    """Typed executor failures and unknown installs stay bounded."""

    def tool_missing(_command: tuple[str, ...]) -> None:
        raise FileNotFoundError("uv")

    harness, stdout, stderr = _cli_harness(
        _FakeHttp(),
        executable=UV_EXECUTABLE,
        command_executor=tool_missing,
    )

    missing = harness.invoke(["update"])

    assert missing.exit_code == 1
    assert "$ uv tool upgrade sidekick-usages" in stdout.getvalue()
    assert "not found on PATH" in stderr.getvalue()

    def command_failed(command: tuple[str, ...]) -> None:
        raise subprocess.CalledProcessError(FAILED_COMMAND_EXIT_CODE, command)

    harness, stdout, stderr = _cli_harness(
        _FakeHttp(),
        executable=UV_EXECUTABLE,
        command_executor=command_failed,
    )

    failed = harness.invoke(["update"])

    assert failed.exit_code == FAILED_COMMAND_EXIT_CODE
    assert "$ uv tool upgrade sidekick-usages" in stdout.getvalue()
    assert stderr.getvalue() == ""

    harness, stdout, stderr = _cli_harness(_FakeHttp())

    unknown = harness.invoke(["update"])

    assert unknown.exit_code == 1
    assert stdout.getvalue() == ""
    assert "uv tool upgrade" in stderr.getvalue()
    assert "pipx upgrade" in stderr.getvalue()
    assert "brew upgrade" in stderr.getvalue()
