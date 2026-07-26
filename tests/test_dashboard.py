"""Load-bearing cached dashboard-state behavior."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
from sidekick_usages.daemon.models.service import ServiceState
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
)
from sidekick_usages.usage.dashboard.service import CachedDashboardService
from tests.test_support import make_application_paths

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
VALID_ACCOUNT_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
CONFLICT_ACCOUNT_ID = SidekickAccountId("22222222-2222-4222-8222-222222222222")
VALID_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CONFLICT_AUTHORITY_ID = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
VALID_IDENTITY = "synthetic-codex-valid"
CONFLICT_IDENTITY = "synthetic-codex-conflict"
EXTERNAL_IDENTITY = "synthetic-claude-external"


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
