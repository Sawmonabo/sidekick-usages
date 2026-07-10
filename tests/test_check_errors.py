"""Command-boundary tests for typed usage-check outcomes."""

import io
import re
from collections.abc import Callable, Iterable
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http import HttpClient
from sidekick_usages.lifetime import (
    LifetimeFailure,
    LifetimeFailureKind,
    LifetimeResult,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.providers.base import Provider
from tests.test_support import (
    FixedClock,
    make_account_store,
    make_application_paths,
)


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = ProviderId.CODEX
    display_name = "Codex CLI"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Iterable[UsageReport | Exception] = (),
        refresh_ok: bool = True,
        provider_id: str = "codex",
    ) -> None:
        self.id = ProviderId(provider_id)
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self.fetch_results = list(fetch_results)
        self.refresh_ok = refresh_ok
        self.fetch_calls = 0

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
        self.fetch_calls += 1
        if not self.fetch_results:
            return UsageReport(
                windows=(UsageWindow("5h", 0.0, None),),
                plan="pro",
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
    label: str = "long.account.name@example.test", provider_id: str = "codex"
) -> Account:
    provider = ProviderId(provider_id)
    credentials = (
        CodexCredentials(access_token="tok")
        if provider is ProviderId.CODEX
        else ClaudeCredentials(access_token="tok")
    )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="pro",
    )


def _install_ctx(
    tmp_path: Path,
    providers: tuple[_FakeProvider, ...],
    accounts: tuple[Account, ...],
    *,
    lifetime_sources: dict[
        ProviderId,
        Callable[[], LifetimeResult],
    ]
    | None = None,
    width: int = 200,
) -> tuple[AccountStore, io.StringIO, io.StringIO]:
    paths = make_application_paths(tmp_path)
    store = make_account_store(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={provider.id: provider for provider in providers},
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources=lifetime_sources or {},
            console=Console(file=stdout, width=width, force_terminal=False),
            err_console=Console(file=stderr, force_terminal=False),
            clock=FixedClock(),
        )
    )
    return store, stdout, stderr


def test_check_renders_partial_success_and_typed_auth_recovery(
    tmp_path: Path,
) -> None:
    """Success and failure remain visible in their provider panels."""
    claude = _FakeProvider(provider_id="claude")
    codex = _FakeProvider(
        fetch_results=[AuthError("Token expired")],
        refresh_ok=False,
    )
    _, stdout, _ = _install_ctx(
        tmp_path,
        (claude, codex),
        (
            _acct("claude-account", "claude"),
            _acct("my work account"),
        ),
    )

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    out = stdout.getvalue()
    assert "╭─ CLAUDE · 1 account ─" in out
    assert "╭─ CODEX · 1 account ─" in out
    assert "⚠ token expired" in out
    assert "Log in to Codex CLI again, then run:" in out
    assert "sidekick-usages refresh 'my work account'" in out


def test_check_provider_filter_uses_only_selected_accounts(
    tmp_path: Path,
) -> None:
    claude = _FakeProvider(provider_id="claude")
    codex = _FakeProvider()
    _, stdout, _ = _install_ctx(
        tmp_path,
        (claude, codex),
        (_acct("claude", "claude"), _acct("codex")),
    )

    result = CliRunner().invoke(cli.app, ["--only", "codex", "check"])

    assert result.exit_code == ExitCode.SUCCESS
    assert claude.fetch_calls == 0
    assert codex.fetch_calls == 1
    out = stdout.getvalue()
    assert "╭─ CODEX · 1 account ─" in out
    assert "╭─ CLAUDE" not in out


def test_lifetime_failure_renders_and_forces_system_error(
    tmp_path: Path,
) -> None:
    acct = _acct()
    provider = _FakeProvider(
        fetch_results=[TransientError("provider unavailable")]
    )
    _, stdout, _ = _install_ctx(
        tmp_path,
        (provider,),
        (acct,),
        lifetime_sources={
            ProviderId.CODEX: lambda: LifetimeFailure(
                LifetimeFailureKind.SOURCE_UNREADABLE
            )
        },
    )

    result = CliRunner().invoke(cli.app, ["check"])

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    out = stdout.getvalue()
    assert "provider unavailable" in out
    assert "lifetime source unreadable" in out
