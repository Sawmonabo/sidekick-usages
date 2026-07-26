"""Typed credential-service inputs and successful outcomes."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.providers.base import ProviderFailure

_MAX_PROMPT_METADATA_BYTES = 1024

type CredentialSource = LocalCredentialSource | TokenCredentialSource
type CredentialSaveResult = CredentialSaveSuccess | ProviderFailure
type CredentialRefreshResult = CredentialRefreshSuccess | ProviderFailure
type CredentialUpdateResult = CredentialUpdateSuccess | ProviderFailure
type CredentialLoginResult = CredentialLoginSuccess | ProviderFailure


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenPromptSpec:
    """Non-secret metadata required to collect one provider token."""

    provider_id: ProviderId
    display_name: str
    token_pattern: re.Pattern[str] = field(repr=False)
    setup_hint: str | None = None

    def __post_init__(self) -> None:
        """Require bounded static display, pattern, and hint metadata."""
        values = (
            self.display_name,
            self.token_pattern.pattern,
            self.setup_hint,
        )
        if any(
            value is not None
            and (
                not value
                or len(value.encode("utf-8")) > _MAX_PROMPT_METADATA_BYTES
            )
            for value in values
        ):
            raise ValueError("Token prompt metadata is invalid.")


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
    """One provider-managed login was verified and committed."""

    label: AccountLabel
