"""Load-bearing cached dashboard-state behavior."""

import io
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.commands import usage, use
from sidekick_usages.cli.context import InvocationContext, MigrationContext
from sidekick_usages.cli.contexts.migration import (
    ManagedAuthDaemonLifecycle,
)
from sidekick_usages.cli.contexts.use import UseContext
from sidekick_usages.cli.dashboard import launch
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardActivationProof,
    DashboardMove,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.runtime import DashboardRuntime
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupAction,
    ServiceSetupDecision,
    ServiceSetupMessage,
    ServiceSetupOutcome,
    ServiceSetupProgress,
)
from sidekick_usages.cli.dashboard.models.use import UseActivationFailure
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.control.client import (
    CONTROL_ACTION_TIMEOUT_SECONDS,
)
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.types import ExecutableFailure
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardExternalRow,
    DashboardFooterKind,
    DashboardService,
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupFailure,
    UsageLookupWorkerResult,
)
from tests.fakes.dashboard.lookup_worker import (
    LookupCancellationProof,
    exercise_lookup_worker_cancellation,
)
from tests.fakes.dashboard.runtime import (
    OneShotRecorder,
    RoutingDashboardProcess,
    RoutingSnapshotSource,
    SetupDaemon,
    interactive_terminal,
    redirected_terminal,
)
from tests.fakes.dashboard.session import (
    SESSION_SOCKET,
    DashboardSessionProof,
    exercise_dashboard_session,
)
from tests.fakes.dashboard.startup import exercise_startup_reconciliation
from tests.fakes.dashboard.state import (
    CLAUDE_ACTIVE_ACCOUNT_ID,
    CLAUDE_PREVIEW_ACCOUNT_ID,
    CODEX_SAVED_ACCOUNT_ID,
    EXTERNAL_PROVIDER_IDENTITY,
    VALID_PROVIDER_IDENTITY,
    controller_snapshot,
    seed_cached_dashboard,
)
from tests.fakes.dashboard.use import (
    RecordingUseActivation,
    scriptable_use_accounts,
)
from tests.fakes.migration.managed_auth import (
    MIGRATION_IDENTITIES,
    managed_auth_scenario,
)
from tests.test_support import CliHarness, FixedClock, make_application_paths

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
ONE_SHOT_ROUTE_COUNT = 3


def _assert_execve_process_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the isolated handoff uses one qualified no-shell replacement."""
    replacements: list[tuple[Path, list[str], dict[str, str]]] = []

    def record_execve(
        executable: Path,
        arguments: list[str],
        environment: dict[str, str],
    ) -> Never:
        replacements.append((executable, arguments, environment))
        raise OSError("Synthetic no-shell replacement failure.")

    with monkeypatch.context() as replacement_boundary:
        replacement_boundary.setattr(launch.os, "execve", record_execve)
        with pytest.raises(
            launch.InteractiveDashboardLaunchError,
        ) as replacement_failure:
            launch.ExecveDashboardProcess().replace(ProviderId.CODEX)

    executable = Path(sys.executable)
    assert isinstance(replacement_failure.value.__cause__, OSError)
    assert replacements == [
        (
            executable,
            [
                sys.executable,
                "-m",
                launch.DASHBOARD_ENTRYPOINT_MODULE,
                "--only",
                ProviderId.CODEX.value,
            ],
            os.environ.copy(),
        )
    ]
    assert replacements[0][2] is not os.environ


def test_cached_dashboard_joins_stable_ids_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First paint preserves cached truth and isolates one stale mismatch."""
    paths = make_application_paths(tmp_path)
    renamed, conflicted = seed_cached_dashboard(paths, REFERENCE_TIME)

    reads: dict[Path, int] = {}
    read_opaque_private = PersistenceFilesystem.read_opaque_private

    def counted_read(
        current: PersistenceFilesystem,
    ) -> FileSnapshot | None:
        reads[current.authority_path] = (
            reads.get(current.authority_path, 0) + 1
        )
        return read_opaque_private(current)

    monkeypatch.setattr(
        PersistenceFilesystem,
        "read_opaque_private",
        counted_read,
    )

    dashboard = CachedDashboardService(paths).load(REFERENCE_TIME)

    assert reads == {
        paths.accounts: 1,
        paths.activity_snapshots: 1,
        paths.usage_snapshots: 1,
    }
    assert tuple(provider.provider_id for provider in dashboard.providers) == (
        ProviderId.CLAUDE,
        ProviderId.CODEX,
    )
    claude, codex = dashboard.providers
    assert claude.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE
    assert not claude.actions_enabled
    assert isinstance(claude.rows[0], DashboardExternalRow)
    assert claude.rows[0].states == (
        DashboardActionState.EXTERNAL_ACTIVE,
        DashboardActionState.SERVICE_UNAVAILABLE,
    )

    assert not dashboard.service.ready
    assert not dashboard.service.compatible
    assert dashboard.service.phase is ServicePhase.READY
    assert dashboard.service.observed_at == OBSERVED_AT
    assert dashboard.service.failure_code is None
    assert codex.active_account_id == renamed.account_id
    assert not codex.actions_enabled
    assert all(isinstance(row, DashboardAccount) for row in codex.rows)
    current, failed = codex.rows
    assert isinstance(current, DashboardAccount)
    assert current.account_id == renamed.account_id
    assert current.label == "after-rename"
    assert current.active
    assert current.usage is not None
    assert current.usage.observed_at == OBSERVED_AT
    assert current.usage.freshness is MetricsFreshness.STALE
    assert current.activity is not None
    assert current.activity.observed_at == OBSERVED_AT
    assert current.activity.freshness is MetricsFreshness.STALE
    assert current.states == (
        DashboardActionState.HEALTHY,
        DashboardActionState.METRICS_STALE,
        DashboardActionState.SERVICE_UNAVAILABLE,
    )
    assert isinstance(failed, DashboardAccount)
    assert failed.account_id == conflicted.account_id
    assert failed.usage is None
    assert failed.states == (
        DashboardActionState.HEALTHY,
        DashboardActionState.REPAIR_REQUIRED,
        DashboardActionState.SERVICE_UNAVAILABLE,
    )
    rendered = repr(dashboard)
    assert EXTERNAL_PROVIDER_IDENTITY not in rendered
    assert VALID_PROVIDER_IDENTITY not in rendered


def test_dashboard_controller_journey_preserves_verified_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One journey proves pure transitions and serialized live activation."""
    snapshot = controller_snapshot(REFERENCE_TIME)
    controller = DashboardController.start(snapshot)

    assert (
        controller.state.focused_provider,
        controller.state.account_id,
        controller.state.external,
    ) == (
        ProviderId.CLAUDE,
        CLAUDE_ACTIVE_ACCOUNT_ID,
        False,
    )
    verified_anchors = controller.state.anchors

    controller = controller.move(DashboardMove.UP)
    assert controller.state.account_id == CLAUDE_PREVIEW_ACCOUNT_ID
    assert controller.state.anchors == verified_anchors
    assert controller.activate_or_repair() == ActivateOrRepairIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
    )
    assert controller.refresh_account() == RefreshAccountIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
    )
    assert controller.refresh_due_accounts() == RefreshDueAccountsIntent()

    controller = controller.move(DashboardMove.UP)
    assert controller.state.account_id == CLAUDE_PREVIEW_ACCOUNT_ID
    controller = controller.restore()
    assert controller.state.account_id == CLAUDE_ACTIVE_ACCOUNT_ID
    controller = controller.move(DashboardMove.DOWN)
    assert controller.state.account_id == CLAUDE_ACTIVE_ACCOUNT_ID

    controller = controller.focus_next_provider()
    assert controller.state.focused_provider is ProviderId.CODEX
    assert controller.state.external
    assert controller.state.account_id is None
    assert controller.activate_or_repair() is None
    assert controller.refresh_account() is None

    controller = controller.move(DashboardMove.UP)
    assert controller.state.account_id == CODEX_SAVED_ACCOUNT_ID
    assert not controller.state.external
    with pytest.raises(ValueError, match="contradicts provider read-back"):
        controller.activation_succeeded(
            DashboardActivationProof(
                provider_id=ProviderId.CODEX,
                account_id=CODEX_SAVED_ACCOUNT_ID,
            )
        )
    assert controller.restore().state.external

    controller = controller.toggle_help()
    assert controller.state.help_visible
    assert not controller.toggle_help().state.help_visible

    claude, codex = snapshot.providers
    due_account, active_account = claude.rows
    assert isinstance(due_account, DashboardAccount)
    all_healthy = DashboardController.start(
        replace(
            snapshot,
            providers=(
                replace(
                    claude,
                    rows=(
                        replace(
                            due_account,
                            credential_health=CredentialHealth.HEALTHY,
                        ),
                        active_account,
                    ),
                ),
                codex,
            ),
        )
    )
    assert all_healthy.refresh_due_accounts() is None

    preparable = DashboardController.start(
        replace(
            snapshot,
            providers=tuple(
                replace(provider, actions_enabled=False)
                for provider in snapshot.providers
            ),
            service=replace(snapshot.service, ready=False),
        )
    ).move(DashboardMove.UP)
    assert preparable.activate_or_repair() == ActivateOrRepairIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
    )

    disabled = DashboardController.start(
        replace(
            preparable.snapshot,
            providers=tuple(
                replace(
                    provider,
                    runtime_state=ProviderRuntimeState.UNSUPPORTED,
                )
                for provider in preparable.snapshot.providers
            ),
        )
    ).move(DashboardMove.UP)
    assert (
        disabled.activate_or_repair(),
        disabled.refresh_account(),
        disabled.refresh_due_accounts(),
    ) == (None, None, None)

    codex_saved = codex.rows[0]
    fallback = DashboardController.start(
        replace(
            snapshot,
            providers=(
                replace(
                    claude,
                    runtime_state=ProviderRuntimeState.LOGGED_OUT,
                    active_account_id=None,
                    rows=(),
                ),
                replace(
                    codex,
                    runtime_state=ProviderRuntimeState.LOGGED_OUT,
                    active_account_id=None,
                    rows=(codex_saved,),
                ),
            ),
        )
    )
    assert fallback.state.focused_provider is ProviderId.CODEX
    assert fallback.state.account_id == CODEX_SAVED_ACCOUNT_ID
    assert not fallback.state.external
    startup = exercise_startup_reconciliation(
        snapshot,
        CLAUDE_ACTIVE_ACCOUNT_ID,
        CLAUDE_PREVIEW_ACCOUNT_ID,
    )
    assert exercise_dashboard_session(
        snapshot,
        active_account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
        preview_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        startup=startup,
        monkeypatch=monkeypatch,
    ) == DashboardSessionProof(
        control_connect_calls=(
            (SESSION_SOCKET, CONTROL_ACTION_TIMEOUT_SECONDS),
        ),
        partial_start_reaped=True,
        startup_reconciliations=(
            ProviderId.CLAUDE,
            ProviderId.CODEX,
            ProviderId.CLAUDE,
        ),
        startup_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        startup_footer_kind=DashboardFooterKind.KEYS,
        activation_locked=True,
        confirmations=(
            (
                DashboardConfirmationKind.SERVICE_SETUP,
                DashboardFooterKind.CONFIRMATION,
                "Sidekick needs one per-user service to maintain accounts "
                "and update supported sessions. It installs without "
                "administrator access. y yes / n no",
            ),
            (
                DashboardConfirmationKind.REMOTE_CONTROL,
                DashboardFooterKind.CONFIRMATION,
                "Claude Remote Control may disconnect during this switch. "
                "Continue? y yes / n no",
            ),
        ),
        activations=(
            (ProviderId.CLAUDE, CLAUDE_PREVIEW_ACCOUNT_ID, False),
            (ProviderId.CLAUDE, CLAUDE_PREVIEW_ACCOUNT_ID, True),
            (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID, False),
            (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID, False),
            (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID, False),
        ),
        setup_events=(
            "status:claude",
            "status:claude",
            "status:claude",
            "status:claude",
            "install:claude",
        ),
        setup_refusal_restored=True,
        setup_refusal_message=(
            "The Sidekick user service was not installed. "
            "Run sidekick-usages in a terminal and approve service setup."
        ),
        verified_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        success_footer_kind=DashboardFooterKind.KEYS,
        setup_not_repeated=True,
        restored_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        failure_footer_kind=DashboardFooterKind.ERROR,
        remote_control_scoped_to_claude=True,
        lookup_failure_reported=True,
        lookup_cancelled=True,
        daemon_cancelled=True,
        stream_released=True,
        closed_clients=5,
        post_close_invalidations=0,
    )


def test_cli_routes_one_cached_frame_before_isolated_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One routing journey preserves TTY and one-shot process boundaries."""
    output = io.StringIO()
    events: list[str] = []
    process = RoutingDashboardProcess(events, output)
    runtime = DashboardRuntime(
        RoutingSnapshotSource(events, REFERENCE_TIME),
        process,
    )
    application = create_app()
    runner = CliRunner()
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        interactive_terminal,
    )

    interactive = runner.invoke(
        application,
        ["--only", "codex"],
        obj=InvocationContext(
            console=Console(file=output, width=100, force_terminal=True),
            dashboard_composer=lambda: runtime,
        ),
    )

    assert interactive.exit_code == 0
    assert events == ["load:codex", "replace:codex"]
    assert "CODEX" in process.frame_at_replace
    assert "CLAUDE" not in process.frame_at_replace

    _assert_execve_process_boundary(monkeypatch)

    def reject_executable(_path: Path) -> Never:
        raise ExecutableQualificationError(ExecutableFailure.UNSAFE)

    monkeypatch.setattr(launch, "qualify_executable", reject_executable)
    qualification_output = io.StringIO()
    qualification_events: list[str] = []
    qualification = runner.invoke(
        application,
        [],
        obj=InvocationContext(
            console=Console(
                file=qualification_output,
                width=100,
                force_terminal=True,
            ),
            dashboard_composer=lambda: DashboardRuntime(
                RoutingSnapshotSource(
                    qualification_events,
                    REFERENCE_TIME,
                ),
                launch.ExecveDashboardProcess(),
            ),
        ),
    )
    qualification_frame = qualification_output.getvalue()

    assert qualification.exit_code == 1
    assert isinstance(
        qualification.exception,
        launch.InteractiveDashboardLaunchError,
    )
    assert isinstance(
        qualification.exception.__cause__,
        ExecutableQualificationError,
    )
    assert qualification_events == ["load:None"]
    assert qualification_frame.endswith(
        f"\x1b[{qualification_frame.count('\n')}B\r"
    )

    monkeypatch.setattr(launch, "qualify_executable", qualify_executable)

    cancellation = exercise_lookup_worker_cancellation(monkeypatch)
    canceled = UsageLookupWorkerResult((), UsageLookupFailure.CANCELED)
    assert cancellation == LookupCancellationProof(
        before_start_joined=True,
        before_start_results=(canceled,),
        before_start_process_count=0,
        worker_started=True,
        active_joined=True,
        active_results=(canceled,),
        active_process_count=1,
        active_reaped=True,
    )

    one_shot = OneShotRecorder()
    monkeypatch.setattr(usage, "run", one_shot)
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        redirected_terminal,
    )
    redirected = runner.invoke(application, [], obj=InvocationContext())
    check = runner.invoke(application, ["check"], obj=InvocationContext())
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        interactive_terminal,
    )
    disabled = runner.invoke(
        application,
        ["--no-interactive"],
        obj=InvocationContext(),
    )
    help_result = runner.invoke(
        application,
        ["--help"],
        obj=InvocationContext(),
    )
    version = runner.invoke(
        application,
        ["--version"],
        obj=InvocationContext(),
    )

    assert (
        redirected.exit_code,
        check.exit_code,
        disabled.exit_code,
        help_result.exit_code,
        version.exit_code,
    ) == (0, 0, 0, 0, 0)
    assert one_shot.calls == ONE_SHOT_ROUTE_COUNT
    assert events == ["load:codex", "replace:codex"]


def test_guided_setup_resumes_once_and_preserves_blocked_actions() -> None:
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
    intent = ActivateOrRepairIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
    )
    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    setup = GuidedServiceSetup(daemon)
    progress: list[ServiceSetupProgress] = []

    confirmation = setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
        progress=progress.append,
    )
    approved = setup.prepare(
        service=unavailable,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.APPROVED,
        progress=progress.append,
    )

    assert confirmation.outcome is ServiceSetupOutcome.CONFIRMATION_REQUIRED
    assert approved.outcome is ServiceSetupOutcome.RESUME
    assert confirmation.intent is approved.intent is intent
    assert daemon.events == [
        "status:claude",
        "status:claude",
        "install:claude",
    ]
    assert progress == [
        ServiceSetupProgress.CHECKING,
        ServiceSetupProgress.CHECKING,
        ServiceSetupProgress.INSTALLING,
        ServiceSetupProgress.READY,
    ]

    daemon.state = ServiceLifecycleState.UNHEALTHY
    progress.clear()
    reused = setup.prepare(
        service=compatible,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
        progress=progress.append,
    )

    assert reused.outcome is ServiceSetupOutcome.RESUME
    assert reused.intent is intent
    assert daemon.events[-2:] == ["status:claude", "restart:claude"]
    assert daemon.events.count("install:claude") == 1
    assert progress == [
        ServiceSetupProgress.CHECKING,
        ServiceSetupProgress.RESTARTING,
        ServiceSetupProgress.READY,
    ]

    blocked_daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    blocked_setup = GuidedServiceSetup(blocked_daemon)
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
        ServiceLifecycleState.READY,
        provider_ready=False,
    )
    failed = GuidedServiceSetup(capability_blocked).prepare(
        service=compatible,
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.APPROVED,
    )
    assert failed.outcome is ServiceSetupOutcome.PROVIDER_UNAVAILABLE
    assert failed.intent is intent
    assert failed.provider_id is ProviderId.CLAUDE
    assert failed.message is ServiceSetupMessage.CLAUDE_UNAVAILABLE
    corrective_action = failed.corrective_action
    assert corrective_action is ServiceSetupAction.CHECK_CLAUDE
    assert (
        corrective_action.value
        == "Run sidekick-usages doctor --provider claude."
    )
    assert capability_blocked.events == ["status:claude"]

    codex_intent = RefreshAccountIntent(
        provider_id=ProviderId.CODEX,
        account_id=CODEX_SAVED_ACCOUNT_ID,
    )
    codex_blocked = SetupDaemon(
        ServiceLifecycleState.READY,
        provider_ready=False,
    )
    codex_failure = GuidedServiceSetup(codex_blocked).prepare(
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


def test_managed_auth_migration_resumes_without_exposing_secrets() -> None:
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

    help_result = harness.invoke(["migrate", "managed-auth", "--help"])
    assert help_result.exit_code == ExitCode.SUCCESS
    assert "--token" not in help_result.output


def test_scriptable_use_dispatches_only_stable_selection_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One command journey proves exact lookup, preparation, and approval."""
    environment: dict[str, str] = {}
    monkeypatch.setattr(use.os, "environ", environment)
    activation = RecordingUseActivation()
    codex, claude, needs_login = scriptable_use_accounts(REFERENCE_TIME)
    output = io.StringIO()
    errors = io.StringIO()
    harness = CliHarness(
        Console(file=output, width=100),
        Console(file=errors, width=100),
        use=UseContext(
            AccountIndex((codex, claude, needs_login)),
            activation,
        ),
    )

    codex_result = harness.invoke(["use", "codex", "shared"])
    claude_result = harness.invoke(
        [
            "use",
            "claude",
            "shared",
            "--allow-remote-control-disconnect",
        ]
    )
    invalid_override = harness.invoke(
        [
            "use",
            "codex",
            "shared",
            "--allow-remote-control-disconnect",
        ]
    )
    missing = harness.invoke(["use", "claude", "missing"])
    preparation = harness.invoke(["use", "codex", "needs-login"])
    environment["ANTHROPIC_API_KEY"] = "synthetic-parent-secret"
    blocked = harness.invoke(["use", "claude", "shared"])
    environment.clear()
    activation.result = UseActivationFailure(
        ClaudeActivationGuardFailure.REMOTE_CONTROL_DISCONNECT_REQUIRED.failure_code
    )
    remote_required = harness.invoke(["use", "claude", "shared"])

    assert codex_result.exit_code == claude_result.exit_code == 0
    assert (
        invalid_override.exit_code
        == missing.exit_code
        == preparation.exit_code
        == blocked.exit_code
        == remote_required.exit_code
        == 1
    )
    assert activation.calls == [
        (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID, False),
        (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID, True),
        (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID, False),
    ]
    assert environment == {}
    rendered = output.getvalue() + errors.getvalue()
    assert "No saved claude account labeled 'missing'." in rendered
    assert "Next: sidekick-usages\n" in rendered
    assert "Account 'needs-login' needs interactive preparation." in rendered
    assert "Next: sidekick-usages codex login needs-login" in rendered
    assert (
        "Remote Control disconnect approval applies only to Claude."
        in rendered
    )
    assert "This shell overrides Claude account selection." in rendered
    assert "Next: unset ANTHROPIC_API_KEY" in rendered
    assert (
        "Next: sidekick-usages use claude shared "
        "--allow-remote-control-disconnect"
    ) in rendered
    assert "synthetic-parent-secret" not in rendered
    assert "Continue?" not in rendered
    assert "daemon install" not in rendered
