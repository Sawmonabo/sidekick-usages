"""Doctor authentication and reporting tests."""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    ClaudeStoredLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
    CredentialHealth,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    FinalizedSelection,
    ProviderAuthObservation,
    SelectedAccountState,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderAuthState,
    ProviderRuntimeState,
    SelectionCode,
    SelectionPhase,
)
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.daemon.selection.models import SelectionStatus
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.persistence.lookup.store import (
    MetricsRefreshObservationStore,
)
from sidekick_usages.persistence.time_codec import canonical_timestamp
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardSnapshot,
)
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshCause,
    MetricsRefreshObservation,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
    MetricsRefreshWriteState,
)
from tests.fakes.daemon.capabilities import (
    make_provider_capability_report,
)
from tests.fakes.dashboard.render import interactive_dashboard_state
from tests.fakes.doctor import doctor_harness
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
)
from tests.support.time import REFERENCE_TIME

_RETRY_ATTEMPTS = 2
_SETUP_AUTHORITY_ID = AuthorityId("00000000-0000-4000-8000-000000000001")


def _seed_recovered_metrics_refresh(tmp_path: Path) -> None:
    """Persist and read one recovered global metrics observation."""
    observation = MetricsRefreshObservation(
        observed_at=REFERENCE_TIME,
        outcome=MetricsRefreshOutcome.RECOVERED,
        attempts=_RETRY_ATTEMPTS,
        retry_causes=(
            MetricsRefreshCause(
                stage=MetricsRefreshStage.WORKER,
                code=PersistenceCode.STORE_LOCKED,
            ),
        ),
    )
    store = MetricsRefreshObservationStore(
        make_application_paths(tmp_path).metrics_refresh_status
    )
    assert store.record(observation) is MetricsRefreshWriteState.SAVED


def _seed_doctor_dashboard(
    tmp_path: Path,
    claude: SavedAccount,
    codex: SavedAccount,
) -> tuple[DashboardSnapshot, str]:
    """Seed metrics telemetry and project cached Doctor account IDs."""
    _seed_recovered_metrics_refresh(tmp_path)
    dashboard, _cursor, _footer = interactive_dashboard_state(REFERENCE_TIME)
    claude_provider, codex_provider = dashboard.providers
    claude_row = claude_provider.rows[0]
    codex_row = codex_provider.rows[0]
    assert isinstance(claude_row, DashboardAccount)
    assert isinstance(codex_row, DashboardAccount)
    assert claude_row.usage is not None
    metrics_observed_at = canonical_timestamp(claude_row.usage.observed_at)
    return (
        replace(
            dashboard,
            providers=(
                replace(
                    claude_provider,
                    runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                    active_account_id=None,
                    verified_at=None,
                    actions_enabled=False,
                    rows=(
                        replace(
                            claude_row,
                            account_id=claude.account_id,
                            label=claude.label,
                            plan=claude.plan,
                            credential_health=claude.credential_health,
                            active=False,
                            states=(DashboardActionState.LOGIN_REQUIRED,),
                        ),
                    ),
                ),
                replace(
                    codex_provider,
                    runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                    active_account_id=codex.account_id,
                    verified_at=REFERENCE_TIME,
                    rows=(
                        replace(
                            codex_row,
                            account_id=codex.account_id,
                            label=codex.label,
                            plan=codex.plan,
                            credential_health=codex.credential_health,
                            active=True,
                            states=(
                                DashboardActionState.RECONCILIATION_REQUIRED,
                            ),
                            usage=None,
                            activity=None,
                        ),
                    ),
                ),
            ),
        ),
        metrics_observed_at,
    )


def test_json_reports_current_auth_state_without_secrets(
    tmp_path: Path,
) -> None:
    login = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="test-only-secret-claude-access",
            refresh_token="test-only-secret-claude-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("user:profile", "user:inference"),
            identity=ClaudeLoginIdentity(
                account_id="test-only-secret-claude-account",
                organization_id="test-only-secret-claude-org",
            ),
        ),
        plan="team",
    )
    codex = Account(
        label=AccountLabel("codex-reconcile"),
        credentials=CodexCredentials(
            access_token="test-only-secret-access",
            refresh_token="test-only-secret-refresh",
            account_id="test-only-secret-account",
        ),
        plan="pro",
    )
    stored_accounts = make_account_store(
        tmp_path,
        (login, codex),
    ).saved_accounts()
    login_saved, saved = stored_accounts
    login_authority = login_saved.authority
    assert isinstance(login_authority, ClaudeAccountAuthority)
    login_saved = replace(
        login_saved,
        authority=ClaudeAccountAuthority(
            setup_token=ClaudeSetupTokenAuthority(
                authority_id=_SETUP_AUTHORITY_ID,
                expires_at=REFERENCE_TIME + timedelta(days=365),
                health=CredentialHealth.HEALTHY,
                observed_at=REFERENCE_TIME,
            ),
            subscription=login_authority.subscription,
        ),
        credential_health=CredentialHealth.LOGIN_REQUIRED,
    )
    authority = saved.authority
    assert isinstance(authority, CodexAccountAuthority)
    provider_identity = ProviderIdentity("test-only-secret-provider")
    saved_generation = AuthorityGeneration("2026-07-25T12:00:00Z")
    selected_generation = AuthorityGeneration("2026-07-25T11:00:00Z")
    saved = replace(
        saved,
        authority=CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=authority.subscription.authority_id,
                provider_identity=provider_identity,
                generation=saved_generation,
                verified_at=REFERENCE_TIME,
                executable_version="0.145.0",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=CredentialHealth.RECONCILIATION_REQUIRED,
    )
    selected = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=saved.account_id,
        epoch=SelectionEpoch(4),
        generation=selected_generation,
        finalized_at=REFERENCE_TIME,
    )
    operation = DueOperation(
        operation_id=OperationId("ee805413-ef89-4380-920c-31ba5a4c948b"),
        provider_id=ProviderId.CODEX,
        account_id=saved.account_id,
        kind=OperationKind.MAINTAIN,
        priority=OperationPriority.SCHEDULED,
        state=OperationState.RETRY_WAIT,
        due_at=REFERENCE_TIME + timedelta(minutes=5),
        updated_at=REFERENCE_TIME,
        attempts=_RETRY_ATTEMPTS,
        failure_code="network_unavailable",
    )
    activation = ActivationRecord(
        provider_id=ProviderId.CODEX,
        operation_id=OperationId("30af0d90-90e5-4387-bef8-25d5b53b7d22"),
        selected_baseline=None,
        native_auth_baseline=ProviderAuthObservation(
            provider_id=ProviderId.CODEX,
            state=ProviderAuthState.ACTIVE,
            provider_identity=provider_identity,
            generation=selected_generation,
            observed_at=REFERENCE_TIME,
        ),
        target_account_id=saved.account_id,
        expected_target_identity=provider_identity,
        target_authority_generation=saved_generation,
        phase=ActivationPhase.TARGET_ACTIVATED,
        started_at=REFERENCE_TIME - timedelta(minutes=2),
        updated_at=REFERENCE_TIME - timedelta(minutes=1),
        failure_code="worker_interrupted",
    )
    dashboard, metrics_observed_at = _seed_doctor_dashboard(
        tmp_path,
        login_saved,
        saved,
    )
    runtime = DoctorRuntimeService(
        (login_saved, saved),
        dashboard,
        (selected,),
        (operation,),
        (activation,),
        selection_statuses=(
            SelectionStatus(
                provider_id=ProviderId.CLAUDE,
                operation_id=None,
                finalized_account_id=None,
                finalized_epoch=None,
                target_account_id=None,
                pending_epoch=None,
                phase=None,
                code=None,
                registered_count=1,
                reachable_count=1,
            ),
            SelectionStatus(
                provider_id=ProviderId.CODEX,
                operation_id=activation.operation_id,
                finalized_account_id=saved.account_id,
                finalized_epoch=SelectionEpoch(4),
                target_account_id=saved.account_id,
                pending_epoch=SelectionEpoch(4),
                phase=SelectionPhase.RECOVERING,
                code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
                registered_count=3,
                reachable_count=2,
                required_count=3,
                ready_count=2,
                adopted_count=1,
                unreachable_count=1,
                active_turn_count=1,
                queued_turn_count=1,
            ),
        ),
        shell_integration_code="integrated",
    )
    harness, output, clock = doctor_harness(
        tmp_path,
        (login_saved, saved),
        runtime,
        capabilities=make_provider_capability_report(codex_ready=False),
    )

    result = harness.invoke(["doctor", "--json"])

    payload = json.loads(output.getvalue())
    diagnostics = {
        diagnostic["label"]: diagnostic for diagnostic in payload["accounts"]
    }
    diagnostic, login_diagnostic = (
        diagnostics["codex-reconcile"],
        diagnostics["team"],
    )
    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert set(diagnostics) == {"team", "codex-reconcile"}
    assert (
        diagnostic["label"],
        diagnostic["provider_available"],
        diagnostic["native_relation"],
        diagnostic["selected_generation_relation"],
        diagnostic["metrics_freshness"],
        diagnostic["warning"],
    ) == (
        "codex-reconcile",
        False,
        "active",
        "older",
        "unavailable",
        "reconciliation_required",
    )
    assert (
        login_diagnostic["provider_available"],
        login_diagnostic["credential_health"],
        login_diagnostic["metrics_freshness"],
        login_diagnostic["metrics_observed_at"],
        login_diagnostic["warning"],
        login_diagnostic["manual_action"],
    ) == (
        True,
        "login_required",
        "stale",
        metrics_observed_at,
        "login_required",
        ["sidekick-usages", "migrate", "managed-auth"],
    )
    authorities = login_diagnostic["authorities"]
    assert (
        authorities["setup_token"]["kind"],
        authorities["setup_token"]["management"],
        authorities["subscription"]["kind"],
        authorities["subscription"]["management"],
    ) == (
        "setup_token",
        "sidekick_stored",
        "subscription_login",
        "sidekick_stored",
    )
    assert diagnostic["manual_action"] == [
        "sidekick-usages",
        "use",
        "codex",
        "codex-reconcile",
    ]
    service, sessions, (claude_session, codex_session) = (
        payload["service"],
        payload["sessions"],
        payload["sessions"]["providers"],
    )
    assert (
        service["backend"],
        service["platform"],
        service["process"],
        service["wsl_rescue_configuration"],
        service["socket_ownership"],
        service["peer_verification"],
        service["protocol"],
    ) == (
        "systemd",
        "healthy",
        "healthy",
        "not_required",
        "healthy",
        "healthy",
        "healthy",
    )
    assert (
        service["protocol_version"],
        sessions["selection_status"],
        sessions["shell_integration"],
        claude_session["provider"],
        claude_session["unmanaged"],
        claude_session["session_enrollment"],
        claude_session["claude_structured_host"],
    ) == (
        3,
        "healthy",
        "integrated",
        "claude",
        None,
        "observed",
        "unavailable",
    )
    assert (
        codex_session["provider"],
        codex_session["finalized_account_id"],
        codex_session["finalized_epoch"],
        codex_session["phase"],
        codex_session["code"],
        codex_session["registered"],
        codex_session["reachable"],
        codex_session["required"],
        codex_session["ready"],
        codex_session["adopted"],
        codex_session["unreachable"],
        codex_session["confirmed_dead_after_commit"],
        codex_session["active_turns"],
        codex_session["queued_turns"],
        codex_session["session_enrollment"],
        codex_session["codex_effective_config"],
    ) == (
        "codex",
        str(saved.account_id),
        4,
        "recovering",
        "selection_recovery_required",
        3,
        2,
        3,
        2,
        1,
        1,
        0,
        1,
        1,
        "observed",
        "unavailable",
    )
    capabilities = {
        result["provider"]: result
        for result in payload["provider_capabilities"]
    }
    assert (
        capabilities["claude"]["ready"],
        Path(capabilities["claude"]["executable"]["path"]).name == "claude",
        capabilities["codex"]["ready"],
        capabilities["codex"]["failure_code"],
        capabilities["codex"]["executable"]["version"],
    ) == (
        True,
        True,
        False,
        "capability_unsupported",
        "0.145.0",
    )
    scheduled, unfinished = (
        payload["operations"]["scheduled"],
        payload["operations"]["unfinished_activations"],
    )
    assert (
        payload["operations"]["queue"],
        payload["operations"]["journal"],
        len(scheduled),
        len(unfinished),
    ) == ("unhealthy", "healthy", 1, 1)
    assert (
        scheduled[0]["state"],
        scheduled[0]["attempts"],
        scheduled[0]["failure_code"],
    ) == (
        "retry_wait",
        _RETRY_ATTEMPTS,
        "network_unavailable",
    )
    assert (
        unfinished[0]["phase"],
        unfinished[0]["failure_code"],
    ) == ("target_activated", "worker_interrupted")
    assert (
        payload["persistence"],
        payload["metrics_refresh"],
    ) == (
        {
            "state": "current",
            "path": str(tmp_path / "accounts.json"),
            "account_count": 2,
            "credential_refresh": "clean",
        },
        {
            "state": "available",
            "observed_at": canonical_timestamp(REFERENCE_TIME),
            "outcome": "recovered",
            "attempts": _RETRY_ATTEMPTS,
            "retry_causes": [
                {
                    "stage": "worker",
                    "code": "store_locked",
                    "provider": None,
                    "account_id": None,
                }
            ],
            "causes": [],
        },
    )
    assert "test-only-secret" not in output.getvalue()
    assert clock.calls == 1
    DoctorRuntimeService(
        (saved,),
        None,
        (),
        (),
        (
            replace(
                activation,
                selected_baseline=SelectedAccountState(
                    provider_id=ProviderId.CODEX,
                    runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                    account_id=None,
                    provider_identity=provider_identity,
                    runtime_generation=selected_generation,
                    verified_at=REFERENCE_TIME,
                    outcome=ActivationOutcome.EXTERNAL_RECONCILED,
                ),
            ),
        ),
    )
    baseline_account = replace(
        saved,
        account_id=SidekickAccountId("00000000-0000-4000-8000-000000000010"),
        label=AccountLabel("codex-baseline"),
    )
    with pytest.raises(ValueError, match="baseline identity does not match"):
        DoctorRuntimeService(
            (baseline_account, saved),
            None,
            (),
            (),
            (
                replace(
                    activation,
                    selected_baseline=SelectedAccountState(
                        provider_id=ProviderId.CODEX,
                        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                        account_id=baseline_account.account_id,
                        provider_identity=ProviderIdentity(
                            "unexpected-baseline"
                        ),
                        runtime_generation=saved_generation,
                        verified_at=REFERENCE_TIME,
                        outcome=ActivationOutcome.VERIFIED,
                    ),
                ),
            ),
        )
    with pytest.raises(ValueError, match="target generation does not match"):
        DoctorRuntimeService(
            (saved,),
            None,
            (),
            (),
            (
                replace(
                    activation,
                    target_authority_generation=AuthorityGeneration(
                        "unexpected-generation"
                    ),
                ),
            ),
        )


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
    saved = store.saved_accounts()[0]
    authority = saved.authority
    assert isinstance(authority, ClaudeAccountAuthority)
    subscription = authority.subscription
    assert isinstance(subscription, ClaudeStoredLoginAuthority)
    saved = replace(
        saved,
        authority=ClaudeAccountAuthority(
            setup_token=authority.setup_token,
            subscription=ClaudeManagedLoginAuthority(
                authority_id=subscription.authority_id,
                provider_identity=ProviderIdentity("synthetic-managed-login"),
                generation=AuthorityGeneration("managed-login-generation"),
                access_expires_at=(REFERENCE_TIME + timedelta(hours=6)),
                refresh_expires_at=(REFERENCE_TIME + timedelta(days=5)),
                verified_at=REFERENCE_TIME,
                executable_version="2.1.220",
                health=CredentialHealth.HEALTHY,
                action=CredentialAction.LOGIN,
            ),
        ),
    )
    runtime = DoctorRuntimeService(
        (saved,),
        None,
        (),
        (),
        (),
        selection_statuses=(
            SelectionStatus(
                provider_id=ProviderId.CLAUDE,
                operation_id=OperationId(
                    "a0b071e0-c34f-41e7-9122-753dc90eef20"
                ),
                finalized_account_id=saved.account_id,
                finalized_epoch=SelectionEpoch(7),
                target_account_id=saved.account_id,
                pending_epoch=SelectionEpoch(8),
                phase=SelectionPhase.AWAITING_READY,
                code=SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT,
                registered_count=2,
                reachable_count=1,
                required_count=2,
                ready_count=1,
                adopted_count=1,
                unreachable_count=1,
                confirmed_dead_count=1,
                active_turn_count=1,
                queued_turn_count=1,
            ),
            SelectionStatus(
                provider_id=ProviderId.CODEX,
                operation_id=None,
                finalized_account_id=None,
                finalized_epoch=None,
                target_account_id=None,
                pending_epoch=None,
                phase=None,
                code=None,
                registered_count=0,
                reachable_count=0,
            ),
        ),
        shell_integration_code="integrated",
    )
    harness, output, _clock = doctor_harness(
        tmp_path,
        (saved,),
        runtime,
    )

    result = harness.invoke(["doctor"])

    rendered = output.getvalue()
    normalized = " ".join(rendered.split())
    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert "authentication: subscription login" in rendered
    assert "access token expires: in 6h" in rendered
    assert "login renewal: required within five days" in rendered
    assert (
        "manual action: sidekick-usages refresh login --provider claude"
    ) in normalized
    assert (
        f"claude: finalized {saved.account_id}@7 · target "
        f"{saved.account_id}@8 · awaiting_ready/"
        "participant_lost_after_commit · participants "
        "2 registered, 1 reachable, 2 required, 1 ready, 1 adopted, "
        "1 unreachable, 1 confirmed dead after commit · turns 1 active, "
        "1 queued · unmanaged unavailable · enrollment observed · "
        "structured host unavailable"
    ) in normalized
    assert "test-only-login" not in rendered
