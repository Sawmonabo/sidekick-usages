"""Native Codex home discovery."""

import os
from pathlib import Path

CODEX_HOME_ENVIRONMENT_KEY = "CODEX_HOME"


def default_codex_home() -> Path:
    """Return the Codex home selected for an ordinary native invocation."""
    configured = os.environ.get(CODEX_HOME_ENVIRONMENT_KEY)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"
