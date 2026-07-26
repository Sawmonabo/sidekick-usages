#!/usr/bin/env python3
"""Measure the dashboard release boundary on its supported platform."""

import os
import sys
from pathlib import Path

PACKAGING_ROOT = Path(__file__).resolve().parent
WINDOWS_MODULE = "dashboard_benchmark.windows"
UNIX_MODULE = "dashboard_benchmark.unix.parent"


def main() -> None:
    """Replace this launcher with the platform-specific release gate."""
    platform_module = WINDOWS_MODULE if os.name == "nt" else UNIX_MODULE
    os.chdir(PACKAGING_ROOT)
    os.execv(
        sys.executable,
        (sys.executable, "-m", platform_module),
    )


if __name__ == "__main__":
    main()
