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
from typer.testing import CliRunner

from sidekick_usages import cli
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
from sidekick_usages.core.types import AccountLabel, ProviderId, RefreshStatus
from sidekick_usages.errors import AuthError, RateLimitError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import Provider
from sidekick_usages.store import AccountStore
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Iterable[UsageReport | Exception] = (),
        detected: DetectedCredentials | None = None,
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
    ) -> DetectedCredentials | None:
        """:return: Scripted detected local credentials."""
        self.credential_homes.append(credential_home)
        return self.detected

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

    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Optionally mutate account like a successful provider refresh."""
        del http
        self.refresh_calls += 1
        if not self.refresh_ok:
            return False
        credentials = account.credentials
        if isinstance(credentials, CodexCredentials):
            account.credentials = replace(
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
            account.credentials = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                expiry=KnownExpiry(REFERENCE_TIME + timedelta(seconds=60)),
            )
        return True

    def run_setup_token(self) -> str | None:
        """:return: None; not used by these tests."""
        return None


def _report() -> UsageReport:
    """Build a one-window usage report."""
    return UsageReport(
        windows=(UsageWindow(name="5h", utilization=0.1, resets_at=None),),
        plan="team",
    )


def _store(tmp_path: Path, account: Account) -> AccountStore:
    """Build a temp account store containing one account."""
    store = AccountStore(make_application_paths(tmp_path).accounts)
    store.upsert(account)
    return store


def _store_many(tmp_path: Path, accounts: Iterable[Account]) -> AccountStore:
    """Build a temp account store containing multiple accounts."""
    store = AccountStore(make_application_paths(tmp_path).accounts)
    for account in accounts:
        store.upsert(account)
    return store


def _empty_store(tmp_path: Path) -> AccountStore:
    """Build an empty temp account store."""
    return AccountStore(make_application_paths(tmp_path).accounts)


def _install_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    account: Account,
    *,
    clock: Clock | None = None,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context for refresh-flow tests."""
    paths = make_application_paths(tmp_path)
    store = _store(tmp_path, account)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=clock or FixedClock(),
        )
    )
    return store, stdout, stderr


def _install_many_ctx(
    tmp_path: Path,
    providers: dict[ProviderId, Provider],
    accounts: Iterable[Account],
    *,
    clock: Clock | None = None,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with multiple saved accounts."""
    paths = make_application_paths(tmp_path)
    store = _store_many(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers=providers,
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=clock or FixedClock(),
        )
    )
    return store, stdout, stderr


def _install_empty_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    *,
    clock: Clock | None = None,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with no saved accounts."""
    paths = make_application_paths(tmp_path)
    store = _empty_store(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=clock or FixedClock(),
        )
    )
    return store, stdout, stderr


def _codex_cache_dir(tmp_path: Path) -> Path:
    """Return the injected private Codex root for a test context."""
    return make_application_paths(tmp_path).private_codex.canonical


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
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

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
    acct = _codex_acct()
    detected = _detected(
        access_token="eyJ-current.access.sig",
        provider_id="codex",
        refresh_token="refresh-current",
        expiry=_seconds(1_770_000_000),
        plan="pro",
        provider_account_id="acct_current",
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.provider_account_id == "acct_current"


def test_refresh_command_imports_default_codex_login_to_private_cache(
    tmp_path: Path,
) -> None:
    """Refreshing a Codex label reads default login and caches a copy."""
    cache_dir = _codex_cache_dir(tmp_path)
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
    store, _, _ = _install_ctx(
        tmp_path,
        provider,
        acct,
        clock=clock,
    )

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

    assert result.exit_code == 0
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(cache_dir / "team")
    assert saved.codex_id_token == "id-token-current"
    assert saved.codex_last_refresh == "2026-06-12T00:00:00Z"
    assert saved.last_refresh_at == REFERENCE_TIME
    cached = json.loads((cache_dir / "team" / "auth.json").read_text())
    assert cached["tokens"]["access_token"] == "eyJ-current.access.sig"
    assert cached["tokens"]["refresh_token"] == "refresh-current"
    assert cached["tokens"]["id_token"] == "id-token-current"
    assert cached["tokens"]["account_id"] == "acct_current"
    assert clock.calls == 1


def test_refresh_command_from_codex_home_overrides_saved_home(
    tmp_path: Path,
) -> None:
    """Manual refresh can explicitly read a non-default source home."""
    cache_dir = _codex_cache_dir(tmp_path)
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
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(
        cli.app,
        ["refresh", "team", "--from-codex-home", str(source_home)],
    )

    assert result.exit_code == 0
    assert provider.credential_homes == [source_home]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(cache_dir / "team")


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
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

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
    )
    provider = _FakeProvider(
        detected=detected,
        provider_id="codex",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(
        cli.app,
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
    store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
        clock=clock,
    )

    result = CliRunner().invoke(cli.app, ["refresh", "--all", "--quiet"])

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
    _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
        clock=clock,
    )

    result = CliRunner().invoke(cli.app, ["refresh", "--all"])

    assert result.exit_code == 0
    assert provider.refresh_calls == 0
    assert clock.calls == 1
    clock.calls = 0

    forced = CliRunner().invoke(cli.app, ["refresh", "--all", "--force"])

    assert forced.exit_code == 0
    assert provider.refresh_calls == 1
    assert clock.calls == 1


def test_refresh_all_persists_failed_refresh_diagnostic(
    tmp_path: Path,
) -> None:
    """Rejected refresh tokens are recorded for doctor and exit 1."""
    acct = _acct(expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)))
    provider = _FakeProvider(refresh_ok=False)
    store, stdout, _ = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
    )

    result = CliRunner().invoke(cli.app, ["refresh", "--all", "--quiet"])

    assert result.exit_code == 1
    assert "team" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert saved.last_refresh_status is RefreshStatus.FAILED
    assert saved.last_refresh_error is not None


def test_expired_account_refreshes_before_first_fetch(tmp_path: Path) -> None:
    """Known-expired accounts refresh before spending a usage request."""
    acct = _acct(expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)))
    provider = _FakeProvider(fetch_results=[_report()])
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is True

    saved = store.get("team")
    assert saved is not None
    assert provider.refresh_calls == 1
    assert provider.fetch_tokens == ["sk-ant-oat01-refreshed"]
    assert saved.access_token == "sk-ant-oat01-refreshed"


def test_expired_codex_account_refreshes_before_first_fetch(
    tmp_path: Path,
) -> None:
    """Codex uses seconds-based expiry for proactive refresh."""
    acct = _codex_acct(
        expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
    )
    provider = _FakeProvider(fetch_results=[_report()], provider_id="codex")
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is True

    saved = store.get("team")
    assert saved is not None
    assert provider.refresh_calls == 1
    assert provider.fetch_tokens == ["sk-ant-oat01-refreshed"]
    assert saved.access_token == "sk-ant-oat01-refreshed"
    assert saved.provider_account_id == "acct_refreshed"


def test_auth_error_refreshes_and_retries_unknown_expiry(
    tmp_path: Path,
) -> None:
    """Unknown-expiry accounts still refresh after a 401 response."""
    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired"), _report()]
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is True

    saved = store.get("team")
    assert saved is not None
    assert provider.refresh_calls == 1
    assert provider.fetch_tokens == [
        "sk-ant-oat01-old",
        "sk-ant-oat01-refreshed",
    ]
    assert saved.access_token == "sk-ant-oat01-refreshed"


def test_successful_fetch_persists_reported_plan(tmp_path: Path) -> None:
    """Provider-reported plans are saved for future account headers."""
    acct = _codex_acct(plan="unknown")
    provider = _FakeProvider(
        fetch_results=[
            UsageReport(
                windows=(
                    UsageWindow(
                        name="5h",
                        utilization=0.1,
                        resets_at=None,
                    ),
                ),
                plan="pro",
            )
        ],
        provider_id="codex",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is True

    saved = store.get("team")
    assert saved is not None
    assert saved.plan == "pro"


def test_successful_fetch_persists_provider_account_id(tmp_path: Path) -> None:
    """Provider-filled account ids are saved for older Codex entries."""
    acct = _codex_acct(plan="pro")
    provider = _FakeProvider(
        fetch_results=[
            UsageReport(
                windows=(
                    UsageWindow(
                        name="5h",
                        utilization=0.1,
                        resets_at=None,
                    ),
                ),
                plan="unknown",
            )
        ],
        provider_id="codex",
        provider_account_id_on_fetch="acct_from_token",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)
    store.save()

    assert cli._fetch_and_render(acct) is True

    saved = (
        AccountStore(make_application_paths(tmp_path).accounts)
        .load()
        .get("team")
    )
    assert saved is not None
    assert saved.provider_account_id == "acct_from_token"


def test_retry_rate_limit_after_refresh_is_rendered_per_account(
    tmp_path: Path,
) -> None:
    """A retry failure after refresh returns False instead of escaping."""
    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[
            AuthError("Token expired"),
            RateLimitError("Rate limited", retry_after=10),
        ]
    )
    _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is False


def test_auth_error_does_not_adopt_current_local_credentials(
    tmp_path: Path,
) -> None:
    """Failed refresh does not blindly copy the current local Claude login."""
    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        detected=_detected(access_token="sk-ant-oat01-current"),
        refresh_ok=False,
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is False

    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"


def test_add_codex_uses_default_login_and_writes_private_cache(
    tmp_path: Path,
) -> None:
    """Adding Codex from default login copies auth into private cache."""
    cache_dir = _codex_cache_dir(tmp_path)
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
    store, _, _ = _install_empty_ctx(tmp_path, provider)

    result = CliRunner().invoke(
        cli.app,
        ["add", "codex", "--label", "team"],
    )

    assert result.exit_code == 0
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(cache_dir / "team")
    assert saved.provider_account_id == "acct_current"
    assert saved.codex_id_token == "id-token-current"
    assert saved.codex_last_refresh == "2026-06-12T00:00:00Z"
    cached = json.loads((cache_dir / "team" / "auth.json").read_text())
    assert cached["tokens"]["access_token"] == "eyJ-current.access.sig"


def test_codex_login_runs_plain_cli_and_imports_private_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex-login leaves global ~/.codex as source for other apps."""
    cache_dir = _codex_cache_dir(tmp_path)
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
    store, _, _ = _install_empty_ctx(tmp_path, provider)
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

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    result = CliRunner().invoke(
        cli.app,
        ["codex-login", "team"],
    )

    assert result.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["argv"] == ["codex", "login"]
    assert calls[0]["check"] is True
    assert "env" not in calls[0]
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(cache_dir / "team")
    assert saved.provider_account_id == "acct_current"
    cached = json.loads((cache_dir / "team" / "auth.json").read_text())
    assert cached["tokens"]["id_token"] == "id-token-current"


def test_codex_export_writes_saved_credentials_to_home(
    tmp_path: Path,
) -> None:
    """Saved Codex credentials can be exported into an isolated home."""
    codex_home = tmp_path / "codex-team"
    acct = _codex_acct(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        provider_account_id="acct_current",
        id_token="id-token-current",
        last_refresh="2026-06-12T00:00:00Z",
    )
    provider = _FakeProvider(provider_id="codex")
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(
        cli.app,
        ["codex-export", "team", "--codex-home", str(codex_home)],
    )

    assert result.exit_code == 0
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
    assert saved.codex_home == str(codex_home)


def test_check_renders_grouped_overview(tmp_path, monkeypatch):
    """`check` collects successes and prints one grouped overview."""
    monkeypatch.setattr(cli, "claude_lifetime_output", lambda: (1, None))
    acct = _acct(plan="max")
    report = UsageReport(
        windows=(
            UsageWindow(
                name="5h",
                utilization=0.1,
                resets_at=REFERENCE_TIME + timedelta(hours=3, minutes=50),
            ),
        ),
        plan="team",
    )
    provider = _FakeProvider(fetch_results=[report])
    clock = FixedClock()
    _, stdout, _ = _install_ctx(tmp_path, provider, acct, clock=clock)

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 0
    assert "CLAUDE" in stdout.getvalue()
    assert "3h 50m" in stdout.getvalue()
    assert clock.calls == 1
