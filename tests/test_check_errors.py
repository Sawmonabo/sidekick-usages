"""Tests for per-account fetch errors recorded and rendered in panels."""

import io
import re
from collections.abc import Iterable
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import DetectedCredentials, Provider
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account, AccountStore


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = "codex"
    display_name = "Codex CLI"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Iterable[UsageReport | Exception] = (),
        refresh_ok: bool = True,
        provider_id: str = "codex",
    ) -> None:
        self.id = provider_id
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self.fetch_results = list(fetch_results)
        self.refresh_ok = refresh_ok

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> DetectedCredentials | None:
        return None

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del http
        if not self.fetch_results:
            return UsageReport(
                windows=[UsageWindow("5h", 0.0, None)],
                plan="pro",
                raw={},
            )
        result = self.fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        del http
        return self.refresh_ok

    def run_setup_token(self) -> str | None:
        return None


def _acct(
    label: str = "a.sawmon@ymail.com", provider_id: str = "codex"
) -> Account:
    return Account(
        label=label,
        provider_id=provider_id,
        access_token="tok",
        plan="pro",
    )


def _install_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    account: Account,
    *,
    width: int = 80,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    store = AccountStore(tmp_path / "accounts.json")
    store.upsert(account)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            console=Console(file=stdout, width=width, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
        )
    )
    return store, stdout, stderr


def test_auth_failure_is_recorded_not_printed(tmp_path: Path) -> None:
    """A 401 with refresh_ok=False records a FetchFailure, prints nothing."""
    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        refresh_ok=False,
    )
    _, stdout, _ = _install_ctx(tmp_path, provider, acct)

    result = cli._fetch_and_render(acct)

    assert result is False
    failures = cli._get_ctx().failures
    assert len(failures) == 1
    _, failure = failures[0]
    assert failure.status == "token expired"
    assert failure.detail[-1] == f"sidekick-usages refresh {acct.label}"
    # Nothing printed at fetch time — output is empty
    assert stdout.getvalue() == ""


def test_refresh_command_quotes_spaced_label(tmp_path: Path) -> None:
    """A label with spaces is shell-quoted in the recorded refresh command."""
    acct = _acct(label="my work acct")
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        refresh_ok=False,
    )
    _install_ctx(tmp_path, provider, acct)

    result = cli._fetch_and_render(acct)

    assert result is False
    failures = cli._get_ctx().failures
    assert len(failures) == 1
    _, failure = failures[0]
    assert failure.detail[-1] == "sidekick-usages refresh 'my work acct'"


def test_generic_error_is_recorded(tmp_path: Path) -> None:
    """A transient error records a FetchFailure with the message."""
    acct = _acct()
    provider = _FakeProvider(fetch_results=[TransientError("boom")])
    _, stdout, _ = _install_ctx(tmp_path, provider, acct)

    result = cli._fetch_and_render(acct)

    assert result is False
    failures = cli._get_ctx().failures
    assert len(failures) == 1
    _, failure = failures[0]
    assert failure.status == "error"
    assert "boom" in failure.detail
    assert stdout.getvalue() == ""


def test_check_renders_error_in_panel(tmp_path: Path, monkeypatch) -> None:
    """Full check: always-401 account exits 1 and renders error in panel."""
    import sidekick_usages.cli as cli_mod  # noqa: PLC0415

    monkeypatch.setattr(cli_mod, "claude_lifetime_output", lambda: (1, None))
    monkeypatch.setattr(cli_mod, "codex_lifetime_output", lambda: (1, None))

    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        refresh_ok=False,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    store = AccountStore(tmp_path / "accounts.json")
    store.upsert(acct)
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider},
            console=Console(file=stdout, width=200, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
        )
    )

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == 1
    out = stdout.getvalue()
    assert "⚠ token expired" in out
    assert "sidekick-usages refresh" in out
    # The error appears INSIDE the panel — after the top strip
    assert out.index("sidekick usages") < out.index("token expired")
