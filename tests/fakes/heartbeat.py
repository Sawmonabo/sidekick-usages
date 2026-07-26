"""Heartbeat test support."""

import io
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.heartbeat.models import (
    HeartbeatProbeResult,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    Provider,
    ProviderAuthenticatedAccount,
)
from sidekick_usages.providers.codex.heartbeat import CodexHeartbeat
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.serialization.json import JsonObject
from tests.support.application import make_app_context
from tests.support.cli import CliHarness
from tests.support.persistence import make_account_store_with_private
from tests.support.time import FixedClock

CODEX_USAGE_FETCHES_FOR_WARM = 2
STANDARD_RESET = datetime(2026, 6, 12, 18, tzinfo=UTC)
SPARK_RESET = datetime(2026, 6, 12, 19, tzinfo=UTC)
ROUNDTRIP_AUDIT_TIME = datetime(2026, 6, 12, 13, tzinfo=UTC)


class FakeHeartbeatProvider(HeartbeatProvider):
    """Record scripted heartbeat calls at the provider boundary."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"

    def __init__(
        self,
        *,
        provider_id: ProviderId = ProviderId.CLAUDE,
        heartbeat_supported: bool = True,
        heartbeat_results: Iterable[HeartbeatProbeResult] = (),
    ) -> None:
        self.id = provider_id
        self.display_name = (
            "Codex CLI" if provider_id is ProviderId.CODEX else "Claude Code"
        )
        self._heartbeat_supported = heartbeat_supported
        self.heartbeat_results = list(heartbeat_results)
        self.supports_calls: list[str] = []
        self.heartbeat_calls: list[tuple[str, str]] = []

    def supports(self, account: Account) -> bool:
        """Record support checks and return the configured result."""
        self.supports_calls.append(account.label)
        return self._heartbeat_supported

    def inspect_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        """Return an inactive window for generic service tests."""
        del account, http, target
        return UsageWindowState(active=False)

    def warm_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        """Record one warm call and return the next scripted result."""
        del http, target
        runtime = account.lease.account
        self.heartbeat_calls.append((runtime.label, runtime.access_token))
        if self.heartbeat_results:
            return self.heartbeat_results.pop(0)
        return HeartbeatProbeResult(
            status=HeartbeatStatus.WARMED,
            message="warmed",
            warmed=True,
            reset_at=STANDARD_RESET,
        )


class FakeCodexHttp(HttpClient):
    """Record Codex heartbeat protocol traffic."""

    def __init__(self, usage_responses: Iterable[JsonObject]) -> None:
        self.usage_responses: list[JsonObject] = list(usage_responses)
        self.get_calls: list[tuple[str, dict[str, str]]] = []
        self.post_calls: list[tuple[str, JsonObject, dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        """Return the next scripted usage response."""
        self.get_calls.append((url, dict(headers)))
        if not self.usage_responses:
            raise AssertionError("unexpected Codex usage fetch")
        return self.usage_responses.pop(0)

    def post_capture_headers(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str],
        *,
        operation: HttpOperation,
    ) -> dict[str, str]:
        """Record one heartbeat request without network access."""
        assert operation is HttpOperation.CODEX_HEARTBEAT
        self.post_calls.append((url, json_body, dict(headers)))
        return {}


def heartbeat_account(
    label: str = "team",
    *,
    provider_id: ProviderId = ProviderId.CLAUDE,
    provider_account_id: str | None = None,
    heartbeat_enabled: bool = False,
    heartbeat_window_resets: dict[str, datetime] | None = None,
    heartbeat_targets: tuple[str, ...] | None = None,
    access_expiry_at: datetime | None = None,
) -> Account:
    """Build one account with synthetic heartbeat credentials."""
    access_token = "old-token" if label == "team" else f"old-token-{label}"
    credentials: Credentials
    if provider_id is ProviderId.CODEX:
        credentials = CodexCredentials(
            access_token=access_token,
            refresh_token=f"refresh-token-{label}",
            expiry=UnknownExpiry(),
            account_id=provider_account_id,
        )
    elif access_expiry_at is None:
        credentials = ClaudeSetupTokenCredentials(access_token=access_token)
    else:
        credentials = ClaudeLoginCredentials(
            access_token=access_token,
            refresh_token="refresh-token",
            access_expiry=KnownExpiry(access_expiry_at),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="team",
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_window_resets=heartbeat_window_resets,
        heartbeat_targets=heartbeat_targets,
    )


def codex_heartbeat() -> CodexHeartbeat:
    """Build the Codex heartbeat adapter."""
    return CodexHeartbeat(CodexProvider())


def install_heartbeat_context(
    tmp_path: Path,
    accounts: Iterable[Account],
    heartbeat_providers: dict[ProviderId, HeartbeatProvider],
    providers: dict[ProviderId, Provider] | None = None,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Compose a heartbeat CLI around isolated persisted state."""
    store, private_credentials = make_account_store_with_private(
        tmp_path,
        accounts,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    active_clock = clock or FixedClock()
    http = HttpClient()
    provider_registry = {} if providers is None else providers
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            provider_registry,
            private_credentials,
            active_clock,
            heartbeat_providers=heartbeat_providers,
        ),
    )
    return harness, store, stdout, stderr
