"""Heartbeat/window-warming behavior tests."""

import io
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.heartbeat import (
    HEARTBEAT_ACTIVE,
    HEARTBEAT_DISABLED,
    HEARTBEAT_FAILED,
    HEARTBEAT_WARMED,
    HeartbeatProbeResult,
    HeartbeatProvider,
    HeartbeatService,
    UsageWindowState,
)
from sidekick_usages.heartbeat.codex import (
    SPARK_HEARTBEAT_MODEL,
    CodexHeartbeat,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import DetectedCredentials, Provider
from sidekick_usages.report import UsageReport
from sidekick_usages.store import Account, AccountStore

CODEX_USAGE_FETCHES_FOR_WARM = 2


class _FakeHeartbeatProvider(HeartbeatProvider):
    """Provider test double with scripted heartbeat and refresh behavior."""

    id = "claude"
    display_name = "Claude Code"

    def __init__(
        self,
        *,
        provider_id: str = "claude",
        heartbeat_supported: bool = True,
        heartbeat_results: Iterable[HeartbeatProbeResult] = (),
    ) -> None:
        self.id = provider_id
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self._heartbeat_supported = heartbeat_supported
        self.heartbeat_results = list(heartbeat_results)
        self.heartbeat_calls: list[tuple[str, str]] = []

    def supports(self, account: Account) -> bool:
        del account
        return self._heartbeat_supported

    def inspect_window(
        self,
        account: Account,
        http: HttpClient,
        target: object,
    ) -> UsageWindowState:
        del account, http, target
        return UsageWindowState(active=False)

    def warm_window(
        self,
        account: Account,
        http: HttpClient,
        target: object,
    ) -> HeartbeatProbeResult:
        del http, target
        self.heartbeat_calls.append((account.label, account.access_token))
        if self.heartbeat_results:
            return self.heartbeat_results.pop(0)
        return HeartbeatProbeResult(
            status=HEARTBEAT_WARMED,
            message="warmed",
            warmed=True,
            reset_at="2026-06-12T18:00:00Z",
        )


class _FakeRefreshProvider(Provider):
    """Provider test double for maintain refresh ordering."""

    id = "claude"
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
        account.access_token = "refreshed-token"
        account.expires_at = int(time.time() * 1000) + 3_600_000
        return True

    def run_setup_token(self) -> str | None:
        return None


def _store(tmp_path: Path, accounts: Iterable[Account]) -> AccountStore:
    store = AccountStore(tmp_path / "accounts.json")
    for account in accounts:
        store.upsert(account)
    return store


def _acct(
    label: str = "team",
    *,
    provider_id: str = "claude",
    provider_account_id: str | None = None,
    heartbeat_enabled: bool = False,
    heartbeat_5h_reset_at: str | None = None,
    heartbeat_window_resets: dict[str, str] | None = None,
    heartbeat_targets: list[str] | None = None,
    refresh_token: str | None = "refresh-token",
    expires_at: int | None = None,
) -> Account:
    return Account(
        label=label,
        provider_id=provider_id,
        access_token="old-token",
        provider_account_id=provider_account_id,
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan="team",
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_5h_reset_at=heartbeat_5h_reset_at,
        heartbeat_window_resets=heartbeat_window_resets,
        heartbeat_targets=heartbeat_targets,
    )


def _install_ctx(
    tmp_path: Path,
    accounts: Iterable[Account],
    heartbeat_providers: dict[str, HeartbeatProvider],
    providers: dict[str, Provider] | None = None,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    store = _store(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers=providers or {},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            heartbeat_providers=heartbeat_providers,
        )
    )
    return store, stdout, stderr


class _FakeCodexHttp(HttpClient):
    """Tiny HTTP double for Codex heartbeat protocol tests."""

    def __init__(self, usage_responses: Iterable[dict[str, Any]]) -> None:
        self.usage_responses = list(usage_responses)
        self.get_calls: list[tuple[str, dict[str, str]]] = []
        self.post_calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def get_json(
        self,
        url: str,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        self.get_calls.append((url, headers))
        if not self.usage_responses:
            raise AssertionError("unexpected Codex usage fetch")
        return self.usage_responses.pop(0)

    def post_capture_headers(
        self,
        url: str,
        json_body: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, str]:
        self.post_calls.append((url, json_body, headers))
        return {}


def test_account_roundtrips_heartbeat_metadata(tmp_path: Path) -> None:
    """Heartbeat settings and diagnostics persist in the account store."""
    store = _store(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                heartbeat_5h_reset_at="2026-06-12T18:00:00Z",
                heartbeat_window_resets={
                    "standard": "2026-06-12T18:00:00Z",
                    "spark": "2026-06-12T19:00:00Z",
                },
                heartbeat_targets=["standard", "spark"],
            )
        ],
    )
    account = store.get("team")
    assert account is not None
    account.last_heartbeat_at = "2026-06-12T13:00:00Z"
    account.last_heartbeat_status = HEARTBEAT_WARMED
    account.last_heartbeat_error = None
    store.save()

    restored = AccountStore(store.path).load().get("team")

    assert restored is not None
    assert restored.heartbeat_enabled is True
    assert restored.heartbeat_5h_reset_at == "2026-06-12T18:00:00Z"
    assert restored.heartbeat_window_resets == {
        "standard": "2026-06-12T18:00:00Z",
        "spark": "2026-06-12T19:00:00Z",
    }
    assert restored.heartbeat_targets == ["standard", "spark"]
    assert restored.last_heartbeat_at == "2026-06-12T13:00:00Z"
    assert restored.last_heartbeat_status == HEARTBEAT_WARMED
    assert restored.last_heartbeat_error is None


def test_heartbeat_all_skips_disabled_accounts(tmp_path: Path) -> None:
    """Scheduler mode only probes accounts explicitly enabled."""
    provider = _FakeHeartbeatProvider()
    store = _store(tmp_path, [_acct(heartbeat_enabled=False)])
    service = HeartbeatService(store, HttpClient(), {"claude": provider})

    outcomes = service.heartbeat_all()

    assert provider.heartbeat_calls == []
    assert outcomes[0].status == HEARTBEAT_DISABLED


def test_heartbeat_label_runs_even_when_disabled(tmp_path: Path) -> None:
    """Explicit label mode is a one-shot warm request."""
    provider = _FakeHeartbeatProvider()
    store = _store(tmp_path, [_acct(heartbeat_enabled=False)])
    service = HeartbeatService(store, HttpClient(), {"claude": provider})

    outcome = service.heartbeat_account(
        store.get("team"), require_enabled=False
    )

    assert outcome.status == HEARTBEAT_WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]
    saved = AccountStore(store.path).load().get("team")
    assert saved is not None
    assert saved.last_heartbeat_status == HEARTBEAT_WARMED
    assert saved.heartbeat_5h_reset_at == "2026-06-12T18:00:00Z"


def test_heartbeat_uses_cached_future_reset(tmp_path: Path) -> None:
    """Daemon ticks do not send repeated probes before the cached reset."""
    provider = _FakeHeartbeatProvider()
    store = _store(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                heartbeat_5h_reset_at="2026-06-12T18:00:00Z",
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {"claude": provider},
        now=lambda: 1_781_280_000.0,
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status == HEARTBEAT_ACTIVE
    assert provider.heartbeat_calls == []


def test_heartbeat_cache_is_target_specific(tmp_path: Path) -> None:
    """A cached Spark reset must not suppress a standard Codex warm."""
    provider = _FakeHeartbeatProvider(provider_id="codex")
    store = _store(
        tmp_path,
        [
            _acct(
                provider_id="codex",
                heartbeat_enabled=True,
                heartbeat_window_resets={
                    "spark": "2026-06-12T18:00:00Z",
                },
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {"codex": provider},
        now=lambda: 1_781_280_000.0,
    )

    outcome = service.heartbeat_account(
        store.get("team"), target_id="standard"
    )

    assert outcome.status == HEARTBEAT_WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]


def test_heartbeat_persists_failure_per_account(tmp_path: Path) -> None:
    """One provider failure is recorded instead of escaping."""
    provider = _FakeHeartbeatProvider(
        heartbeat_results=[
            HeartbeatProbeResult(
                status=HEARTBEAT_FAILED,
                message="rate limited",
                action_required=True,
                warmed=False,
            )
        ]
    )
    store = _store(tmp_path, [_acct(heartbeat_enabled=True)])
    service = HeartbeatService(store, HttpClient(), {"claude": provider})

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status == HEARTBEAT_FAILED
    assert outcome.action_required is True
    saved = AccountStore(store.path).load().get("team")
    assert saved is not None
    assert saved.last_heartbeat_status == HEARTBEAT_FAILED
    assert saved.last_heartbeat_error == "rate limited"


def test_heartbeat_enable_disable_and_status_cli(tmp_path: Path) -> None:
    """Heartbeat config is managed through the CLI."""
    provider = _FakeHeartbeatProvider()
    store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct()],
        {"claude": provider},
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
    assert "enabled" in stdout.getvalue()
    assert "heartbeat: on" in stdout.getvalue()
    assert "disabled" in stdout.getvalue()


def test_heartbeat_label_cli_runs_one_shot_when_disabled(
    tmp_path: Path,
) -> None:
    """The documented heartbeat <label> form runs a one-shot probe."""
    provider = _FakeHeartbeatProvider()
    store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct("team", heartbeat_enabled=False)],
        {"claude": provider},
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
        {"claude": provider},
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
        [_acct(provider_id="codex", provider_account_id="acct-codex")],
        {"codex": CodexHeartbeat()},
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
        provider_id="codex",
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

    result = CodexHeartbeat().run(account, http)

    assert result.status == HEARTBEAT_WARMED
    assert result.reset_at == "2026-06-12T18:00:00Z"
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
        provider_id="codex",
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

    result = CodexHeartbeat().run(account, http, target_id="spark")

    assert result.status == HEARTBEAT_WARMED
    assert result.reset_at == "2026-06-12T19:00:00Z"
    assert result.target_id == "spark"
    assert len(http.post_calls) == 1
    _, body, _ = http.post_calls[0]
    assert body["model"] == SPARK_HEARTBEAT_MODEL


def test_codex_heartbeat_fails_when_target_window_stays_inactive() -> None:
    """A successful POST is not reported as warmed unless usage confirms it."""
    account = _acct(
        provider_id="codex",
        provider_account_id="acct-codex",
    )
    http = _FakeCodexHttp(
        [
            {"rate_limit": {"primary_window": {"used_percent": 0}}},
            {"rate_limit": {"primary_window": {"used_percent": 1}}},
        ]
    )

    result = CodexHeartbeat().run(account, http)

    assert result.status == HEARTBEAT_FAILED
    assert result.warmed is False
    assert "did not become active" in result.message


def test_codex_heartbeat_can_enable_all_targets(tmp_path: Path) -> None:
    """Codex daemon opt-in can explicitly include standard and Spark windows."""
    store, stdout, _ = _install_ctx(
        tmp_path,
        [_acct(provider_id="codex", provider_account_id="acct-codex")],
        {"codex": CodexHeartbeat()},
    )

    result = CliRunner().invoke(
        cli.app,
        ["heartbeat", "enable", "team", "--target", "all"],
    )

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.heartbeat_enabled is True
    assert saved.heartbeat_targets == ["standard", "spark"]
    assert "team: enabled" in stdout.getvalue()


def test_codex_heartbeat_skips_when_usage_window_is_active() -> None:
    """Codex usage state is inspected before sending a model request."""
    account = _acct(
        provider_id="codex",
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

    result = CodexHeartbeat().run(account, http)

    assert result.status == HEARTBEAT_ACTIVE
    assert result.reset_at == "2026-06-12T18:00:00Z"
    assert http.post_calls == []


def test_maintain_refreshes_before_heartbeat(tmp_path: Path) -> None:
    """The scheduler command refreshes tokens before window warming."""
    now = int(time.time() * 1000)
    refresh_provider = _FakeRefreshProvider()
    heartbeat_provider = _FakeHeartbeatProvider()
    _install_ctx(
        tmp_path,
        [
            _acct(
                heartbeat_enabled=True,
                refresh_token="refresh-token",
                expires_at=now - 60_000,
            )
        ],
        {"claude": heartbeat_provider},
        providers={"claude": refresh_provider},
    )

    result = CliRunner().invoke(cli.app, ["maintain", "--quiet"])

    assert result.exit_code == 0
    assert refresh_provider.refresh_calls == 1
    assert heartbeat_provider.heartbeat_calls == [("team", "refreshed-token")]
