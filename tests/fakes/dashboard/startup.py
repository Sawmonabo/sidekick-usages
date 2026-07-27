"""Synthetic provider-isolated dashboard startup reconciliation."""

from pathlib import Path

from sidekick_usages.cli.dashboard.models.session import (
    DashboardStartupReconciliation,
    DashboardStartupReconciliationState,
)
from sidekick_usages.cli.dashboard.session import (
    LOOKUP_FAILED_MESSAGE,
    InteractiveDashboardSession,
)
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardNavigationKind,
    DashboardSnapshot,
    DashboardStatus,
    DashboardStatusKind,
)
from tests.fakes.dashboard.runtime import SetupDaemon
from tests.fakes.dashboard.session.control import SessionControlConnector
from tests.fakes.dashboard.session.models import (
    SESSION_SOCKET,
    DashboardStartupProof,
)
from tests.fakes.dashboard.session.snapshots import (
    SessionInvalidationProbe,
    SessionLookupWorker,
    SessionMetricsRefreshSink,
    SessionSnapshotSource,
)
from tests.fakes.dashboard.setup import guided_setup
from tests.support.time import FixedClock


def exercise_startup_reconciliation(
    snapshot: DashboardSnapshot,
    active_account_id: SidekickAccountId,
    preview_account_id: SidekickAccountId,
    acknowledgement_path: Path,
) -> DashboardStartupProof:
    """Prove degraded startup isolates providers and retries once."""
    snapshots = SessionSnapshotSource(snapshot)
    daemon = SetupDaemon(ServiceLifecycleState.UNHEALTHY)
    connector = SessionControlConnector(daemon, snapshots)
    connector.reconciliation_targets = {
        ProviderId.CLAUDE: preview_account_id,
    }
    connector.reconciliation_failures = {ProviderId.CLAUDE}
    connector.allow_degraded = True
    invalidation = SessionInvalidationProbe()
    lookup = SessionLookupWorker(
        active_account_id,
        block=True,
        account_failure=True,
    )
    session = InteractiveDashboardSession(
        snapshot,
        snapshots=snapshots,
        only=None,
        lookup=lookup,
        metrics_refresh=SessionMetricsRefreshSink(
            FixedClock(snapshot.reference_time)
        ),
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=guided_setup(daemon, acknowledgement_path),
        environment={},
    )
    session.bind_invalidator(invalidation)
    session.start()
    try:
        expected_reconciliations = [
            ProviderId.CLAUDE,
            ProviderId.CODEX,
            ProviderId.CLAUDE,
        ]
        invalidation.wait_for(
            lambda: (
                connector.reconciliations == expected_reconciliations
                and session.view.controller.account_id == preview_account_id
            )
        )
        assert session.view.footer == DashboardFooter(
            navigation=DashboardNavigationKind.KEYS
        )
        session.startup_reconciled(
            DashboardStartupReconciliation(
                ProviderId.CLAUDE,
                DashboardStartupReconciliationState.UNAVAILABLE,
            )
        )
        invalidation.wait_for(lambda: session.view.footer.status is not None)
        invalidations_before_lookup = invalidation.count
        lookup.release()
        invalidation.wait_for(
            lambda: invalidation.count >= invalidations_before_lookup + 2
        )
        startup_status = session.view.footer.status
        assert startup_status is not None
        assert startup_status.kind is DashboardStatusKind.ERROR
        session.startup_reconciled(
            DashboardStartupReconciliation(
                ProviderId.CLAUDE,
                DashboardStartupReconciliationState.VERIFIED,
            )
        )
        expected_footer = DashboardFooter(
            navigation=DashboardNavigationKind.KEYS,
            status=DashboardStatus(
                kind=DashboardStatusKind.ERROR,
                message=LOOKUP_FAILED_MESSAGE,
            ),
        )
        invalidation.wait_for(lambda: session.view.footer == expected_footer)
        return (
            tuple(connector.reconciliations),
            session.view.controller.account_id,
            session.view.footer,
        )
    finally:
        session.close()
