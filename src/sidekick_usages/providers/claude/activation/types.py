"""Closed types for native Claude activation safety."""

from enum import StrEnum
from typing import Protocol

CLAUDE_ACTIVATION_FAILURE_CODE_PREFIX = "claude_activation_"


class ClaudeActivationGuardFailure(StrEnum):
    """Safe reasons a native Claude switch cannot proceed."""

    ALTERNATE_PROFILE = "alternate_profile_conflict"
    ANTHROPIC_API_KEY = "anthropic_api_key_conflict"
    ANTHROPIC_AUTH_OVERRIDE = "anthropic_auth_token_conflict"
    API_KEY_HELPER = "api_key_helper_conflict"
    CLOUD_PROVIDER = "cloud_provider_conflict"
    CONFIGURATION_UNREADABLE = "configuration_unreadable"
    GATEWAY = "gateway_conflict"
    CLAUDE_OAUTH_OVERRIDE = "oauth_token_conflict"
    REMOTE_CONTROL_INCOMPATIBLE = "remote_control_incompatible"

    @property
    def action_required(self) -> bool:
        """Return whether the user must resolve or approve the guard."""
        return True

    @property
    def failure_code(self) -> str:
        """Return the sanitized activation outcome code."""
        return CLAUDE_ACTIVATION_FAILURE_CODE_PREFIX + self.value


class ClaudeForegroundState(StrEnum):
    """Proof state for exact same-user foreground Claude processes."""

    CLEAR = "clear"
    PRESENT = "present"
    PROOF_UNAVAILABLE = "proof_unavailable"


class ClaudeRemoteControlState(StrEnum):
    """Structured capability state for incompatible Remote Control."""

    INACTIVE = "inactive"
    ACTIVE_INCOMPATIBLE = "active_incompatible"
    PROOF_UNAVAILABLE = "proof_unavailable"


class ClaudeRemoteControlProbe(Protocol):
    """Return structured Remote Control capability evidence."""

    def __call__(self) -> ClaudeRemoteControlState:
        """Return the exact integrated Remote Control state."""
