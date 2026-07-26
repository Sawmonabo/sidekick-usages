"""Synthetic provider-isolated dashboard startup reconciliation."""

from sidekick_usages.cli.dashboard.session import InteractiveDashboardSession
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.usage.dashboard.models import DashboardSnapshot
from tests.fakes.dashboard.runtime import SetupDaemon
from tests.fakes.dashboard.session import (
    SESSION_SOCKET,
    DashboardStartupProof,
    SessionControlConnector,
    SessionInvalidationProbe,
    SessionLookupWorker,
    SessionSnapshotSource,
)


def exercise_startup_reconciliation(
    snapshot: DashboardSnapshot,
    active_account_id: SidekickAccountId,
    preview_account_id: SidekickAccountId,
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
    session = InteractiveDashboardSession(
        snapshot,
        snapshots=snapshots,
        only=None,
        lookup=SessionLookupWorker(active_account_id),
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=GuidedServiceSetup(daemon),
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
        return (
            tuple(connector.reconciliations),
            session.view.controller.account_id,
            session.view.footer.kind,
        )
    finally:
        session.close()
