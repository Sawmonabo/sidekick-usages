"""Load-bearing cached dashboard-state behavior."""

import io
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.commands import usage
from sidekick_usages.cli.context import InvocationContext
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
from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    MetricsFreshness,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    AccountUsageSnapshot,
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.models.lifecycle import DaemonOperationResult
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.service import PackageVersion, ServicePhase
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.snapshots.activity import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardExternalRow,
    DashboardProvider,
    DashboardService,
    DashboardSnapshot,
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.test_support import make_application_paths

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
CLAUDE_PREVIEW_ACCOUNT_ID = SidekickAccountId(
    "33333333-3333-4333-8333-333333333333"
)
CLAUDE_ACTIVE_ACCOUNT_ID = SidekickAccountId(
    "44444444-4444-4444-8444-444444444444"
)
CODEX_SAVED_ACCOUNT_ID = SidekickAccountId(
    "55555555-5555-4555-8555-555555555555"
)
VALID_ACCOUNT_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
CONFLICT_ACCOUNT_ID = SidekickAccountId("22222222-2222-4222-8222-222222222222")
VALID_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CONFLICT_AUTHORITY_ID = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
VALID_IDENTITY = "synthetic-codex-valid"
CONFLICT_IDENTITY = "synthetic-codex-conflict"
EXTERNAL_IDENTITY = "synthetic-claude-external"
ONE_SHOT_ROUTE_COUNT = 3


class RoutingSnapshotSource:
    """Record one provider-scoped cached read."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return the synthetic dashboard with the requested scope."""
        self._events.append(f"load:{only}")
        snapshot = _controller_snapshot()
        if only is None:
            return snapshot
        return replace(
            snapshot,
            providers=(
                replace(snapshot.providers[0], rows=()),
                snapshot.providers[1],
            ),
        )


class RoutingDashboardProcess:
    """Record replacement only after observing the cached frame."""

    def __init__(self, events: list[str], output: io.StringIO) -> None:
        self._events = events
        self._output = output
        self.frame_at_replace = ""

    def replace(self, only: ProviderId | None) -> None:
        """Capture the exact output visible at the replacement boundary."""
        self.frame_at_replace = self._output.getvalue()
        self._events.append(f"replace:{only}")


class OneShotRecorder:
    """Record stable one-shot routing without composing providers."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _ctx: object) -> None:
        """Record one existing workflow dispatch."""
        self.calls += 1


def _interactive_terminal() -> bool:
    return True


def _redirected_terminal() -> bool:
    return False


class SetupDaemon(DaemonManager):
    """Record guided setup without opening platform boundaries."""

    def __init__(self, state: ServiceLifecycleState) -> None:
        self.state = state
        self.events: list[str] = []

    def status(self) -> DaemonOperationResult:
        """Record one current service check."""
        self.events.append("status")
        return self._result(self.state)

    def restart(self) -> DaemonOperationResult:
        """Record one bounded restart."""
        self.events.append("restart")
        self.state = ServiceLifecycleState.READY
        return self._result(self.state)

    def install(self) -> DaemonOperationResult:
        """Record one approved user-level installation."""
        self.events.append("install")
        self.state = ServiceLifecycleState.READY
        return self._result(self.state)

    @staticmethod
    def _result(state: ServiceLifecycleState) -> DaemonOperationResult:
        return DaemonOperationResult(
            ServiceBackendId.SYSTEMD,
            state,
            "Synthetic user-service result.",
        )


def _account(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
    label: str,
    identity: str,
) -> SavedAccount:
    return SavedAccount(
        account_id=account_id,
        label=AccountLabel(label),
        provider_id=ProviderId.CODEX,
        plan="pro",
        authority=CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=authority_id,
                provider_identity=ProviderIdentity(identity),
                generation=AuthorityGeneration("generation-private"),
                verified_at=OBSERVED_AT,
                executable_version="0.145.0",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )


def _usage(
    account: SavedAccount,
    identity: str,
    utilization: float,
) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        account_id=account.account_id,
        provider_id=account.provider_id,
        provider_identity=ProviderIdentity(identity),
        plan=account.plan,
        report=UsageReport(
            windows=(
                UsageWindow(
                    "5h",
                    utilization,
                    REFERENCE_TIME + timedelta(hours=3),
                ),
            ),
            plan=account.plan,
        ),
        fetched_at=OBSERVED_AT,
    )


def _seed_dashboard(
    paths: ApplicationPaths,
) -> tuple[SavedAccount, SavedAccount]:
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    original = _account(
        VALID_ACCOUNT_ID,
        VALID_AUTHORITY_ID,
        "before-rename",
        VALID_IDENTITY,
    )
    renamed = replace(original, label=AccountLabel("after-rename"))
    conflicted = _account(
        CONFLICT_ACCOUNT_ID,
        CONFLICT_AUTHORITY_ID,
        "conflicted",
        CONFLICT_IDENTITY,
    )
    filesystem.commit_opaque_private(
        encode_version_three(VersionThreeDocument((renamed, conflicted)))
    )
    usage = UsageSnapshotStore(paths.usage_snapshots)
    usage.save(_usage(original, VALID_IDENTITY, 51))
    usage.save(_usage(conflicted, "unrelated-identity", 96))
    ActivitySnapshotStore(paths.activity_snapshots).save(
        AccountTokenActivitySnapshot(
            provider_id=ProviderId.CODEX,
            provider_account_id=VALID_IDENTITY,
            summary=TokenActivitySummary(
                total_tokens=9_617_297_075,
                scope=TokenActivityScope.ACCOUNT,
            ),
            fetched_at=OBSERVED_AT,
        )
    )
    selected = SelectedStateStore(paths.selected_state)
    selected.save(
        SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
            account_id=None,
            provider_identity=ProviderIdentity(EXTERNAL_IDENTITY),
            runtime_generation=AuthorityGeneration("external-generation"),
            verified_at=OBSERVED_AT,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )
    )
    selected.save(
        SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=renamed.account_id,
            provider_identity=ProviderIdentity(VALID_IDENTITY),
            runtime_generation=AuthorityGeneration("active-generation"),
            verified_at=OBSERVED_AT,
            outcome=ActivationOutcome.VERIFIED,
        )
    )
    ServiceStateStore(paths.service_state).save(
        ServiceState(
            protocol_version=1,
            package_version=PackageVersion("0.6.0"),
            phase=ServicePhase.READY,
            revision=1,
            observed_at=OBSERVED_AT,
            queue_recovered=True,
            journals_reconciled=True,
            broker_ready=True,
            active_workers=0,
            failure_code=None,
        )
    )
    return renamed, conflicted


def _controller_snapshot() -> DashboardSnapshot:
    claude_rows = (
        DashboardAccount(
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
            label=AccountLabel("claude-preview"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            credential_health=CredentialHealth.REFRESH_DUE,
            active=False,
            states=(DashboardActionState.REPAIR_REQUIRED,),
        ),
        DashboardAccount(
            account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
            label=AccountLabel("claude-active"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            credential_health=CredentialHealth.HEALTHY,
            active=True,
            states=(DashboardActionState.HEALTHY,),
        ),
    )
    codex_rows = (
        DashboardAccount(
            account_id=CODEX_SAVED_ACCOUNT_ID,
            label=AccountLabel("codex-saved"),
            provider_id=ProviderId.CODEX,
            plan="pro",
            credential_health=CredentialHealth.HEALTHY,
            active=False,
            states=(DashboardActionState.HEALTHY,),
        ),
        DashboardExternalRow(
            provider_id=ProviderId.CODEX,
            observed_at=OBSERVED_AT,
            states=(DashboardActionState.EXTERNAL_ACTIVE,),
        ),
    )
    return DashboardSnapshot(
        providers=(
            DashboardProvider(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                active_account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
                verified_at=OBSERVED_AT,
                actions_enabled=True,
                rows=claude_rows,
            ),
            DashboardProvider(
                provider_id=ProviderId.CODEX,
                runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                active_account_id=None,
                verified_at=OBSERVED_AT,
                actions_enabled=True,
                rows=codex_rows,
            ),
        ),
        service=DashboardService(
            ready=True,
            compatible=True,
            phase=ServicePhase.READY,
            observed_at=OBSERVED_AT,
            failure_code=None,
        ),
        reference_time=REFERENCE_TIME,
    )


def test_cached_dashboard_joins_stable_ids_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First paint preserves cached truth and isolates one stale mismatch."""
    paths = make_application_paths(tmp_path)
    renamed, conflicted = _seed_dashboard(paths)

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
    assert EXTERNAL_IDENTITY not in rendered
    assert VALID_IDENTITY not in rendered


def test_dashboard_controller_journey_preserves_verified_truth() -> None:
    """One pure journey proves cursor, intent, and activation semantics."""
    snapshot = _controller_snapshot()
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
    runtime = DashboardRuntime(RoutingSnapshotSource(events), process)
    application = create_app()
    runner = CliRunner()
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        _interactive_terminal,
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

    one_shot = OneShotRecorder()
    monkeypatch.setattr(usage, "run", one_shot)
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        _redirected_terminal,
    )
    redirected = runner.invoke(application, [], obj=InvocationContext())
    check = runner.invoke(application, ["check"], obj=InvocationContext())
    monkeypatch.setattr(
        launch,
        "interactive_dashboard_supported",
        _interactive_terminal,
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
