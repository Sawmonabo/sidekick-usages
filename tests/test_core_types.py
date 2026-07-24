"""Behavior and boundary tests for shared core vocabulary."""

import subprocess
import sys
from pathlib import Path

import pytest

from sidekick_usages.core.types import (
    ExitCode,
    ProviderId,
    TokenActivityScope,
    highest_exit_code,
)


def test_provider_ids_preserve_the_closed_string_boundary() -> None:
    """Supported provider names remain the exact public string values."""
    assert (ProviderId.CLAUDE, ProviderId.CODEX) == ("claude", "codex")
    with pytest.raises(ValueError, match="unsupported"):
        ProviderId("unsupported")
    assert tuple(TokenActivityScope) == (
        "account",
        "local_installation",
    )


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
    assert highest_exit_code() is ExitCode.SUCCESS
    assert (
        highest_exit_code(
            ExitCode.MANUAL_ACTION,
            ExitCode.SUCCESS,
            ExitCode.SCHEDULER_ERROR,
            ExitCode.SYSTEM_ERROR,
        )
        is ExitCode.SCHEDULER_ERROR
    )
    assert (
        highest_exit_code(
            ExitCode.MANUAL_ACTION,
            ExitCode.SYSTEM_ERROR,
        )
        is ExitCode.SYSTEM_ERROR
    )


def test_importing_core_does_not_load_outer_layers() -> None:
    """The core package has no runtime dependency on outer application code."""
    script = """
import json
import sys
import sidekick_usages.core
import sidekick_usages.core.expiry
import sidekick_usages.core.models

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

    package_root = Path(__file__).parents[1] / "src" / "sidekick_usages"
    assert (package_root / "core" / "expiry.py").is_file()
    assert (package_root / "core" / "models.py").is_file()
    assert not (package_root / "store.py").exists()
    assert not (package_root / "report.py").exists()
