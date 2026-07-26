"""Load-bearing cached dashboard-state behavior."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardActivationProof,
    DashboardMove,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardConfirmationKind,
)
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    MetricsFreshness,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    CONTROL_ACTION_TIMEOUT_SECONDS,
)
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardExternalRow,
    DashboardFooterKind,
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.fakes.dashboard.session.journey import exercise_dashboard_session
from tests.fakes.dashboard.session.models import (
    SESSION_SOCKET,
    DashboardSessionProof,
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
from tests.support.persistence import make_application_paths
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)


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


@REQUIRES_MANAGED_RUNTIME
def test_dashboard_controller_journey_preserves_verified_truth(
    tmp_path: Path,
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
        setup_progress_sanitized=True,
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
