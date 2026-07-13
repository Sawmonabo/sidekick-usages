"""Supported Claude provider facade."""

from sidekick_usages.providers.claude.activity import (
    ClaudeActivity,
    discover_claude_config_dir,
)
from sidekick_usages.providers.claude.credential_schemas import PROFILE_SCOPE
from sidekick_usages.providers.claude.provider import (
    ClaudeProvider,
    ClaudeSetupToken,
    SetupTokenCapture,
    SetupTokenMissing,
    SetupTokenRejected,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)

__all__ = [
    "PROFILE_SCOPE",
    "ClaudeActivity",
    "ClaudeProvider",
    "ClaudeSetupToken",
    "SetupTokenCapture",
    "SetupTokenMissing",
    "SetupTokenRejected",
    "SetupTokenSuccess",
    "SetupTokenTimedOut",
    "SetupTokenUnreadable",
    "discover_claude_config_dir",
]
