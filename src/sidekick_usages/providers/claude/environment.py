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


def claude_private_profile_environment(
    source_environment: Mapping[str, str] | None,
    *,
    process_home: Path,
    config_directory: Path,
) -> dict[str, str]:
    """Build a credential-free private-profile environment."""
    environment = _profile_environment(
        source_environment,
        process_home=process_home,
    )
    _require_absolute_path(config_directory)
    environment[CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY] = str(config_directory)
    _validate_environment(environment)
    return environment


def claude_native_profile_environment(
    source_environment: Mapping[str, str] | None,
    *,
    process_home: Path,
    config_directory: Path,
) -> dict[str, str]:
    """Build a credential-free native-default profile environment."""
    _require_native_default(process_home, config_directory)
    environment = _profile_environment(
        source_environment,
        process_home=process_home,
    )
    if CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY in environment:
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)
    return environment


def claude_private_refresh_environment(
    source_environment: Mapping[str, str] | None,
    *,
    process_home: Path,
    config_directory: Path,
    refresh_token: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    """Build a private-profile environment for official token login."""
    environment = claude_private_profile_environment(
        source_environment,
        process_home=process_home,
        config_directory=config_directory,
    )
    return _refresh_environment(environment, refresh_token, scopes)


def claude_native_refresh_environment(
    source_environment: Mapping[str, str] | None,
    *,
    process_home: Path,
    config_directory: Path,
    refresh_token: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    """Build a native-default environment for official token login."""
    environment = claude_native_profile_environment(
        source_environment,
        process_home=process_home,
        config_directory=config_directory,
    )
    return _refresh_environment(environment, refresh_token, scopes)


def _profile_environment(
    source_environment: Mapping[str, str] | None,
    *,
    process_home: Path,
) -> dict[str, str]:
    """Build the common credential-free profile environment."""
    _require_absolute_path(process_home)
    environment = _safe_environment(source_environment)
    environment.update(
        {
            "HOME": str(process_home),
            "USERPROFILE": str(process_home),
            "APPDATA": str(process_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(process_home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(process_home / ".config"),
        }
    )
    _validate_environment(environment)
    return environment


def _refresh_environment(
    environment: dict[str, str],
    refresh_token: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    environment.update(
        {
            CLAUDE_OAUTH_PROVISIONING_ENVIRONMENT_KEY: refresh_token,
            CLAUDE_REFRESH_SCOPES_ENVIRONMENT_KEY: (
                encode_claude_refresh_scopes(scopes)
            ),
        }
    )
    _validate_environment(environment)
    return environment


def _require_native_default(
    process_home: Path,
    config_directory: Path,
) -> None:
    _require_absolute_path(process_home)
    _require_absolute_path(config_directory)
    if config_directory != process_home / ".claude":
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)


def _require_absolute_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)


def encode_claude_refresh_scopes(scopes: tuple[str, ...]) -> str:
    """Validate and encode unambiguous Claude OAuth scope tokens."""
    if (
        not isinstance(scopes, tuple)
        or not scopes
        or len(scopes) != len(set(scopes))
        or any(
            not isinstance(scope, str)
            or not scope
            or any(character.isspace() for character in scope)
            for scope in scopes
        )
    ):
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)
    encoded = " ".join(scopes)
    _validate_environment({CLAUDE_REFRESH_SCOPES_ENVIRONMENT_KEY: encoded})
    return encoded


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
