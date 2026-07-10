"""Typed credential-service inputs and successful outcomes."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.providers.base import ProviderFailure


@dataclass(frozen=True, slots=True, kw_only=True)
class LocalCredentialSource:
    """One provider-owned local credential source."""

    provider_id: ProviderId
    credential_home: Path | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenCredentialSource:
    """One manually supplied provider token."""

    provider_id: ProviderId
    token: str = field(repr=False)


type CredentialSource = LocalCredentialSource | TokenCredentialSource


@dataclass(frozen=True, slots=True)
class CredentialSaveSuccess:
    """One account was durably created or updated."""

    label: AccountLabel
    created: bool
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class CredentialRefreshSuccess:
    """One account's credentials were durably refreshed."""

    label: AccountLabel


@dataclass(frozen=True, slots=True)
class CredentialUpdateSuccess:
    """One provider-discovered account update was durably persisted."""

    label: AccountLabel


@dataclass(frozen=True, slots=True)
class CredentialLoginSuccess:
    """One Codex login was imported into Sidekick-owned storage."""

    label: AccountLabel
    created: bool


@dataclass(frozen=True, slots=True)
class CredentialExportSuccess:
    """One account was exported to a protected isolated Codex home."""

    label: AccountLabel
    target_home: Path
    auth_path: Path


type CredentialSaveResult = CredentialSaveSuccess | ProviderFailure
type CredentialRefreshResult = CredentialRefreshSuccess | ProviderFailure
type CredentialUpdateResult = CredentialUpdateSuccess | ProviderFailure
type CredentialLoginResult = CredentialLoginSuccess | ProviderFailure
type CredentialExportResult = CredentialExportSuccess | ProviderFailure


__all__ = [
    "CredentialExportResult",
    "CredentialExportSuccess",
    "CredentialLoginResult",
    "CredentialLoginSuccess",
    "CredentialRefreshResult",
    "CredentialRefreshSuccess",
    "CredentialSaveResult",
    "CredentialSaveSuccess",
    "CredentialSource",
    "CredentialUpdateResult",
    "CredentialUpdateSuccess",
    "LocalCredentialSource",
    "TokenCredentialSource",
]
