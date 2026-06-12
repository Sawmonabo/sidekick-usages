"""CLI refresh-flow regression tests."""

import io
import json
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.errors import AuthError, ForbiddenError, RateLimitError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import DetectedCredentials, Provider
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account, AccountStore


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = "claude"
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
        self.id = provider_id
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
            account.provider_account_id = self.provider_account_id_on_fetch
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
        account.access_token = "sk-ant-oat01-refreshed"
        account.refresh_token = "refresh-new"
        if self.id == "codex":
            account.expires_at = int(time.time()) + 60
            account.provider_account_id = "acct_refreshed"
        else:
            account.expires_at = int(time.time() * 1000) + 60_000
        return True

    def run_setup_token(self) -> str | None:
        """:return: None; not used by these tests."""
        return None


def _report() -> UsageReport:
    """Build a one-window usage report."""
    return UsageReport(
        windows=[UsageWindow(name="5h", utilization=0.1, resets_at=None)],
        plan="team",
        raw={},
    )


def _store(tmp_path: Path, account: Account) -> AccountStore:
    """Build a temp account store containing one account."""
    store = AccountStore(tmp_path / "accounts.json")
    store.upsert(account)
    return store


def _store_many(tmp_path: Path, accounts: Iterable[Account]) -> AccountStore:
    """Build a temp account store containing multiple accounts."""
    store = AccountStore(tmp_path / "accounts.json")
    for account in accounts:
        store.upsert(account)
    return store


def _empty_store(tmp_path: Path) -> AccountStore:
    """Build an empty temp account store."""
    return AccountStore(tmp_path / "accounts.json")


def _install_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    account: Account,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context for refresh-flow tests."""
    store = _store(tmp_path, account)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
        )
    )
    return store, stdout, stderr


def _install_many_ctx(
    tmp_path: Path,
    providers: dict[str, Provider],
    accounts: Iterable[Account],
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with multiple saved accounts."""
    store = _store_many(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers=providers,
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
        )
    )
    return store, stdout, stderr


def _install_empty_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with no saved accounts."""
    store = _empty_store(tmp_path)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            console=Console(file=stdout, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
        )
    )
    return store, stdout, stderr


def _set_codex_cache_dir(tmp_path: Path, monkeypatch: Any) -> Path:
    """Point sidekick's Codex cache at a temp directory."""
    cache_dir = tmp_path / "sidekick-codex-cache"
    monkeypatch.setattr(cli, "CODEX_CACHE_DIR", cache_dir)
    return cache_dir


def _acct(
    *,
    access_token: str = "sk-ant-oat01-old",
    refresh_token: str | None = "refresh-old",
    expires_at: int | None = None,
    scopes: list[str] | None = None,
    provider_id: str = "claude",
    plan: str = "team",
    codex_home: str | None = None,
) -> Account:
    """Build an account fixture."""
    return Account(
        label="team",
        provider_id=provider_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan=plan,
        scopes=scopes,
        codex_home=codex_home,
    )


def test_refresh_command_persists_detected_empty_scopes(
    tmp_path: Path,
) -> None:
    """Manual refresh can clear stale scope metadata with ``[]``."""
    acct = _acct(scopes=["user:profile"])
    provider = _FakeProvider(
        detected=DetectedCredentials(
            access_token="sk-ant-oat01-current",
            refresh_token="refresh-current",
            expires_at=1770000000000,
            plan="team",
            scopes=[],
        )
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

    assert result.exit_code == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-current"
    assert saved.refresh_token == "refresh-current"
    assert saved.scopes == []


def test_refresh_command_persists_detected_provider_account_id(
    tmp_path: Path,
) -> None:
    """Manual refresh records the Codex account id used by usage fetch."""
    acct = _acct(provider_id="codex")
    detected = DetectedCredentials(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        expires_at=1_770_000_000,
        plan="pro",
    )
    detected.provider_account_id = "acct_current"
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
    monkeypatch: Any,
) -> None:
    """Refreshing a Codex label reads default login and caches a copy."""
    cache_dir = _set_codex_cache_dir(tmp_path, monkeypatch)
    old_home = tmp_path / "old-external-home"
    acct = _acct(provider_id="codex", codex_home=str(old_home))
    acct.provider_account_id = "acct_current"
    provider = _FakeProvider(
        detected=DetectedCredentials(
            access_token="eyJ-current.access.sig",
            refresh_token="refresh-current",
            expires_at=1_770_000_000,
            plan="pro",
            provider_account_id="acct_current",
            id_token="id-token-current",
            last_refresh="2026-06-12T00:00:00Z",
        ),
        provider_id="codex",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    result = CliRunner().invoke(cli.app, ["refresh", "team"])

    assert result.exit_code == 0
    assert provider.credential_homes == [None]
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home == str(cache_dir / "team")
    assert saved.codex_id_token == "id-token-current"
    assert saved.codex_last_refresh == "2026-06-12T00:00:00Z"
    cached = json.loads((cache_dir / "team" / "auth.json").read_text())
    assert cached["tokens"]["access_token"] == "eyJ-current.access.sig"
    assert cached["tokens"]["refresh_token"] == "refresh-current"
    assert cached["tokens"]["id_token"] == "id-token-current"
    assert cached["tokens"]["account_id"] == "acct_current"


def test_refresh_command_from_codex_home_overrides_saved_home(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Manual refresh can explicitly read a non-default source home."""
    cache_dir = _set_codex_cache_dir(tmp_path, monkeypatch)
    old_home = tmp_path / "codex-old"
    source_home = tmp_path / "codex-source"
    acct = _acct(provider_id="codex", codex_home=str(old_home))
    acct.provider_account_id = "acct_current"
    provider = _FakeProvider(
        detected=DetectedCredentials(
            access_token="eyJ-current.access.sig",
            refresh_token="refresh-current",
            expires_at=1_770_000_000,
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
    acct = _acct(provider_id="codex")
    acct.provider_account_id = "acct_saved"
    detected = DetectedCredentials(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        expires_at=1_770_000_000,
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
    acct = _acct(provider_id="codex")
    acct.provider_account_id = "acct_saved"
    detected = DetectedCredentials(
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
        expires_at=1_770_000_000,
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
    acct = _acct(expires_at=int(time.time() * 1000) - 1_000)
    provider = _FakeProvider(
        detected=DetectedCredentials(access_token="sk-ant-oat01-local")
    )
    store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {"claude": provider},
        [acct],
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
    assert saved.last_refresh_status == "ok"
    assert saved.last_refresh_error is None


def test_refresh_all_skips_fresh_tokens_unless_forced(
    tmp_path: Path,
) -> None:
    """Bulk maintenance avoids needless refreshes until forced."""
    acct = _acct(expires_at=int(time.time() * 1000) + 3_600_000)
    provider = _FakeProvider()
    _install_many_ctx(tmp_path, {"claude": provider}, [acct])

    result = CliRunner().invoke(cli.app, ["refresh", "--all"])

    assert result.exit_code == 0
    assert provider.refresh_calls == 0

    forced = CliRunner().invoke(cli.app, ["refresh", "--all", "--force"])

    assert forced.exit_code == 0
    assert provider.refresh_calls == 1


def test_refresh_all_persists_failed_refresh_diagnostic(
    tmp_path: Path,
) -> None:
    """Rejected refresh tokens are recorded for doctor and exit 1."""
    acct = _acct(expires_at=int(time.time() * 1000) - 1_000)
    provider = _FakeProvider(refresh_ok=False)
    store, stdout, _ = _install_many_ctx(
        tmp_path,
        {"claude": provider},
        [acct],
    )

    result = CliRunner().invoke(cli.app, ["refresh", "--all", "--quiet"])

    assert result.exit_code == 1
    assert "team" in stdout.getvalue()
    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert saved.last_refresh_status == "failed"
    assert saved.last_refresh_error is not None


def test_expired_account_refreshes_before_first_fetch(tmp_path: Path) -> None:
    """Known-expired accounts refresh before spending a usage request."""
    acct = _acct(expires_at=int(time.time() * 1000) - 1_000)
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
    acct = _acct(
        expires_at=int(time.time()) - 1,
        provider_id="codex",
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
    acct = _acct(expires_at=None)
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
    acct = _acct(provider_id="codex", plan="unknown")
    provider = _FakeProvider(
        fetch_results=[
            UsageReport(
                windows=[
                    UsageWindow(
                        name="5h",
                        utilization=0.1,
                        resets_at=None,
                    )
                ],
                plan="pro",
                raw={},
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
    acct = _acct(provider_id="codex", plan="pro")
    provider = _FakeProvider(
        fetch_results=[
            UsageReport(
                windows=[
                    UsageWindow(
                        name="5h",
                        utilization=0.1,
                        resets_at=None,
                    )
                ],
                plan="unknown",
                raw={},
            )
        ],
        provider_id="codex",
        provider_account_id_on_fetch="acct_from_token",
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)
    store.save()

    assert cli._fetch_and_render(acct) is True

    saved = AccountStore(tmp_path / "accounts.json").load().get("team")
    assert saved is not None
    assert saved.provider_account_id == "acct_from_token"


def test_retry_rate_limit_after_refresh_is_rendered_per_account(
    tmp_path: Path,
) -> None:
    """A retry failure after refresh returns False instead of escaping."""
    acct = _acct(expires_at=None)
    provider = _FakeProvider(
        fetch_results=[
            AuthError("Token expired"),
            RateLimitError("Rate limited", retry_after=10),
        ]
    )
    _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is False


def test_codex_bodyless_forbidden_retries_once(tmp_path: Path) -> None:
    """A transient Codex 403 with no API body is retried once."""
    acct = _acct(provider_id="codex", plan="pro")
    provider = _FakeProvider(
        fetch_results=[
            ForbiddenError("HTTP 403 Forbidden"),
            _report(),
        ],
        provider_id="codex",
    )
    _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is True

    assert provider.fetch_tokens == [
        "sk-ant-oat01-old",
        "sk-ant-oat01-old",
    ]


def test_auth_error_does_not_adopt_current_local_credentials(
    tmp_path: Path,
) -> None:
    """Failed refresh does not blindly copy the current local Claude login."""
    acct = _acct(expires_at=None)
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        detected=DetectedCredentials(access_token="sk-ant-oat01-current"),
        refresh_ok=False,
    )
    store, _, _ = _install_ctx(tmp_path, provider, acct)

    assert cli._fetch_and_render(acct) is False

    saved = store.get("team")
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"


def test_add_codex_uses_default_login_and_writes_private_cache(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Adding Codex from default login copies auth into private cache."""
    cache_dir = _set_codex_cache_dir(tmp_path, monkeypatch)
    provider = _FakeProvider(
        detected=DetectedCredentials(
            access_token="eyJ-current.access.sig",
            refresh_token="refresh-current",
            expires_at=1_770_000_000,
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
    monkeypatch: Any,
) -> None:
    """codex-login leaves global ~/.codex as source for other apps."""
    cache_dir = _set_codex_cache_dir(tmp_path, monkeypatch)
    provider = _FakeProvider(
        detected=DetectedCredentials(
            access_token="eyJ-current.access.sig",
            refresh_token="refresh-current",
            expires_at=1_770_000_000,
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
    acct = _acct(
        provider_id="codex",
        access_token="eyJ-current.access.sig",
        refresh_token="refresh-current",
    )
    acct.provider_account_id = "acct_current"
    acct.codex_id_token = "id-token-current"
    acct.codex_last_refresh = "2026-06-12T00:00:00Z"
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
