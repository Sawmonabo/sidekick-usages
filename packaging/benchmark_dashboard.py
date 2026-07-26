#!/usr/bin/env python3
"""Measure the dashboard release boundary on its supported platform."""

import os
import subprocess
import sys
from pathlib import Path

PACKAGING_ROOT = Path(__file__).resolve().parent
WINDOWS_MODULE = "dashboard_benchmark.windows"
UNIX_MODULE = "dashboard_benchmark.unix.parent"


def main() -> int:
    """Run the platform gate and propagate its output and exit status."""
    platform_module = WINDOWS_MODULE if os.name == "nt" else UNIX_MODULE
    result = subprocess.run(
        [sys.executable, "-m", platform_module],
        cwd=PACKAGING_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
