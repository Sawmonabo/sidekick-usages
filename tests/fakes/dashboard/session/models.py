"""Dashboard-session constants and captured proof models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.cli.dashboard.models.controller import (
    ClaudeAssociationRequest,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
)
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
)
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshObservation,
)

SESSION_WAIT_SECONDS = 2.0
DEFAULT_TEST_CONTROL_TIMEOUT_SECONDS = 5.0
SESSION_SOCKET = Path("/synthetic/sidekick-supervisor.sock")
SESSION_REQUEST_ID = RequestId("66666666-6666-4666-8666-666666666666")
SESSION_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
REMOTE_CONTROL_REQUIRED_CODE = (
    ClaudeActivationGuardFailure.REMOTE_CONTROL_DISCONNECT_REQUIRED
).failure_code

type DashboardConfirmationProof = tuple[
    DashboardConfirmationKind | None,
    DashboardFooter,
]
type DashboardStartupProof = tuple[
    tuple[ProviderId, ...],
    SidekickAccountId | None,
    DashboardFooter,
]
type DashboardLookupFailureProof = tuple[
    MetricsFreshness,
    bool,
]


@dataclass(frozen=True, slots=True)
class DashboardCacheRetryProof:
    """One bounded cache-only retry result."""

    lookup_runs: int
    snapshot_loads: int
    footer: DashboardFooter
    observation: MetricsRefreshObservation


@dataclass(frozen=True, slots=True)
class DashboardMetricsRetryProof:
    """Worker and cache retry behavior from isolated session owners."""

    worker_runs: int
    worker_footer: DashboardFooter
    worker_observation: MetricsRefreshObservation
    recovered_cache: DashboardCacheRetryProof
    failed_cache: DashboardCacheRetryProof


@dataclass(frozen=True, slots=True)
class DashboardSessionProof:
    """Load-bearing states captured from one serialized session journey."""

    control_connect_calls: tuple[tuple[Path, float | None], ...]
    association_request: ClaudeAssociationRequest | None
    association_skipped_daemon: bool
    selection_refusal_footer: DashboardFooter
    partial_start_reaped: bool
    startup_reconciliations: tuple[ProviderId, ...]
    startup_account_id: SidekickAccountId | None
    startup_footer: DashboardFooter
    activation_locked: bool
    confirmations: tuple[DashboardConfirmationProof, ...]
    activations: tuple[tuple[ProviderId, SidekickAccountId, bool], ...]
    setup_events: tuple[str, ...]
    setup_progress_sanitized: bool
    setup_refusal_restored: bool
    setup_refusal_message: str | None
    verified_account_id: SidekickAccountId | None
    success_footer: DashboardFooter
    setup_not_repeated: bool
    restored_account_id: SidekickAccountId | None
    failure_footer: DashboardFooter
    remote_control_scoped_to_claude: bool
    lookup_failure: DashboardLookupFailureProof
    metrics_refresh: MetricsRefreshObservation
    metrics_retry: DashboardMetricsRetryProof
    lookup_cancelled: bool
    daemon_cancelled: bool
    stream_released: bool
    closed_clients: int
    post_close_invalidations: int
