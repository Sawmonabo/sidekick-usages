"""CLI provider-owned credential workflow regression tests."""

import json
from pathlib import Path

import pytest

from sidekick_usages.core.models import UsageReport
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.providers.claude import provider as claude_provider_module
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.codex import auth as codex_auth_module
from sidekick_usages.providers.codex.provider import CodexProvider
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


@pytest.mark.parametrize(
    ("command", "deprecated"),
    [
        (["codex", "login"], False),
        (["codex-login"], True),
    ],
)
def test_codex_login_runs_plain_cli_and_imports_private_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    *,
    deprecated: bool,
) -> None:
    """Both spellings leave global ~/.codex as the explicit source."""
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

    monkeypatch.setattr(codex_auth_module.subprocess, "run", fake_run)

    result = harness.invoke([*command, "team"])

    assert result.exit_code == 0
    assert ("DeprecationWarning" in result.stderr) is deprecated
    assert "DeprecationWarning" not in result.stdout
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


@pytest.mark.parametrize(
    ("command", "deprecated"),
    [
        (["codex", "export"], False),
        (["codex-export"], True),
    ],
)
def test_codex_export_writes_saved_credentials_to_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    *,
    deprecated: bool,
) -> None:
    """Both spellings export the same saved credentials and warning channel."""
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
        [*command, "team", "--codex-home", str(codex_home)],
    )

    assert result.exit_code == 0
    assert ("DeprecationWarning" in result.stderr) is deprecated
    assert "DeprecationWarning" not in result.stdout
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


@pytest.mark.parametrize(
    ("command", "deprecated"),
    [
        (["claude", "setup-token"], False),
        (["setup-token", "claude"], True),
    ],
)
def test_setup_token_delegates_only_to_claude_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    *,
    deprecated: bool,
) -> None:
    """Both spellings delegate to Claude's one narrow capability."""
    token = "sk-ant-oat01-synthetic-setup-token"
    raw_secret = "oauth-code=must-not-reach-terminal"
    provider = ClaudeProvider(FixedClock())
    monkeypatch.setattr(
        claude_provider_module.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )

    def capture(
        command: list[str],
        timeout: int,
    ) -> claude_provider_module._CapturedSetupOutput:
        assert command == ["/usr/bin/claude", "setup-token"]
        assert timeout > 0
        return claude_provider_module._CapturedSetupOutput(
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
        "fetch_usage",
        lambda account, http: UsageReport(),
    )
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        (),
        claude_setup_token=provider,
    )

    result = harness.invoke(
        [*command, "--label", "setup"],
    )

    assert result.exit_code == 0
    assert ("DeprecationWarning" in result.stderr) is deprecated
    assert "DeprecationWarning" not in result.stdout
    assert "Saved 'setup'." in stdout.getvalue()
    assert raw_secret not in result.output
    assert raw_secret not in stdout.getvalue()
    assert raw_secret not in stderr.getvalue()
    saved = store.get("setup")
    assert saved is not None
    assert saved.access_token == token


def test_setup_token_codex_returns_typed_unsupported_outcome(
    tmp_path: Path,
) -> None:
    """Codex setup-token fails cleanly without a generic provider method."""
    harness, _, _, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CODEX: CodexProvider(FixedClock())},
        (),
    )

    result = harness.invoke(["setup-token", "codex"])

    assert result.exit_code == 1
    assert "doesn't expose a long-lived token generator" in stderr.getvalue()
