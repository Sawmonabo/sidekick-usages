"""Higher-priority credential and session guards for Claude activation."""

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
    ClaudeRemoteControlProbe,
    ClaudeRemoteControlState,
)
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
    CLAUDE_SECURE_STORAGE_CONFIG_DIR_ENVIRONMENT_KEY,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import ClaudeNativeProfile
from sidekick_usages.serialization.json import JsonValue, decode_json_object

_MAXIMUM_SETTINGS_BYTES = 1024 * 1024
_SETTINGS_FILE = "settings.json"
_API_KEY_HELPER_SETTING = "apiKeyHelper"
_ENVIRONMENT_SETTING = "env"
_LINUX_MANAGED_SETTINGS = Path("/etc/claude-code/managed-settings.json")
_MACOS_MANAGED_SETTINGS = Path(
    "/Library/Application Support/ClaudeCode/managed-settings.json"
)
_CLOUD_PROVIDER_KEYS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
)
_GATEWAY_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
)
_PROFILE_OVERRIDE_KEYS = (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
    CLAUDE_SECURE_STORAGE_CONFIG_DIR_ENVIRONMENT_KEY,
)
_DIRECT_ENVIRONMENT_FAILURES = (
    (
        "ANTHROPIC_AUTH_TOKEN",
        ClaudeActivationGuardFailure.ANTHROPIC_AUTH_OVERRIDE,
    ),
    (
        "ANTHROPIC_API_KEY",
        ClaudeActivationGuardFailure.ANTHROPIC_API_KEY,
    ),
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        ClaudeActivationGuardFailure.CLAUDE_OAUTH_OVERRIDE,
    ),
)
_MANAGED_SETTINGS_PATHS = {
    ClaudeManagedPlatform.LINUX_FILE: (_LINUX_MANAGED_SETTINGS,),
    ClaudeManagedPlatform.WSL_FILE: (_LINUX_MANAGED_SETTINGS,),
    ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN: (_MACOS_MANAGED_SETTINGS,),
    ClaudeManagedPlatform.MACOS_X64_KEYCHAIN: (_MACOS_MANAGED_SETTINGS,),
}


def claude_environment_conflict(
    environment: Mapping[str, str],
) -> ClaudeActivationGuardFailure | None:
    """Return the highest-priority inherited Claude conflict, if any."""
    if any(key in environment for key in _PROFILE_OVERRIDE_KEYS):
        return ClaudeActivationGuardFailure.ALTERNATE_PROFILE
    if any(environment.get(key) for key in _CLOUD_PROVIDER_KEYS):
        return ClaudeActivationGuardFailure.CLOUD_PROVIDER
    for key, failure in _DIRECT_ENVIRONMENT_FAILURES:
        if environment.get(key):
            return failure
    if any(environment.get(key) for key in _GATEWAY_KEYS):
        return ClaudeActivationGuardFailure.GATEWAY
    return None


def claude_environment_conflict_keys(
    failure: ClaudeActivationGuardFailure,
) -> tuple[str, ...]:
    """Return the environment names associated with one detected conflict."""
    if failure is ClaudeActivationGuardFailure.ALTERNATE_PROFILE:
        return _PROFILE_OVERRIDE_KEYS
    if failure is ClaudeActivationGuardFailure.CLOUD_PROVIDER:
        return _CLOUD_PROVIDER_KEYS
    for key, candidate in _DIRECT_ENVIRONMENT_FAILURES:
        if failure is candidate:
            return (key,)
    if failure is ClaudeActivationGuardFailure.GATEWAY:
        return _GATEWAY_KEYS
    raise ValueError("The activation failure is not environment-based.")


def claude_native_switch_conflict(
    capabilities: ClaudeCapabilities,
    environment: Mapping[str, str],
    remote_control_probe: ClaudeRemoteControlProbe,
) -> ClaudeActivationGuardFailure | None:
    """Return a settings or environment conflict before native mutation."""
    profile = capabilities.profile
    if not isinstance(profile, ClaudeNativeProfile):
        return ClaudeActivationGuardFailure.CONFIGURATION_UNREADABLE
    settings_conflict = _settings_conflict(
        profile,
        capabilities.platform,
    )
    if settings_conflict is not None:
        return settings_conflict
    environment_conflict = claude_environment_conflict(environment)
    if environment_conflict is not None:
        return environment_conflict
    if remote_control_probe() is ClaudeRemoteControlState.ACTIVE_INCOMPATIBLE:
        return ClaudeActivationGuardFailure.REMOTE_CONTROL_INCOMPATIBLE
    return None


def _settings_conflict(
    profile: ClaudeNativeProfile,
    platform: ClaudeManagedPlatform,
) -> ClaudeActivationGuardFailure | None:
    paths = (
        profile.config_directory / _SETTINGS_FILE,
        *_MANAGED_SETTINGS_PATHS.get(platform, ()),
    )
    for path in paths:
        conflict = _settings_file_conflict(path)
        if conflict is not None:
            return conflict
    return None


def _settings_file_conflict(
    path: Path,
) -> ClaudeActivationGuardFailure | None:
    try:
        payload = _read_settings(path)
    except OSError, ValueError:
        return ClaudeActivationGuardFailure.CONFIGURATION_UNREADABLE
    if payload is None:
        return None
    try:
        settings = decode_json_object(payload)
    except InvalidPayloadError:
        return ClaudeActivationGuardFailure.CONFIGURATION_UNREADABLE
    try:
        return _decoded_settings_conflict(settings)
    except ValueError:
        return ClaudeActivationGuardFailure.CONFIGURATION_UNREADABLE


def _decoded_settings_conflict(
    settings: Mapping[str, JsonValue],
) -> ClaudeActivationGuardFailure | None:
    helper = settings.get(_API_KEY_HELPER_SETTING)
    if helper is not None:
        if not isinstance(helper, str):
            raise ValueError("Claude API-key helper is malformed.")
        if helper:
            return ClaudeActivationGuardFailure.API_KEY_HELPER
    configured_environment = settings.get(_ENVIRONMENT_SETTING)
    if configured_environment is None:
        return None
    if not isinstance(configured_environment, dict):
        raise ValueError("Claude settings environment is malformed.")
    environment: dict[str, str] = {}
    for key, value in configured_environment.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("Claude settings environment is malformed.")
        environment[key] = value
    return claude_environment_conflict(environment)


def _read_settings(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    try:
        file_status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_status.st_mode)
            or file_status.st_size > _MAXIMUM_SETTINGS_BYTES
        ):
            raise ValueError("Claude settings file is unsafe.")
        with os.fdopen(descriptor, "rb", closefd=False) as settings_file:
            payload = settings_file.read(_MAXIMUM_SETTINGS_BYTES + 1)
        if len(payload) > _MAXIMUM_SETTINGS_BYTES:
            raise ValueError("Claude settings file exceeded its safe bound.")
        return payload
    finally:
        os.close(descriptor)
