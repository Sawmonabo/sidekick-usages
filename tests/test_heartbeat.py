"""Heartbeat/window-warming behavior tests."""

import io
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.heartbeat import (
    HeartbeatProbeResult,
    HeartbeatProvider,
    HeartbeatService,
    HeartbeatTarget,
    UsageWindowState,
)
from sidekick_usages.heartbeat.codex import (
    SPARK_HEARTBEAT_MODEL,
    CodexHeartbeat,
)
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.codex import CodexProvider
from sidekick_usages.serialization import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_account_store,
    make_application_paths,
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
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> UsageWindowState:
        del account, http, target
        return UsageWindowState(active=False)

    def warm_window(
        self,
        account: Account,
        http: HttpClient,
        target: HeartbeatTarget,
    ) -> HeartbeatProbeResult:
        del http, target
        self.heartbeat_calls.append((account.label, account.access_token))
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
    ) -> DetectedCredentials | None:
        del credential_home
        return None

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        return UsageReport()

    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        del http
        self.refresh_calls += 1
        account.credentials = replace(
            account.credentials,
            access_token="refreshed-token",
            expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        )
        return True

    def run_setup_token(self) -> str | None:
        return None


def _store(tmp_path: Path, accounts: Iterable[Account]) -> AccountStore:
    return make_account_store(tmp_path, accounts)


def _acct(
    label: str = "team",
    *,
    provider_id: ProviderId = ProviderId.CLAUDE,
    provider_account_id: str | None = None,
    heartbeat_enabled: bool = False,
    heartbeat_5h_reset_at: datetime | None = None,
    heartbeat_window_resets: dict[str, datetime] | None = None,
    heartbeat_targets: tuple[str, ...] | None = None,
    refresh_token: str | None = "refresh-token",
    expiry_at: datetime | None = None,
) -> Account:
    expiry = (
        KnownExpiry(expiry_at) if expiry_at is not None else UnknownExpiry()
    )
    credentials = (
        ClaudeCredentials(
            access_token="old-token",
            refresh_token=refresh_token,
            expiry=expiry,
        )
        if provider_id is ProviderId.CLAUDE
        else CodexCredentials(
            access_token="old-token",
            refresh_token=refresh_token,
            expiry=expiry,
            account_id=provider_account_id,
        )
    )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="team",
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_5h_reset_at=heartbeat_5h_reset_at,
        heartbeat_window_resets=heartbeat_window_resets,
        heartbeat_targets=heartbeat_targets,
    )


def _install_ctx(
    tmp_path: Path,
    accounts: Iterable[Account],
    heartbeat_providers: dict[ProviderId, HeartbeatProvider],
    providers: dict[ProviderId, Provider] | None = None,
    clock: Clock | None = None,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    paths = make_application_paths(tmp_path)
    store = _store(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers=providers or {},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=clock or FixedClock(),
            heartbeat_providers=heartbeat_providers,
        )
    )
    return store, stdout, stderr


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
                heartbeat_5h_reset_at=_STANDARD_RESET,
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
    assert restored.heartbeat_5h_reset_at == _STANDARD_RESET
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
    )

    outcomes = service.heartbeat_all()

    assert provider.heartbeat_calls == []
    assert outcomes[0].status is HeartbeatStatus.DISABLED


def test_heartbeat_label_runs_even_when_disabled(tmp_path: Path) -> None:
    """Explicit label mode is a one-shot warm request."""
    provider = _FakeHeartbeatProvider()
    store = _store(tmp_path, [_acct(heartbeat_enabled=False)])
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=FixedClock(),
    )

    outcome = service.heartbeat_account(
        store.get("team"), require_enabled=False
    )

    assert outcome.status is HeartbeatStatus.WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_status is HeartbeatStatus.WARMED
    assert saved.heartbeat_5h_reset_at == _STANDARD_RESET


def test_heartbeat_decision_samples_clock_once(tmp_path: Path) -> None:
    """Auth and cached-reset checks share one heartbeat reference time."""
    provider = _FakeHeartbeatProvider()
    clock = FixedClock()
    store = _store(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                expiry_at=REFERENCE_TIME + timedelta(hours=1),
                heartbeat_5h_reset_at=_STANDARD_RESET,
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=clock,
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
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status is HeartbeatStatus.FAILED
    assert outcome.action_required is True
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_at == REFERENCE_TIME
    assert saved.last_heartbeat_status is HeartbeatStatus.FAILED
    assert saved.last_heartbeat_error == "rate limited"


def test_heartbeat_enable_disable_and_status_cli(tmp_path: Path) -> None:
    """Heartbeat config is managed through the CLI."""
    provider = _FakeHeartbeatProvider()
    store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct()],
        {ProviderId.CLAUDE: provider},
    )

    enabled = CliRunner().invoke(cli.app, ["heartbeat", "enable", "team"])
    status = CliRunner().invoke(cli.app, ["heartbeat", "status"])
    disabled = CliRunner().invoke(cli.app, ["heartbeat", "disable", "team"])

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
    provider = _FakeHeartbeatProvider()
    _, stdout, _ = _install_ctx(
        tmp_path,
        [_acct()],
        {ProviderId.CLAUDE: provider},
    )

    result = CliRunner().invoke(
        cli.app,
        ["heartbeat", "status", "--json"],
    )

    assert result.exit_code == 0
    payload = json.loads(stdout.getvalue())
    assert payload["accounts"][0]["label"] == "team"
    assert ROBOT_LINES[2] not in stdout.getvalue()


def test_empty_heartbeat_registry_remains_unsupported(
    tmp_path: Path,
) -> None:
    """An explicitly empty registry must not activate default providers."""
    _, stdout, _ = _install_ctx(tmp_path, [_acct()], {})

    result = CliRunner().invoke(cli.app, ["heartbeat", "status"])

    assert result.exit_code == 0
    assert "heartbeat: unsupported" in stdout.getvalue()
    assert "supported: no" in stdout.getvalue()


def test_heartbeat_label_cli_runs_one_shot_when_disabled(
    tmp_path: Path,
) -> None:
    """The documented heartbeat <label> form runs a one-shot probe."""
    provider = _FakeHeartbeatProvider()
    store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct("team", heartbeat_enabled=False)],
        {ProviderId.CLAUDE: provider},
    )

    result = CliRunner().invoke(cli.app, ["heartbeat", "team"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("team", "old-token")]
    assert "team: warmed" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is False


def test_heartbeat_all_quiet_runs_enabled_only(tmp_path: Path) -> None:
    """Quiet all-account mode is scheduler friendly."""
    provider = _FakeHeartbeatProvider()
    _, stdout, _ = _install_ctx(
        tmp_path,
        [
            _acct("enabled", heartbeat_enabled=True),
            _acct("disabled", heartbeat_enabled=False),
        ],
        {ProviderId.CLAUDE: provider},
    )

    result = CliRunner().invoke(cli.app, ["heartbeat", "--all", "--quiet"])

    assert result.exit_code == 0
    assert provider.heartbeat_calls == [("enabled", "old-token")]
    assert stdout.getvalue() == ""


def test_heartbeat_enable_accepts_codex_with_saved_account_id(
    tmp_path: Path,
) -> None:
    """Codex accounts with saved account ids can opt into heartbeat."""
    store, stdout, _ = _install_ctx(
        tmp_path,
        [
            _acct(
                provider_id=ProviderId.CODEX,
                provider_account_id="acct-codex",
            )
        ],
        {ProviderId.CODEX: _codex_heartbeat()},
    )

    result = CliRunner().invoke(cli.app, ["heartbeat", "enable", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is True
    assert "team: enabled" in stdout.getvalue()


def test_codex_heartbeat_warms_standard_window_with_mini() -> None:
    """Codex standard heartbeat uses the cheapest standard-window model."""
    account = _acct(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = _FakeCodexHttp(
        [
            {"rate_limit": {"primary_window": {"used_percent": 0}}},
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                }
            },
        ]
    )

    result = _codex_heartbeat().run(account, http)

    assert result.status is HeartbeatStatus.WARMED
    assert result.reset_at == _STANDARD_RESET
    assert len(http.get_calls) == CODEX_USAGE_FETCHES_FOR_WARM
    assert len(http.post_calls) == 1
    url, body, headers = http.post_calls[0]
    assert url == "https://chatgpt.com/backend-api/codex/responses"
    assert body["model"] == "gpt-5.4-mini"
    assert body["model"] != SPARK_HEARTBEAT_MODEL
    assert body["instructions"] == "Reply with exactly: ok"
    assert body["stream"] is True
    assert body["store"] is False
    assert body["reasoning"] == {"effort": "low"}
    assert body["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ok"}],
        }
    ]
    assert headers["Authorization"] == "Bearer old-token"
    assert headers["ChatGPT-Account-ID"] == "acct-codex"
    assert headers["Accept"] == "text/event-stream"


def test_codex_heartbeat_warms_spark_window_with_spark_model() -> None:
    """Codex Spark heartbeat targets the separate Spark rate limit."""
    account = _acct(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = _FakeCodexHttp(
        [
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {"used_percent": 0},
                        },
                    }
                ],
            },
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                },
                "additional_rate_limits": [
                    {
                        "limit_name": "GPT-5.3-Codex-Spark",
                        "rate_limit": {
                            "primary_window": {
                                "used_percent": 1,
                                "resets_at": "2026-06-12T19:00:00Z",
                            },
                        },
                    }
                ],
            },
        ]
    )

    result = _codex_heartbeat().run(account, http, target_id="spark")

    assert result.status is HeartbeatStatus.WARMED
    assert result.reset_at == _SPARK_RESET
    assert result.target_id == "spark"
    assert len(http.post_calls) == 1
    _, body, _ = http.post_calls[0]
    assert body["model"] == SPARK_HEARTBEAT_MODEL


def test_codex_heartbeat_fails_when_target_window_stays_inactive() -> None:
    """A successful POST is not reported as warmed unless usage confirms it."""
    account = _acct(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = _FakeCodexHttp(
        [
            {"rate_limit": {"primary_window": {"used_percent": 0}}},
            {"rate_limit": {"primary_window": {"used_percent": 1}}},
        ]
    )

    result = _codex_heartbeat().run(account, http)

    assert result.status is HeartbeatStatus.FAILED
    assert result.warmed is False
    assert "did not become active" in result.message


def test_codex_heartbeat_can_enable_all_targets(tmp_path: Path) -> None:
    """Codex opt-in can include standard and Spark windows."""
    store, stdout, _ = _install_ctx(
        tmp_path,
        [
            _acct(
                provider_id=ProviderId.CODEX,
                provider_account_id="acct-codex",
            )
        ],
        {ProviderId.CODEX: _codex_heartbeat()},
    )

    result = CliRunner().invoke(
        cli.app,
        ["heartbeat", "enable", "team", "--target", "all"],
    )

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is True
    assert saved.heartbeat_targets == ("standard", "spark")
    assert "team: enabled" in stdout.getvalue()


def test_codex_heartbeat_skips_when_usage_window_is_active() -> None:
    """Codex usage state is inspected before sending a model request."""
    account = _acct(
        provider_id=ProviderId.CODEX,
        provider_account_id="acct-codex",
    )
    http = _FakeCodexHttp(
        [
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 1,
                        "resets_at": "2026-06-12T18:00:00Z",
                    }
                }
            }
        ]
    )

    result = _codex_heartbeat().run(account, http)

    assert result.status is HeartbeatStatus.ACTIVE
    assert result.reset_at == _STANDARD_RESET
    assert http.post_calls == []


def test_maintain_refreshes_before_heartbeat(tmp_path: Path) -> None:
    """The scheduler command refreshes tokens before window warming."""
    clock = FixedClock()
    refresh_provider = _FakeRefreshProvider()
    heartbeat_provider = _FakeHeartbeatProvider()
    _install_ctx(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                refresh_token="refresh-token",
                expiry_at=REFERENCE_TIME - timedelta(minutes=1),
            )
        ],
        {ProviderId.CLAUDE: heartbeat_provider},
        providers={ProviderId.CLAUDE: refresh_provider},
        clock=clock,
    )

    result = CliRunner().invoke(cli.app, ["maintain", "--quiet"])

    assert result.exit_code == 0
    assert refresh_provider.refresh_calls == 1
    assert heartbeat_provider.heartbeat_calls == [("team", "refreshed-token")]
