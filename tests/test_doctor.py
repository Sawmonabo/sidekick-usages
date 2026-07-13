"""Doctor command diagnostics tests."""

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rich.console import Console

from sidekick_usages.branding import ROBOT_LINES
from sidekick_usages.cli.context import (
    DoctorContext,
    DoctorFailed,
    DoctorReady,
)
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    RefreshStatus,
)
from sidekick_usages.doctor import (
    DoctorReadyResult,
    DoctorService,
    doctor_json,
    render_doctor,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import Sha256Digest
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.migrations.location import (
    CanonicalSelection,
    EmptySelection,
    LocationCandidate,
    LocationMigrationAssessment,
    LocationRole,
    ReadyLocationSelection,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationAssessment,
)
from sidekick_usages.persistence.observations import StoredGeneration
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    make_account_store,
)


def _ready_assessment(
    tmp_path: Path,
    account_count: int,
) -> LocationMigrationAssessment[ReadyLocationSelection]:
    path = (tmp_path / "accounts.json").resolve()
    candidates: tuple[LocationCandidate, ...]
    selection: ReadyLocationSelection
    if account_count:
        schema = PersistenceAssessment(
            code=PersistenceCode.CURRENT,
            generation=StoredGeneration.VERSION_ONE,
            schema_version=1,
            account_count=account_count,
            safe_path=path,
            artifact_basename=None,
            write_blocked=False,
            next_command=None,
            message="Account storage is current.",
            issues=(),
        )
        candidate = LocationCandidate(
            role=LocationRole.CANONICAL,
            path=path,
            assessment=schema,
            account_digest=Sha256Digest("a" * 64),
            private_auth_digest=Sha256Digest("b" * 64),
        )
        candidates = (candidate,)
        selection = CanonicalSelection(candidate)
    else:
        candidates = ()
        selection = EmptySelection()
    return LocationMigrationAssessment(
        selection=selection,
        candidates=candidates,
        source=path,
        destination=path,
        private_auth_summary=PrivateAuthMigrationAssessment(()),
        artifact_basename=None,
        issues=(),
        write_blocked=False,
        next_command=None,
    )


def _install_ctx(
    tmp_path: Path,
    accounts: list[Account],
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO, FixedClock]:
    """Install an isolated CLI context for doctor tests."""
    store = make_account_store(tmp_path, accounts)
    stdout = io.StringIO()
    stderr = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    heartbeat_providers = build_heartbeat_registry(providers)
    assessment = _ready_assessment(tmp_path, len(accounts))
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        doctor=DoctorContext(
            DoctorReady(
                DoctorService(
                    tuple(store),
                    providers,
                    heartbeat_providers,
                    clock,
                ),
                assessment,
            )
        ),
    )
    return harness, store, stdout, stderr, clock


def test_doctor_json_reports_refreshability_and_redacts_tokens(
    tmp_path: Path,
) -> None:
    """Doctor JSON exposes account state without leaking secrets."""
    oauth = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-secret-access-token-value",
            refresh_token="secret-refresh-token",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("user:profile", "user:inference"),
            identity=ClaudeLoginIdentity(
                account_id="secret-account-identity",
                organization_id="secret-organization-identity",
            ),
        ),
        plan="team",
        heartbeat_enabled=True,
        heartbeat_5h_reset_at=datetime(2026, 6, 12, 18, tzinfo=UTC),
        last_heartbeat_status=HeartbeatStatus.ACTIVE,
    )
    setup = Account(
        label=AccountLabel("setup"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-setup-token-value"
        ),
        plan="max",
    )
    harness, _, stdout, _, clock = _install_ctx(tmp_path, [oauth, setup])

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == 0
    payload = json.loads(stdout.getvalue())
    accounts = {item["label"]: item for item in payload["accounts"]}
    assert accounts["team"]["credential_kind"] == "subscription_login"
    assert accounts["team"]["access_expires_at"] == (
        "2026-06-12T13:34:56.789000Z"
    )
    assert accounts["team"]["access_expiry_state"] == "valid"
    assert accounts["team"]["refresh_expires_at"] == (
        "2026-09-10T12:34:56.789000Z"
    )
    assert accounts["team"]["refresh_expiry_state"] == "valid"
    assert accounts["team"]["login_renewal_state"] == "current"
    assert accounts["team"]["identity_state"] == "known"
    assert accounts["team"]["can_auto_refresh"] is True
    assert accounts["team"]["usage_route"] == "/api/oauth/usage"
    assert accounts["team"]["heartbeat_supported"] is True
    assert accounts["team"]["heartbeat_enabled"] is True
    assert accounts["team"]["heartbeat_5h_reset_at"] == "2026-06-12T18:00:00Z"
    assert accounts["team"]["last_heartbeat_status"] == "active"
    assert accounts["setup"]["can_auto_refresh"] is False
    assert accounts["setup"]["credential_kind"] == "setup_token"
    assert accounts["setup"]["access_expires_at"] is None
    assert accounts["setup"]["access_expiry_state"] == "unknown"
    assert accounts["setup"]["refresh_expires_at"] is None
    assert accounts["setup"]["refresh_expiry_state"] == "unknown"
    assert accounts["setup"]["login_renewal_state"] == "not_applicable"
    assert accounts["setup"]["identity_state"] == "unavailable"
    assert accounts["setup"]["usage_route"] == "/v1/messages headers"
    assert accounts["setup"]["heartbeat_supported"] is True
    persistence = payload["persistence"]
    assert persistence["code"] == "canonical_selected"
    assert persistence["candidates"][0]["schema"]["code"] == "current"
    assert clock.calls == 1
    rendered = stdout.getvalue()
    assert "secret-refresh-token" not in rendered
    assert "sk-ant-oat01-secret-access-token-value" not in rendered
    assert "secret-account-identity" not in rendered
    assert "secret-organization-identity" not in rendered
    for diagnostic in accounts.values():
        assert "expires_at" not in diagnostic
        assert "expires_at_local" not in diagnostic
        assert "expiry_state" not in diagnostic
        assert "identity_fingerprint" not in diagnostic


def test_doctor_human_names_authentication_and_separate_lifetimes(
    tmp_path: Path,
) -> None:
    """Human doctor copy distinguishes access and login lifetimes."""
    login = Account(
        label=AccountLabel("login"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-login-access",
            refresh_token="login-refresh",
            access_expiry=KnownExpiry(
                REFERENCE_TIME + timedelta(hours=6, minutes=42)
            ),
            refresh_expiry=KnownExpiry(datetime(2026, 12, 1, 12, tzinfo=UTC)),
            scopes=("user:profile",),
        ),
    )
    setup = Account(
        label=AccountLabel("setup"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-setup-access"
        ),
    )
    harness, _, stdout, _, _ = _install_ctx(tmp_path, [login, setup])

    result = harness.invoke(["doctor"])

    assert result.exit_code == ExitCode.SUCCESS
    output = stdout.getvalue()
    assert "authentication: subscription login" in output
    assert "access token expires: in 6h 42m" in output
    assert "login expires: Dec 1, 2026" in output
    assert "authentication: setup token" in output
    assert (
        "setup-token expiry: provider does not expose the token's "
        "issued-at timestamp"
    ) in output
    assert "identity:" not in output


def test_doctor_warns_when_login_renewal_is_due(tmp_path: Path) -> None:
    """Human and JSON doctor views expose one derived renewal action."""
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
    harness, _, stdout, _, _ = _install_ctx(tmp_path, [login])

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    account = json.loads(stdout.getvalue())["accounts"][0]
    assert account["login_renewal_state"] == "renewal_due"
    assert account["manual_action_required"] is True

    stdout.seek(0)
    stdout.truncate()
    result = harness.invoke(["doctor"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    output = stdout.getvalue()
    assert "login renewal: required within five days" in output
    assert "test-only-login" not in output


def test_doctor_views_share_one_completed_typed_result(tmp_path: Path) -> None:
    """Human and JSON views preserve the same completed diagnostics."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-test-token",
            refresh_token="test-refresh-token",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
    )
    harness, _, _, _, clock = _install_ctx(tmp_path, [account])
    context = harness.doctor
    assert context is not None
    state = context.state
    assert isinstance(state, DoctorReady)
    completed = DoctorReadyResult(
        tuple(state.service.diagnostics()),
        state.assessment,
    )
    human = io.StringIO()
    Console(file=human, force_terminal=False, width=120).print(
        render_doctor(completed, width=120)
    )
    machine = doctor_json(completed)
    decoded = json.loads(json.dumps(machine))

    assert "team" in human.getvalue()
    assert "location: canonical_selected" in human.getvalue()
    assert decoded["accounts"][0]["label"] == "team"
    assert decoded["persistence"]["code"] == "canonical_selected"
    assert decoded == machine
    assert clock.calls == 1


def test_doctor_json_represents_composition_failure(tmp_path: Path) -> None:
    """A pre-assessment failure remains machine-readable and actionable."""
    safe_path = (tmp_path / "accounts.json").resolve()
    failure = PersistenceCompositionFailure(
        code=PersistenceCode.UNREADABLE,
        safe_path=safe_path,
        artifact_basename=safe_path.name,
        message="The persistence location could not be read safely.",
        next_command=("sidekick-usages", "doctor"),
    )
    stdout = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        doctor=DoctorContext(DoctorFailed(failure)),
    )

    result = harness.invoke(["doctor", "--json"])
    payload = json.loads(stdout.getvalue())

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    assert payload["accounts"] == []
    assert payload["persistence"]["code"] == "unreadable"
    assert payload["persistence"]["safe_path"] == str(safe_path)


def test_doctor_reports_previous_refresh_rejection(
    tmp_path: Path,
) -> None:
    """Doctor flags accounts after a saved-token refresh fails."""
    account = Account(
        label=AccountLabel("dead"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-old-token-value",
            refresh_token="refresh-token",
            access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(hours=1)),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
        last_refresh_status=RefreshStatus.FAILED,
        last_refresh_error="Claude CLI refresh failed: status code 400",
    )
    harness, _, stdout, _, _ = _install_ctx(tmp_path, [account])

    result = harness.invoke(["doctor"])

    assert result.exit_code == 1
    out = stdout.getvalue()
    assert "dead" in out
    assert "manual action: yes" in out
    assert "heartbeat supported:" in out
    assert "Claude CLI refresh failed" in out
    assert out.count(ROBOT_LINES[2]) == 1
    assert "doctor · account diagnostics" in out


def test_doctor_filters_by_provider_and_label(tmp_path: Path) -> None:
    """Doctor filters are composable."""
    claude = Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-claude"
        ),
        plan="team",
    )
    codex = Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="eyJ.codex.token",
            refresh_token="codex-refresh",
        ),
        plan="pro",
    )
    harness, _, stdout, _, _ = _install_ctx(tmp_path, [claude, codex])

    result = harness.invoke(
        ["doctor", "--provider", "codex", "--label", "codex-pro"],
    )

    assert result.exit_code == 0
    out = stdout.getvalue()
    assert "codex-pro" in out
    assert "claude-team" not in out
