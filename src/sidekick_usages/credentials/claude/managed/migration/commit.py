"""Durable account and metrics commit for managed Claude migration."""

from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.managed.authority.service import (
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.migration.failures import (
    migration_failure,
)
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.providers.base import ProviderFailureKind
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)


class ClaudeMigrationCommitCoordinator:
    """Commit one proven Claude authority with recoverable metrics identity."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        usage_snapshots: UsageSnapshotStore,
        clock: Clock,
    ) -> None:
        self._paths = paths
        self._store = store
        self._usage_snapshots = usage_snapshots
        self._clock = clock

    def recover_pending(self) -> None:
        """Resolve durable Claude usage intents during composition."""
        account_ids = self._usage_snapshots.pending_identity_promotions(
            ProviderId.CLAUDE
        )
        for account_id in account_ids:
            lock = OperationAuthorityLock(
                self._paths.durable_operations,
                account_id,
            )
            with lock.hold():
                self._usage_snapshots.recover_identity_promotion(
                    account_id,
                    self._store.read_saved(account_id),
                )

    def recover_account(self, account: SavedAccount) -> None:
        """Resolve one account's durable usage intent under its caller lock."""
        self._usage_snapshots.recover_identity_promotion(
            account.account_id,
            account,
        )

    def promote_managed_identity(self, account: SavedAccount) -> None:
        """Bind usage to the account's current managed provider identity."""
        provider_identity = account.provider_identity
        if provider_identity is None:
            raise ValueError("Managed Claude account has no identity.")
        self._usage_snapshots.promote_identity(
            account.account_id,
            ProviderId.CLAUDE,
            provider_identity,
        )

    def commit(
        self,
        current: SavedAccount,
        authority_id: AuthorityId,
        snapshot: ClaudeAuthoritySnapshot,
    ) -> CredentialLoginResult:
        """Commit account authority, then finalize recoverable usage state."""
        authority = current.authority
        if not isinstance(authority, ClaudeAccountAuthority):
            raise ValueError("Claude account authority changed type.")
        completed_at = self._clock.now()
        candidate = replace(
            current,
            plan=snapshot.plan,
            authority=ClaudeAccountAuthority(
                setup_token=authority.setup_token,
                subscription=managed_login_authority(
                    snapshot,
                    authority_id,
                    completed_at,
                ),
            ),
            credential_health=CredentialHealth.HEALTHY,
            last_refresh_at=completed_at,
            last_refresh_status=RefreshStatus.OK,
            last_refresh_error_code=None,
        )
        try:
            self._usage_snapshots.begin_identity_promotion(
                current.account_id,
                ProviderId.CLAUDE,
                snapshot.provider_identity,
            )
        except PersistenceError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "Saved Claude usage could not be prepared; the account "
                "was not changed.",
                action_required=False,
            )
        try:
            self._store.migrate_stored_authority(
                candidate,
                expected=current,
            )
        except SourceChangedError:
            return self._reconcile_commit_error(
                current,
                candidate,
                snapshot.provider_identity,
                source_changed=True,
            )
        except PersistenceError:
            return self._reconcile_commit_error(
                current,
                candidate,
                snapshot.provider_identity,
                source_changed=False,
            )
        return self._complete_committed_migration(
            current,
            snapshot.provider_identity,
        )

    def _reconcile_commit_error(
        self,
        current: SavedAccount,
        candidate: SavedAccount,
        provider_identity: ProviderIdentity,
        *,
        source_changed: bool,
    ) -> CredentialLoginResult:
        """Resolve an account-store error without misreporting a commit."""
        try:
            observed = self._store.read_saved(current.account_id)
        except PersistenceError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "Managed Claude migration completion could not be verified; "
                "retry safely.",
                action_required=False,
            )
        if observed is not None and _migration_authority_matches(
            observed,
            candidate,
        ):
            return self._complete_committed_migration(
                current,
                provider_identity,
            )
        cleanup_pending = False
        try:
            self._usage_snapshots.abort_identity_promotion(
                current.account_id,
                ProviderId.CLAUDE,
                provider_identity,
            )
        except PersistenceError:
            cleanup_pending = True
        if source_changed:
            message = "The saved Claude account changed during migration."
            failure_kind = ProviderFailureKind.REJECTED
        else:
            message = "The managed Claude login was not committed."
            failure_kind = ProviderFailureKind.UNREADABLE
        if cleanup_pending:
            message += " Usage state will recover automatically."
        return migration_failure(
            failure_kind,
            message,
            action_required=False,
        )

    def _complete_committed_migration(
        self,
        current: SavedAccount,
        provider_identity: ProviderIdentity,
    ) -> CredentialLoginResult:
        """Finish metrics identity or leave its durable recovery intent."""
        self._complete_usage_promotion(
            current.account_id,
            provider_identity,
        )
        return CredentialLoginSuccess(current.label)

    def _complete_usage_promotion(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
    ) -> None:
        """Complete promotion or retain its durable recovery intent."""
        try:
            self._usage_snapshots.promote_identity(
                account_id,
                ProviderId.CLAUDE,
                provider_identity,
            )
        except PersistenceError:
            return


def _migration_authority_matches(
    observed: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether fresh state proves the migration authority committed."""
    return (
        observed.account_id == candidate.account_id
        and observed.label == candidate.label
        and observed.provider_id is candidate.provider_id
        and observed.authority == candidate.authority
    )
