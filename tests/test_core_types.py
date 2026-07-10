"""Behavior and boundary tests for shared core vocabulary."""

import subprocess
import sys

import pytest

from sidekick_usages.core import ExitCode, ProviderId


def test_provider_ids_preserve_the_closed_string_boundary() -> None:
    """Supported provider names remain the exact public string values."""
    assert (ProviderId.CLAUDE, ProviderId.CODEX) == ("claude", "codex")
    with pytest.raises(ValueError, match="unsupported"):
        ProviderId("unsupported")


def test_exit_codes_preserve_the_closed_process_boundary() -> None:
    """Application outcomes retain their documented process integers."""
    assert (
        ExitCode.SUCCESS,
        ExitCode.MANUAL_ACTION,
        ExitCode.SYSTEM_ERROR,
        ExitCode.SCHEDULER_ERROR,
    ) == (0, 1, 2, 3)
    with pytest.raises(ValueError, match="4"):
        ExitCode(4)


def test_importing_core_does_not_load_outer_layers() -> None:
    """The core package has no runtime dependency on outer application code."""
    script = """
import json
import sys
import sidekick_usages.core

forbidden = (
    "pydantic",
    "rich",
    "typer",
    "urllib3",
    "sidekick_usages.cli",
    "sidekick_usages.clock",
    "sidekick_usages.http",
    "sidekick_usages.paths",
    "sidekick_usages.persistence",
    "sidekick_usages.providers",
    "sidekick_usages.store",
)
loaded = sorted(
    name for name in sys.modules if name.startswith(forbidden)
)
print(json.dumps(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == "[]"
