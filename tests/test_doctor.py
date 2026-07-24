"""Doctor command diagnostics tests."""

import io
import json
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from sidekick_usages.cli.context import (
    DoctorContext,
    DoctorFailed,
    DoctorReady,
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
from sidekick_usages.doctor.service import DoctorService
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


def _harness(
    tmp_path: Path,
    accounts: tuple[Account, ...],
) -> tuple[CliHarness, io.StringIO, FixedClock]:
    store = make_account_store(tmp_path, accounts)
    output = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    context = DoctorContext(
        DoctorReady(
            DoctorService(
                tuple(store),
                providers,
                build_heartbeat_registry(providers),
                clock,
            ),
            PersistenceStatus(
                PersistenceState.CURRENT,
                store.path,
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
    setup = Account(
        label=AccountLabel("setup"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="test-only-secret-setup"
        ),
        plan="max",
    )
    harness, output, clock = _harness(tmp_path, (login, setup))

    result = harness.invoke(["doctor", "--json"])

    payload = json.loads(output.getvalue())
    accounts = {item["label"]: item for item in payload["accounts"]}
    assert result.exit_code == ExitCode.SUCCESS
    assert accounts["team"]["credential_kind"] == "subscription_login"
    assert accounts["team"]["access_expiry_state"] == "valid"
    assert accounts["team"]["refresh_expiry_state"] == "valid"
    assert accounts["team"]["identity_state"] == "known"
    assert accounts["team"]["can_auto_refresh"] is True
    assert accounts["team"]["provider_available"] is True
    assert accounts["setup"]["credential_kind"] == "setup_token"
    assert accounts["setup"]["can_auto_refresh"] is False
    assert payload["persistence"] == {
        "state": "current",
        "path": str(tmp_path / "accounts.json"),
        "account_count": 2,
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
        "test-only-secret-setup",
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
    harness, output, _clock = _harness(tmp_path, (login,))

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
    harness, output, _clock = _harness(tmp_path, (claude, codex))

    result = harness.invoke(
        ["doctor", "--provider", "codex", "--label", "codex-pro"]
    )

    assert result.exit_code == ExitCode.SUCCESS
    assert "codex-pro" in output.getvalue()
    assert "claude-team" not in output.getvalue()
