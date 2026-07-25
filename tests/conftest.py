"""Shared pytest fixtures with repository-wide lifecycle ownership."""

import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def short_socket_root() -> Iterator[Path]:
    """Provide a root below the Unix socket path-length limit."""
    with tempfile.TemporaryDirectory(prefix="sku-") as root:
        yield Path(root)
