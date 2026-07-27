"""CLI-scoped pytest fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent CLI tests from reading the developer's native Codex login."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-codex"))
