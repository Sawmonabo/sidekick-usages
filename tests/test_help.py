"""Meaningful integration checks for shared branded help output."""

from __future__ import annotations

import pytest
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
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0
    assert result.output.count(ROBOT_LINES[2]) == 1
    assert result.output.index(ROBOT_LINES[2]) < result.output.index(usage)


def test_narrow_help_uses_shared_narrow_header() -> None:
    result = CliRunner().invoke(
        cli.app,
        ["doctor", "--help"],
        terminal_width=40,
    )
    assert result.exit_code == 0
    assert f"{ROBOT_LINES[2]}  sidekick usages" in result.output
    assert BRAND_DESCRIPTION not in result.output
    assert result.output.index(ROBOT_LINES[2]) < result.output.index("Usage:")


def test_version_remains_one_unbranded_line() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert result.output == f"sidekick-usages {cli.__version__}\n"
    assert ROBOT_LINES[2] not in result.output
