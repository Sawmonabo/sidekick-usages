"""Typed no-secret boundaries for one managed-auth CLI journey."""

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from sidekick_usages.cli.contexts.models import Composed
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardApplicationResult,
)
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    ClaudeStoredLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
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
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.migration.managed_auth.service import (
    ManagedAuthMigrationCoordinator,
)
from sidekick_usages.credentials.migration.types.managed_auth import (
    ManagedAuthServiceLifecycle,
)
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.auth.login.models import CodexLoginEvent

REFERENCE_TIME = datetime(2026, 7, 25, 14, tzinfo=UTC)
OBSERVED_AT = REFERENCE_TIME - timedelta(hours=2)
CLAUDE_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
CODEX_READY_ACCOUNT_ID = SidekickAccountId(
    "55555555-5555-4555-8555-555555555555"
)
CODEX_RETRY_ACCOUNT_ID = SidekickAccountId(
    "11111111-1111-4111-8111-111111111111"
)
CLAUDE_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
SETUP_AUTHORITY_ID = AuthorityId("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
CODEX_READY_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
CODEX_RETRY_AUTHORITY_ID = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
MIGRATION_IDENTITIES = (
    "synthetic-codex-valid",
    "synthetic-codex-conflict",
    "synthetic-claude-migration",
)


class _MigrationAccounts:
    """Mutable no-secret account index for one migration journey."""

    def __init__(self, accounts: tuple[SavedAccount, ...]) -> None:
        self._accounts = {account.account_id: account for account in accounts}

    def saved_accounts(
        self,
        provider_id: ProviderId | None = None,
    ) -> tuple[SavedAccount, ...]:
        """Return current accounts in original insertion order."""
        return tuple(
            account
            for account in self._accounts.values()
            if provider_id is None or account.provider_id is provider_id
        )

    def read_saved(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | None:
        """Return one current account."""
        return self._accounts.get(account_id)

    def replace(self, account: SavedAccount) -> None:
        """Replace one exact account after synthetic provider proof."""
        if account.account_id not in self._accounts:
            raise AssertionError("Migration account disappeared.")
        self._accounts[account.account_id] = account

    def label(
        self,
        provider_id: ProviderId,
        label: str,
    ) -> SavedAccount:
        """Return one exact synthetic account."""
        account_label = AccountLabel(label)
        for account in self.saved_accounts(provider_id):
            if account.label == account_label:
                return account
        raise AssertionError(f"Missing synthetic {provider_id} account.")


class _CodexMigration:
    """Simulate independent official Codex account migrations."""

    def __init__(
        self,
        accounts: _MigrationAccounts,
        trace: list[str],
    ) -> None:
        self.accounts = accounts
        self.trace = trace
        self.login_events: list[str] = []
        self.fail_labels = {AccountLabel("codex-retry")}

    def migrate(
        self,
        label: AccountLabel,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CredentialLoginResult:
        """Advance one Codex authority or return one safe account failure."""
        del device_auth
        self.trace.append(f"codex:{label}")
        account = self.accounts.label(ProviderId.CODEX, label)
        authority = account.authority
        if not isinstance(authority, CodexAccountAuthority):
            raise AssertionError("Expected Codex authority.")
        subscription = authority.subscription
        if isinstance(subscription, CodexStoredAuthority):
            self.login_events.append(str(label))
            events(CodexLoginEvent("https://auth.openai.com/authorize"))
        if label in self.fail_labels:
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.REJECTED,
                message="Official Codex login is still required.",
            )
        if isinstance(subscription, CodexStoredAuthority):
            identity = subscription.provider_identity
            if identity is None:
                raise AssertionError("Expected synthetic Codex identity.")
            self.accounts.replace(
                replace(
                    account,
                    authority=CodexAccountAuthority(
                        subscription=CodexManagedAuthority(
                            authority_id=subscription.authority_id,
                            provider_identity=identity,
                            generation=AuthorityGeneration(f"managed-{label}"),
                            verified_at=REFERENCE_TIME,
                            executable_version="0.145.0",
                            health=CredentialHealth.HEALTHY,
                        )
                    ),
                    credential_health=CredentialHealth.HEALTHY,
                )
            )
        return CredentialLoginSuccess(label)


class _ClaudeMigration:
    """Simulate the existing official Claude profile migration."""

    def __init__(
        self,
        accounts: _MigrationAccounts,
        trace: list[str],
    ) -> None:
        self.accounts = accounts
        self.trace = trace
        self._return_due_authority = True
        self._cancel_guided_association = True
        self.guided_account_ids: list[SidekickAccountId] = []
        self.guided_expected_identities: list[ProviderIdentity] = []

    def migrate(
        self,
        label: AccountLabel,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Advance one Claude subscription while preserving setup metadata."""
        del establish_identity, interactive
        self.trace.append(f"claude:{label}")
        account = self.accounts.label(ProviderId.CLAUDE, label)
        authority = account.authority
        if not isinstance(authority, ClaudeAccountAuthority):
            raise AssertionError("Expected Claude authority.")
        subscription = authority.subscription
        if isinstance(
            subscription,
            ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority,
        ):
            identity = subscription.provider_identity
            if identity is None:
                raise AssertionError("Expected synthetic Claude identity.")
            access_expires_at = REFERENCE_TIME + (
                timedelta(minutes=5)
                if self._return_due_authority
                else timedelta(hours=8)
            )
            self._return_due_authority = False
            self.accounts.replace(
                replace(
                    account,
                    authority=ClaudeAccountAuthority(
                        setup_token=authority.setup_token,
                        subscription=ClaudeManagedLoginAuthority(
                            authority_id=subscription.authority_id,
                            provider_identity=identity,
                            generation=AuthorityGeneration(f"managed-{label}"),
                            access_expires_at=access_expires_at,
                            refresh_expires_at=REFERENCE_TIME
                            + timedelta(days=30),
                            verified_at=REFERENCE_TIME,
                            executable_version="2.1.220",
                            health=CredentialHealth.HEALTHY,
                            action=CredentialAction.NONE,
                        ),
                    ),
                    credential_health=CredentialHealth.HEALTHY,
                )
            )
        return CredentialLoginSuccess(label)

    def migrate_account(
        self,
        account_id: SidekickAccountId,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Record one guided stable-ID association outcome."""
        if not establish_identity or not interactive:
            raise AssertionError("Guided association must be interactive.")
        self.trace.append("claude:guided")
        self.guided_account_ids.append(account_id)
        account = self.accounts.read_saved(account_id)
        if account is None:
            raise AssertionError("Guided account disappeared.")
        if self._cancel_guided_association:
            self._cancel_guided_association = False
            return ProviderFailure(
                provider_id=ProviderId.CLAUDE,
                kind=ProviderFailureKind.REJECTED,
                message="Official Claude login was canceled.",
            )
        return CredentialLoginSuccess(account.label)

    def prove_native_identity(self) -> ProviderIdentity:
        """Return one fresh synthetic native identity proof."""
        self.trace.append("claude:native-proof")
        return ProviderIdentity(MIGRATION_IDENTITIES[2])

    def associate_account(
        self,
        account_id: SidekickAccountId,
        *,
        expected_identity: ProviderIdentity,
    ) -> CredentialLoginResult:
        """Bind guided migration to the exact synthetic native proof."""
        self.guided_expected_identities.append(expected_identity)
        return self.migrate_account(
            account_id,
            establish_identity=True,
            interactive=True,
        )

    def restore_setup_only(
        self,
        account_id: SidekickAccountId,
        *,
        expected_identity: ProviderIdentity,
    ) -> CredentialLoginResult:
        """Restore one synthetic setup-only authority."""
        account = self.accounts.read_saved(account_id)
        if account is None or not isinstance(
            account.authority,
            ClaudeAccountAuthority,
        ):
            raise AssertionError("Expected synthetic Claude account.")
        authority = account.authority
        subscription = authority.subscription
        if (
            authority.setup_token is None
            or not isinstance(subscription, ClaudeManagedLoginAuthority)
            or subscription.provider_identity != expected_identity
        ):
            raise AssertionError("Synthetic association did not match.")
        self.accounts.replace(
            replace(
                account,
                authority=ClaudeAccountAuthority(
                    setup_token=authority.setup_token,
                    subscription=None,
                ),
                credential_health=authority.setup_token.health,
                last_refresh_at=None,
                last_refresh_status=None,
                last_refresh_error_code=None,
            )
        )
        return CredentialLoginSuccess(account.label)


@dataclass(slots=True)
class ManagedAuthScenario:
    """One resumable cross-provider migration fixture."""

    accounts: _MigrationAccounts
    codex: _CodexMigration
    claude: _ClaudeMigration
    trace: list[str]
    setup_authority: ClaudeSetupTokenAuthority
    dashboard_results: list[DashboardApplicationResult] = field(
        default_factory=list
    )

    def coordinator(
        self,
        service: ManagedAuthServiceLifecycle,
        clock: Clock,
    ) -> ManagedAuthMigrationCoordinator:
        """Build the production coordinator around typed test boundaries."""
        return ManagedAuthMigrationCoordinator(
            self.accounts,
            service,
            clock,
            self.codex,
            self.claude,
        )

    def allow_codex_retry(self) -> None:
        """Allow the previously rejected Codex account to complete."""
        self.codex.fail_labels.clear()

    def restore_claude_setup_only(self) -> None:
        """Return the synthetic Claude account to setup-only authority."""
        account = self.accounts.label(ProviderId.CLAUDE, "claude-team")
        result = self.claude.restore_setup_only(
            account.account_id,
            expected_identity=ProviderIdentity(MIGRATION_IDENTITIES[2]),
        )
        if not isinstance(result, CredentialLoginSuccess):
            raise AssertionError("Synthetic setup authority was not restored.")

    def compose_claude(
        self,
        *,
        paths: ApplicationPaths | None = None,
        clock: Clock | None = None,
    ) -> Composed[_ClaudeMigration]:
        """Compose one guided fake and retain exact cleanup counts."""
        del paths, clock
        self.trace.append("association:composed")
        resources = ExitStack()
        resources.callback(self._record_guided_close)
        return Composed(self.claude, resources)

    def _record_guided_close(self) -> None:
        """Record one closed guided composition."""
        self.trace.append("association:closed")

    def launch_dashboard(self) -> DashboardApplicationResult:
        """Return one result only after recording a closed dashboard."""
        self.trace.append("dashboard:closed")
        if not self.dashboard_results:
            raise AssertionError("Synthetic dashboard result is unavailable.")
        return self.dashboard_results.pop(0)

    @property
    def codex_login_events(self) -> tuple[str, ...]:
        """Return labels that required an official Codex login event."""
        return tuple(self.codex.login_events)

    @property
    def setup_preserved(self) -> bool:
        """Return whether the exact Claude setup authority was preserved."""
        account = self.accounts.label(ProviderId.CLAUDE, "claude-team")
        authority = account.authority
        return (
            isinstance(authority, ClaudeAccountAuthority)
            and authority.setup_token == self.setup_authority
            and isinstance(
                authority.subscription,
                ClaudeManagedLoginAuthority,
            )
        )

    @property
    def retry_is_stored(self) -> bool:
        """Return whether the rejected Codex account remains resumable."""
        account = self.accounts.label(ProviderId.CODEX, "codex-retry")
        return isinstance(account.authority.subscription, CodexStoredAuthority)

    @property
    def all_managed(self) -> bool:
        """Return whether every final account has a managed authority."""
        return all(
            account.has_managed_authority
            for account in self.accounts.saved_accounts()
        )


def managed_auth_scenario() -> ManagedAuthScenario:
    """Build one Claude-first index to prove canonical provider ordering."""
    claude = _legacy_claude()
    authority = claude.authority
    if (
        not isinstance(authority, ClaudeAccountAuthority)
        or authority.setup_token is None
    ):
        raise AssertionError("Expected synthetic setup-token authority.")
    accounts = _MigrationAccounts(
        (
            claude,
            _legacy_codex(
                CODEX_READY_ACCOUNT_ID,
                CODEX_READY_AUTHORITY_ID,
                "codex-ready",
                MIGRATION_IDENTITIES[0],
            ),
            _legacy_codex(
                CODEX_RETRY_ACCOUNT_ID,
                CODEX_RETRY_AUTHORITY_ID,
                "codex-retry",
                MIGRATION_IDENTITIES[1],
            ),
        )
    )
    trace: list[str] = []
    return ManagedAuthScenario(
        accounts,
        _CodexMigration(accounts, trace),
        _ClaudeMigration(accounts, trace),
        trace,
        authority.setup_token,
    )


def _legacy_codex(
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
            subscription=CodexStoredAuthority(
                authority_id=authority_id,
                provider_identity=ProviderIdentity(identity),
                expires_at=REFERENCE_TIME - timedelta(hours=1),
                generation=AuthorityGeneration("legacy-generation"),
                health=CredentialHealth.LOGIN_REQUIRED,
                observed_at=OBSERVED_AT,
            )
        ),
        credential_health=CredentialHealth.LOGIN_REQUIRED,
    )


def _legacy_claude() -> SavedAccount:
    return SavedAccount(
        account_id=CLAUDE_ACCOUNT_ID,
        label=AccountLabel("claude-team"),
        provider_id=ProviderId.CLAUDE,
        plan="team",
        authority=ClaudeAccountAuthority(
            setup_token=ClaudeSetupTokenAuthority(
                authority_id=SETUP_AUTHORITY_ID,
                expires_at=REFERENCE_TIME + timedelta(days=180),
                health=CredentialHealth.HEALTHY,
                observed_at=OBSERVED_AT,
            ),
            subscription=ClaudeStoredLoginAuthority(
                authority_id=CLAUDE_AUTHORITY_ID,
                provider_identity=ProviderIdentity(MIGRATION_IDENTITIES[2]),
                access_expires_at=REFERENCE_TIME - timedelta(hours=1),
                refresh_expires_at=REFERENCE_TIME + timedelta(days=30),
                health=CredentialHealth.LOGIN_REQUIRED,
                observed_at=OBSERVED_AT,
            ),
        ),
        credential_health=CredentialHealth.LOGIN_REQUIRED,
    )
