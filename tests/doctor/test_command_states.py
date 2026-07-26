"""Doctor command state and filtering tests."""

import io
import json
from dataclasses import replace
from pathlib import Path

from rich.console import Console

from sidekick_usages.cli.contexts.models import DoctorContext, DoctorFailed
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
)
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.models.status import PersistenceFailure
from sidekick_usages.persistence.types.error import PersistenceCode
from tests.fakes.daemon.capabilities import (
    StaticProviderCapabilityService,
    make_provider_capability_report,
)
from tests.fakes.doctor import doctor_harness
from tests.support.cli import CliHarness
from tests.support.daemon import make_supervisor_health
from tests.support.persistence import make_account_store
from tests.support.time import REFERENCE_TIME

_SUPERVISOR_HEALTH = make_supervisor_health(
    queue=ServiceComponentState.UNHEALTHY,
)


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
            StaticProviderCapabilityService(make_provider_capability_report()),
        ),
    )

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    payload = json.loads(output.getvalue())
    assert payload["accounts"] == "unavailable"
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

    healthy_supervisor = make_supervisor_health()
    primary_harness, _primary_output, _primary_clock = doctor_harness(
        tmp_path / "primary-absent",
        (),
        supervisor=replace(
            healthy_supervisor,
            platform=ServiceComponentState.ABSENT,
        ),
    )
    downstream_harness, _downstream_output, _downstream_clock = doctor_harness(
        tmp_path / "downstream-absent",
        (),
        supervisor=replace(
            healthy_supervisor,
            socket=ServiceComponentState.ABSENT,
        ),
    )
    capability_harness, _capability_output, _capability_clock = doctor_harness(
        tmp_path / "provider-capability",
        (),
        capabilities=make_provider_capability_report(codex_ready=False),
        supervisor=healthy_supervisor,
    )
    refresh_harness, _refresh_output, _refresh_clock = doctor_harness(
        tmp_path / "refresh-state",
        (),
        supervisor=healthy_supervisor,
        refresh_state=CredentialRefreshStateKind.RECOVERABLE,
    )

    primary_result = primary_harness.invoke(["doctor", "--json"])
    downstream_result = downstream_harness.invoke(["doctor", "--json"])
    capability_result = capability_harness.invoke(
        ["doctor", "--provider", "codex", "--json"]
    )
    refresh_result = refresh_harness.invoke(["doctor", "--json"])

    assert primary_result.exit_code == ExitCode.MANUAL_ACTION
    assert downstream_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert capability_result.exit_code == ExitCode.SYSTEM_ERROR
    assert refresh_result.exit_code == ExitCode.SYSTEM_ERROR


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
    harness, output, _clock = doctor_harness(
        tmp_path,
        store.saved_accounts(),
    )

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
    assert set(capability_service.requested_provider_ids) == {ProviderId.CODEX}

    provider_operation = DueOperation(
        operation_id=OperationId("6ba03c6d-3f05-4b3c-927e-67fe5d650e23"),
        provider_id=ProviderId.CODEX,
        account_id=None,
        kind=OperationKind.RECONCILE_NATIVE,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.SCHEDULED,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )
    operation_harness, operation_output, _operation_clock = doctor_harness(
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
    assert (
        operation_payload["operations"]["scheduled"][0]["account_label"]
        is None
    )

    claude_saved = next(
        account
        for account in store.saved_accounts()
        if account.provider_id is ProviderId.CLAUDE
    )
    claude_harness, claude_output, _claude_clock = doctor_harness(
        tmp_path / "claude-scope",
        (claude_saved,),
        supervisor=replace(
            _SUPERVISOR_HEALTH,
            queue=ServiceComponentState.HEALTHY,
            broker=ServiceComponentState.UNHEALTHY,
            broker_failure_code="version_unsupported",
        ),
    )

    no_account_result = claude_harness.invoke(
        ["doctor", "--provider", "codex"]
    )
    assert no_account_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert "provider capabilities\n  codex:" in claude_output.getvalue()
    assert "broker failure: version_unsupported" in claude_output.getvalue()
    claude_output.seek(0)
    claude_output.truncate()
    codex_json_result = claude_harness.invoke(
        ["doctor", "--provider", "codex", "--json"]
    )
    codex_payload = json.loads(claude_output.getvalue())
    assert codex_json_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert (
        codex_payload["service"]["broker_failure_code"]
        == "version_unsupported"
    )
    claude_output.seek(0)
    claude_output.truncate()
    claude_result = claude_harness.invoke(
        ["doctor", "--provider", "claude", "--json"]
    )

    claude_payload = json.loads(claude_output.getvalue())
    assert claude_result.exit_code == ExitCode.MANUAL_ACTION
    assert claude_payload["service"]["broker"] == "not_required"
    assert claude_payload["service"]["broker_failure_code"] is None
    assert [
        result["provider"]
        for result in claude_payload["provider_capabilities"]
    ] == ["claude"]
    assert claude_payload["accounts"][0]["manual_action"] == [
        "sidekick-usages",
        "migrate",
        "managed-auth",
    ]
