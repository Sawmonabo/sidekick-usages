"""Command-boundary tests for typed usage-check outcomes."""

import io
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from rich.console import Console

from sidekick_usages.cli.contexts.composition import compose_app_context
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    TokenActivityReading,
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.credentials.authorities import AuthenticatedSavedAccount
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.usage.activity import AccountTokenActivitySource
from tests.fakes.codex.app_server.executable import (
    configure_codex_logins,
    write_fake_codex,
)
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.managed import (
    managed_saved_account,
    seed_managed_accounts,
)
from tests.support.application import make_app_context
from tests.support.cli import CliHarness
from tests.support.persistence import make_account_store_with_private
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import FixedClock

_MANAGED_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_MANAGED_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_MANAGED_GENERATION = "2026-07-24T10:00:00.000000000Z"
_MANAGED_PROVIDER_IDENTITY = "acct-managed-check"


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = ProviderId.CODEX
    display_name = "Codex CLI"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Mapping[str, UsageReport | Exception] | None = None,
        refresh_ok: bool = True,
        provider_id: str = "codex",
    ) -> None:
        self.id = ProviderId(provider_id)
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self.fetch_results = dict(fetch_results or {})
        self.refresh_ok = refresh_ok
        self.fetch_calls = 0

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        del token
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.UNSUPPORTED,
            message="Manual test credentials are unsupported.",
        )

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del http
        self.fetch_calls += 1
        result = self.fetch_results.get(str(account.label))
        if result is None:
            return UsageReport(
                windows=(UsageWindow("5h", 0.0, None),),
                plan="pro",
            )
        if isinstance(result, Exception):
            raise result
        return result

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del http
        if not self.refresh_ok:
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test refresh rejected.",
            )
        return RefreshSuccess(credentials=account.credentials)


class _ScriptedAccountActivity(AccountTokenActivitySource):
    """Return account activity or raise its scripted operational error."""

    provider_id = ProviderId.CODEX

    def __init__(
        self,
        steps: Mapping[str, TokenActivityReading | TransientError],
    ) -> None:
        self.steps = dict(steps)
        self.calls: list[AccountLabel] = []

    def read(
        self,
        account: AuthenticatedSavedAccount,
        http: HttpClient,
    ) -> TokenActivityReading:
        del http
        runtime = account.lease.account
        self.calls.append(runtime.label)
        step = self.steps[str(runtime.label)]
        if isinstance(step, TransientError):
            raise step
        return step


def _acct(
    label: str = "long.account.name@example.test", provider_id: str = "codex"
) -> Account:
    provider = ProviderId(provider_id)
    credentials = (
        CodexCredentials(access_token=f"tok-{label}")
        if provider is ProviderId.CODEX
        else ClaudeSetupTokenCredentials(access_token=f"tok-{label}")
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
    account_activity_source: AccountTokenActivitySource | None = None,
    width: int = 200,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    store, private_credentials = make_account_store_with_private(
        tmp_path,
        accounts,
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    clock = FixedClock()
    http = HttpClient()
    provider_registry: dict[ProviderId, Provider] = {
        provider.id: provider for provider in providers
    }
    harness = CliHarness(
        console=Console(file=stdout, width=width, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            provider_registry,
            private_credentials,
            clock,
            heartbeat_providers={},
            account_activity_sources=(
                {}
                if account_activity_source is None
                else {ProviderId.CODEX: account_activity_source}
            ),
        ),
    )
    return harness, store, stdout, stderr


@REQUIRES_MANAGED_RUNTIME
def test_no_interactive_check_reads_managed_codex_authority(
    tmp_path: Path,
) -> None:
    """One-shot usage opens one managed Codex home under its account lock."""
    account = managed_saved_account(
        _MANAGED_ACCOUNT_ID,
        _MANAGED_AUTHORITY_ID,
        "managed-codex",
        _MANAGED_PROVIDER_IDENTITY,
        _MANAGED_GENERATION,
    )
    paths, _store, _private = seed_managed_accounts(
        tmp_path,
        (account,),
        {
            _MANAGED_ACCOUNT_ID: managed_auth(
                _MANAGED_PROVIDER_IDENTITY,
                _MANAGED_GENERATION,
            )
        },
    )
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(tmp_path, schema_root)
    configure_codex_logins(tmp_path, {})
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    provider = _FakeProvider()
    stdout = io.StringIO()
    composed = compose_app_context(
        paths=paths,
        clock=FixedClock(),
        providers={ProviderId.CODEX: provider},
        heartbeat_providers={},
        local_activity_sources={},
        account_activity_sources={},
        environment=environment,
    )
    try:
        result = CliHarness(
            console=Console(file=stdout, width=200, force_terminal=False),
            err_console=Console(file=io.StringIO(), force_terminal=False),
            application=composed.value,
        ).invoke(["--only", "codex", "--no-interactive"])
    finally:
        composed.close()

    assert result.exit_code == ExitCode.SUCCESS
    assert provider.fetch_calls == 1
    output = stdout.getvalue()
    assert "CODEX · 1 account" in output
    assert "provider-managed credential authority" not in output


def test_check_renders_partial_success_and_typed_auth_recovery(
    tmp_path: Path,
) -> None:
    """Success and failure remain visible in their provider panels."""
    claude = _FakeProvider(provider_id="claude")
    report = UsageReport(
        windows=(UsageWindow("5h", 0.0, None),),
        plan="pro",
    )
    codex = _FakeProvider(
        fetch_results={
            "codex-ok": report,
            "my work account": AuthError("Token expired"),
        },
        refresh_ok=False,
    )
    activity = _ScriptedAccountActivity(
        {
            "codex-ok": TokenActivitySummary(
                total_tokens=7_449_473_297,
                scope=TokenActivityScope.ACCOUNT,
                since=date(2026, 4, 7),
            )
        }
    )
    harness, _, stdout, _ = _install_ctx(
        tmp_path,
        (claude, codex),
        (
            _acct("claude-account", "claude"),
            _acct("codex-ok"),
            _acct("my work account"),
        ),
        account_activity_source=activity,
    )

    result = harness.invoke(["check"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    out = stdout.getvalue()
    assert "CLAUDE · 1 account" in out
    assert "CODEX · 2 accounts" in out
    assert "7,449,473,297 tokens" in out
    assert "since Apr 7, 2026" in out
    assert "known tokens" not in out
    assert "⚠ login required" in out
    assert "Run official managed Codex login:" in out
    assert "sidekick-usages codex login 'my work account'" in out
    assert activity.calls == ["codex-ok"]


def test_check_provider_filter_uses_only_selected_accounts(
    tmp_path: Path,
) -> None:
    claude = _FakeProvider(provider_id="claude")
    codex = _FakeProvider()
    harness, _, stdout, _ = _install_ctx(
        tmp_path,
        (claude, codex),
        (_acct("claude", "claude"), _acct("codex")),
    )

    result = harness.invoke(["--only", "codex", "check"])

    assert result.exit_code == ExitCode.SUCCESS
    assert claude.fetch_calls == 0
    assert codex.fetch_calls == 1
    out = stdout.getvalue()
    assert "CODEX · 1 account" in out
    assert "CLAUDE ·" not in out


def test_activity_failure_renders_before_forcing_system_error(
    tmp_path: Path,
) -> None:
    acct = _acct()
    provider = _FakeProvider(
        fetch_results={str(acct.label): TransientError("provider unavailable")}
    )
    activity = _ScriptedAccountActivity(
        {str(acct.label): TransientError("test-only provider response detail")}
    )
    harness, _, stdout, _ = _install_ctx(
        tmp_path,
        (provider,),
        (acct,),
        account_activity_source=activity,
    )

    result = harness.invoke(["check"])

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    out = stdout.getvalue()
    assert "provider unavailable" in out
    assert "token activity temporarily unavailable" in out
    assert "test-only provider response detail" not in out
