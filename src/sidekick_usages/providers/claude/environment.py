"""Closed environments for official Claude processes."""

import os
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.platform.environment import (
    SAFE_PROCESS_ENVIRONMENT_KEYS,
    SAFE_PROVIDER_ENVIRONMENT_KEYS,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.types import ClaudeProcessFailure

CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY = "CLAUDE_CONFIG_DIR"
CLAUDE_OAUTH_PROVISIONING_ENVIRONMENT_KEY = "CLAUDE_CODE_OAUTH_REFRESH_TOKEN"
CLAUDE_REFRESH_SCOPES_ENVIRONMENT_KEY = "CLAUDE_CODE_OAUTH_SCOPES"
_MAXIMUM_ENVIRONMENT_VALUE_BYTES = 16 * 1024
_WINDOWS_PROCESS_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)
_CLAUDE_SAFE_ENVIRONMENT_KEYS = (
    SAFE_PROVIDER_ENVIRONMENT_KEYS | _WINDOWS_PROCESS_ENVIRONMENT_KEYS
)


def claude_probe_environment(
    source_environment: Mapping[str, str] | None,
    *,
    isolated_home: Path,
    config_directory: Path,
) -> dict[str, str]:
    """Build a credential-free environment for capability probes."""
    environment = _safe_environment(source_environment)
    environment.update(
        {
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY: str(config_directory),
        }
    )
    _validate_environment(environment)
    return environment


def claude_refresh_environment(
    source_environment: Mapping[str, str] | None,
    *,
    isolated_home: Path,
    refresh_token: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    """Build the closed environment for official refresh-token login."""
    environment = _safe_environment(source_environment)
    environment.update(
        {
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "APPDATA": str(isolated_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(isolated_home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY: str(isolated_home / ".claude"),
            CLAUDE_OAUTH_PROVISIONING_ENVIRONMENT_KEY: refresh_token,
            CLAUDE_REFRESH_SCOPES_ENVIRONMENT_KEY: " ".join(scopes),
        }
    )
    _validate_environment(environment)
    return environment


def claude_keychain_environment(
    source_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    """Build a credential-free environment for macOS Keychain reads."""
    environment = _selected_environment(
        source_environment,
        SAFE_PROCESS_ENVIRONMENT_KEYS,
    )
    environment.setdefault("PATH", os.defpath)
    _validate_environment(environment)
    return environment


def _safe_environment(
    source_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    environment = _selected_environment(
        source_environment,
        _CLAUDE_SAFE_ENVIRONMENT_KEYS,
    )
    environment.setdefault("PATH", os.defpath)
    return environment


def _selected_environment(
    source_environment: Mapping[str, str] | None,
    allowed_keys: frozenset[str],
) -> dict[str, str]:
    source = os.environ if source_environment is None else source_environment
    return {key: value for key, value in source.items() if key in allowed_keys}


def _validate_environment(environment: Mapping[str, str]) -> None:
    for value in environment.values():
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise ClaudeProcessError(
                ClaudeProcessFailure.PROCESS_UNSAFE
            ) from None
        if "\0" in value or len(encoded) > _MAXIMUM_ENVIRONMENT_VALUE_BYTES:
            raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)
