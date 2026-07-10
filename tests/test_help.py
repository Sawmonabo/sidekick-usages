"""Meaningful integration checks for shared branded help output."""

import click
import pytest
from rich.cells import cell_len
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.branding import BRAND_DESCRIPTION, ROBOT_LINES


def test_root_help_is_branded_before_usage_without_loading_state() -> None:
    cli._ContextState.ctx = None
    result = CliRunner().invoke(cli.app, ["--help"])
    assert result.exit_code == 0
    assert result.output.count(ROBOT_LINES[2]) == 1
    assert result.output.index(ROBOT_LINES[2]) < result.output.index("Usage:")
    assert "Check Claude Code and Codex CLI usage across" not in result.output
    assert cli._ContextState.ctx is None


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
    result = CliRunner().invoke(cli.app, args, env={"CI": "true"})
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
        cli.app,
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
    help_result = runner.invoke(
        cli.app,
        ["doctor", "--help"],
        env={"COLUMNS": "80"},
        terminal_width=120,
    )
    error_result = runner.invoke(
        cli.app,
        ["doctor", "--unknown-option"],
        env={"COLUMNS": "80"},
    )

    assert help_result.exit_code == 0
    assert error_result.exit_code == click.UsageError.exit_code
    assert set(_panel_widths(error_result.output)) == {80}


def test_version_remains_one_unbranded_line() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"sidekick-usages {cli.__version__}\n"
    assert ROBOT_LINES[2] not in result.output
