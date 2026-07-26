"""Shared operating-system test capabilities."""

import sys

import pytest

MANAGED_RUNTIME_SUPPORTED = sys.platform != "win32"
MANAGED_RUNTIME_REASON = "Managed runtimes require Linux, WSL, or macOS."
REQUIRES_MANAGED_RUNTIME = pytest.mark.skipif(
    not MANAGED_RUNTIME_SUPPORTED,
    reason=MANAGED_RUNTIME_REASON,
)
