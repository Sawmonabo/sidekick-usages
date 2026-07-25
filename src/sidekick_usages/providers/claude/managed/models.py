"""Immutable managed-Claude capability models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import ClaudeExecutable


@dataclass(frozen=True, slots=True)
class ClaudeProfile:
    """One stable Sidekick-owned Claude config directory."""

    account_id: SidekickAccountId
    config_directory: Path

    def __post_init__(self) -> None:
        """Require one absolute account-ID-derived directory."""
        if (
            not self.config_directory.is_absolute()
            or self.config_directory.name != str(self.account_id)
        ):
            raise ValueError("Claude managed profile path is invalid.")


@dataclass(frozen=True, slots=True)
class ClaudeCapabilities:
    """Complete proof of one supported managed Claude boundary."""

    executable: ClaudeExecutable
    profile: ClaudeProfile
    platform: ClaudeManagedPlatform
