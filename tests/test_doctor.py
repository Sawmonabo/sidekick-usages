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
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
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
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardSnapshot,
)
from tests.fakes.daemon.capabilities import (
    StaticProviderCapabilityService,
    make_provider_capability_report,
)
from tests.fakes.dashboard.render import interactive_dashboard_state
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
_RETRY_ATTEMPTS = 2
_SETUP_AUTHORITY_ID = AuthorityId(
    "00000000-0000-4000-8000-000000000001"
)


def _harness(
    tmp_path: Path,
    accounts: tuple[SavedAccount, ...],
    dashboard: DashboardSnapshot | None = None,
    selected_states: tuple[SelectedAccountState, ...] = (),
    operations: tuple[DueOperation, ...] = (),
    activations: tuple[ActivationRecord, ...] = (),
    capabilities: ProviderCapabilityReport | None = None,
    supervisor: SupervisorHealth = _SUPERVISOR_HEALTH,
) -> tuple[CliHarness, io.StringIO, FixedClock]:
    output = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    heartbeat_providers = build_heartbeat_registry(providers)
    capability_report = (
        make_provider_capability_report()
        if capabilities is None
        else capabilities
    )
    capability_service = StaticProviderCapabilityService(
        capability_report
    )
    context = DoctorContext(
        DoctorReady(
            DoctorService(
                accounts,
                capability_service,
                heartbeat_providers.keys(),
                clock,
                DoctorRuntimeService(
                    accounts,
                    dashboard,
                    selected_states,
                    operations,
                    activations,
                ),
            ),
            PersistenceStatus(
                PersistenceState.CURRENT,
                tmp_path / "accounts.json",
                len(accounts),
            ),
            CredentialRefreshState(CredentialRefreshStateKind.CLEAN),
        ),
        supervisor,
        capability_service,
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
    selected = SelectedAccountState(
        provider_id=ProviderId.CODEX,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        account_id=saved.account_id,
        provider_identity=provider_identity,
        runtime_generation=selected_generation,
        verified_at=REFERENCE_TIME,
        outcome=ActivationOutcome.VERIFIED,
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
        operation_id=OperationId(
            "30af0d90-90e5-4387-bef8-25d5b53b7d22"
        ),
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
    dashboard, _cursor, _footer = interactive_dashboard_state(
        REFERENCE_TIME
    )
    claude_provider, codex_provider = dashboard.providers
    claude_row = claude_provider.rows[0]
    codex_row = codex_provider.rows[0]
    assert isinstance(claude_row, DashboardAccount)
    assert isinstance(codex_row, DashboardAccount)
    dashboard = replace(
        dashboard,
        providers=(
            replace(
                claude_provider,
                runtime_state=None,
                active_account_id=None,
                verified_at=None,
                actions_enabled=False,
                rows=(
                    replace(
                        claude_row,
                        account_id=login_saved.account_id,
                        label=login_saved.label,
                        plan=login_saved.plan,
                        credential_health=(
                            login_saved.credential_health
                        ),
                        active=False,
                        states=(DashboardActionState.LOGIN_REQUIRED,),
                    ),
                ),
            ),
            replace(
                codex_provider,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                active_account_id=saved.account_id,
                verified_at=REFERENCE_TIME,
                rows=(
                    replace(
                        codex_row,
                        account_id=saved.account_id,
                        label=saved.label,
                        plan=saved.plan,
                        credential_health=saved.credential_health,
                        active=True,
                        states=(
                            DashboardActionState.RECONCILIATION_REQUIRED,
                        ),
                    ),
                ),
            ),
        ),
    )
    harness, output, clock = _harness(
        tmp_path,
        (login_saved, saved),
        dashboard,
        selected_states=(selected,),
        operations=(operation,),
        activations=(activation,),
        capabilities=make_provider_capability_report(codex_ready=False),
    )

    result = harness.invoke(["doctor", "--json"])

    payload = json.loads(output.getvalue())
    diagnostics = {
        diagnostic["label"]: diagnostic
        for diagnostic in payload["accounts"]
    }
    diagnostic = diagnostics["codex-reconcile"]
    login_diagnostic = diagnostics["team"]
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
        "2026-06-12T10:34:56.789Z",
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
    service = payload["service"]
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
    capabilities = {
        result["provider"]: result
        for result in payload["provider_capabilities"]
    }
    assert (
        capabilities["claude"]["ready"],
        capabilities["claude"]["executable"]["path"].endswith("/claude"),
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
    scheduled = payload["operations"]["scheduled"]
    assert (
        payload["operations"]["queue"],
        payload["operations"]["journal"],
    ) == ("unhealthy", "healthy")
    assert len(scheduled) == 1
    assert (
        scheduled[0]["state"],
        scheduled[0]["attempts"],
        scheduled[0]["failure_code"],
    ) == (
        "retry_wait",
        _RETRY_ATTEMPTS,
        "network_unavailable",
    )
    unfinished = payload["operations"]["unfinished_activations"]
    assert len(unfinished) == 1
    assert (
        unfinished[0]["phase"],
        unfinished[0]["failure_code"],
    ) == ("target_activated", "worker_interrupted")
    assert payload["persistence"] == {
        "state": "current",
        "path": str(tmp_path / "accounts.json"),
        "account_count": 2,
        "credential_refresh": "clean",
    }
    assert "test-only-secret" not in output.getvalue()
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
                provider_identity=ProviderIdentity(
                    "synthetic-managed-login"
                ),
                generation=AuthorityGeneration("managed-login-generation"),
                access_expires_at=(
                    REFERENCE_TIME + timedelta(hours=6)
                ),
                refresh_expires_at=(
                    REFERENCE_TIME + timedelta(days=5)
                ),
                verified_at=REFERENCE_TIME,
                executable_version="2.1.220",
                health=CredentialHealth.HEALTHY,
                action=CredentialAction.LOGIN,
            ),
        ),
    )
    harness, output, _clock = _harness(tmp_path, (saved,))

    result = harness.invoke(["doctor"])

    rendered = output.getvalue()
    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert "authentication: subscription login" in rendered
    assert "access token expires: in 6h" in rendered
    assert "login renewal: required within five days" in rendered
    assert (
        "manual action: sidekick-usages refresh login "
        "--provider claude"
    ) in rendered
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
        doctor=DoctorContext(
            DoctorFailed(failure),
            _SUPERVISOR_HEALTH,
            StaticProviderCapabilityService(
                make_provider_capability_report()
            ),
        ),
    )

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    payload = json.loads(output.getvalue())
    assert payload["accounts"] == []
    assert payload["persistence"] == {
        "state": "unreadable",
        "account_count": None,
        "path": str(path),
        "artifact_basename": "accounts.json",
        "message": "The account store could not be read safely.",
    }
    assert payload["operations"] == {
        "queue": "unhealthy",
        "journal": "healthy",
        "scheduled": "unavailable",
        "unfinished_activations": "unavailable",
    }


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

    rendered = output.getvalue()
    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert "codex-pro" in rendered
    assert "claude-team" not in rendered
    assert "provider capabilities\n  codex:" in rendered
    assert "  claude:" not in rendered
    assert "manual action: sidekick-usages migrate managed-auth" in rendered
    doctor_context = harness.doctor
    assert doctor_context is not None
    capability_service = doctor_context.capabilities
    assert isinstance(
        capability_service,
        StaticProviderCapabilityService,
    )
    assert capability_service.requested_provider_ids
    assert set(capability_service.requested_provider_ids) == {
        ProviderId.CODEX
    }

    provider_operation = DueOperation(
        operation_id=OperationId(
            "6ba03c6d-3f05-4b3c-927e-67fe5d650e23"
        ),
        provider_id=ProviderId.CODEX,
        account_id=None,
        kind=OperationKind.RECONCILE_NATIVE,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    operation_harness, operation_output, _operation_clock = _harness(
        tmp_path / "provider-operation",
        (store.saved_accounts()[0],),
        operations=(provider_operation,),
    )

    operation_result = operation_harness.invoke(
        ["doctor", "--provider", "codex", "--json"]
    )

    operation_payload = json.loads(operation_output.getvalue())
    assert operation_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert operation_payload["accounts"] == []
    assert operation_payload["operations"]["scheduled"][0][
        "account_label"
    ] is None

    claude_saved = next(
        account
        for account in store.saved_accounts()
        if account.provider_id is ProviderId.CLAUDE
    )
    claude_harness, claude_output, _claude_clock = _harness(
        tmp_path / "claude-scope",
        (claude_saved,),
        supervisor=replace(
            _SUPERVISOR_HEALTH,
            queue=ServiceComponentState.HEALTHY,
            broker=ServiceComponentState.UNHEALTHY,
        ),
    )

    claude_result = claude_harness.invoke(
        ["doctor", "--provider", "claude", "--json"]
    )

    claude_payload = json.loads(claude_output.getvalue())
    assert claude_result.exit_code == ExitCode.SUCCESS
    assert claude_payload["service"]["broker"] == "not_required"
    assert [
        result["provider"]
        for result in claude_payload["provider_capabilities"]
    ] == ["claude"]
