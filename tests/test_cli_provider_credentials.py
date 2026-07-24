"""CLI provider-owned credential workflow regression tests."""

import json
from pathlib import Path

import pytest

import sidekick_usages.providers.claude.provider
import sidekick_usages.providers.codex.auth
from sidekick_usages.core.models import UsageReport
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.providers.claude.provider import ClaudeProvider
from tests.test_cli_refresh import (
    _codex_acct,
    _codex_cache_home,
    _detected,
    _FakeProvider,
    _install_ctx,
    _install_empty_ctx,
    _install_many_ctx,
    _isolate_default_codex_home,
    _seconds,
)
from tests.test_support import FixedClock

pytestmark = pytest.mark.usefixtures(
    _isolate_default_codex_home.__name__,
)


def test_codex_login_runs_plain_cli_and_imports_private_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical login leaves global ~/.codex as the explicit source."""
    provider = _FakeProvider(
        detected=_detected(
            access_token="eyJ-current.access.sig",
            provider_id="codex",
            refresh_token="refresh-current",
            expiry=_seconds(1_770_000_000),
            plan="pro",
            provider_account_id="acct_current",
            id_token="id-token-current",
            last_refresh="2026-06-12T00:00:00Z",
        ),
        provider_id="codex",
    )
    harness, store, stdout, _ = _install_empty_ctx(tmp_path, provider)
    calls: list[dict[str, object]] = []

    def fake_run(
        argv: list[str],
        *,
        check: bool,
        env: dict[str, str] | None = None,
    ) -> None:
        call: dict[str, object] = {"argv": argv, "check": check}
        if env is not None:
            call["env"] = env
        calls.append(call)

    monkeypatch.setattr(
        sidekick_usages.providers.codex.auth.subprocess, "run", fake_run
    )

    result = harness.invoke(["codex", "login", "team"])

    assert result.exit_code == 0
    assert "DeprecationWarning" not in result.output
    assert "Updated Codex login for 'team'." in stdout.getvalue()
    assert len(calls) == 1
    assert calls[0]["argv"] == ["codex", "login"]
    assert calls[0]["check"] is True
    assert "env" not in calls[0]
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    cache_home = _codex_cache_home(tmp_path)
    assert saved.codex_home == str(cache_home)
    assert saved.provider_account_id == "acct_current"
    cached = json.loads((cache_home / "auth.json").read_text())
    assert cached["tokens"]["id_token"] == "id-token-current"


def test_codex_export_writes_saved_credentials_to_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical export writes saved credentials without warning output."""
    codex_home = tmp_path / "codex-team"
    acct = _codex_acct(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        provider_account_id="acct_current",
        id_token="id-token-current",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(provider_id="codex")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-default"))
    harness, store, stdout, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(
        [
            "codex",
            "export",
            "team",
            "--codex-home",
            str(codex_home),
        ],
    )

    assert result.exit_code == 0
    assert "DeprecationWarning" not in result.output
    assert "Exported 'team' to Codex home" in stdout.getvalue()
    auth = json.loads((codex_home / "auth.json").read_text())
    assert auth["auth_mode"] == "chatgpt"
    assert auth["last_refresh"] == "2026-06-12T00:00:00Z"
    assert auth["tokens"] == {
        "access_token": "eyJ-current.access.sig",
        "refresh_token": "refresh-current",
        "id_token": "id-token-current",
        "account_id": "acct_current",
    }
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home is None


@pytest.mark.parametrize(
    ("source_account_id", "expected_exit"),
    [
        ("acct_current", ExitCode.SUCCESS),
        ("acct_other", ExitCode.MANUAL_ACTION),
    ],
)
def test_codex_export_reads_default_source_without_mutating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_account_id: str,
    expected_exit: ExitCode,
) -> None:
    source_home = tmp_path / "default-codex"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_text(
        json.dumps(
            {
                "future_metadata": {"preserve": True},
                "tokens": {
                    "account_id": source_account_id,
                    "id_token": "id-from-default-source",
                },
            }
        )
    )
    original_source = source_auth.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    target_home = tmp_path / "exported-codex"
    account = _codex_acct(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        provider_account_id="acct_current",
        id_token=None,
    )
    provider = _FakeProvider(provider_id="codex")
    harness, _, _, _ = _install_ctx(tmp_path, provider, account)

    result = harness.invoke(
        ["codex", "export", "team", "--codex-home", str(target_home)],
    )

    assert result.exit_code == expected_exit
    assert source_auth.read_bytes() == original_source
    assert provider.refresh_calls == 0
    if expected_exit is ExitCode.SUCCESS:
        exported = json.loads((target_home / "auth.json").read_text())
        assert exported["tokens"]["id_token"] == "id-from-default-source"
    else:
        assert target_home.is_dir()
        assert not (target_home / "auth.json").exists()
        assert not (target_home / "config.toml").exists()


def test_setup_token_delegates_only_to_claude_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical setup-token delegates to Claude's narrow capability."""
    token = "sk-ant-oat01-synthetic-setup-token"
    raw_secret = "oauth-code=must-not-reach-terminal"
    provider = ClaudeProvider(FixedClock())
    monkeypatch.setattr(
        sidekick_usages.providers.claude.provider.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def capture(
        command: list[str],
        timeout: int,
    ) -> sidekick_usages.providers.claude.provider._CapturedSetupOutput:
        assert command == ["/usr/bin/claude", "setup-token"]
        assert timeout > 0
        return sidekick_usages.providers.claude.provider._CapturedSetupOutput(
            0,
            f"{raw_secret}\nToken: {token}\n".encode(),
        )

    monkeypatch.setattr(
        ClaudeProvider,
        "_capture_setup_output",
        staticmethod(capture),
    )
    monkeypatch.setattr(
        provider,
        "validate_credentials",
        lambda account, http: UsageReport(),
    )
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        (),
        claude_setup_token=provider,
    )

    result = harness.invoke(
        ["claude", "setup-token", "--label", "setup"],
    )

    assert result.exit_code == 0
    assert "DeprecationWarning" not in result.output
    assert "Saved 'setup'." in stdout.getvalue()
    assert raw_secret not in result.output
    assert raw_secret not in stdout.getvalue()
    assert raw_secret not in stderr.getvalue()
    saved = store.get("setup")
    assert saved is not None
    assert saved.access_token == token
