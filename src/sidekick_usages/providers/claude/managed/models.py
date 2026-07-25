"""Immutable managed-Claude capability models."""

from dataclasses import dataclass

from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeManagedProfile,
)


@dataclass(frozen=True, slots=True)
class ClaudeCapabilities:
    """Complete proof of one supported managed Claude boundary."""

    executable: ClaudeExecutable
    profile: ClaudeManagedProfile
    platform: ClaudeManagedPlatform
