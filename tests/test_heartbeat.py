"""Heartbeat/window-warming behavior tests."""

import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    UsageReport,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.heartbeat import (
    HeartbeatOutcome,
    HeartbeatProbeResult,
    HeartbeatProvider,
    HeartbeatService,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.heartbeat.render import (
    HeartbeatOutputChannel,
    build_heartbeat_status_rows,
    heartbeat_status_json,
    render_heartbeat_outcomes,
    render_heartbeat_status,
)
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderAuthenticatedAccount,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.codex import CodexProvider
from sidekick_usages.providers.codex.heartbeat import (
    CodexHeartbeat,
)
from sidekick_usages.serialization import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    RuntimeCredentialResolver,
    make_account_store,
    make_account_store_with_private,
    make_app_context,
)

CODEX_USAGE_FETCHES_FOR_WARM = 2
_STANDARD_RESET = datetime(2026, 6, 12, 18, tzinfo=UTC)
_SPARK_RESET = datetime(2026, 6, 12, 19, tzinfo=UTC)
_ROUNDTRIP_AUDIT_TIME = datetime(2026, 6, 12, 13, tzinfo=UTC)


def _codex_heartbeat() -> CodexHeartbeat:
    return CodexHeartbeat(CodexProvider(FixedClock()))


def test_heartbeat_reset_models_require_aware_utc_datetimes() -> None:
    """Heartbeat boundary results normalize aware time and reject naive."""
    offset = REFERENCE_TIME.astimezone(timezone(timedelta(hours=-4)))
    results = (
        UsageWindowState(active=True, reset_at=offset),
        HeartbeatProbeResult(
            status=HeartbeatStatus.ACTIVE,
            message="active",
            warmed=False,
            reset_at=offset,
        ),
    )

    assert tuple(result.reset_at for result in results) == (
        REFERENCE_TIME,
        REFERENCE_TIME,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        UsageWindowState(
            active=True,
            reset_at=REFERENCE_TIME.replace(tzinfo=None),
        )


class _FakeHeartbeatProvider(HeartbeatProvider):
    """Provider test double with scripted heartbeat and refresh behavior."""

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
        self.supports_calls.append(account.label)
        return self._heartbeat_supported

    def inspect_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        del account, http, target
        return UsageWindowState(active=False)

    def warm_window(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        del http, target
        runtime = account.lease.account
        self.heartbeat_calls.append((runtime.label, runtime.access_token))
        if self.heartbeat_results:
            return self.heartbeat_results.pop(0)
        return HeartbeatProbeResult(
            status=HeartbeatStatus.WARMED,
            message="warmed",
            warmed=True,
            reset_at=_STANDARD_RESET,
        )


class _FakeRefreshProvider(Provider):
    """Provider test double for maintain refresh ordering."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"

    def __init__(self) -> None:
        self.refresh_calls = 0

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
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


def _store(tmp_path: Path, accounts: Iterable[Account]) -> AccountStore:
    return make_account_store(tmp_path, accounts)


def _acct(
    label: str = "team",
    *,
    provider_id: ProviderId = ProviderId.CLAUDE,
    provider_account_id: str | None = None,
    heartbeat_enabled: bool = False,
    heartbeat_window_resets: dict[str, datetime] | None = None,
    heartbeat_targets: tuple[str, ...] | None = None,
) -> Account:
    access_token = "old-token" if label == "team" else f"old-token-{label}"
    credentials = (
        ClaudeSetupTokenCredentials(access_token=access_token)
        if provider_id is ProviderId.CLAUDE
        else CodexCredentials(
            access_token=access_token,
            refresh_token=f"refresh-token-{label}",
            expiry=UnknownExpiry(),
            account_id=provider_account_id,
        )
    )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="team",
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_window_resets=heartbeat_window_resets,
        heartbeat_targets=heartbeat_targets,
    )


def _claude_login_acct(
    *,
    access_expiry_at: datetime,
    heartbeat_enabled: bool = False,
    heartbeat_window_resets: dict[str, datetime] | None = None,
) -> Account:
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="old-token",
            refresh_token="refresh-token",
            access_expiry=KnownExpiry(access_expiry_at),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_window_resets=heartbeat_window_resets,
    )


def _install_ctx(
    tmp_path: Path,
    accounts: Iterable[Account],
    heartbeat_providers: dict[ProviderId, HeartbeatProvider],
    providers: dict[ProviderId, Provider] | None = None,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
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


class _FakeCodexHttp(HttpClient):
    """Tiny HTTP double for Codex heartbeat protocol tests."""

    def __init__(self, usage_responses: Iterable[JsonObject]) -> None:
        self.usage_responses: list[JsonObject] = list(usage_responses)
        self.get_calls: list[tuple[str, dict[str, str]]] = []
        self.post_calls: list[tuple[str, JsonObject, dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
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
        assert operation is HttpOperation.CODEX_HEARTBEAT
        self.post_calls.append((url, json_body, dict(headers)))
        return {}


def test_account_roundtrips_heartbeat_metadata(tmp_path: Path) -> None:
    """Heartbeat settings and diagnostics persist in the account store."""
    store = _store(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                heartbeat_window_resets={
                    "standard": _STANDARD_RESET,
                    "spark": _SPARK_RESET,
                },
                heartbeat_targets=("standard", "spark"),
            )
        ],
    )
    account = store.get("team")
    assert account is not None
    account.last_heartbeat_at = _ROUNDTRIP_AUDIT_TIME
    account.last_heartbeat_status = HeartbeatStatus.WARMED
    account.last_heartbeat_error = None
    store.persist(account)

    restored = make_account_store(tmp_path).get("team")

    assert restored is not None
    assert restored.heartbeat_enabled is True
    assert restored.heartbeat_window_resets == {
        "standard": _STANDARD_RESET,
        "spark": _SPARK_RESET,
    }
    assert restored.heartbeat_targets == ("standard", "spark")
    assert restored.last_heartbeat_at == _ROUNDTRIP_AUDIT_TIME
    assert restored.last_heartbeat_status is HeartbeatStatus.WARMED
    assert restored.last_heartbeat_error is None


def test_heartbeat_all_skips_disabled_accounts(tmp_path: Path) -> None:
    """Scheduler mode only probes accounts explicitly enabled."""
    provider = _FakeHeartbeatProvider()
    store = _store(tmp_path, [_acct(heartbeat_enabled=False)])
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    outcomes = service.heartbeat_all()

    assert provider.heartbeat_calls == []
    assert outcomes[0].status is HeartbeatStatus.DISABLED


def test_heartbeat_service_owns_support_display_and_explicit_empty_mapping(
    tmp_path: Path,
) -> None:
    account = _acct()
    store = _store(tmp_path, [account])
    injected: dict[ProviderId, HeartbeatProvider] = {}
    empty = HeartbeatService(
        store,
        HttpClient(),
        injected,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )
    injected[ProviderId.CLAUDE] = _FakeHeartbeatProvider()

    assert empty.support_label(account) == "unsupported"
    assert empty.support_labels((account,)) == {"team": "unsupported"}

    configured = HeartbeatService(
        store,
        HttpClient(),
        injected,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )
    assert configured.support_label(account) == "off"
    account.last_heartbeat_status = HeartbeatStatus.FAILED
    assert configured.support_labels((account,)) == {"team": "needs-login"}


def test_heartbeat_label_runs_even_when_disabled(tmp_path: Path) -> None:
    """Explicit label mode is a one-shot warm request."""
    provider = _FakeHeartbeatProvider()
    store = _store(tmp_path, [_acct(heartbeat_enabled=False)])
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(
        store.get("team"), require_enabled=False
    )

    assert outcome.status is HeartbeatStatus.WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_status is HeartbeatStatus.WARMED
    assert saved.heartbeat_window_resets == {"standard": _STANDARD_RESET}


def test_heartbeat_decision_samples_clock_once(tmp_path: Path) -> None:
    """Auth and cached-reset checks share one heartbeat reference time."""
    provider = _FakeHeartbeatProvider()
    clock = FixedClock()
    store = _store(
        tmp_path,
        [
            _claude_login_acct(
                heartbeat_enabled=True,
                access_expiry_at=REFERENCE_TIME + timedelta(hours=1),
                heartbeat_window_resets={"standard": _STANDARD_RESET},
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status is HeartbeatStatus.ACTIVE
    assert provider.heartbeat_calls == []
    assert clock.calls == 1


def test_heartbeat_cache_is_target_specific(tmp_path: Path) -> None:
    """A cached Spark reset must not suppress a standard Codex warm."""
    provider = _FakeHeartbeatProvider(provider_id=ProviderId.CODEX)
    clock = FixedClock()
    store = _store(
        tmp_path,
        [
            _acct(
                provider_id=ProviderId.CODEX,
                heartbeat_enabled=True,
                heartbeat_window_resets={
                    "spark": _STANDARD_RESET,
                },
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(
        store.get("team"), target_id="standard"
    )

    assert outcome.status is HeartbeatStatus.WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]


def test_heartbeat_persists_failure_per_account(tmp_path: Path) -> None:
    """One provider failure is recorded instead of escaping."""
    provider = _FakeHeartbeatProvider(
        heartbeat_results=[
            HeartbeatProbeResult(
                status=HeartbeatStatus.FAILED,
                message="rate limited",
                action_required=True,
                warmed=False,
            )
        ]
    )
    store = _store(tmp_path, [_acct(heartbeat_enabled=True)])
    clock = FixedClock()
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status is HeartbeatStatus.FAILED
    assert outcome.action_required is True
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_at == REFERENCE_TIME
    assert saved.last_heartbeat_status is HeartbeatStatus.FAILED
    assert saved.last_heartbeat_error == "provider_failure"


def test_heartbeat_enable_disable_and_status_cli(tmp_path: Path) -> None:
    """Heartbeat config is managed through the CLI."""
    provider = _FakeHeartbeatProvider()
    harness, store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct()],
        {ProviderId.CLAUDE: provider},
    )

    enabled = harness.invoke(["heartbeat", "enable", "team"])
    status = harness.invoke(["heartbeat", "status"])
    disabled = harness.invoke(["heartbeat", "disable", "team"])

    assert enabled.exit_code == 0
    assert status.exit_code == 0
    assert disabled.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is False
    rendered = stdout.getvalue()
    assert "enabled" in rendered
    assert "heartbeat: on" in rendered
    assert "disabled" in rendered
    assert rendered.count(ROBOT_LINES[2]) == 1
    assert "heartbeat status" in rendered


def test_heartbeat_status_json_remains_machine_readable(
    tmp_path: Path,
) -> None:
    label = "long-" + "account" * 20
    provider = _FakeHeartbeatProvider()
    harness, _, stdout, _ = _install_ctx(
        tmp_path,
        [_acct(label)],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "status", "--json"])

    assert result.exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["accounts"][0]["label"] == label
    assert ROBOT_LINES[2] not in stdout.getvalue()


def test_status_builders_share_one_typed_row_for_human_and_json() -> None:
    """Human and machine views cannot derive different account facts."""
    account = _acct(
        heartbeat_enabled=True,
        heartbeat_window_resets={
            "standard": _STANDARD_RESET,
            "spark": _SPARK_RESET,
        },
        heartbeat_targets=("standard", "spark"),
    )
    account.last_heartbeat_at = _ROUNDTRIP_AUDIT_TIME
    account.last_heartbeat_status = HeartbeatStatus.WARMED
    rows = build_heartbeat_status_rows(
        (account,),
        {account.label: "on"},
    )

    payload = heartbeat_status_json(rows)
    assert payload == {
        "accounts": [
            {
                "label": "team",
                "provider": "claude",
                "plan": "team",
                "heartbeat": "on",
                "heartbeat_supported": True,
                "heartbeat_enabled": True,
                "heartbeat_window_resets": {
                    "standard": "2026-06-12T18:00:00Z",
                    "spark": "2026-06-12T19:00:00Z",
                },
                "heartbeat_targets": ["standard", "spark"],
                "last_heartbeat_at": "2026-06-12T13:00:00Z",
                "last_heartbeat_status": "warmed",
                "last_heartbeat_error": None,
            }
        ]
    }
    output = io.StringIO()
    Console(file=output, force_terminal=False).print(
        render_heartbeat_status(rows, width=80)
    )
    rendered = output.getvalue()
    assert "heartbeat: on" in rendered
    assert "cached spark reset: 2026-06-12T19:00:00Z" in rendered
    assert "last heartbeat: warmed" in rendered


def test_quiet_outcome_builder_keeps_only_actionable_error_channel() -> None:
    """Scheduled quiet rendering suppresses success but never a failure."""
    outcomes = (
        HeartbeatOutcome(
            label=AccountLabel("healthy"),
            provider_id=ProviderId.CLAUDE,
            status=HeartbeatStatus.WARMED,
            message="warmed",
        ),
        HeartbeatOutcome(
            label=AccountLabel("failed"),
            provider_id=ProviderId.CLAUDE,
            status=HeartbeatStatus.FAILED,
            message="rate limited",
            action_required=True,
            exit_code=ExitCode.MANUAL_ACTION,
        ),
    )

    rendered = render_heartbeat_outcomes(outcomes, quiet=True)

    assert tuple(item.channel for item in rendered) == (
        HeartbeatOutputChannel.STDERR,
    )
    error_output = io.StringIO()
    Console(file=error_output, force_terminal=False).print(
        rendered[0].renderable
    )
    assert error_output.getvalue() == "failed: rate limited\n"


def test_empty_heartbeat_registry_remains_unsupported(
    tmp_path: Path,
) -> None:
    """An explicitly empty registry must not activate default providers."""
    harness, _, stdout, _ = _install_ctx(tmp_path, [_acct()], {})

    result = harness.invoke(["heartbeat", "status"])

    assert result.exit_code == 0
    assert "heartbeat: unsupported" in stdout.getvalue()
    assert "supported: no" in stdout.getvalue()


def test_heartbeat_label_cli_runs_one_shot_when_disabled(
    tmp_path: Path,
) -> None:
    """The documented heartbeat <label> form runs a one-shot probe."""
    provider = _FakeHeartbeatProvider()
    harness, store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct("team", heartbeat_enabled=False)],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "team"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("team", "old-token")]
    assert "team: warmed" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is False


def test_heartbeat_all_quiet_runs_enabled_only(tmp_path: Path) -> None:
    """Quiet all-account mode is scheduler friendly."""
    provider = _FakeHeartbeatProvider()
    harness, _, stdout, _ = _install_ctx(
        tmp_path,
        [
            _acct("enabled", heartbeat_enabled=True),
            _acct("disabled", heartbeat_enabled=False),
        ],
        {ProviderId.CLAUDE: provider},
    )

    result = harness.invoke(["heartbeat", "--all", "--quiet"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("enabled", "old-token-enabled")]
    assert stdout.getvalue() == ""


def test_maintain_refreshes_before_heartbeat(tmp_path: Path) -> None:
    """The scheduler command refreshes tokens before window warming."""
    clock = FixedClock()
    refresh_provider = _FakeRefreshProvider()
    heartbeat_provider = _FakeHeartbeatProvider()
    harness, _, _, _ = _install_ctx(
        tmp_path,
        [
            _claude_login_acct(
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


def test_maintain_preserves_setup_token_failure_cause(tmp_path: Path) -> None:
    """A rejected setup token never receives login recovery wording."""
    account = _acct(heartbeat_enabled=True)
    account.last_refresh_status = RefreshStatus.FAILED
    account.last_refresh_error = "Claude rejected the saved setup token."
    provider = _FakeHeartbeatProvider()
    harness, _, stdout, stderr = _install_ctx(
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
