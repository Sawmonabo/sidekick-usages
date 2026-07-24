"""Codex provider facade and typed refresh workflow."""

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import ExpiredExpiry, classify_expiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.codex.auth import (
    codex_timestamp,
    detect_auth_credentials,
    require_codex_credentials,
)
from sidekick_usages.providers.codex.schemas import (
    account_id_from_token,
    credentials_from_access_token,
    parse_auth_credentials,
    plan_from_token,
    refresh_expiry,
    validate_refresh_payload,
)
from sidekick_usages.providers.codex.usage import fetch_usage
from sidekick_usages.serialization import JsonObject

OAUTH_REFRESH_ENDPOINT = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

TOKEN_RE = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\."
    r"[A-Za-z0-9_\-]+"
)


@dataclass(frozen=True, slots=True)
class _PreparedRefresh:
    credentials: CodexCredentials = field(repr=False)
    plan: str | None


class CodexProvider(Provider):
    """Codex CLI integration."""

    id = ProviderId.CODEX
    display_name = "Codex CLI"
    token_pattern = TOKEN_RE

    def __init__(self, clock: Clock) -> None:
        """Use an injected wall clock for credential expiry."""
        self.clock = clock

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """Read one Codex login without mutating its active auth home."""
        result = detect_auth_credentials(credential_home)
        if isinstance(result, ProviderFailure):
            return result
        return self._classify_detection(result)

    def credentials_from_token(self, token: str) -> CredentialDetection:
        """Validate a pasted Codex token without reading active login state."""
        try:
            result = credentials_from_access_token(token)
        except ProviderBoundaryError as error:
            return error.failure
        return self._classify_detection(result)

    def parse_auth_blob(self, blob: JsonObject) -> CredentialDetection:
        """Validate an already-decoded auth blob for CLI import flows."""
        try:
            result = parse_auth_credentials(blob)
        except ProviderBoundaryError as error:
            return error.failure
        return self._classify_detection(result)

    def _classify_detection(
        self,
        result: DetectedCredentials,
    ) -> CredentialDetection:
        if isinstance(
            classify_expiry(result.expiry, now=self.clock.now()),
            ExpiredExpiry,
        ):
            return _failure(
                ProviderFailureKind.EXPIRED,
                "The Codex access token is expired; log in again.",
            )
        return result

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
        """Return a validated credential replacement without mutation."""
        credentials = require_codex_credentials(account)
        refresh_token = credentials.refresh_token
        if refresh_token is None:
            return _failure(
                ProviderFailureKind.INCOMPLETE,
                "No Codex refresh token is saved; log in again.",
            )
        prepared = self._exchange_refresh(credentials, refresh_token, http)
        if isinstance(prepared, ProviderFailure):
            return prepared
        return RefreshSuccess(
            credentials=prepared.credentials,
            plan=prepared.plan or account.plan,
        )

    def _exchange_refresh(
        self,
        credentials: CodexCredentials,
        refresh_token: str,
        http: HttpClient,
    ) -> _PreparedRefresh | ProviderFailure:
        """Exchange and validate a Codex refresh response."""
        try:
            existing_account_id = (
                credentials.account_id
                or account_id_from_token(credentials.access_token)
            )
            response = http.post_form(
                OAUTH_REFRESH_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
                operation=HttpOperation.CODEX_REFRESH,
            )
            payload = validate_refresh_payload(response)
            new_account_id = account_id_from_token(payload.access_token)
            if new_account_id is None:
                return _failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Codex refresh returned no account identity; "
                    "log in again.",
                )
            if (
                existing_account_id is not None
                and new_account_id != existing_account_id
            ):
                return _failure(
                    ProviderFailureKind.IDENTITY_MISMATCH,
                    "Codex refresh returned credentials for another account; "
                    "log in to the intended account.",
                )
            reference_time = self.clock.now()
            expiry = refresh_expiry(payload, reference_time)
            if isinstance(
                classify_expiry(expiry, now=reference_time),
                ExpiredExpiry,
            ):
                return _failure(
                    ProviderFailureKind.EXPIRED,
                    "Codex returned an already-expired access token.",
                )
            updated = replace(
                credentials,
                access_token=payload.access_token,
                account_id=new_account_id,
                refresh_token=(
                    payload.refresh_token or credentials.refresh_token
                ),
                id_token=payload.id_token or credentials.id_token,
                expiry=expiry,
                auth_last_refresh=codex_timestamp(reference_time),
            )
            plan = plan_from_token(payload.access_token)
        except AuthError:
            return _failure(
                ProviderFailureKind.REJECTED,
                "Codex rejected the saved refresh token; log in again.",
            )
        except ProviderBoundaryError as error:
            return error.failure
        return _PreparedRefresh(
            updated,
            plan,
        )


def _failure(kind: ProviderFailureKind, message: str) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )


__all__ = ["CodexProvider"]
