"""Isolated subprocess boundary for the released-v0.6 compatibility oracle."""

import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

_COMMAND_TIMEOUT_SECONDS = 60

type ProcessRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str] | None],
    subprocess.CompletedProcess[str],
]


def run_process(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded argv-only subprocess."""
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


def isolated_environment(home: Path) -> dict[str, str]:
    """Return a minimal environment rooted in one temporary home."""
    environment = {
        "HOME": str(home),
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("PATH", "SYSTEMROOT", "WINDIR"):
        if (value := os.environ.get(name)) is not None:
            environment[name] = value
    return environment


def install_network_guard() -> None:
    """Deny socket operations in the current compatibility process."""

    def deny_network(event: str, _args: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            raise PermissionError("Network access is disabled.")

    sys.addaudithook(deny_network)


__all__ = [
    "ProcessRunner",
    "install_network_guard",
    "isolated_environment",
    "run_process",
]
