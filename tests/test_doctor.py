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
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    CredentialHealth,
    OperationId,
    ProviderIdentity,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
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
from tests.fakes.daemon.capabilities import make_provider_capability_report
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


def _harness(
    tmp_path: Path,
    accounts: tuple[SavedAccount, ...],
    selected_states: tuple[SelectedAccountState, ...] = (),
    operations: tuple[DueOperation, ...] = (),
    activations: tuple[ActivationRecord, ...] = (),
    capabilities: ProviderCapabilityReport | None = None,
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
    context = DoctorContext(
        DoctorReady(
            DoctorService(
                accounts,
                capability_report.ready_provider_ids,
                heartbeat_providers.keys(),
                clock,
                DoctorRuntimeService(
                    accounts,
                    None,
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
        _SUPERVISOR_HEALTH,
        capability_report,
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
    account = Account(
        label=AccountLabel("codex-reconcile"),
        credentials=CodexCredentials(
            access_token="test-only-secret-access",
            refresh_token="test-only-secret-refresh",
            account_id="test-only-secret-account",
        ),
        plan="pro",
    )
    saved = make_account_store(tmp_path, (account,)).saved_accounts()[0]
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
    harness, output, clock = _harness(
        tmp_path,
        (saved,),
        selected_states=(selected,),
        operations=(operation,),
        activations=(activation,),
        capabilities=make_provider_capability_report(codex_ready=False),
    )

    result = harness.invoke(["doctor", "--json"])

    payload = json.loads(output.getvalue())
    diagnostic = payload["accounts"][0]
    assert result.exit_code == ExitCode.MANUAL_ACTION
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
    assert diagnostic["manual_action"] == [
        "sidekick-usages",
        "use",
        "codex",
        "codex-reconcile",
    ]
    service = payload["service"]
    assert (
        service["wsl_rescue_configuration"],
        service["socket_ownership"],
        service["peer_verification"],
    ) == ("not_required", "healthy", "healthy")
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
        doctor=DoctorContext(
            DoctorFailed(failure),
            _SUPERVISOR_HEALTH,
            make_provider_capability_report(),
        ),
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
    assert payload["operations"]["queue"] == "unhealthy"


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
