"""Representative secret-free dashboard benchmark fixtures."""

from datetime import UTC, date, datetime, timedelta
from uuid import UUID

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
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
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    TokenActivityScope,
)
from sidekick_usages.daemon.types.service import ServicePhase
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
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardActivity,
    DashboardProvider,
    DashboardService,
    DashboardSnapshot,
    DashboardUsage,
)

REFERENCE_TIME = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)
ACCOUNT_EXPIRY = REFERENCE_TIME + timedelta(days=7)
RESET_TIME = REFERENCE_TIME + timedelta(hours=3, minutes=30)
MINIMUM_ACCOUNT_COUNT = 2
REFERENCE_ACCOUNT_COUNT = 6
EXPANDED_ACCOUNT_COUNT = 24


def seed_cached_dashboard(
    paths: ApplicationPaths,
    account_count: int,
) -> None:
    """Persist representative secret-free cache state below isolated paths."""
    accounts = saved_accounts(account_count)
    seed_saved_accounts(paths, accounts)
    UsageSnapshotStore(paths.usage_snapshots).save_many(
        tuple(
            AccountUsageSnapshot(
                account_id=account.account_id,
                provider_id=account.provider_id,
                provider_identity=account.provider_identity,
                plan=account.plan,
                report=_usage_report(account, ordinal),
                fetched_at=REFERENCE_TIME,
            )
            for ordinal, account in enumerate(accounts)
        )
    )
    ActivitySnapshotStore(paths.activity_snapshots).save_many(
        tuple(
            AccountTokenActivitySnapshot(
                provider_id=account.provider_id,
                provider_account_id=str(account.provider_identity),
                summary=_activity_summary(account, ordinal),
                fetched_at=REFERENCE_TIME,
            )
            for ordinal, account in enumerate(accounts)
            if account.provider_id is ProviderId.CODEX
            and account.provider_identity is not None
        ),
        (),
    )


def seed_saved_accounts(
    paths: ApplicationPaths,
    accounts: tuple[SavedAccount, ...],
) -> None:
    """Persist one synthetic no-secret account authority."""
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    filesystem.commit_opaque_private(
        encode_version_three(VersionThreeDocument(accounts))
    )


def saved_accounts(account_count: int) -> tuple[SavedAccount, ...]:
    """Return one deterministic mixed-provider account population."""
    if account_count < MINIMUM_ACCOUNT_COUNT:
        raise ValueError("Dashboard benchmark requires both providers.")
    claude_count = max(1, account_count * 2 // 3)
    return tuple(
        _saved_account(
            index,
            (ProviderId.CLAUDE if index < claude_count else ProviderId.CODEX),
        )
        for index in range(account_count)
    )


def dashboard_snapshot(account_count: int) -> DashboardSnapshot:
    """Return one representative cached dashboard snapshot."""
    accounts = saved_accounts(account_count)
    return DashboardSnapshot(
        providers=tuple(
            _dashboard_provider(provider_id, accounts)
            for provider_id in ProviderId
        ),
        service=DashboardService(
            ready=True,
            compatible=True,
            phase=ServicePhase.READY,
            observed_at=REFERENCE_TIME,
            failure_code=None,
        ),
        reference_time=REFERENCE_TIME,
    )


def _saved_account(index: int, provider_id: ProviderId) -> SavedAccount:
    account_id = SidekickAccountId(str(UUID(int=index + 1)))
    authority_id = AuthorityId(str(UUID(int=10_000 + index)))
    identity = ProviderIdentity(f"benchmark-provider-{index}")
    generation = AuthorityGeneration(f"benchmark-generation-{index}")
    if provider_id is ProviderId.CLAUDE:
        authority = ClaudeAccountAuthority(
            subscription=ClaudeManagedLoginAuthority(
                authority_id=authority_id,
                provider_identity=identity,
                generation=generation,
                access_expires_at=ACCOUNT_EXPIRY,
                refresh_expires_at=None,
                verified_at=REFERENCE_TIME,
                executable_version="benchmark",
                health=CredentialHealth.HEALTHY,
                action=CredentialAction.NONE,
            )
        )
        plan = "max"
    else:
        authority = CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=authority_id,
                provider_identity=identity,
                generation=generation,
                verified_at=REFERENCE_TIME,
                executable_version="benchmark",
                health=CredentialHealth.HEALTHY,
            )
        )
        plan = "pro"
    return SavedAccount(
        account_id=account_id,
        label=AccountLabel(f"{provider_id.value}-{index + 1}@example.test"),
        provider_id=provider_id,
        plan=plan,
        authority=authority,
        credential_health=CredentialHealth.HEALTHY,
    )


def _dashboard_provider(
    provider_id: ProviderId,
    accounts: tuple[SavedAccount, ...],
) -> DashboardProvider:
    provider_accounts = tuple(
        account for account in accounts if account.provider_id is provider_id
    )
    active_account_id = provider_accounts[0].account_id
    return DashboardProvider(
        provider_id=provider_id,
        runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
        active_account_id=active_account_id,
        verified_at=REFERENCE_TIME,
        actions_enabled=True,
        rows=tuple(
            _dashboard_account(
                account,
                active=account.account_id == active_account_id,
                ordinal=ordinal,
            )
            for ordinal, account in enumerate(provider_accounts)
        ),
    )


def _dashboard_account(
    account: SavedAccount,
    *,
    active: bool,
    ordinal: int,
) -> DashboardAccount:
    return DashboardAccount(
        account_id=account.account_id,
        label=account.label,
        provider_id=account.provider_id,
        plan=account.plan,
        credential_health=account.credential_health,
        active=active,
        states=(
            DashboardActionState.HEALTHY,
            DashboardActionState.METRICS_STALE,
        ),
        usage=DashboardUsage(
            plan=account.plan,
            report=_usage_report(account, ordinal),
            observed_at=REFERENCE_TIME,
        ),
        activity=DashboardActivity(
            summary=_activity_summary(account, ordinal),
            observed_at=REFERENCE_TIME,
        ),
    )


def _usage_report(
    account: SavedAccount,
    ordinal: int,
) -> UsageReport:
    return UsageReport(
        windows=(
            UsageWindow("5h", float(ordinal % 20), RESET_TIME),
            UsageWindow("7d", float(40 + ordinal % 50), RESET_TIME),
        ),
        plan=account.plan,
    )


def _activity_summary(
    account: SavedAccount,
    ordinal: int,
) -> TokenActivitySummary:
    return TokenActivitySummary(
        total_tokens=1_000_000 + ordinal,
        scope=(
            TokenActivityScope.LOCAL_INSTALLATION
            if account.provider_id is ProviderId.CLAUDE
            else TokenActivityScope.ACCOUNT
        ),
        since=date(2026, 1, 1),
    )
