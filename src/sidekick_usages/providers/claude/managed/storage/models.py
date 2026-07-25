"""Immutable protected Claude storage models."""

from dataclasses import dataclass, field
from datetime import datetime

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
)
from sidekick_usages.providers.claude.models import ClaudeManagedProfile


@dataclass(frozen=True, slots=True)
class ClaudeKeychainTarget:
    """One exact non-secret macOS Keychain lookup target."""

    account: str = field(repr=False)
    service: str


@dataclass(frozen=True, slots=True)
class ClaudeAuthoritySnapshot:
    """Secret-free metadata for one protected credential generation."""

    profile: ClaudeManagedProfile
    executable_version: str
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    plan: str
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    health: CredentialHealth
    action: CredentialAction
