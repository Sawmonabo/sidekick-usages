"""Meaningful integration checks for shared branded help output."""

from collections.abc import Callable
from typing import Never

import click
import pytest
from rich.cells import cell_len
from typer.testing import CliRunner

from sidekick_usages import __version__
from sidekick_usages.branding.content import BRAND_DESCRIPTION, ROBOT_LINES
from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts.models import InvocationComposers

CLI_APP = create_app()


def _sentinel(
    name: str,
    calls: list[str],
) -> Callable[[], Never]:
    def compose() -> Never:
        calls.append(name)
        raise AssertionError(f"Informational path composed {name} context.")

    return compose


def _no_composition_context(calls: list[str]) -> InvocationContext:
    return InvocationContext(
        composers=InvocationComposers(
            application=_sentinel("app", calls),
            persistence=_sentinel("persistence", calls),
            doctor=_sentinel("doctor", calls),
            daemon=_sentinel("daemon", calls),
            migration=_sentinel("migration", calls),
            update=_sentinel("update", calls),
        ),
        use_composer=_sentinel("use", calls),
    )


@pytest.mark.parametrize(
    "args",
    [
        ["--help"],
        ["refresh", "--help"],
        ["doctor", "--help"],
        ["daemon", "status", "--help"],
        ["claude", "--help"],
        ["claude", "setup-token", "--help"],
        ["codex", "--help"],
        ["codex", "login", "--help"],
        ["session", "--help"],
        ["session", "claude", "--help"],
        ["session", "codex", "--help"],
        ["session", "shell", "--help"],
        ["session", "shell", "install", "--help"],
    ],
)
def test_help_is_branded_before_usage_without_loading_state(
    args: list[str],
) -> None:
    calls: list[str] = []
    result = CliRunner().invoke(
        CLI_APP,
        args,
        obj=_no_composition_context(calls),
    )
    assert result.exit_code == 0
    assert result.output.count(ROBOT_LINES[2]) == 1
    assert result.output.index(ROBOT_LINES[2]) < result.output.index("Usage:")
    assert "Check Claude Code and Codex CLI usage across" not in result.output
    assert calls == []


@pytest.mark.parametrize(
    "path",
    [
        [],
        ["refresh"],
        ["claude"],
        ["claude", "setup-token"],
        ["codex"],
        ["codex", "login"],
        ["heartbeat"],
        ["daemon", "status"],
        ["session"],
        ["session", "claude"],
        ["session", "codex"],
        ["session", "shell"],
        ["session", "shell", "install"],
    ],
)
def test_short_help_alias_matches_long_help_at_every_command_level(
    path: list[str],
) -> None:
    """Root settings provide equivalent help through nested command levels."""
    calls: list[str] = []
    short = CliRunner().invoke(
        CLI_APP,
        [*path, "-h"],
        obj=_no_composition_context(calls),
    )
    long = CliRunner().invoke(
        CLI_APP,
        [*path, "--help"],
        obj=_no_composition_context(calls),
    )

    assert short.exit_code == long.exit_code == 0
    assert click.unstyle(short.output) == click.unstyle(long.output)
    assert calls == []


@pytest.mark.parametrize(
    ("args", "usage"),
    [
        (["doctor", "--help"], "Usage: sidekick-usages doctor"),
        (["heartbeat", "--help"], "Usage: sidekick-usages heartbeat"),
        (
            ["daemon", "status", "--help"],
            "Usage: sidekick-usages daemon status",
        ),
    ],
)
def test_leaf_and_nested_help_share_one_header(
    args: list[str],
    usage: str,
) -> None:
    result = CliRunner().invoke(CLI_APP, args, env={"CI": "true"})
    assert result.exit_code == 0
    output = click.unstyle(result.output)
    assert output.count(ROBOT_LINES[2]) == 1
    assert output.index(ROBOT_LINES[2]) < output.index(usage)


def _panel_widths(output: str) -> list[int]:
    """Return the rendered widths of Rich panel borders."""
    return [
        cell_len(line)
        for line in click.unstyle(output).splitlines()
        if line.startswith(("╭", "╰", "┌", "└"))
    ]


@pytest.mark.parametrize(
    ("args", "width", "shows_description"),
    [
        (["doctor", "--help"], 40, False),
        (["--help"], 85, True),
        (["doctor", "--help"], 120, True),
    ],
)
def test_help_uses_one_width_policy(
    args: list[str],
    width: int,
    shows_description: bool,
) -> None:
    result = CliRunner().invoke(
        CLI_APP,
        args,
        env={"COLUMNS": "80"},
        terminal_width=width,
    )

    assert result.exit_code == 0
    output = click.unstyle(result.output)
    lines = output.splitlines()
    panel_widths = _panel_widths(output)
    assert set(panel_widths) == {width}
    assert "─" * width in lines
    assert max(cell_len(line) for line in lines) <= width
    assert (BRAND_DESCRIPTION in output) is shows_description


def test_help_width_override_does_not_leak_to_errors() -> None:
    runner = CliRunner()
    baseline_error = runner.invoke(
        CLI_APP,
        ["doctor", "--unknown-option"],
        env={"COLUMNS": "80"},
    )
    help_result = runner.invoke(
        CLI_APP,
        ["doctor", "--help"],
        env={"COLUMNS": "80"},
        terminal_width=120,
    )
    error_result = runner.invoke(
        CLI_APP,
        ["doctor", "--unknown-option"],
        env={"COLUMNS": "80"},
    )

    assert baseline_error.exit_code == click.UsageError.exit_code
    assert help_result.exit_code == 0
    assert error_result.exit_code == click.UsageError.exit_code
    baseline_widths = _panel_widths(baseline_error.output)
    assert baseline_widths
    assert _panel_widths(error_result.output) == baseline_widths


def test_doctor_help_does_not_advertise_removed_auth_option() -> None:
    result = CliRunner().invoke(CLI_APP, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "--auth" not in click.unstyle(result.output)


@pytest.mark.parametrize(
    ("args", "commands"),
    [
        (
            ["claude", "--help"],
            ("setup-token",),
        ),
        (["codex", "--help"], ("login",)),
    ],
)
def test_provider_help_lists_only_canonical_commands(
    args: list[str],
    commands: tuple[str, ...],
) -> None:
    """Provider groups advertise their owned canonical capabilities."""
    result = CliRunner().invoke(CLI_APP, args)

    assert result.exit_code == 0
    output = click.unstyle(result.stdout)
    assert all(command in output for command in commands)
    assert "deprecated" not in output.lower()


def test_root_help_has_only_canonical_provider_groups() -> None:
    """Root help exposes provider groups without compatibility aliases."""
    result = CliRunner().invoke(CLI_APP, ["--help"])

    assert result.exit_code == 0
    output = click.unstyle(result.stdout)
    assert all(
        command in output for command in ("claude", "codex", "session", "use")
    )
    assert "codex-login" not in output
    assert "codex-export" not in output
    assert "(deprecated)" not in output
    assert result.stderr == ""


def test_version_is_exact_and_does_not_compose_any_context() -> None:
    """The eager version path stays unbranded and skips operational state."""
    calls: list[str] = []
    version_result = CliRunner().invoke(
        CLI_APP,
        ["--version"],
        obj=_no_composition_context(calls),
    )

    assert version_result.exit_code == 0
    assert version_result.output == f"sidekick-usages {__version__}\n"
    assert calls == []
