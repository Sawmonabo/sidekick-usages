"""Heartbeat maintenance-ordering tests."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ExitCode, ProviderId, RefreshStatus
from sidekick_usages.http.client import HttpClient
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from tests.fakes.heartbeat import (
    FakeHeartbeatProvider,
    heartbeat_account,
    install_heartbeat_context,
)
from tests.support.time import REFERENCE_TIME, FixedClock


class FakeRefreshProvider(Provider):
    """Record refresh calls for maintenance ordering."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"

    def __init__(self) -> None:
        self.refresh_calls = 0

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """Return missing for unused native-credential detection."""
        del credential_home
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        """Reject unused manual token conversion."""
        del token
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.UNSUPPORTED,
            message="Manual test credentials are unsupported.",
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
        credentials = account.credentials
        if not isinstance(credentials, ClaudeLoginCredentials):
            raise AssertionError("Refresh fixture requires a Claude login.")
        return RefreshSuccess(
            credentials=replace(
                credentials,
                access_token="refreshed-token",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            ),
        )


def test_maintain_refreshes_before_heartbeat(tmp_path: Path) -> None:
    """The scheduler command refreshes tokens before window warming."""
    clock = FixedClock()
    refresh_provider = FakeRefreshProvider()
    heartbeat_provider = FakeHeartbeatProvider()
    harness, _, _, _ = install_heartbeat_context(
        tmp_path,
        [
            heartbeat_account(
                heartbeat_enabled=True,
                access_expiry_at=REFERENCE_TIME - timedelta(minutes=1),
            )
        ],
        {ProviderId.CLAUDE: heartbeat_provider},
        providers={ProviderId.CLAUDE: refresh_provider},
        clock=clock,
    )

    result = harness.invoke(["maintain", "--quiet"])

    assert result.exit_code == 0
    assert refresh_provider.refresh_calls == 1
    assert heartbeat_provider.heartbeat_calls == [("team", "refreshed-token")]


def test_maintain_preserves_setup_token_failure_cause(
    tmp_path: Path,
) -> None:
    """A rejected setup token never receives login recovery wording."""
    account = heartbeat_account(heartbeat_enabled=True)
    account.last_refresh_status = RefreshStatus.FAILED
    account.last_refresh_error = "provider_failure"
    provider = FakeHeartbeatProvider()
    harness, _, stdout, stderr = install_heartbeat_context(
        tmp_path,
        [account],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["maintain", "--quiet"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert provider.heartbeat_calls == []
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == (
        "team: Claude rejected the saved setup token.\n"
    )
