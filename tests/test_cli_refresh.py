"""CLI refresh-flow regression tests."""

import io
import json
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.cli.token_input import TokenInput
from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.codex import private_codex_home
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.claude import ClaudeSetupToken
from sidekick_usages.providers.claude import provider as claude_provider_module
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.codex import auth as codex_auth_module
from sidekick_usages.providers.codex.provider import CodexProvider
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    make_account_store_with_private,
    make_app_context,
    make_application_paths,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_default_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent CLI tests from reading the developer's active Codex login."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-default"))


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Iterable[UsageReport | Exception] = (),
        detected: DetectedCredentials | ProviderFailure | None = None,
        refresh_ok: bool = True,
        provider_id: str = "claude",
        provider_account_id_on_fetch: str | None = None,
    ) -> None:
        """:param fetch_results: Values or exceptions returned in order."""
        self.id = ProviderId(provider_id)
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self.fetch_results = list(fetch_results)
        self.detected = detected
        self.refresh_ok = refresh_ok
        self.provider_account_id_on_fetch = provider_account_id_on_fetch
        self.fetch_tokens: list[str] = []
        self.refresh_calls = 0
        self.credential_homes: list[Path | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """:return: Scripted detected local credentials."""
        self.credential_homes.append(credential_home)
        if self.detected is not None:
            return self.detected
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        credentials = (
            ClaudeCredentials(access_token=token)
            if self.id is ProviderId.CLAUDE
            else CodexCredentials(access_token=token)
        )
        return DetectedCredentials(credentials=credentials)

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Return or raise the next scripted fetch result."""
        del http
        self.fetch_tokens.append(account.access_token)
        if self.provider_account_id_on_fetch is not None:
            credentials = account.credentials
            assert isinstance(credentials, CodexCredentials)
            account.credentials = replace(
                credentials,
                account_id=self.provider_account_id_on_fetch,
            )
        if not self.fetch_results:
            return _report()
        result = self.fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Return one scripted immutable credential refresh."""
        del http
        self.refresh_calls += 1
        if not self.refresh_ok:
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test refresh rejected.",
            )
        credentials = account.credentials
        if isinstance(credentials, CodexCredentials):
            updated = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                expiry=KnownExpiry(
                    REFERENCE_TIME.replace(microsecond=0)
                    + timedelta(seconds=60)
                ),
                account_id="acct_refreshed",
            )
        else:
            updated = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                expiry=KnownExpiry(REFERENCE_TIME + timedelta(seconds=60)),
            )
        return RefreshSuccess(credentials=updated)


def _report() -> UsageReport:
    """Build a one-window usage report."""
    return UsageReport(
        windows=(UsageWindow(name="5h", utilization=0.1, resets_at=None),),
        plan="team",
    )


def _install_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    account: Account,
    *,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context for refresh-flow tests."""
    store, private = make_account_store_with_private(tmp_path, (account,))
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    providers: dict[ProviderId, Provider] = {provider.id: provider}
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
        ),
    )
    return harness, store, stdout, stderr


def _install_many_ctx(
    tmp_path: Path,
    providers: dict[ProviderId, Provider],
    accounts: Iterable[Account],
    *,
    clock: Clock | None = None,
    claude_setup_token: ClaudeSetupToken | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with multiple saved accounts."""
    store, private = make_account_store_with_private(tmp_path, accounts)
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
            claude_setup_token=claude_setup_token,
        ),
    )
    return harness, store, stdout, stderr


def _install_empty_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    *,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with no saved accounts."""
    store, private = make_account_store_with_private(tmp_path)
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    providers: dict[ProviderId, Provider] = {provider.id: provider}
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
        ),
    )
    return harness, store, stdout, stderr


def _codex_cache_dir(tmp_path: Path) -> Path:
    """Return the injected private Codex root for a test context."""
    return make_application_paths(tmp_path).private_codex.canonical


def _codex_cache_home(tmp_path: Path, label: str = "team") -> Path:
    """Return the deterministic collision-resistant private bundle path."""
    root = make_application_paths(tmp_path).private_codex.canonical
    return private_codex_home(root, label)


def _acct(
    *,
    access_token: str = "sk-ant-oat01-old",
    refresh_token: str | None = "refresh-old",
    expiry: Expiry | None = None,
    scopes: tuple[str, ...] | None = None,
    plan: str = "team",
) -> Account:
    """Build a Claude account fixture."""
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry or UnknownExpiry(),
            scopes=scopes,
        ),
        plan=plan,
    )


def _codex_acct(
    *,
    access_token: str = "sk-ant-oat01-old",
    refresh_token: str | None = "refresh-old",
    expiry: Expiry | None = None,
    plan: str = "team",
    codex_home: str | None = None,
    provider_account_id: str | None = None,
    id_token: str | None = None,
    last_refresh: str | None = None,
) -> Account:
    """Build a Codex account fixture."""
    return Account(
        label=AccountLabel("team"),
        credentials=CodexCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry or UnknownExpiry(),
            account_id=provider_account_id,
            auth_home=codex_home,
            id_token=id_token,
            auth_last_refresh=last_refresh,
        ),
        plan=plan,
    )


def _detected(
    *,
    access_token: str,
    provider_id: str = "claude",
    refresh_token: str | None = None,
    expiry: Expiry | None = None,
    plan: str = "unknown",
    scopes: tuple[str, ...] | None = None,
    provider_account_id: str | None = None,
    id_token: str | None = None,
    last_refresh: str | None = None,
) -> DetectedCredentials:
    """Build one provider-compatible detected credential result."""
    provider = ProviderId(provider_id)
    expiry_value = expiry or UnknownExpiry()
    credentials = (
        ClaudeCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry_value,
            scopes=scopes,
        )
        if provider is ProviderId.CLAUDE
        else CodexCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry_value,
            account_id=provider_account_id,
            id_token=id_token,
            auth_last_refresh=last_refresh,
        )
    )
    return DetectedCredentials(credentials=credentials, plan=plan)


def _seconds(value: int) -> KnownExpiry:
    """Build a strict whole-second expiry fixture."""
    return KnownExpiry(_EPOCH + timedelta(seconds=value))


def _milliseconds(value: int) -> KnownExpiry:
    """Build a strict whole-millisecond expiry fixture."""
    return KnownExpiry(_EPOCH + timedelta(milliseconds=value))


def test_refresh_command_persists_detected_empty_scopes(
    tmp_path: Path,
) -> None:
    """Manual refresh can clear stale scope metadata with ``[]``."""
    acct = _acct(scopes=("user:profile",))
    provider = _FakeProvider(
        detected=_detected(
            access_token="sk-ant-oat01-current",
            refresh_token="refresh-current",
            expiry=_milliseconds(1_770_000_000_000),
            plan="team",
            scopes=(),
        )
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(["refresh", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-current"
    assert saved.refresh_token == "refresh-current"
    assert saved.scopes == ()


def test_refresh_command_persists_detected_provider_account_id(
    tmp_path: Path,
) -> None:
    """Manual refresh records the Codex account id used by usage fetch."""
    acct = _codex_acct(provider_account_id="acct_current")
    detected = _detected(
        access_token="eyJ-current.access.sig",
        provider_id="codex",
        refresh_token="refresh-current",
        expiry=_seconds(1_770_000_000),
        plan="pro",
        provider_account_id="acct_current",
        id_token="id-token-current",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(["refresh", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.provider_account_id == "acct_current"


def test_refresh_command_imports_default_codex_login_to_private_bundle(
    tmp_path: Path,
) -> None:
    """Refreshing a Codex label reads default login and caches a copy."""
    old_home = tmp_path / "old-external-home"
    acct = _codex_acct(
        codex_home=str(old_home),
        provider_account_id="acct_current",
    )
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
    clock = FixedClock()
    harness, store, _, _ = _install_ctx(
        tmp_path,
        provider,
        acct,
        clock=clock,
    )

    result = harness.invoke(["refresh", "team"])

    assert result.exit_code == 0
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    cache_home = _codex_cache_home(tmp_path)
    assert saved.codex_home == str(cache_home)
    assert saved.codex_id_token == "id-token-current"
    assert saved.codex_last_refresh == "2026-06-12T00:00:00Z"
    assert saved.last_refresh_at == REFERENCE_TIME
    cached = json.loads((cache_home / "auth.json").read_text())
    assert cached["tokens"]["access_token"] == "eyJ-current.access.sig"
    assert cached["tokens"]["refresh_token"] == "refresh-current"
    assert cached["tokens"]["id_token"] == "id-token-current"
    assert cached["tokens"]["account_id"] == "acct_current"
    assert clock.calls == 1


def test_refresh_command_from_codex_home_overrides_saved_home(
    tmp_path: Path,
) -> None:
    """Manual refresh can explicitly read a non-default source home."""
    old_home = tmp_path / "codex-old"
    source_home = tmp_path / "codex-source"
    acct = _codex_acct(
        codex_home=str(old_home),
        provider_account_id="acct_current",
    )
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
    harness, store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(
        ["refresh", "team", "--from-codex-home", str(source_home)],
    )

    assert result.exit_code == 0
    assert provider.credential_homes == [source_home]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(_codex_cache_home(tmp_path))


def test_refresh_command_rejects_provider_account_id_mismatch(
    tmp_path: Path,
) -> None:
    """Manual refresh refuses to copy the wrong Codex login into a label."""
    acct = _codex_acct(provider_account_id="acct_saved")
    detected = _detected(
        access_token="eyJ-current.access.sig",
        provider_id="codex",
        refresh_token="refresh-current",
        expiry=_seconds(1_770_000_000),
        plan="pro",
        provider_account_id="acct_current",
        id_token="id-token-current",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(["refresh", "team"])

    assert result.exit_code == 1
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert saved.refresh_token == "refresh-old"
    assert saved.provider_account_id == "acct_saved"


def test_refresh_command_replace_identity_allows_provider_account_id_mismatch(
    tmp_path: Path,
) -> None:
    """Explicit replacement recovers a label that already has bad identity."""
    acct = _codex_acct(provider_account_id="acct_saved")
    detected = _detected(
        access_token="eyJ-current.access.sig",
        provider_id="codex",
        refresh_token="refresh-current",
        expiry=_seconds(1_770_000_000),
        plan="pro",
        provider_account_id="acct_current",
        id_token="id-token-current",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = harness.invoke(
        ["refresh", "team", "--replace-identity"],
    )

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "eyJ-current.access.sig"
    assert saved.refresh_token == "refresh-current"
    assert saved.provider_account_id == "acct_current"


def test_refresh_all_refreshes_due_tokens_without_detecting_local_credentials(
    tmp_path: Path,
) -> None:
    """Bulk maintenance refresh uses saved refresh tokens only."""
    acct = _acct(expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)))
    provider = _FakeProvider(
        detected=_detected(access_token="sk-ant-oat01-local")
    )
    clock = FixedClock()
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
        clock=clock,
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == 0
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""
    assert provider.refresh_calls == 1
    assert provider.credential_homes == []
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-refreshed"
    assert saved.last_refresh_at == REFERENCE_TIME
    assert saved.last_refresh_status is RefreshStatus.OK
    assert saved.last_refresh_error is None


def test_refresh_all_skips_fresh_tokens_unless_forced(
    tmp_path: Path,
) -> None:
    """Bulk maintenance avoids needless refreshes until forced."""
    acct = _acct(expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)))
    provider = _FakeProvider()
    clock = FixedClock()
    harness, _, _, _ = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
        clock=clock,
    )

    result = harness.invoke(["refresh", "--all"])

    assert result.exit_code == 0
    assert provider.refresh_calls == 0
    assert clock.calls == 1
    clock.calls = 0

    forced = harness.invoke(["refresh", "--all", "--force"])

    assert forced.exit_code == 0
    assert provider.refresh_calls == 1
    assert clock.calls == 1


def test_refresh_all_persists_failed_refresh_diagnostic(
    tmp_path: Path,
) -> None:
    """Rejected refresh tokens are recorded for doctor and exit 1."""
    acct = _acct(expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)))
    provider = _FakeProvider(refresh_ok=False)
    harness, store, stdout, _ = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == 1
    assert "team" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert saved.last_refresh_status is RefreshStatus.FAILED
    assert saved.last_refresh_error is not None


def test_add_codex_uses_default_login_and_writes_private_bundle(
    tmp_path: Path,
) -> None:
    """Adding Codex from the default login saves a private auth bundle."""
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
    harness, store, _, _ = _install_empty_ctx(tmp_path, provider)

    result = harness.invoke(["add", "codex", "--label", "team"])

    assert result.exit_code == 0
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    cache_home = _codex_cache_home(tmp_path)
    assert saved.codex_home == str(cache_home)
    assert saved.provider_account_id == "acct_current"
    assert saved.codex_id_token == "id-token-current"
    assert saved.codex_last_refresh == "2026-06-12T00:00:00Z"
    cached = json.loads((cache_home / "auth.json").read_text())
    assert cached["tokens"]["access_token"] == "eyJ-current.access.sig"


@pytest.mark.parametrize("failure_kind", list(ProviderFailureKind))
def test_add_prompts_only_for_missing_local_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: ProviderFailureKind,
) -> None:
    """Only an absent local login authorizes interactive token fallback."""
    provider = _FakeProvider(
        detected=ProviderFailure(
            provider_id=ProviderId.CLAUDE,
            kind=failure_kind,
            message=f"Synthetic {failure_kind} credential failure.",
        )
    )
    harness, store, _, _ = _install_empty_ctx(tmp_path, provider)
    prompt_calls = 0

    def read_token(
        _input: TokenInput,
        prompt: str = "Paste OAuth token",
    ) -> str:
        nonlocal prompt_calls
        del prompt
        prompt_calls += 1
        return "sk-ant-oat01-prompted-test-token"

    monkeypatch.setattr(TokenInput, "read", read_token)

    result = harness.invoke(["add", "claude", "--label", "prompted"])

    saved = store.get("prompted")
    if failure_kind is ProviderFailureKind.MISSING:
        assert result.exit_code == ExitCode.SUCCESS
        assert prompt_calls == 1
        assert saved is not None
        assert saved.access_token == "sk-ant-oat01-prompted-test-token"
    else:
        assert result.exit_code == ExitCode.MANUAL_ACTION
        assert prompt_calls == 0
        assert saved is None


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
