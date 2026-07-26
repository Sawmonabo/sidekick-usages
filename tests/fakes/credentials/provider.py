"""Credential-provider and CLI-context test boundaries."""

import io
import re
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.claude.types import ClaudeSetupToken
from tests.support.application import make_app_context
from tests.support.cli import CliHarness
from tests.support.persistence import make_account_store_with_private
from tests.support.time import REFERENCE_TIME, FixedClock


class FakeCredentialProvider(Provider):
    """Script only the credential behavior exercised by CLI tests."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        *,
        detected: CredentialDetection | None = None,
        refresh_ok: bool = True,
    ) -> None:
        self.detected = detected
        self.refresh_ok = refresh_ok
        self.refresh_calls = 0
        self.credential_homes: list[Path | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        self.credential_homes.append(credential_home)
        if self.detected is not None:
            return self.detected
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        return DetectedCredentials(
            credentials=ClaudeSetupTokenCredentials(access_token=token)
        )

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        return UsageReport()

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        self.refresh_calls += 1
        if not self.refresh_ok:
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test refresh rejected.",
            )
        credentials = account.credentials
        if not isinstance(credentials, ClaudeLoginCredentials):
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test login refresh requires a subscription.",
            )
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                access_expiry=KnownExpiry(
                    REFERENCE_TIME + timedelta(seconds=60)
                ),
                refresh_expiry=credentials.refresh_expiry,
                scopes=credentials.scopes,
                identity=credentials.identity,
            )
        )


def claude_login_account(
    *,
    access_expiry: KnownExpiry,
) -> Account:
    """Build one legacy Claude login used by refresh-operation tests."""
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-old",
            refresh_token="refresh-old",
            access_expiry=access_expiry,
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
    )


def detected_setup_token(access_token: str) -> DetectedCredentials:
    """Build one provider-compatible setup-token detection."""
    return DetectedCredentials(
        credentials=ClaudeSetupTokenCredentials(access_token=access_token)
    )


def install_cli_context(
    root: Path,
    providers: dict[ProviderId, Provider],
    accounts: Iterable[Account] = (),
    *,
    clock: Clock | None = None,
    claude_setup_token: ClaudeSetupToken | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Build one isolated CLI application around explicit test boundaries."""
    store, private = make_account_store_with_private(root, accounts)
    application_clock = FixedClock() if clock is None else clock
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            HttpClient(),
            providers,
            private,
            application_clock,
            heartbeat_providers={},
            claude_setup_token=claude_setup_token,
        ),
    )
    return harness, store, stdout, stderr
