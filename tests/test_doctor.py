"""Doctor command diagnostics tests."""

import io
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from sidekick_usages.cli.context import (
    DoctorContext,
    DoctorFailed,
    DoctorReady,
)
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeSetupTokenAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialHealth,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.persistence.types.status import PersistenceState
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    make_account_store,
    make_supervisor_health,
)

_SUPERVISOR_HEALTH = make_supervisor_health(
    queue=ServiceComponentState.UNHEALTHY,
)
_SETUP_AUTHORITY_ID = AuthorityId("00000000-0000-4000-8000-000000000001")


def _harness(
    tmp_path: Path,
    accounts: tuple[SavedAccount, ...],
) -> tuple[CliHarness, io.StringIO, FixedClock]:
    output = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    heartbeat_providers = build_heartbeat_registry(providers)
    context = DoctorContext(
        DoctorReady(
            DoctorService(
                accounts,
                providers.keys(),
                heartbeat_providers.keys(),
                clock,
            ),
            PersistenceStatus(
                PersistenceState.CURRENT,
                tmp_path / "accounts.json",
                len(accounts),
            ),
            CredentialRefreshState(CredentialRefreshStateKind.CLEAN),
        ),
        _SUPERVISOR_HEALTH,
    )
    return (
        CliHarness(
            console=Console(file=output, force_terminal=False, width=160),
            err_console=Console(
                file=io.StringIO(),
                force_terminal=False,
            ),
            doctor=context,
        ),
        output,
        clock,
    )


def test_json_reports_current_auth_state_without_secrets(
    tmp_path: Path,
) -> None:
    login = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="test-only-secret-access",
            refresh_token="test-only-secret-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("user:profile", "user:inference"),
            identity=ClaudeLoginIdentity(
                account_id="test-only-secret-account",
                organization_id="test-only-secret-org",
            ),
        ),
        plan="team",
    )
    store = make_account_store(tmp_path, (login,))
    saved = store.saved_accounts()[0]
    authority = saved.authority
    assert isinstance(authority, ClaudeAccountAuthority)
    saved = replace(
        saved,
        authority=ClaudeAccountAuthority(
            setup_token=ClaudeSetupTokenAuthority(
                authority_id=_SETUP_AUTHORITY_ID,
                expires_at=REFERENCE_TIME + timedelta(days=365),
                health=CredentialHealth.HEALTHY,
                observed_at=REFERENCE_TIME,
            ),
            subscription=authority.subscription,
        ),
    )
    harness, output, clock = _harness(tmp_path, (saved,))

    result = harness.invoke(["doctor", "--json"])

    payload = json.loads(output.getvalue())
    accounts = payload["accounts"]
    authorities = accounts[0]["authorities"]
    assert result.exit_code == ExitCode.SUCCESS
    assert len(accounts) == 1
    assert accounts[0]["label"] == "team"
    assert accounts[0]["identity_state"] == "known"
    assert accounts[0]["provider_available"] is True
    setup_authority = authorities["setup_token"]
    subscription_authority = authorities["subscription"]
    assert setup_authority["kind"] == "setup_token"
    assert setup_authority["management"] == "sidekick_stored"
    assert setup_authority["can_auto_refresh"] is False
    assert subscription_authority["kind"] == "subscription_login"
    assert subscription_authority["management"] == "sidekick_stored"
    assert subscription_authority["can_auto_refresh"] is True
    assert payload["persistence"] == {
        "state": "current",
        "path": str(tmp_path / "accounts.json"),
        "account_count": 1,
        "credential_refresh": "clean",
    }
    assert payload["supervisor"] == {
        "backend": "systemd",
        "cli_version": "0.7.0",
        "supervisor_version": "0.7.0",
        "platform": "healthy",
        "process": "healthy",
        "protocol": "healthy",
        "queue": "unhealthy",
        "journal": "healthy",
        "broker": "not_required",
    }
    rendered = output.getvalue()
    for secret in (
        "test-only-secret-access",
        "test-only-secret-refresh",
        "test-only-secret-account",
        "test-only-secret-org",
    ):
        assert secret not in rendered
    assert clock.calls == 1


def test_human_view_explains_login_renewal_action(
    tmp_path: Path,
) -> None:
    login = Account(
        label=AccountLabel("login"),
        credentials=ClaudeLoginCredentials(
            access_token="test-only-login-access",
            refresh_token="test-only-login-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=6)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=5)),
            scopes=("user:profile",),
        ),
    )
    store = make_account_store(tmp_path, (login,))
    harness, output, _clock = _harness(tmp_path, store.saved_accounts())

    result = harness.invoke(["doctor"])

    rendered = output.getvalue()
    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert "authentication: subscription login" in rendered
    assert "access token expires: in 6h" in rendered
    assert "login renewal: required within five days" in rendered
    assert "test-only-login" not in rendered


def test_json_represents_current_store_failure(tmp_path: Path) -> None:
    path = (tmp_path / "accounts.json").resolve()
    failure = PersistenceFailure(
        PersistenceCode.UNREADABLE,
        path,
        "The account store could not be read safely.",
        path.name,
    )
    output = io.StringIO()
    harness = CliHarness(
        console=Console(file=output, force_terminal=False),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        doctor=DoctorContext(DoctorFailed(failure), _SUPERVISOR_HEALTH),
    )

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    payload = json.loads(output.getvalue())
    assert payload["accounts"] == []
    assert payload["persistence"] == {
        "state": "unreadable",
        "account_count": None,
        "path": str(path),
        "artifact_basename": "accounts.json",
        "message": "The account store could not be read safely.",
    }
    assert payload["supervisor"]["queue"] == "unhealthy"


def test_filters_are_composable(tmp_path: Path) -> None:
    claude = Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="test-only-claude"
        ),
    )
    codex = Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="test-only-codex",
            refresh_token="test-only-codex-refresh",
        ),
    )
    store = make_account_store(tmp_path, (claude, codex))
    harness, output, _clock = _harness(tmp_path, store.saved_accounts())

    result = harness.invoke(
        ["doctor", "--provider", "codex", "--label", "codex-pro"]
    )

    assert result.exit_code == ExitCode.SUCCESS
    assert "codex-pro" in output.getvalue()
    assert "claude-team" not in output.getvalue()
