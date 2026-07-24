"""CLI provider-owned credential workflow regression tests."""

import json
import os
from pathlib import Path

import pytest

import sidekick_usages.providers.claude.provider
from sidekick_usages.core.accounts.models import CodexStoredAuthority
from sidekick_usages.core.models import UsageReport
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
)
from sidekick_usages.persistence.credentials.repository import (
    authority_bundle_name,
)
from sidekick_usages.providers.claude.provider import ClaudeProvider
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.executable import (
    configure_codex_logins,
    write_fake_codex,
)
from tests.fakes.codex.managed import managed_subscription
from tests.fakes.codex.models import FakeCodexLogin
from tests.fakes.codex.schema import write_codex_schema
from tests.test_cli_refresh import (
    _codex_acct,
    _FakeProvider,
    _install_ctx,
    _install_many_ctx,
    _isolate_default_codex_home,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

pytestmark = pytest.mark.usefixtures(
    _isolate_default_codex_home.__name__,
)


def _install_fake_codex_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    logins: dict[Path, FakeCodexLogin],
) -> None:
    """Install and configure one release-shaped fake Codex executable."""
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(tmp_path, schema_root)
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    )
    configure_codex_logins(tmp_path, logins)


def _seed_native_codex_home() -> tuple[Path, bytes]:
    """Seed the isolated native home with an unrelated official login."""
    native_home = Path(os.environ["CODEX_HOME"])
    native_home.mkdir()
    native_auth = managed_auth(
        "acct_native",
        "2026-07-20T00:00:00.000000001Z",
    )
    (native_home / "auth.json").write_bytes(native_auth)
    return native_home, native_auth


def test_codex_login_migrates_accounts_independently_without_native_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation preserves one account while another migration succeeds."""
    account_a = _codex_acct(
        access_token="eyJ-alpha.access.sig",
        refresh_token="legacy-refresh-alpha",
        plan="alpha-plan",
        provider_account_id="acct_alpha",
        id_token="legacy-id-alpha",
        last_refresh="2026-06-12T00:00:00Z",
    )
    account_a.label = AccountLabel("alpha")
    account_a.heartbeat_enabled = True
    account_a.heartbeat_targets = ("five-hour",)
    account_a.last_heartbeat_at = REFERENCE_TIME
    account_a.last_heartbeat_status = HeartbeatStatus.WARMED
    account_b = _codex_acct(
        access_token="eyJ-beta.access.sig",
        refresh_token="legacy-refresh-beta",
        plan="beta-plan",
        provider_account_id="acct_beta",
        id_token="legacy-id-beta",
        last_refresh="2026-06-12T00:00:00Z",
    )
    account_b.label = AccountLabel("beta")
    provider = _FakeProvider(provider_id="codex")
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CODEX: provider},
        (account_a, account_b),
    )
    paths = make_application_paths(tmp_path)
    saved_a, saved_b = store.saved_accounts()
    subscription_a = saved_a.authority.subscription
    subscription_b = saved_b.authority.subscription
    assert isinstance(subscription_a, CodexStoredAuthority)
    assert isinstance(subscription_b, CodexStoredAuthority)
    legacy_a = paths.private_credentials / authority_bundle_name(
        saved_a.account_id,
        subscription_a.authority_id,
    )
    legacy_b = paths.private_credentials / authority_bundle_name(
        saved_b.account_id,
        subscription_b.authority_id,
    )
    home_a = paths.private_codex_profiles / str(saved_a.account_id)
    home_b = paths.private_codex_profiles / str(saved_b.account_id)
    native_home, native_auth = _seed_native_codex_home()
    metrics = b'{"synthetic":"metrics-history"}'
    paths.activity_snapshots.write_bytes(metrics)
    _install_fake_codex_login(
        tmp_path,
        monkeypatch,
        {
            home_a: FakeCodexLogin(
                "acct_alpha",
                "2026-07-21T00:00:00.000000001Z",
                "2026-07-21T00:00:00.000000002Z",
                outcome="cancelled",
            ),
            home_b: FakeCodexLogin(
                "acct_beta",
                "2026-07-21T00:00:00.000000003Z",
                "2026-07-21T00:00:00.000000004Z",
            ),
        },
    )

    cancelled = harness.invoke(["codex", "login", "alpha"])
    migrated_b = harness.invoke(["codex", "login", "beta"])

    assert (
        cancelled.exit_code,
        migrated_b.exit_code,
        legacy_a.is_dir(),
        legacy_b.exists(),
    ) == (
        ExitCode.MANUAL_ACTION,
        ExitCode.SUCCESS,
        True,
        False,
    )
    current_a = store.read_saved(saved_a.account_id)
    assert current_a is not None
    assert not current_a.has_managed_authority
    current_b = store.read_saved(saved_b.account_id)
    assert current_b is not None
    managed_b = managed_subscription(current_b)
    assert (
        str(managed_b.provider_identity),
        str(managed_b.generation),
    ) == (
        "acct_beta",
        "2026-07-21T00:00:00.000000004Z",
    )
    configure_codex_logins(
        tmp_path,
        {
            home_a: FakeCodexLogin(
                "acct_alpha",
                "2026-07-21T00:00:00.000000005Z",
                "2026-07-21T00:00:00.000000006Z",
            ),
            home_b: FakeCodexLogin(
                "acct_beta",
                "2026-07-21T00:00:00.000000003Z",
                "2026-07-21T00:00:00.000000004Z",
            ),
        },
    )

    migrated_a = harness.invoke(["refresh", "alpha"])

    assert migrated_a.exit_code == ExitCode.SUCCESS
    current_a = store.read_saved(saved_a.account_id)
    assert current_a is not None
    managed_a = managed_subscription(current_a)
    assert (
        str(managed_a.provider_identity),
        str(managed_a.generation),
        current_a.label,
        current_a.plan,
        current_a.heartbeat_enabled,
        current_a.heartbeat_targets,
        current_a.last_heartbeat_at,
        current_a.last_heartbeat_status,
    ) == (
        "acct_alpha",
        "2026-07-21T00:00:00.000000006Z",
        saved_a.label,
        saved_a.plan,
        saved_a.heartbeat_enabled,
        saved_a.heartbeat_targets,
        saved_a.last_heartbeat_at,
        saved_a.last_heartbeat_status,
    )
    assert (
        legacy_a.exists(),
        (home_a / "auth.json").read_bytes() == native_auth,
        (home_b / "auth.json").read_bytes() == native_auth,
        (native_home / "auth.json").read_bytes(),
        paths.activity_snapshots.read_bytes(),
    ) == (False, False, False, native_auth, metrics)
    output = stdout.getvalue() + stderr.getvalue()
    assert (
        "https://auth.openai.com/oauth/authorize" in output,
        "raw-provider-secret" in output,
    ) == (True, False)
    events = (tmp_path / "events.jsonl").read_text()
    assert (
        "eyJ-alpha.access.sig" in events,
        "eyJ-beta.access.sig" in events,
    ) == (False, False)
    method_events = [
        json.loads(line)
        for line in events.splitlines()
        if '"method": "account/login/start"' in line
    ]
    assert {
        (event["codex_home"], event["cwd"]) for event in method_events
    } == {
        (str(home_a), str(home_a)),
        (str(home_b), str(home_b)),
    }


def test_codex_login_recovers_proven_home_after_interrupted_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry verifies the final home without repeating official login."""
    account = _codex_acct(
        access_token="eyJ-current.access.sig",
        refresh_token="legacy-refresh",
        provider_account_id="acct_current",
        id_token="legacy-id",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(provider_id="codex")
    harness, store, stdout, _ = _install_many_ctx(
        tmp_path,
        {ProviderId.CODEX: provider},
        (account,),
    )
    paths = make_application_paths(tmp_path)
    saved = store.saved_accounts()[0]
    subscription = saved.authority.subscription
    assert isinstance(subscription, CodexStoredAuthority)
    legacy = paths.private_credentials / authority_bundle_name(
        saved.account_id,
        subscription.authority_id,
    )
    managed_home = paths.private_codex_profiles / str(saved.account_id)
    native_home, native_auth = _seed_native_codex_home()
    _install_fake_codex_login(
        tmp_path,
        monkeypatch,
        {
            managed_home: FakeCodexLogin(
                "acct_current",
                "2026-07-21T00:00:00.000000001Z",
                "2026-07-21T00:00:00.000000002Z",
            )
        },
    )
    commit = store.migrate_codex_authority

    def interrupt_commit(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise OSError("synthetic interruption")

    monkeypatch.setattr(store, "migrate_codex_authority", interrupt_commit)
    interrupted = harness.invoke(["codex", "login", "team", "--device-auth"])

    assert interrupted.exit_code == ExitCode.MANUAL_ACTION
    assert legacy.is_dir()
    assert managed_home.joinpath("auth.json").is_file()
    interrupted_account = store.read_saved(saved.account_id)
    assert interrupted_account is not None
    assert not interrupted_account.has_managed_authority
    configure_codex_logins(
        tmp_path,
        {
            managed_home: FakeCodexLogin(
                "acct_current",
                "2026-07-21T00:00:00.000000001Z",
                "2026-07-21T00:00:00.000000003Z",
            )
        },
    )
    monkeypatch.setattr(store, "migrate_codex_authority", commit)

    recovered = harness.invoke(["codex", "login", "team", "--device-auth"])

    assert recovered.exit_code == ExitCode.SUCCESS
    current = store.read_saved(saved.account_id)
    assert current is not None
    subscription = managed_subscription(current)
    assert str(subscription.provider_identity) == "acct_current"
    assert str(subscription.generation) == "2026-07-21T00:00:00.000000003Z"
    assert not legacy.exists()
    assert (native_home / "auth.json").read_bytes() == native_auth
    events = (tmp_path / "events.jsonl").read_text()
    assert events.count('"method": "account/login/start"') == 1
    assert "https://auth.openai.com/codex/device" in stdout.getvalue()
    assert "SAFE-CODE" in stdout.getvalue()


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
