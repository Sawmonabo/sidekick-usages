"""Load-bearing cached dashboard-state behavior."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.commands import usage, use
from sidekick_usages.cli.context import InvocationContext
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
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupAction,
    ServiceSetupDecision,
    ServiceSetupOutcome,
    ServiceSetupProgress,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
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
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardExternalRow,
    DashboardService,
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.fakes.dashboard.runtime import (
    OneShotRecorder,
    RoutingDashboardProcess,
    RoutingSnapshotSource,
    SetupDaemon,
    interactive_terminal,
    redirected_terminal,
)
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
from tests.test_support import CliHarness, make_application_paths

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
ONE_SHOT_ROUTE_COUNT = 3


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


def test_dashboard_controller_journey_preserves_verified_truth() -> None:
    """One pure journey proves cursor, intent, and activation semantics."""
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
    controller = controller.activation_succeeded(
        DashboardActivationProof(
            provider_id=ProviderId.CODEX,
            account_id=CODEX_SAVED_ACCOUNT_ID,
        )
    )
    assert controller.state.focused_provider is ProviderId.CODEX
    assert controller.state.account_id == CODEX_SAVED_ACCOUNT_ID
    controller = controller.move(DashboardMove.DOWN).restore()
    assert controller.state.account_id == CODEX_SAVED_ACCOUNT_ID
    assert not controller.state.external

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

    disabled = DashboardController.start(
        replace(
            snapshot,
            providers=tuple(
                replace(provider, actions_enabled=False)
                for provider in snapshot.providers
            ),
            service=replace(snapshot.service, ready=False),
        )
    ).move(DashboardMove.UP)
    assert disabled.activate_or_repair() is None
    assert disabled.refresh_account() is None
    assert disabled.refresh_due_accounts() is None

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
    intent = object()
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
    assert daemon.events == ["status", "status", "install"]
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
    assert daemon.events[-2:] == ["status", "restart"]
    assert daemon.events.count("install") == 1
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
    assert blocked_daemon.events == ["status", "status"]


def test_scriptable_use_dispatches_only_stable_selection_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One command journey proves exact lookup, preparation, and approval."""
    environment: dict[str, str] = {}
    monkeypatch.setattr(use.os, "environ", environment)
    activation = RecordingUseActivation()
    codex, claude, needs_login = scriptable_use_accounts(
        REFERENCE_TIME
    )
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

    assert codex_result.exit_code == claude_result.exit_code == 0
    assert (
        invalid_override.exit_code
        == missing.exit_code
        == preparation.exit_code
        == blocked.exit_code
        == 1
    )
    assert activation.calls == [
        (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID, False),
        (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID, True),
    ]
    assert environment == {
        "ANTHROPIC_API_KEY": "synthetic-parent-secret"
    }
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
    assert "synthetic-parent-secret" not in rendered
    assert "Continue?" not in rendered
    assert "daemon install" not in rendered
