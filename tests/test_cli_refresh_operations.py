"""CLI import, bulk refresh, and add workflow regression tests."""

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.cli.token_input import TokenInput
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import Account, ClaudeLoginCredentials
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from tests.test_cli_refresh import (
    _claude_login_account,
    _detected,
    _FakeProvider,
    _install_empty_ctx,
    _install_many_ctx,
    _isolate_default_codex_home,
)
from tests.test_support import REFERENCE_TIME, FixedClock

_MAINTENANCE_REFRESH_CLOCK_CALLS = 3

pytestmark = pytest.mark.usefixtures(
    _isolate_default_codex_home.__name__,
)


def _claude_login_with_lifetimes(
    *,
    access_expiry: KnownExpiry,
    login_remaining: timedelta,
) -> Account:
    """Build one quoted-label login with independent test lifetimes."""
    account = _claude_login_account(access_expiry=access_expiry)
    account.label = AccountLabel("team account")
    credentials = account.credentials
    assert isinstance(credentials, ClaudeLoginCredentials)
    account.credentials = replace(
        credentials,
        refresh_expiry=KnownExpiry(REFERENCE_TIME + login_remaining),
    )
    return account


def test_refresh_all_refreshes_due_tokens_without_detecting_local_credentials(
    tmp_path: Path,
) -> None:
    """Bulk maintenance refresh uses saved refresh tokens only."""
    acct = _claude_login_account(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1))
    )
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
    acct = _claude_login_account(
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1))
    )
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
    assert clock.calls == _MAINTENANCE_REFRESH_CLOCK_CALLS


def test_refresh_all_quiet_prints_one_login_renewal_action(
    tmp_path: Path,
) -> None:
    """Quiet maintenance preserves the manual login-renewal warning."""
    acct = _claude_login_account(
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1))
    )
    credentials = acct.credentials
    assert isinstance(credentials, ClaudeLoginCredentials)
    acct.credentials = replace(
        credentials,
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=5)),
    )
    provider = _FakeProvider()
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [acct],
        clock=FixedClock(),
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = stdout.getvalue()
    assert output.count("Claude login expires within five days.") == 1
    assert output.count("Action:") == 1
    assert "sidekick-usages refresh team" in output
    assert stderr.getvalue() == ""
    assert provider.refresh_calls == 0
    saved = store.get("team")
    assert saved is not None
    assert saved.last_refresh_status is None


def test_refresh_all_renders_renewal_after_successful_access_refresh(
    tmp_path: Path,
) -> None:
    """Successful access rotation cannot hide the login-renewal action."""
    account = _claude_login_with_lifetimes(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
        login_remaining=timedelta(days=2),
    )
    provider = _FakeProvider()
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [account],
        clock=FixedClock(),
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = " ".join(stdout.getvalue().split())
    assert (
        output.count(
            "Access token refreshed; Claude login expires within five days."
        )
        == 1
    )
    assert output.count("Action:") == 1
    assert output.count("sidekick-usages refresh 'team account'") == 1
    assert "team account: refreshed" not in output
    assert stderr.getvalue() == ""
    saved = store.get("team account")
    assert saved is not None
    assert saved.last_refresh_status is RefreshStatus.OK
    assert saved.last_refresh_error is None


def test_refresh_all_renders_renewal_after_failed_access_refresh(
    tmp_path: Path,
) -> None:
    """A failed access rotation retains one login-mode recovery action."""
    account = _claude_login_with_lifetimes(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
        login_remaining=timedelta(days=2),
    )
    provider = _FakeProvider(refresh_ok=False)
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [account],
        clock=FixedClock(),
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = " ".join(stdout.getvalue().split())
    assert output.count("Test refresh rejected.") == 1
    assert output.count("Claude login expires within five days.") == 1
    assert output.count("Action:") == 1
    assert output.count("sidekick-usages refresh 'team account'") == 1
    assert stderr.getvalue() == ""
    saved = store.get("team account")
    assert saved is not None
    assert saved.last_refresh_status is RefreshStatus.FAILED
    assert saved.last_refresh_error == "Test refresh rejected."
    assert "five days" not in saved.last_refresh_error


def test_refresh_all_renders_action_for_rejected_subscription_login(
    tmp_path: Path,
) -> None:
    """Ordinary login rejection renders one method-appropriate action."""
    account = _claude_login_with_lifetimes(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
        login_remaining=timedelta(days=30),
    )
    provider = _FakeProvider(refresh_ok=False)
    harness, _, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        [account],
        clock=FixedClock(),
    )

    result = harness.invoke(["refresh", "--all", "--quiet"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = " ".join(stdout.getvalue().split())
    assert output.count("Test refresh rejected.") == 1
    assert output.count("Action:") == 1
    assert output.count("sidekick-usages refresh 'team account'") == 1
    assert stderr.getvalue() == ""


def test_refresh_all_persists_failed_refresh_diagnostic(
    tmp_path: Path,
) -> None:
    """Rejected refresh tokens are recorded for doctor and exit 1."""
    acct = _claude_login_account(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1))
    )
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
