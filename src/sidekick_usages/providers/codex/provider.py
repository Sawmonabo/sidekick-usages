"""Codex managed-login policy and usage facade."""

import re
from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.http.client import HttpClient
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
)
from sidekick_usages.providers.codex.usage import fetch_usage

TOKEN_RE = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\."
    r"[A-Za-z0-9_\-]+"
)
_MANAGED_LOGIN_REQUIRED = (
    "Codex accounts require official login in their managed home."
)


class CodexProvider(Provider):
    """Codex CLI integration."""

    id = ProviderId.CODEX
    display_name = "Codex CLI"
    token_pattern = TOKEN_RE

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """Reject native or supplied-home credential adoption."""
        return _managed_login_required()

    def credentials_from_token(self, token: str) -> CredentialDetection:
        """Reject manual Codex token adoption."""
        return _managed_login_required()

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage through the Codex-owned usage adapter."""
        return fetch_usage(account, http)

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Require official managed login for every Codex refresh."""
        return _managed_login_required()


def _failure(kind: ProviderFailureKind, message: str) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )


def _managed_login_required() -> ProviderFailure:
    """Return the single retired-import and direct-refresh policy."""
    return _failure(
        ProviderFailureKind.REJECTED,
        _MANAGED_LOGIN_REQUIRED,
    )
