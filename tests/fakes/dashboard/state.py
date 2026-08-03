"""Reusable cached and controller dashboard state."""

from dataclasses import replace
from datetime import date, datetime, timedelta

from sidekick_usages import __version__
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
    ProviderTokenActivitySnapshot,
    TokenActivitySummary,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    ProviderAuthObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION
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
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardProvider,
    DashboardService,
    DashboardSnapshot,
)
from tests.support.persistence import seed_finalized_selections

CLAUDE_PREVIEW_ACCOUNT_ID = SidekickAccountId(
    "33333333-3333-4333-8333-333333333333"
)
CLAUDE_ACTIVE_ACCOUNT_ID = SidekickAccountId(
    "44444444-4444-4444-8444-444444444444"
)
CLAUDE_REPAIR_ACCOUNT_ID = SidekickAccountId(
    "66666666-6666-4666-8666-666666666666"
)
CLAUDE_SAVED_ACCOUNT_ID = SidekickAccountId(
    "77777777-7777-4777-8777-777777777777"
)
CLAUDE_ACTIVITY_TOTAL = 1_076_418_075
CODEX_SAVED_ACCOUNT_ID = SidekickAccountId(
    "55555555-5555-4555-8555-555555555555"
)
CODEX_RECONCILIATION_ACCOUNT_ID = SidekickAccountId(
    "88888888-8888-4888-8888-888888888888"
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
CODEX_SAVED_GENERATION = AuthorityGeneration("2026-07-25T10:00:00Z")
CODEX_NEWER_GENERATION = AuthorityGeneration("2026-07-25T10:00:01Z")
_CODEX_RUNTIME_GENERATION = AuthorityGeneration(
    "synthetic-codex-access-fingerprint"
)


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
    renamed = replace(
        original,
        label=AccountLabel("after-rename"),
        credential_health=CredentialHealth.RECONCILIATION_REQUIRED,
    )
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
    ActivitySnapshotStore(paths.activity_snapshots).save_many(
        (
            AccountTokenActivitySnapshot(
                provider_id=ProviderId.CODEX,
                provider_account_id=VALID_PROVIDER_IDENTITY,
                summary=TokenActivitySummary(
                    total_tokens=9_617_297_075,
                    scope=TokenActivityScope.ACCOUNT,
                ),
                fetched_at=observed_at,
            ),
        ),
        (
            ProviderTokenActivitySnapshot(
                provider_id=ProviderId.CLAUDE,
                summary=TokenActivitySummary(
                    total_tokens=CLAUDE_ACTIVITY_TOTAL,
                    scope=TokenActivityScope.LOCAL_INSTALLATION,
                    since=date(2025, 12, 28),
                ),
                fetched_at=observed_at,
            ),
        ),
    )
    claude_observation = ProviderAuthObservation(
        provider_id=ProviderId.CLAUDE,
        state=ProviderAuthState.ACTIVE,
        provider_identity=ProviderIdentity(EXTERNAL_PROVIDER_IDENTITY),
        generation=AuthorityGeneration("external-generation"),
        observed_at=observed_at,
    )
    codex_selected = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=renamed.account_id,
        epoch=SelectionEpoch(0),
        generation=CODEX_NEWER_GENERATION,
        finalized_at=observed_at,
    )
    current_selection = SelectedStateStore(paths.selected_state).load(
        ProviderId.CODEX
    )
    if current_selection is None:
        seed_finalized_selections(paths, codex_selected)
    elif current_selection != codex_selected:
        raise AssertionError("Synthetic Codex selection changed.")
    observations = RuntimeAuthObservationStore(paths.durable_operations)
    observations.save_native(claude_observation)
    codex_observation = ProviderAuthObservation(
        provider_id=ProviderId.CODEX,
        state=ProviderAuthState.ACTIVE,
        provider_identity=ProviderIdentity(VALID_PROVIDER_IDENTITY),
        generation=_CODEX_RUNTIME_GENERATION,
        observed_at=codex_selected.finalized_at,
    )
    observations.save_native(codex_observation)
    observations.save_projection(codex_observation)
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


def seed_broker_degraded_dashboard(
    paths: ApplicationPaths,
    reference_time: datetime,
) -> None:
    """Persist current broker-only service degradation."""
    ServiceStateStore(paths.service_state).save(
        ServiceState(
            protocol_version=PROTOCOL_VERSION,
            package_version=PackageVersion(__version__),
            phase=ServicePhase.DEGRADED,
            revision=2,
            observed_at=reference_time,
            queue_recovered=True,
            journals_reconciled=True,
            broker_ready=False,
            active_workers=0,
            failure_code=(ServiceFailureCode.CODEX_BROKER_UNAVAILABLE.value),
        )
    )


def controller_snapshot(reference_time: datetime) -> DashboardSnapshot:
    """Build saved rows with repair and runtime-mismatch states."""
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
        DashboardAccount(
            account_id=CLAUDE_REPAIR_ACCOUNT_ID,
            label=AccountLabel("claude-repair"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            credential_health=CredentialHealth.LOGIN_REQUIRED,
            active=False,
            states=(DashboardActionState.LOGIN_REQUIRED,),
        ),
        DashboardAccount(
            account_id=CLAUDE_SAVED_ACCOUNT_ID,
            label=AccountLabel("claude-saved"),
            provider_id=ProviderId.CLAUDE,
            plan="max",
            credential_health=CredentialHealth.HEALTHY,
            active=False,
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
        DashboardAccount(
            account_id=CODEX_RECONCILIATION_ACCOUNT_ID,
            label=AccountLabel("codex-reconciliation"),
            provider_id=ProviderId.CODEX,
            plan="pro",
            credential_health=CredentialHealth.RECONCILIATION_REQUIRED,
            active=False,
            states=(DashboardActionState.RECONCILIATION_REQUIRED,),
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
                generation=CODEX_SAVED_GENERATION,
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
