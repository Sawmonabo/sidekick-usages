"""Claude subscription-login renewal boundary tests."""

from dataclasses import replace
from datetime import timedelta

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    RefreshStatus,
)
from sidekick_usages.maintenance import TokenMaintenanceService
from tests.fakes.usage import (
    InMemoryAccountStore,
    ScriptedCredentialCoordinator,
)
from tests.support.time import REFERENCE_TIME, FixedClock


def _login_account(*, refresh_expiry: KnownExpiry | UnknownExpiry) -> Account:
    """Build one synthetic login with a fresh access token."""
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="test-only-login-access",
            refresh_token="test-only-login-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=refresh_expiry,
            scopes=("user:profile",),
        ),
        plan="team",
    )


@pytest.mark.parametrize(
    "remaining",
    [timedelta(days=5), timedelta(days=1)],
)
def test_maintenance_warns_at_or_inside_login_renewal_window(
    remaining: timedelta,
) -> None:
    """A still-valid login near expiry requires one non-failure action."""
    account = _login_account(
        refresh_expiry=KnownExpiry(REFERENCE_TIME + remaining)
    )
    store = InMemoryAccountStore((account,))
    refresher = ScriptedCredentialCoordinator(store)

    outcome = TokenMaintenanceService(
        store,
        refresher,
        clock=FixedClock(),
    ).refresh_account(store.saved_accounts()[0])

    assert outcome.status is RefreshStatus.SKIPPED
    assert outcome.exit_code is ExitCode.MANUAL_ACTION
    assert outcome.action_required is True
    assert outcome.message == "Claude login expires within five days."
    assert refresher.calls == []
    assert store.persisted == []
    assert store.saved("team").last_refresh_status is None


def test_maintenance_does_not_warn_outside_login_renewal_window() -> None:
    """One second outside the renewal window remains a normal skip."""
    account = _login_account(
        refresh_expiry=KnownExpiry(
            REFERENCE_TIME + timedelta(days=5, seconds=1)
        )
    )
    store = InMemoryAccountStore((account,))
    refresher = ScriptedCredentialCoordinator(store)

    outcome = TokenMaintenanceService(
        store,
        refresher,
        clock=FixedClock(),
    ).refresh_account(store.saved_accounts()[0])

    assert outcome.status is RefreshStatus.SKIPPED
    assert outcome.exit_code is ExitCode.SUCCESS
    assert outcome.action_required is False
    assert outcome.message == "fresh"
    assert refresher.calls == []
    assert store.persisted == []


def test_maintenance_expired_login_fails_closed_without_refresh() -> None:
    """An expired login never reaches the credential-bearing provider path."""
    account = _login_account(
        refresh_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1))
    )
    store = InMemoryAccountStore((account,))
    refresher = ScriptedCredentialCoordinator(store)

    outcome = TokenMaintenanceService(
        store,
        refresher,
        clock=FixedClock(),
    ).refresh_account(store.saved_accounts()[0], force=True)

    assert outcome.status is RefreshStatus.SKIPPED
    assert outcome.exit_code is ExitCode.MANUAL_ACTION
    assert outcome.action_required is True
    assert outcome.message == "Claude login has expired."
    assert refresher.calls == []
    assert store.persisted == []
    assert store.saved("team").last_refresh_status is None


def test_unknown_and_setup_credentials_have_no_login_renewal_warning() -> None:
    """Unobservable and inapplicable lifetimes do not invent a warning."""
    unknown = _login_account(refresh_expiry=UnknownExpiry())
    setup = Account(
        label=AccountLabel("setup"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="test-only-setup-token"
        ),
        plan="max",
    )
    store = InMemoryAccountStore((unknown, setup))
    refresher = ScriptedCredentialCoordinator(store)
    service = TokenMaintenanceService(store, refresher, clock=FixedClock())

    outcomes = [
        service.refresh_account(account) for account in store.saved_accounts()
    ]

    assert [outcome.exit_code for outcome in outcomes] == [
        ExitCode.SUCCESS,
        ExitCode.SUCCESS,
    ]
    assert all(not outcome.action_required for outcome in outcomes)
    assert refresher.calls == []
    assert store.persisted == []


def test_due_access_refresh_preserves_login_renewal_warning() -> None:
    """Access refresh and login renewal remain independent transitions."""
    account = _login_account(
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=2))
    )
    credentials = account.credentials
    assert isinstance(credentials, ClaudeLoginCredentials)
    account.credentials = replace(
        credentials,
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1)),
    )
    store = InMemoryAccountStore((account,))
    refresher = ScriptedCredentialCoordinator(store)

    outcome = TokenMaintenanceService(
        store,
        refresher,
        clock=FixedClock(),
    ).refresh_account(store.saved_accounts()[0])

    assert outcome.status is RefreshStatus.OK
    assert outcome.refreshed is True
    assert outcome.exit_code is ExitCode.MANUAL_ACTION
    assert outcome.action_required is True
    assert outcome.message == (
        "Access token refreshed; Claude login expires within five days."
    )
    assert refresher.calls == ["team"]
    assert len(store.persisted) == 1
