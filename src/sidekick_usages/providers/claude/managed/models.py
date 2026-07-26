"""Immutable managed-Claude capability models."""

from dataclasses import dataclass

from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
)
from sidekick_usages.providers.claude.types import ClaudeProfile


@dataclass(frozen=True, slots=True)
class ClaudeCapabilities:
    """Complete proof of one supported Claude auth boundary."""

    executable: ClaudeExecutable
    profile: ClaudeProfile
    platform: ClaudeManagedPlatform
