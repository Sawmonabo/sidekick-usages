"""Provider-free environment for the installed-console benchmark."""

from collections.abc import Mapping
from pathlib import Path

PROVIDER_ENVIRONMENT_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "OPENAI_",
)
ISOLATED_CONSOLE_PATHS = (
    ("CODEX_HOME", Path("providers") / "codex"),
    ("XDG_CACHE_HOME", Path("xdg") / "cache"),
    ("XDG_CONFIG_HOME", Path("xdg") / "config"),
    ("XDG_DATA_HOME", Path("xdg") / "data"),
    ("XDG_RUNTIME_DIR", Path("xdg") / "runtime"),
    ("XDG_STATE_HOME", Path("xdg") / "state"),
)
ALLOWED_PROVIDER_ENVIRONMENT_VARIABLES = frozenset({"CODEX_HOME"})


def isolated_console_environment(
    source: Mapping[str, str],
    *,
    home: Path,
    console_script: Path,
) -> dict[str, str]:
    """Confine native paths and remove inherited provider overrides."""
    environment = {
        name: value
        for name, value in source.items()
        if not name.startswith(PROVIDER_ENVIRONMENT_PREFIXES)
    }
    environment.update(
        {
            name: str(home / relative)
            for name, relative in ISOLATED_CONSOLE_PATHS
        }
    )
    environment["PATH"] = str(console_script.parent)
    return environment
