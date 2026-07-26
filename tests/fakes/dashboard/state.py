"""Reusable cached and controller dashboard state."""

from dataclasses import replace
from datetime import datetime, timedelta

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
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
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.snapshots.activity.store import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
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

CLAUDE_PREVIEW_ACCOUNT_ID = SidekickAccountId(
    "33333333-3333-4333-8333-333333333333"
)
CLAUDE_ACTIVE_ACCOUNT_ID = SidekickAccountId(
    "44444444-4444-4444-8444-444444444444"
)
CODEX_SAVED_ACCOUNT_ID = SidekickAccountId(
    "55555555-5555-4555-8555-555555555555"
)
VALID_PROVIDER_IDENTITY = "synthetic-codex-valid"
EXTERNAL_PROVIDER_IDENTITY = "synthetic-claude-external"
_VALID_ACCOUNT_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_CONFLICT_ACCOUNT_ID = SidekickAccountId(
    "22222222-2222-4222-8222-222222222222"
)
_VALID_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_CONFLICT_AUTHORITY_ID = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_CONFLICT_PROVIDER_IDENTITY = "synthetic-codex-conflict"


def seed_cached_dashboard(
    paths: ApplicationPaths,
    reference_time: datetime,
) -> tuple[SavedAccount, SavedAccount]:
    """Persist one renamed account, one mismatch, and passive state."""
    observed_at = reference_time - timedelta(hours=2)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    original = saved_codex_account(
        _VALID_ACCOUNT_ID,
        _VALID_AUTHORITY_ID,
        "before-rename",
        VALID_PROVIDER_IDENTITY,
        observed_at,
    )
    renamed = replace(original, label=AccountLabel("after-rename"))
    conflicted = saved_codex_account(
        _CONFLICT_ACCOUNT_ID,
        _CONFLICT_AUTHORITY_ID,
        "conflicted",
        _CONFLICT_PROVIDER_IDENTITY,
        observed_at,
    )
    filesystem.commit_opaque_private(
        encode_version_three(VersionThreeDocument((renamed, conflicted)))
    )
    usage = UsageSnapshotStore(paths.usage_snapshots)
    usage.save(
        _usage(
            original,
            VALID_PROVIDER_IDENTITY,
            51,
            reference_time,
            observed_at,
        )
    )
    usage.save(
        _usage(
            conflicted,
            "unrelated-identity",
            96,
            reference_time,
            observed_at,
        )
    )
    ActivitySnapshotStore(paths.activity_snapshots).save(
        AccountTokenActivitySnapshot(
            provider_id=ProviderId.CODEX,
            provider_account_id=VALID_PROVIDER_IDENTITY,
            summary=TokenActivitySummary(
                total_tokens=9_617_297_075,
                scope=TokenActivityScope.ACCOUNT,
            ),
            fetched_at=observed_at,
        )
    )
    selected = SelectedStateStore(paths.selected_state)
    selected.save(
        SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
            account_id=None,
            provider_identity=ProviderIdentity(EXTERNAL_PROVIDER_IDENTITY),
            runtime_generation=AuthorityGeneration("external-generation"),
            verified_at=observed_at,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )
    )
    selected.save(
        SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=renamed.account_id,
            provider_identity=ProviderIdentity(VALID_PROVIDER_IDENTITY),
            runtime_generation=AuthorityGeneration("active-generation"),
            verified_at=observed_at,
            outcome=ActivationOutcome.VERIFIED,
        )
    )
    ServiceStateStore(paths.service_state).save(
        ServiceState(
            protocol_version=1,
            package_version=PackageVersion("0.6.0"),
            phase=ServicePhase.READY,
            revision=1,
            observed_at=observed_at,
            queue_recovered=True,
            journals_reconciled=True,
            broker_ready=True,
            active_workers=0,
            failure_code=None,
        )
    )
    return renamed, conflicted


def controller_snapshot(reference_time: datetime) -> DashboardSnapshot:
    """Build the controller's saved, repair, and external account state."""
    observed_at = reference_time - timedelta(hours=2)
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
            observed_at=observed_at,
            states=(DashboardActionState.EXTERNAL_ACTIVE,),
        ),
    )
    return DashboardSnapshot(
        providers=(
            DashboardProvider(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                active_account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
                verified_at=observed_at,
                actions_enabled=True,
                rows=claude_rows,
            ),
            DashboardProvider(
                provider_id=ProviderId.CODEX,
                runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
                active_account_id=None,
                verified_at=observed_at,
                actions_enabled=True,
                rows=codex_rows,
            ),
        ),
        service=DashboardService(
            ready=True,
            compatible=True,
            phase=ServicePhase.READY,
            observed_at=observed_at,
            failure_code=None,
        ),
        reference_time=reference_time,
    )


def saved_codex_account(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
    label: str,
    identity: str,
    observed_at: datetime,
    *,
    credential_health: CredentialHealth = CredentialHealth.HEALTHY,
) -> SavedAccount:
    """Build one secret-free managed Codex account."""
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
                verified_at=observed_at,
                executable_version="0.145.0",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=credential_health,
    )


def _usage(
    account: SavedAccount,
    identity: str,
    utilization: float,
    reference_time: datetime,
    observed_at: datetime,
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
                    reference_time + timedelta(hours=3),
                ),
            ),
            plan=account.plan,
        ),
        fetched_at=observed_at,
    )
