"""Smoke test: the package and its CLI entry point import cleanly."""

import sidekick_usages
from sidekick_usages import cli


def test_package_version_is_set() -> None:
    """``__version__`` is a non-empty string."""
    assert isinstance(sidekick_usages.__version__, str)
    assert sidekick_usages.__version__


def test_cli_app_is_exposed() -> None:
    """``sidekick_usages.cli.app`` is the Typer entry point."""
    assert hasattr(cli, "app")
