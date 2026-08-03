"""Interactive dashboard action behavior."""

import io
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.cli.contexts.migration import (
    ManagedAuthDaemonLifecycle,
)
from sidekick_usages.cli.contexts.models import MigrationContext
from sidekick_usages.cli.contexts.use import UseContext
from sidekick_usages.cli.dashboard.models.controller import (
    RefreshAccountIntent,
    SelectAccountIntent,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupAction,
    ServiceSetupDecision,
    ServiceSetupMessage,
    ServiceSetupOutcome,
)
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.usage.dashboard.models import (
    DashboardService,
)
from tests.fakes.dashboard.runtime import (
    SetupDaemon,
)
from tests.fakes.dashboard.setup import (
    exercise_setup_acknowledgement,
    guided_setup,
)
from tests.fakes.dashboard.state import (
    CLAUDE_ACTIVE_ACCOUNT_ID,
    CLAUDE_PREVIEW_ACCOUNT_ID,
    CODEX_SAVED_ACCOUNT_ID,
)
from tests.fakes.dashboard.use import (
    RecordingUseSelection,
    scriptable_use_accounts,
)
from tests.fakes.migration.managed_auth import (
    MIGRATION_IDENTITIES,
    managed_auth_scenario,
)
from tests.support.cli import CliHarness
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import FixedClock

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)


@REQUIRES_MANAGED_RUNTIME
def test_guided_setup_resumes_once_and_preserves_blocked_actions(
    tmp_path: Path,
) -> None:
    """One setup journey installs, reuses, refuses, and stays script-safe."""
    unavailable = DashboardService(
        ready=False,
        compatible=False,
        phase=None,
        observed_at=None,
        failure_code=None,
    )
    compatible = replace(
        unavailable,
        compatible=True,
        phase=ServicePhase.DEGRADED,
    )
    intent = SelectAccountIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
    )
    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    acknowledgement_path = tmp_path / "setup-acknowledgement.json"
    setup = guided_setup(daemon, acknowledgement_path)

    confirmation = setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
    )
    approved = setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.APPROVED,
    )

    assert confirmation.outcome is ServiceSetupOutcome.CONFIRMATION_REQUIRED
    assert approved.outcome is ServiceSetupOutcome.RESUME
    assert confirmation.intent is approved.intent is intent
    assert daemon.events == [
        "status:claude",
        "status:claude",
        "install:claude",
    ]
    assert exercise_setup_acknowledgement(
        acknowledgement_path,
        service=unavailable,
        intent=intent,
    ) == (
        True,
        ServiceSetupOutcome.RESUME,
        ("status:claude", "install:claude"),
        ServiceSetupOutcome.CONFIRMATION_REQUIRED,
        ("status:claude",),
        True,
    )

    daemon.state = ServiceLifecycleState.UNHEALTHY
    reused = setup.prepare(
        service=compatible,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
    )

    assert reused.outcome is ServiceSetupOutcome.RESUME
    assert reused.intent is intent
    assert daemon.events[-2:] == ["status:claude", "restart:claude"]
    assert daemon.events.count("install:claude") == 1

    blocked_daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    blocked_setup = guided_setup(
        blocked_daemon,
        tmp_path / "blocked-setup-acknowledgement.json",
    )
    refused = blocked_setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.REFUSED,
    )
    noninteractive = blocked_setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=False,
        decision=ServiceSetupDecision.APPROVED,
    )
    assert (refused.outcome, noninteractive.outcome) == (
        ServiceSetupOutcome.REFUSED,
        ServiceSetupOutcome.NONINTERACTIVE,
    )
    assert refused.intent is noninteractive.intent is intent
    assert refused.corrective_action is ServiceSetupAction.OPEN_DASHBOARD
    assert (
        noninteractive.corrective_action is ServiceSetupAction.OPEN_DASHBOARD
    )
    assert blocked_daemon.events == ["status:claude", "status:claude"]

    capability_blocked = SetupDaemon(
        ServiceLifecycleState.ABSENT,
        provider_ready=False,
    )
    failed_acknowledgement_path = (
        tmp_path / "failed-setup-acknowledgement.json"
    )
    failed = guided_setup(
        capability_blocked,
        failed_acknowledgement_path,
    ).prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.APPROVED,
    )
    corrective_action = failed.corrective_action
    assert (
        failed.outcome,
        failed.intent,
        failed.provider_id,
        failed.message,
        corrective_action,
        None if corrective_action is None else corrective_action.value,
        tuple(capability_blocked.events),
        failed_acknowledgement_path.exists(),
    ) == (
        ServiceSetupOutcome.PROVIDER_UNAVAILABLE,
        intent,
        ProviderId.CLAUDE,
        ServiceSetupMessage.CLAUDE_UNAVAILABLE,
        ServiceSetupAction.CHECK_CLAUDE,
        "Run sidekick-usages doctor --provider claude.",
        ("status:claude", "install:claude"),
        False,
    )

    codex_intent = RefreshAccountIntent(
        provider_id=ProviderId.CODEX,
        account_id=CODEX_SAVED_ACCOUNT_ID,
    )
    codex_blocked = SetupDaemon(
        ServiceLifecycleState.READY,
        provider_ready=False,
    )
    codex_failure = guided_setup(
        codex_blocked,
        tmp_path / "codex-setup-acknowledgement.json",
    ).prepare(
        service=compatible,
        intent=codex_intent,
        interactive=True,
        decision=ServiceSetupDecision.APPROVED,
    )
    assert codex_failure.provider_id is ProviderId.CODEX
    assert codex_failure.message is ServiceSetupMessage.CODEX_UNAVAILABLE
    codex_action = codex_failure.corrective_action
    assert codex_action is ServiceSetupAction.CHECK_CODEX
    assert codex_action.value == "Run sidekick-usages doctor --provider codex."
    assert codex_blocked.events == ["status:codex"]


def test_managed_auth_migration_resumes_without_exposing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One CLI journey proves ordering, isolation, resume, and final proof."""
    scenario = managed_auth_scenario()
    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    clock = FixedClock(REFERENCE_TIME)
    output = io.StringIO()
    errors = io.StringIO()
    harness = CliHarness(
        console=Console(file=output, force_terminal=False, width=120),
        err_console=Console(file=errors, force_terminal=False, width=120),
        migration=MigrationContext(
            scenario.coordinator(
                ManagedAuthDaemonLifecycle(daemon),
                clock,
            )
        ),
    )

    first = harness.invoke(["migrate", "managed-auth", "--yes"])

    assert first.exit_code == ExitCode.MANUAL_ACTION
    assert scenario.trace == [
        "codex:codex-ready",
        "codex:codex-retry",
        "claude:claude-team",
    ]
    assert daemon.events == ["status", "install"]
    assert scenario.codex_login_events == ("codex-ready", "codex-retry")
    assert scenario.setup_preserved
    assert scenario.retry_is_stored
    first_output = output.getvalue() + errors.getvalue()
    assert "codex · codex-retry · action_required" in first_output
    assert "claude · claude-team · action_required" in first_output
    assert "due state could not be proven" in first_output
    assert "Rerun this command to resume." in first_output

    scenario.allow_codex_retry()
    second = harness.invoke(["migrate", "managed-auth", "--yes"])

    assert second.exit_code == ExitCode.SUCCESS
    assert scenario.trace[3:] == [
        "codex:codex-ready",
        "codex:codex-retry",
        "claude:claude-team",
    ]
    assert daemon.events == ["status", "install", "status"]
    assert scenario.codex_login_events == (
        "codex-ready",
        "codex-retry",
        "codex-retry",
    )
    assert scenario.all_managed
    rendered = output.getvalue() + errors.getvalue()
    assert "All saved accounts have verified managed authorities." in rendered
    for identity in MIGRATION_IDENTITIES:
        assert identity not in rendered

    scenario.restore_claude_setup_only()
    guided_before = tuple(scenario.claude.guided_account_ids)
    claude_calls_before = scenario.trace.count("claude:claude-team")
    unattended = harness.invoke(["migrate", "managed-auth", "--yes"])
    assert unattended.exit_code == ExitCode.MANUAL_ACTION
    assert tuple(scenario.claude.guided_account_ids) == guided_before
    assert scenario.trace.count("claude:claude-team") == claude_calls_before

    help_result = harness.invoke(["migrate", "managed-auth", "--help"])
    assert help_result.exit_code == ExitCode.SUCCESS
    assert "--token" not in help_result.output


def test_scriptable_use_dispatches_only_stable_selection_contract() -> None:
    """One command journey proves exact lookup and preparation."""
    selection = RecordingUseSelection()
    codex, claude, needs_login = scriptable_use_accounts(REFERENCE_TIME)
    output = io.StringIO()
    errors = io.StringIO()
    harness = CliHarness(
        Console(file=output, width=100),
        Console(file=errors, width=100),
        use=UseContext(
            AccountIndex((codex, claude, needs_login)),
            selection,
        ),
    )

    codex_result = harness.invoke(["use", "codex", "shared"])
    claude_result = harness.invoke(["use", "claude", "shared"])
    removed_override = harness.invoke(
        [
            "use",
            "claude",
            "shared",
            "--allow-remote-control-disconnect",
        ]
    )
    missing = harness.invoke(["use", "claude", "missing"])
    preparation = harness.invoke(["use", "codex", "needs-login"])
    assert codex_result.exit_code == claude_result.exit_code == 0
    assert missing.exit_code == preparation.exit_code == 1
    assert removed_override.exit_code != 0
    assert selection.calls == [
        (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID),
        (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID),
    ]
    rendered = output.getvalue() + errors.getvalue()
    assert "No saved claude account labeled 'missing'." in rendered
    assert "Next: sidekick-usages\n" in rendered
    assert "Account 'needs-login' needs interactive preparation." in rendered
    assert "Next: sidekick-usages codex login needs-login" in rendered
    assert "--allow-remote-control-disconnect" not in rendered
    assert "Continue?" not in rendered
    assert "daemon install" not in rendered
