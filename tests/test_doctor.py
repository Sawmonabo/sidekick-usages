"""Doctor command diagnostics tests."""

import io
import json
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.heartbeat import build_heartbeat_registry
from sidekick_usages.http import HttpClient
from sidekick_usages.providers import build_provider_registry
from sidekick_usages.store import Account, AccountStore
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)


def _install_ctx(
    tmp_path: Path,
    accounts: list[Account],
) -> tuple[AccountStore, io.StringIO, io.StringIO, FixedClock]:
    """Install an isolated CLI context for doctor tests."""
    paths = make_application_paths(tmp_path)
    store = AccountStore(paths.accounts)
    for account in accounts:
        store.upsert(account)
    stdout = io.StringIO()
    stderr = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers=providers,
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=clock,
            heartbeat_providers=build_heartbeat_registry(providers),
        )
    )
    return store, stdout, stderr, clock


def test_doctor_json_reports_refreshability_and_redacts_tokens(
    tmp_path: Path,
) -> None:
    """Doctor JSON exposes account state without leaking secrets."""
    oauth = Account(
        label="team",
        provider_id="claude",
        access_token="sk-ant-oat01-secret-access-token-value",
        refresh_token="secret-refresh-token",
        expires_at=int(REFERENCE_TIME.timestamp() * 1000) + 3_600_000,
        plan="team",
        scopes=["user:profile", "user:inference"],
        heartbeat_enabled=True,
        heartbeat_5h_reset_at="2026-06-12T18:00:00Z",
        last_heartbeat_status="active",
    )
    setup = Account(
        label="setup",
        provider_id="claude",
        access_token="sk-ant-oat01-setup-token-value",
        expires_at=None,
        plan="max",
        scopes=[],
    )
    _, stdout, _, clock = _install_ctx(tmp_path, [oauth, setup])

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(stdout.getvalue())
    accounts = {item["label"]: item for item in payload["accounts"]}
    assert accounts["team"]["can_auto_refresh"] is True
    assert accounts["team"]["expiry_state"] == "fresh"
    assert accounts["team"]["usage_route"] == "/api/oauth/usage"
    assert accounts["team"]["heartbeat_supported"] is True
    assert accounts["team"]["heartbeat_enabled"] is True
    assert accounts["team"]["heartbeat_5h_reset_at"] == "2026-06-12T18:00:00Z"
    assert accounts["team"]["last_heartbeat_status"] == "active"
    assert accounts["setup"]["can_auto_refresh"] is False
    assert accounts["setup"]["usage_route"] == "/v1/messages headers"
    assert accounts["setup"]["heartbeat_supported"] is True
    assert clock.calls == 1
    rendered = stdout.getvalue()
    assert "secret-refresh-token" not in rendered
    assert "sk-ant-oat01-secret-access-token-value" not in rendered


def test_doctor_reports_previous_refresh_rejection(
    tmp_path: Path,
) -> None:
    """Doctor flags accounts after a saved-token refresh fails."""
    account = Account(
        label="dead",
        provider_id="claude",
        access_token="sk-ant-oat01-old-token-value",
        refresh_token="refresh-token",
        plan="team",
        scopes=["user:profile"],
        last_refresh_status="failed",
        last_refresh_error="Claude CLI refresh failed: status code 400",
    )
    _, stdout, _, _ = _install_ctx(tmp_path, [account])

    result = CliRunner().invoke(cli.app, ["doctor"])

    assert result.exit_code == 1
    out = stdout.getvalue()
    assert "dead" in out
    assert "manual action: yes" in out
    assert "heartbeat supported:" in out
    assert "Claude CLI refresh failed" in out
    assert out.count(ROBOT_LINES[2]) == 1
    assert "doctor · account diagnostics" in out


def test_doctor_filters_by_provider_and_label(tmp_path: Path) -> None:
    """Doctor filters are composable."""
    claude = Account(
        label="claude-team",
        provider_id="claude",
        access_token="sk-ant-oat01-claude",
        plan="team",
    )
    codex = Account(
        label="codex-pro",
        provider_id="codex",
        access_token="eyJ.codex.token",
        refresh_token="codex-refresh",
        plan="pro",
    )
    _, stdout, _, _ = _install_ctx(tmp_path, [claude, codex])

    result = CliRunner().invoke(
        cli.app,
        ["doctor", "--provider", "codex", "--label", "codex-pro"],
    )

    assert result.exit_code == 0
    out = stdout.getvalue()
    assert "codex-pro" in out
    assert "claude-team" not in out
