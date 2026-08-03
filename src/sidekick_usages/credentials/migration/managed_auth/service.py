"""Resumable provider-neutral managed-auth migration."""

from collections.abc import Callable
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import classify_expiry, refresh_due
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.claude.lifetime import CLAUDE_REFRESH_MARGIN
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.migration.models.managed_auth import (
    ManagedAuthAccountResult,
    ManagedAuthPlan,
    ManagedAuthReport,
    ManagedAuthTarget,
)
from sidekick_usages.credentials.migration.models.service import (
    ManagedAuthServiceResult,
)
from sidekick_usages.credentials.migration.types.managed_auth import (
    MANAGED_AUTH_PROVIDER_ORDER,
    ClaudeManagedMigration,
    CodexManagedMigration,
    ManagedAuthAccounts,
    ManagedAuthAction,
    ManagedAuthOutcome,
    ManagedAuthServiceLifecycle,
)
from sidekick_usages.credentials.migration.types.service import (
    ManagedAuthServiceState,
)
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


class ManagedAuthMigrationCoordinator:
    """Migrate every account through its existing provider coordinator."""

    def __init__(
        self,
        accounts: ManagedAuthAccounts,
        service: ManagedAuthServiceLifecycle,
        clock: Clock,
        codex: CodexManagedMigration,
        claude: ClaudeManagedMigration,
    ) -> None:
        self._accounts = accounts
        self._service = service
        self._clock = clock
        self._codex = codex
        self._claude = claude

    def plan(self) -> ManagedAuthPlan:
        """Build a secret-safe preview from the durable account index."""
        return self._plan(self._accounts.saved_accounts())

    def restore_claude_setup_only(
        self,
        label: AccountLabel,
    ) -> CredentialLoginResult:
        """Restore one explicitly selected Claude setup-only authority."""
        account = next(
            (
                candidate
                for candidate in self._accounts.saved_accounts()
                if candidate.provider_id is ProviderId.CLAUDE
                and candidate.label == label
            ),
            None,
        )
        if account is None:
            return ProviderFailure(
                provider_id=ProviderId.CLAUDE,
                kind=ProviderFailureKind.MISSING,
                message=f"No Claude account named '{label}'.",
                action_required=True,
            )
        authority = account.authority
        if (
            not isinstance(authority, ClaudeAccountAuthority)
            or authority.setup_token is None
            or not isinstance(
                authority.subscription,
                ClaudeManagedLoginAuthority,
            )
        ):
            return ProviderFailure(
                provider_id=ProviderId.CLAUDE,
                kind=ProviderFailureKind.REJECTED,
                message=(
                    "The Claude account has no managed association to remove."
                ),
                action_required=True,
            )
        return self._claude.restore_setup_only(
            account.account_id,
            expected_identity=authority.subscription.provider_identity,
        )

    def _plan(self, accounts: tuple[SavedAccount, ...]) -> ManagedAuthPlan:
        """Order one validated account snapshot for migration."""
        return ManagedAuthPlan(
            tuple(
                self._target(account)
                for provider_id in MANAGED_AUTH_PROVIDER_ORDER
                for account in accounts
                if account.provider_id is provider_id
            )
        )

    def migrate(
        self,
        *,
        interactive: bool,
        device_auth: bool,
        approve_claude_association: Callable[[ManagedAuthTarget], bool],
        codex_events: CodexLoginEventSink,
    ) -> ManagedAuthReport:
        """Ensure readiness, then migrate every account independently."""
        plan = self.plan()
        service = self._ensure_service()
        if service.state is not ManagedAuthServiceState.READY:
            return ManagedAuthReport(
                service=service,
                accounts=(),
                all_accounts_verified=False,
            )
        results = tuple(
            self._migrate_target(
                target,
                interactive=interactive,
                device_auth=device_auth,
                approve_claude_association=approve_claude_association,
                codex_events=codex_events,
            )
            for target in plan.targets
        )
        current_accounts = self._accounts.saved_accounts()
        current_plan = self._plan(current_accounts)
        reference_time = self._clock.now()
        all_accounts_verified = _target_keys(current_plan) == _target_keys(
            plan
        ) and all(
            _managed_authority_ready(account, reference_time)
            for account in current_accounts
        )
        return ManagedAuthReport(
            service=service,
            accounts=results,
            all_accounts_verified=all_accounts_verified,
        )

    def _ensure_service(self) -> ManagedAuthServiceResult:
        current = self._service.status()
        if current.state is ManagedAuthServiceState.READY:
            return current
        if current.state is ManagedAuthServiceState.INSTALL_REQUIRED:
            return self._service.install()
        if current.state is ManagedAuthServiceState.RESTART_REQUIRED:
            return self._service.restart()
        return current

    def _migrate_target(
        self,
        target: ManagedAuthTarget,
        *,
        interactive: bool,
        device_auth: bool,
        approve_claude_association: Callable[[ManagedAuthTarget], bool],
        codex_events: CodexLoginEventSink,
    ) -> ManagedAuthAccountResult:
        before = self._accounts.read_saved(target.account_id)
        if before is None or before.label != target.label:
            return self._failure(
                target,
                ProviderFailureKind.MISSING,
                "The saved account changed before migration; rerun the "
                "managed-auth command.",
            )
        setup_before = _setup_authority(before)
        if (
            target.action is ManagedAuthAction.ASSOCIATE
            and not approve_claude_association(target)
        ):
            return ManagedAuthAccountResult(
                provider_id=target.provider_id,
                account_id=target.account_id,
                label=target.label,
                outcome=ManagedAuthOutcome.CANCELED,
                message=(
                    "Identity association was canceled; the saved setup "
                    "token remains unchanged."
                ),
                failure_kind=ProviderFailureKind.REJECTED,
            )
        if target.provider_id is ProviderId.CODEX:
            migrated = self._codex.migrate(
                target.label,
                device_auth=device_auth,
                events=codex_events,
            )
        else:
            migrated = self._claude.migrate(
                target.label,
                establish_identity=(
                    target.action is ManagedAuthAction.ASSOCIATE
                ),
                interactive=interactive,
            )
        if isinstance(migrated, ProviderFailure):
            return self._failure(
                target,
                migrated.kind,
                migrated.message,
            )
        if not isinstance(migrated, CredentialLoginSuccess):
            raise TypeError("Provider migration returned an invalid result.")
        return self._prove_ready(target, setup_before)

    def _prove_ready(
        self,
        target: ManagedAuthTarget,
        setup_before: ClaudeSetupTokenAuthority | None,
    ) -> ManagedAuthAccountResult:
        account = self._accounts.read_saved(target.account_id)
        if (
            account is None
            or account.label != target.label
            or not _managed_authority_ready(account, self._clock.now())
        ):
            return self._failure(
                target,
                ProviderFailureKind.INCOMPLETE,
                "The managed authority or its due state could not be "
                "proven; rerun this account migration.",
            )
        if (
            target.provider_id is ProviderId.CLAUDE
            and _setup_authority(account) != setup_before
        ):
            return self._failure(
                target,
                ProviderFailureKind.IDENTITY_MISMATCH,
                "The Claude setup-token authority changed unexpectedly; "
                "run doctor before retrying.",
            )
        return ManagedAuthAccountResult(
            provider_id=target.provider_id,
            account_id=target.account_id,
            label=target.label,
            outcome=ManagedAuthOutcome.READY,
            message="Managed authority is verified and ready.",
        )

    @staticmethod
    def _failure(
        target: ManagedAuthTarget,
        kind: ProviderFailureKind,
        message: str,
    ) -> ManagedAuthAccountResult:
        return ManagedAuthAccountResult(
            provider_id=target.provider_id,
            account_id=target.account_id,
            label=target.label,
            outcome=ManagedAuthOutcome.ACTION_REQUIRED,
            message=message,
            failure_kind=kind,
        )

    @staticmethod
    def _target(account: SavedAccount) -> ManagedAuthTarget:
        authority = account.authority
        if isinstance(authority, CodexAccountAuthority):
            action = (
                ManagedAuthAction.VERIFY
                if isinstance(authority.subscription, CodexManagedAuthority)
                else ManagedAuthAction.MIGRATE
            )
        elif isinstance(authority, ClaudeAccountAuthority):
            if isinstance(
                authority.subscription,
                ClaudeManagedLoginAuthority,
            ):
                action = ManagedAuthAction.VERIFY
            elif authority.subscription is None:
                action = ManagedAuthAction.ASSOCIATE
            else:
                action = ManagedAuthAction.MIGRATE
        else:
            raise TypeError("Saved account authority is invalid.")
        return ManagedAuthTarget(
            provider_id=account.provider_id,
            account_id=account.account_id,
            label=account.label,
            action=action,
        )


def _managed_authority_ready(
    account: SavedAccount,
    reference_time: datetime,
) -> bool:
    """Prove one current managed authority and non-due state."""
    authority = account.authority
    if isinstance(authority, CodexAccountAuthority):
        managed = authority.subscription
        authority_ready = (
            isinstance(managed, CodexManagedAuthority)
            and managed.health is CredentialHealth.HEALTHY
        )
    elif isinstance(authority, ClaudeAccountAuthority):
        managed = authority.subscription
        authority_ready = (
            isinstance(managed, ClaudeManagedLoginAuthority)
            and managed.health is CredentialHealth.HEALTHY
            and managed.action is CredentialAction.NONE
            and not refresh_due(
                classify_expiry(
                    account.access_expiry,
                    now=reference_time,
                ),
                now=reference_time,
                margin=CLAUDE_REFRESH_MARGIN,
            )
        )
    else:
        return False
    return (
        authority_ready
        and account.credential_health is CredentialHealth.HEALTHY
    )


def _setup_authority(
    account: SavedAccount,
) -> ClaudeSetupTokenAuthority | None:
    """Return the exact optional Claude setup-token metadata."""
    authority = account.authority
    return (
        authority.setup_token
        if isinstance(authority, ClaudeAccountAuthority)
        else None
    )


def _target_keys(
    plan: ManagedAuthPlan,
) -> tuple[tuple[ProviderId, SidekickAccountId, AccountLabel], ...]:
    """Return stable secret-free target identities without mutable actions."""
    return tuple(
        (target.provider_id, target.account_id, target.label)
        for target in plan.targets
    )
