"""Load-bearing cached dashboard-state behavior."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    ClaudeAssociationRequest,
    DashboardActivationProof,
    DashboardMove,
    DashboardSelectionRefusal,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
)
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
)
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
    ProviderAuthState,
    ProviderRuntimeState,
    SelectionCode,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    CONTROL_ACTION_TIMEOUT_SECONDS,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.filesystem.reader import PrivateFileReader
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    PersistenceCode,
    UsageSnapshotFailureKind,
)
from sidekick_usages.providers.codex.auth.generation import (
    codex_generation_relation,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardFooter,
    DashboardNavigationKind,
    DashboardSnapshot,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshCause,
    MetricsRefreshFailureCode,
    MetricsRefreshObservation,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
)
from sidekick_usages.usage.models import FetchFailureKind
from tests.fakes.dashboard.render import CLAUDE_ACTIVE_ID
from tests.fakes.dashboard.session.journey import exercise_dashboard_session
from tests.fakes.dashboard.session.models import (
    SESSION_SOCKET,
    DashboardCacheRetryProof,
    DashboardMetricsRetryProof,
    DashboardSessionProof,
)
from tests.fakes.dashboard.startup import exercise_startup_reconciliation
from tests.fakes.dashboard.state import (
    CLAUDE_ACTIVE_ACCOUNT_ID,
    CLAUDE_ACTIVITY_TOTAL,
    CLAUDE_PREVIEW_ACCOUNT_ID,
    CLAUDE_REPAIR_ACCOUNT_ID,
    CLAUDE_SAVED_ACCOUNT_ID,
    CODEX_NEWER_GENERATION,
    CODEX_RECONCILIATION_ACCOUNT_ID,
    CODEX_SAVED_ACCOUNT_ID,
    CODEX_SAVED_GENERATION,
    EXTERNAL_PROVIDER_IDENTITY,
    VALID_PROVIDER_IDENTITY,
    controller_snapshot,
    seed_broker_degraded_dashboard,
    seed_cached_dashboard,
)
from tests.fakes.migration.managed_auth import managed_auth_scenario
from tests.support.persistence import make_application_paths
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
LOOKUP_FAILED_MESSAGE = (
    "Live metrics are unavailable. Run: sidekick-usages doctor"
)
MALFORMED_DERIVED_CACHE = (
    b'{"schema_version":1,"schema_version":1,"accounts":{}}\n'
)
CACHE_RETRY_CAUSES = (
    MetricsRefreshCause(
        stage=MetricsRefreshStage.CACHE_READ,
        code=MetricsRefreshFailureCode.ACTIVITY_MALFORMED,
    ),
    MetricsRefreshCause(
        stage=MetricsRefreshStage.CACHE_READ,
        code=MetricsRefreshFailureCode.USAGE_READ,
    ),
)


def _exercise_malformed_metric_cache_isolation(
    paths: ApplicationPaths,
    dashboard: DashboardSnapshot,
) -> str:
    """Prove cache isolation and return the original secret-free rendering."""
    claude, codex = dashboard.providers
    current = codex.rows[0]
    assert isinstance(current, DashboardAccount)

    PersistenceFilesystem(paths.usage_snapshots).commit_opaque_private(
        MALFORMED_DERIVED_CACHE
    )
    retained_activity = CachedDashboardService(paths).load(REFERENCE_TIME)
    retained_activity_current = retained_activity.providers[1].rows[0]
    assert isinstance(retained_activity_current, DashboardAccount)
    assert (
        retained_activity.usage_cache_issue,
        retained_activity.activity_cache_issue,
        retained_activity.providers[0].activity,
        retained_activity_current.usage,
        retained_activity_current.activity,
        paths.usage_snapshots.read_bytes(),
    ) == (
        UsageSnapshotFailureKind.MALFORMED,
        None,
        claude.activity,
        None,
        current.activity,
        MALFORMED_DERIVED_CACHE,
    )
    seed_cached_dashboard(paths, REFERENCE_TIME)

    PersistenceFilesystem(paths.activity_snapshots).commit_opaque_private(
        MALFORMED_DERIVED_CACHE
    )
    retained_usage = CachedDashboardService(paths).load(REFERENCE_TIME)
    retained_usage_current = retained_usage.providers[1].rows[0]
    assert isinstance(retained_usage_current, DashboardAccount)
    assert (
        retained_usage.activity_cache_issue,
        retained_usage.usage_cache_issue,
        retained_usage.providers[0].activity,
        retained_usage_current.usage,
        retained_usage_current.activity,
    ) == (
        ActivitySnapshotFailureKind.MALFORMED,
        None,
        None,
        current.usage,
        None,
    )
    return repr(dashboard)


def test_cached_dashboard_joins_stable_ids_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First paint preserves cached truth and isolates one stale mismatch."""
    paths = make_application_paths(tmp_path)
    renamed, conflicted = seed_cached_dashboard(paths, REFERENCE_TIME)

    reads: dict[Path, int] = {}
    read_opaque_private = PrivateFileReader.read_opaque_private

    def counted_read(
        current: PrivateFileReader,
    ) -> FileSnapshot | None:
        reads[current.authority_path] = (
            reads.get(current.authority_path, 0) + 1
        )
        return read_opaque_private(current)

    monkeypatch.setattr(
        PrivateFileReader,
        "read_opaque_private",
        counted_read,
    )

    dashboard = CachedDashboardService(paths).load(REFERENCE_TIME)

    assert reads == {
        paths.accounts: 1,
        paths.activity_snapshots: 1,
        (
            paths.durable_operations
            / "runtime-observations"
            / "native"
            / "claude.json"
        ): 1,
        (
            paths.durable_operations
            / "runtime-observations"
            / "native"
            / "codex.json"
        ): 1,
        (
            paths.durable_operations
            / "runtime-observations"
            / "projection"
            / "codex.json"
        ): 1,
        paths.selected_state: 1,
        paths.service_state: 1,
        paths.usage_snapshots: 1,
    }
    assert tuple(provider.provider_id for provider in dashboard.providers) == (
        ProviderId.CLAUDE,
        ProviderId.CODEX,
    )
    claude, codex = dashboard.providers
    assert claude.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE
    assert claude.status.unmanaged_sessions == 1
    assert not claude.actions_enabled
    assert claude.rows == ()
    assert claude.activity is not None
    assert claude.activity.summary.total_tokens == CLAUDE_ACTIVITY_TOTAL
    assert claude.activity.observed_at == OBSERVED_AT

    assert not dashboard.service.ready
    assert not dashboard.service.compatible
    assert dashboard.service.phase is ServicePhase.READY
    assert dashboard.service.observed_at == OBSERVED_AT
    assert dashboard.service.failure_code is None
    assert (
        codex.active_account_id,
        codex_generation_relation(
            CODEX_SAVED_GENERATION,
            CODEX_NEWER_GENERATION,
        ),
        codex.status.unmanaged_sessions,
        tuple(row.account_id for row in codex.rows),
    ) == (
        renamed.account_id,
        AuthorityGenerationRelation.NEWER,
        0,
        (renamed.account_id, conflicted.account_id),
    )
    assert not codex.actions_enabled
    assert all(isinstance(row, DashboardAccount) for row in codex.rows)
    current, failed = codex.rows
    assert isinstance(current, DashboardAccount)
    assert current.account_id == renamed.account_id
    assert current.label == "after-rename"
    assert current.active
    assert current.usage is not None
    assert current.usage.observed_at == OBSERVED_AT
    assert current.activity is not None
    assert current.activity.observed_at == OBSERVED_AT
    assert current.metrics_freshness is None
    assert current.states == (DashboardActionState.RECONCILIATION_REQUIRED,)
    assert (
        replace(
            current, metrics_freshness=MetricsFreshness.FRESH
        ).metrics_freshness
        is MetricsFreshness.FRESH
    )
    assert (
        replace(
            current, metrics_freshness=MetricsFreshness.STALE
        ).metrics_freshness
        is MetricsFreshness.STALE
    )
    assert isinstance(failed, DashboardAccount)
    assert failed.account_id == conflicted.account_id
    assert failed.usage is None
    assert failed.activity is None
    assert failed.metrics_freshness is None
    assert (
        replace(
            failed, metrics_freshness=MetricsFreshness.UNAVAILABLE
        ).metrics_freshness
        is MetricsFreshness.UNAVAILABLE
    )
    assert failed.states == (
        DashboardActionState.HEALTHY,
        DashboardActionState.REPAIR_REQUIRED,
    )
    rendered = _exercise_malformed_metric_cache_isolation(paths, dashboard)
    assert EXTERNAL_PROVIDER_IDENTITY not in rendered
    assert VALID_PROVIDER_IDENTITY not in rendered


def test_cached_dashboard_scopes_codex_broker_degradation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Broker failure blocks managed Codex but not migration or Claude."""
    paths = make_application_paths(tmp_path)
    managed_accounts = seed_cached_dashboard(paths, REFERENCE_TIME)
    seed_broker_degraded_dashboard(
        paths,
        REFERENCE_TIME,
    )

    managed = CachedDashboardService(paths).load(REFERENCE_TIME)
    managed_claude, managed_codex = managed.providers
    assert (
        managed.service.ready,
        managed.service.compatible,
        managed_claude.actions_enabled,
        managed_codex.actions_enabled,
    ) == (False, True, True, False)
    assert managed_claude.rows == ()
    assert all(isinstance(row, DashboardAccount) for row in managed_codex.rows)
    assert tuple(row.states for row in managed_codex.rows) == (
        (DashboardActionState.RECONCILIATION_REQUIRED,),
        (
            DashboardActionState.HEALTHY,
            DashboardActionState.REPAIR_REQUIRED,
        ),
    )
    selected_before = PrivateFileReader(
        paths.selected_state
    ).read_opaque_private()
    runtime_store = RuntimeAuthObservationStore(paths.durable_operations)
    runtime_before = runtime_store.observe_native(ProviderId.CODEX)
    assert runtime_before is not None
    runtime_store.save_native(
        replace(
            runtime_before,
            state=ProviderAuthState.UNREADABLE,
            provider_identity=None,
            generation=None,
            observed_at=REFERENCE_TIME,
        )
    )
    provider = CachedDashboardService(paths).load(REFERENCE_TIME).providers[1]
    assert (
        provider.runtime_state,
        provider.active_account_id,
        provider.actions_enabled,
    ) == (ProviderRuntimeState.UNREADABLE, None, False)
    assert tuple(row.states for row in provider.rows) == tuple(
        row.states for row in managed_codex.rows
    )
    assert all(
        isinstance(row, DashboardAccount) and not row.active
        for row in provider.rows
    )
    assert (
        PrivateFileReader(paths.selected_state).read_opaque_private()
        == selected_before
    )
    runtime_store.save_native(runtime_before)
    repair = DashboardController.start(managed).focus_next_provider()
    assert repair.activate_or_repair() == ActivateOrRepairIntent(
        provider_id=ProviderId.CODEX,
        account_id=managed_accounts[0].account_id,
    )

    unmanaged_accounts = tuple(
        replace(account, credential_health=CredentialHealth.UNKNOWN)
        for account in managed_auth_scenario().accounts.saved_accounts(
            ProviderId.CODEX
        )
    )

    def load_unmanaged(
        _reader: AccountIndexReader,
    ) -> tuple[SavedAccount, ...]:
        return unmanaged_accounts

    monkeypatch.setattr(AccountIndexReader, "load", load_unmanaged)
    unmanaged = CachedDashboardService(paths).load(REFERENCE_TIME)
    unmanaged_claude, unmanaged_codex = unmanaged.providers
    assert (
        unmanaged.service.ready,
        unmanaged.service.compatible,
        unmanaged_claude.actions_enabled,
        unmanaged_codex.actions_enabled,
    ) == (False, True, True, True)
    assert len(unmanaged_codex.rows) == len(unmanaged_accounts)
    assert all(
        isinstance(row, DashboardAccount)
        and row.states[0] is DashboardActionState.LOGIN_REQUIRED
        for row in unmanaged_codex.rows
    )

    setup_source = managed_auth_scenario().accounts.saved_accounts(
        ProviderId.CLAUDE
    )[0]
    setup_authority = setup_source.authority
    assert isinstance(setup_authority, ClaudeAccountAuthority)
    assert setup_authority.setup_token is not None
    setup_only_accounts = tuple(
        replace(
            setup_source,
            account_id=account_id,
            authority=ClaudeAccountAuthority(
                setup_token=setup_authority.setup_token
            ),
            credential_health=health,
        )
        for account_id, health in (
            (CLAUDE_PREVIEW_ACCOUNT_ID, CredentialHealth.HEALTHY),
            (CLAUDE_ACTIVE_ACCOUNT_ID, CredentialHealth.UNKNOWN),
            (CODEX_SAVED_ACCOUNT_ID, CredentialHealth.LOGIN_REQUIRED),
        )
    )

    def load_setup_only(
        _reader: AccountIndexReader,
    ) -> tuple[SavedAccount, ...]:
        return setup_only_accounts

    monkeypatch.setattr(AccountIndexReader, "load", load_setup_only)
    setup_only = CachedDashboardService(paths).load(REFERENCE_TIME)
    setup_rows = tuple(
        row
        for row in setup_only.providers[0].rows
        if isinstance(row, DashboardAccount)
    )
    assert tuple(row.states[0] for row in setup_rows) == (
        DashboardActionState.SWITCH_SETUP_REQUIRED,
        DashboardActionState.SWITCH_SETUP_REQUIRED,
        DashboardActionState.SETUP_REGENERATION_REQUIRED,
    )


@REQUIRES_MANAGED_RUNTIME
def test_dashboard_controller_journey_preserves_verified_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One journey proves pure transitions and serialized live activation."""
    snapshot = controller_snapshot(REFERENCE_TIME)
    controller = DashboardController.start(snapshot)

    rows = tuple(
        row for provider in snapshot.providers for row in provider.rows
    )
    assert [row.account_id for row in rows] == [
        CLAUDE_PREVIEW_ACCOUNT_ID,
        CLAUDE_ACTIVE_ACCOUNT_ID,
        CLAUDE_REPAIR_ACCOUNT_ID,
        CLAUDE_SAVED_ACCOUNT_ID,
        CODEX_SAVED_ACCOUNT_ID,
        CODEX_RECONCILIATION_ACCOUNT_ID,
    ]
    assert [len(provider.rows) for provider in snapshot.providers] == [4, 2]
    assert controller.state.account_id == CLAUDE_ACTIVE_ACCOUNT_ID
    assert controller.activate_or_repair() == ActivateOrRepairIntent(
        provider_id=ProviderId.CLAUDE,
        account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
    )

    assert (
        controller.state.focused_provider,
        controller.state.account_id,
    ) == (
        ProviderId.CLAUDE,
        CLAUDE_ACTIVE_ACCOUNT_ID,
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
    assert controller.state.account_id == CLAUDE_REPAIR_ACCOUNT_ID

    controller = controller.focus_next_provider()
    assert (
        controller.state.focused_provider,
        controller.state.account_id,
    ) == (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID)
    assert controller.activate_or_repair() == ActivateOrRepairIntent(
        provider_id=ProviderId.CODEX,
        account_id=CODEX_SAVED_ACCOUNT_ID,
    )
    with pytest.raises(ValueError, match="contradicts provider read-back"):
        controller.activation_succeeded(
            DashboardActivationProof(
                provider_id=ProviderId.CODEX,
                account_id=CODEX_SAVED_ACCOUNT_ID,
            )
        )
    assert controller.restore().state.account_id == CODEX_SAVED_ACCOUNT_ID

    controller = controller.toggle_help()
    assert controller.state.help_visible
    assert not controller.toggle_help().state.help_visible

    claude, codex = snapshot.providers
    due_account = claude.rows[0]
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
                        *claude.rows[1:],
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
    uncommitted = replace(
        preparable,
        snapshot=replace(
            preparable.snapshot,
            service=snapshot.service,
            providers=(
                replace(
                    preparable.snapshot.providers[0],
                    runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                    active_account_id=None,
                    actions_enabled=True,
                ),
                preparable.snapshot.providers[1],
            ),
        ),
    )
    assert (
        uncommitted.snapshot.service.ready,
        uncommitted.snapshot.providers[0].actions_enabled,
        preparable.activate_or_repair(),
        uncommitted.activate_or_repair(),
        uncommitted.refresh_account(),
    ) == (
        True,
        True,
        ActivateOrRepairIntent(
            provider_id=ProviderId.CLAUDE,
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        ),
        ActivateOrRepairIntent(
            provider_id=ProviderId.CLAUDE,
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        ),
        RefreshAccountIntent(
            provider_id=ProviderId.CLAUDE,
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        ),
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
    )
    assert (
        disabled.state.focused_provider,
        disabled.state.account_id,
    ) == (ProviderId.CLAUDE, CLAUDE_PREVIEW_ACCOUNT_ID)
    assert (
        disabled.activate_or_repair(),
        disabled.refresh_account(),
        disabled.refresh_due_accounts(),
    ) == (
        DashboardSelectionRefusal(
            provider_id=ProviderId.CLAUDE,
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
            code=SelectionCode.UNSUPPORTED_PROVIDER_VERSION,
        ),
        None,
        None,
    )

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
    assert (
        fallback.state.focused_provider,
        fallback.state.account_id,
    ) == (ProviderId.CODEX, CODEX_SAVED_ACCOUNT_ID)
    startup = exercise_startup_reconciliation(
        snapshot,
        CLAUDE_ACTIVE_ACCOUNT_ID,
        CLAUDE_PREVIEW_ACCOUNT_ID,
        tmp_path / "startup-setup-acknowledgement.json",
    )
    assert exercise_dashboard_session(
        snapshot,
        active_account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
        preview_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        startup=startup,
        state_root=tmp_path,
        monkeypatch=monkeypatch,
    ) == DashboardSessionProof(
        control_connect_calls=(
            (SESSION_SOCKET, CONTROL_ACTION_TIMEOUT_SECONDS),
        ),
        association_request=ClaudeAssociationRequest(
            account_id=CLAUDE_PREVIEW_ACCOUNT_ID
        ),
        association_skipped_daemon=True,
        selection_refusal_footer=DashboardFooter(
            navigation=DashboardNavigationKind.KEYS,
            status=DashboardStatus(
                kind=DashboardStatusKind.ERROR,
                message=(
                    "Saved account selection is unavailable: "
                    "provider_unavailable."
                ),
            ),
        ),
        partial_start_reaped=True,
        startup_reconciliations=(
            ProviderId.CLAUDE,
            ProviderId.CODEX,
            ProviderId.CLAUDE,
        ),
        startup_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        startup_footer=DashboardFooter(
            navigation=DashboardNavigationKind.KEYS,
            status=DashboardStatus(
                kind=DashboardStatusKind.ERROR,
                message=LOOKUP_FAILED_MESSAGE,
            ),
        ),
        activation_locked=True,
        confirmations=(
            (
                DashboardConfirmationKind.SERVICE_SETUP,
                DashboardFooter(
                    navigation=DashboardNavigationKind.KEYS,
                    status=DashboardStatus(
                        kind=DashboardStatusKind.CONFIRMATION,
                        message=(
                            "Sidekick needs one per-user service to maintain "
                            "accounts and update supported sessions. It "
                            "installs without administrator access. "
                            "y yes / n no"
                        ),
                    ),
                ),
            ),
        ),
        activations=(
            (ProviderId.CLAUDE, CLAUDE_PREVIEW_ACCOUNT_ID),
            (ProviderId.CLAUDE, CLAUDE_ACTIVE_ACCOUNT_ID),
            (ProviderId.CODEX, CODEX_RECONCILIATION_ACCOUNT_ID),
        ),
        setup_events=(
            "status:claude",
            "status:claude",
            "status:claude",
            "status:claude",
            "install:claude",
        ),
        setup_progress_sanitized=True,
        setup_refusal_restored=True,
        setup_refusal_message=(
            "The Sidekick user service was not installed. "
            "Run sidekick-usages in a terminal and approve service setup."
        ),
        verified_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        success_footer=DashboardFooter(
            navigation=DashboardNavigationKind.KEYS
        ),
        setup_not_repeated=True,
        restored_account_id=CLAUDE_PREVIEW_ACCOUNT_ID,
        failure_footer=DashboardFooter(
            navigation=DashboardNavigationKind.KEYS,
            status=DashboardStatus(
                kind=DashboardStatusKind.ERROR,
                message=(
                    "Account action failed. Run sidekick-usages doctor "
                    "--provider claude"
                ),
            ),
        ),
        lookup_failure=(MetricsFreshness.UNAVAILABLE, True),
        metrics_refresh=MetricsRefreshObservation(
            observed_at=REFERENCE_TIME,
            outcome=MetricsRefreshOutcome.PARTIAL,
            attempts=1,
            causes=(
                MetricsRefreshCause(
                    stage=MetricsRefreshStage.ACCOUNT,
                    code=FetchFailureKind.TRANSIENT,
                    provider_id=ProviderId.CLAUDE,
                    account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
                ),
                MetricsRefreshCause(
                    stage=MetricsRefreshStage.CACHE_READ,
                    code=MetricsRefreshFailureCode.ACTIVITY_MALFORMED,
                ),
            ),
        ),
        metrics_retry=DashboardMetricsRetryProof(
            worker_runs=2,
            worker_footer=DashboardFooter(
                navigation=DashboardNavigationKind.KEYS
            ),
            worker_observation=MetricsRefreshObservation(
                observed_at=REFERENCE_TIME,
                outcome=MetricsRefreshOutcome.RECOVERED,
                attempts=2,
                retry_causes=(
                    MetricsRefreshCause(
                        stage=MetricsRefreshStage.ACCOUNT,
                        code=FetchFailureKind.TRANSIENT,
                        provider_id=ProviderId.CLAUDE,
                        account_id=CLAUDE_ACTIVE_ID,
                    ),
                    MetricsRefreshCause(
                        stage=MetricsRefreshStage.WORKER,
                        code=PersistenceCode.STORE_LOCKED,
                    ),
                ),
            ),
            recovered_cache=DashboardCacheRetryProof(
                lookup_runs=1,
                snapshot_loads=2,
                footer=DashboardFooter(
                    navigation=DashboardNavigationKind.KEYS
                ),
                observation=MetricsRefreshObservation(
                    observed_at=REFERENCE_TIME,
                    outcome=MetricsRefreshOutcome.RECOVERED,
                    attempts=2,
                    retry_causes=CACHE_RETRY_CAUSES,
                ),
            ),
            failed_cache=DashboardCacheRetryProof(
                lookup_runs=1,
                snapshot_loads=2,
                footer=DashboardFooter(
                    navigation=DashboardNavigationKind.KEYS
                ),
                observation=MetricsRefreshObservation(
                    observed_at=REFERENCE_TIME,
                    outcome=MetricsRefreshOutcome.FAILED,
                    attempts=2,
                    retry_causes=CACHE_RETRY_CAUSES,
                    causes=(
                        *CACHE_RETRY_CAUSES,
                        MetricsRefreshCause(
                            stage=MetricsRefreshStage.SNAPSHOT_RELOAD,
                            code=PersistenceCode.UNREADABLE,
                        ),
                    ),
                ),
            ),
        ),
        lookup_cancelled=True,
        daemon_cancelled=True,
        stream_released=True,
        closed_clients=3,
        post_close_invalidations=0,
    )
