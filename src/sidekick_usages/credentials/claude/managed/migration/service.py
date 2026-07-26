"""Official Claude migration into stable private profiles."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeStoredLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import ClaudeLoginCredentials
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
)
from sidekick_usages.credentials.authorities import (
    CredentialAuthorityError,
    CredentialResolver,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeAuthorityExpectation,
    ClaudeExchangeFailure,
    ClaudeExchangeSuccess,
    authority_expectation,
)
from sidekick_usages.credentials.claude.exchange.service import (
    ClaudeOfficialLoginExchange,
    verified_claude_exchange,
)
from sidekick_usages.credentials.claude.exchange.types import (
    ClaudeExchangeFailureKind,
    claude_exchange_storage_failure,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_authority_matches,
)
from sidekick_usages.credentials.claude.managed.migration.commit import (
    ClaudeMigrationCommitCoordinator,
)
from sidekick_usages.credentials.claude.managed.migration.failures import (
    exchange_failure,
    migration_failure,
)
from sidekick_usages.credentials.claude.managed.migration.models import (
    ClaudeMigrationRuntime,
)
from sidekick_usages.credentials.claude.managed.profile import (
    prepare_claude_managed_profile,
)
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
)
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.login.models import (
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.auth.login.service import (
    run_interactive_official_claude_login,
    verify_official_claude_login_status,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.environment import (
    claude_private_profile_environment,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities


class ClaudeManagedMigrationCoordinator:
    """Migrate one saved Claude account without touching native login."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        resolver: CredentialResolver,
        profiles: PrivateCredentialTree,
        usage_snapshots: UsageSnapshotStore,
        clock: Clock,
        *,
        runtime: ClaudeMigrationRuntime | None = None,
    ) -> None:
        resolved_runtime = (
            ClaudeMigrationRuntime() if runtime is None else runtime
        )
        self._paths = paths
        self._store = store
        self._resolver = resolver
        self._profiles = profiles
        self._clock = clock
        self._environment = resolved_runtime.environment
        self._host = resolved_runtime.host
        self._runner = resolved_runtime.runner
        self._interactive_runner = resolved_runtime.interactive_runner
        self._authority_id_factory = resolved_runtime.authority_id_factory
        self._reader = ClaudeManagedAuthorityReader(paths, profiles)
        self._exchange = ClaudeOfficialLoginExchange(
            self._reader,
            clock,
            environment=resolved_runtime.environment,
            runner=resolved_runtime.runner,
        )
        self._commits = ClaudeMigrationCommitCoordinator(
            paths,
            store,
            usage_snapshots,
            clock,
        )
        self._commits.recover_pending()

    def migrate(
        self,
        label: AccountLabel,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Migrate or recover one exact Claude account."""
        account_id = self._store.resolve_account_id(
            ProviderId.CLAUDE,
            label,
        )
        if account_id is None:
            return migration_failure(
                ProviderFailureKind.MISSING,
                f"No Claude account named '{label}'.",
            )
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        try:
            with lock.hold():
                return self._migrate_locked(
                    account_id,
                    label,
                    establish_identity=establish_identity,
                    interactive=interactive,
                )
        except PersistenceError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "Managed Claude migration is unavailable; retry shortly.",
                action_required=False,
            )

    def _migrate_locked(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        account = self._current_account(account_id, label)
        if isinstance(account, ProviderFailure):
            return account
        self._commits.recover_account(account)
        try:
            capabilities = prepare_claude_managed_profile(
                self._paths,
                self._profiles,
                account.account_id,
                environment=self._environment,
                host=self._host,
                runner=self._runner,
            )
        except ClaudeManagedError:
            return migration_failure(
                ProviderFailureKind.UNSUPPORTED,
                "Managed Claude profiles are unavailable on this system.",
            )
        return self._migrate_account(
            account,
            capabilities,
            establish_identity=establish_identity,
            interactive=interactive,
        )

    def _migrate_account(
        self,
        account: SavedAccount,
        capabilities: ClaudeCapabilities,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Dispatch one saved authority to its exact migration path."""
        authority = account.authority
        if not isinstance(authority, ClaudeAccountAuthority):
            return migration_failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                "The saved label does not belong to Claude.",
            )
        subscription = authority.subscription
        if isinstance(subscription, ClaudeManagedLoginAuthority):
            return self._verify_managed(account, subscription, capabilities)
        if isinstance(subscription, ClaudeStoredLoginAuthority):
            return self._migrate_stored(
                account,
                subscription,
                capabilities,
                interactive=interactive,
            )
        if authority.setup_token is None:
            return migration_failure(
                ProviderFailureKind.MALFORMED,
                "The saved Claude account has no credential authority.",
            )
        return self._enroll_setup_account(
            account,
            capabilities,
            establish_identity=establish_identity,
            interactive=interactive,
        )

    def _migrate_stored(
        self,
        account: SavedAccount,
        subscription: ClaudeStoredLoginAuthority,
        capabilities: ClaudeCapabilities,
        *,
        interactive: bool,
    ) -> CredentialLoginResult:
        try:
            with self._resolver.open(account) as authenticated:
                credentials = authenticated.lease.account.credentials
                if not isinstance(credentials, ClaudeLoginCredentials):
                    return migration_failure(
                        ProviderFailureKind.MALFORMED,
                        "The saved Claude subscription authority is "
                        "malformed.",
                    )
                expectation = _stored_expectation(
                    credentials,
                    subscription.provider_identity,
                )
                if isinstance(expectation, ProviderFailure):
                    return expectation
                recovered = self._recover_stored_profile(
                    capabilities,
                    expectation,
                )
                if isinstance(recovered, ClaudeExchangeSuccess):
                    exchanged = recovered
                elif isinstance(
                    recovered, ClaudeExchangeFailure
                ) and recovered.kind in {
                    ClaudeExchangeFailureKind.MISSING,
                    ClaudeExchangeFailureKind.UNCHANGED,
                }:
                    exchanged = self._exchange.provision(
                        capabilities,
                        expectation,
                        credentials.refresh_token,
                    )
                else:
                    exchanged = recovered
        except CredentialAuthorityError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "The saved Claude subscription authority is unavailable.",
            )
        if (
            isinstance(exchanged, ClaudeExchangeFailure)
            and interactive
            and exchanged.kind
            in {
                ClaudeExchangeFailureKind.LOGIN_FAILED,
                ClaudeExchangeFailureKind.MISSING,
            }
        ):
            exchanged = self._interactive_stored_exchange(
                capabilities,
                expectation,
            )
        if isinstance(exchanged, ProviderFailure):
            return exchanged
        if isinstance(exchanged, ClaudeExchangeFailure):
            return exchange_failure(exchanged.kind)
        return self._commits.commit(
            account,
            subscription.authority_id,
            exchanged.snapshot,
        )

    def _interactive_stored_exchange(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
    ) -> ClaudeExchangeSuccess | ClaudeExchangeFailure | ProviderFailure:
        """Replace a rejected legacy refresh through official browser login."""
        logged_in = self._interactive_login(capabilities)
        if isinstance(logged_in, ProviderFailure):
            return logged_in
        observed = self._read_profile(
            capabilities,
            expected_identity=expectation.provider_identity,
        )
        if isinstance(observed, ClaudeExchangeFailure):
            return observed
        return self._refresh_existing_profile(
            capabilities,
            expectation.provider_identity,
        )

    def _enroll_setup_account(
        self,
        account: SavedAccount,
        capabilities: ClaudeCapabilities,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        if not establish_identity:
            return migration_failure(
                ProviderFailureKind.INCOMPLETE,
                "Confirm identity association with --replace-identity.",
            )
        if not interactive:
            return migration_failure(
                ProviderFailureKind.REJECTED,
                "Setup-token account enrollment requires an interactive "
                "terminal.",
            )
        initial = self._setup_enrollment_profile(capabilities)
        if isinstance(initial, ProviderFailure):
            return initial
        exchanged = self._refresh_existing_profile(
            capabilities,
            initial.provider_identity,
        )
        if isinstance(exchanged, ClaudeExchangeFailure):
            return exchange_failure(exchanged.kind)
        return self._commits.commit(
            account,
            self._authority_id_factory(),
            exchanged.snapshot,
        )

    def _recover_stored_profile(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
    ) -> ClaudeExchangeSuccess | ClaudeExchangeFailure:
        """Adopt a verified prior login attempt without repeating it."""
        observed = self._read_profile(
            capabilities,
            expected_identity=expectation.provider_identity,
        )
        if isinstance(observed, ClaudeExchangeFailure):
            return observed
        return verified_claude_exchange(expectation, observed)

    def _setup_enrollment_profile(
        self,
        capabilities: ClaudeCapabilities,
    ) -> ClaudeAuthoritySnapshot | ProviderFailure:
        """Read or create the first private subscription profile."""
        initial = self._read_profile(capabilities, expected_identity=None)
        if isinstance(initial, ClaudeExchangeFailure):
            if initial.kind is not ClaudeExchangeFailureKind.MISSING:
                return exchange_failure(initial.kind)
            logged_in = self._interactive_login(capabilities)
            if isinstance(logged_in, ProviderFailure):
                return logged_in
            initial = self._read_profile(
                capabilities,
                expected_identity=None,
            )
        if isinstance(initial, ClaudeExchangeFailure):
            return exchange_failure(initial.kind)
        return initial

    def _interactive_login(
        self,
        capabilities: ClaudeCapabilities,
    ) -> ProviderFailure | None:
        environment: dict[str, str] = {}
        try:
            environment.update(
                claude_private_profile_environment(
                    self._environment,
                    process_home=capabilities.profile.config_directory,
                    config_directory=capabilities.profile.config_directory,
                )
            )
            result = run_interactive_official_claude_login(
                capabilities.executable,
                environment,
                capabilities.profile.config_directory,
                runner=self._interactive_runner,
            )
        except ClaudeManagedError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "Official Claude login could not be completed.",
            )
        finally:
            environment.clear()
        if result is ClaudeOfficialLoginResult.FAILED:
            return migration_failure(
                ProviderFailureKind.REJECTED,
                "Official Claude login did not complete successfully.",
            )
        try:
            environment = claude_private_profile_environment(
                self._environment,
                process_home=capabilities.profile.config_directory,
                config_directory=capabilities.profile.config_directory,
            )
            verify_official_claude_login_status(
                capabilities.executable,
                environment,
                capabilities.profile.config_directory,
                runner=self._runner,
            )
        except ClaudeManagedError:
            return migration_failure(
                ProviderFailureKind.REJECTED,
                "Official Claude login could not be verified.",
            )
        return None

    def _refresh_existing_profile(
        self,
        capabilities: ClaudeCapabilities,
        expected_identity: ProviderIdentity,
    ) -> ClaudeExchangeSuccess | ClaudeExchangeFailure:
        try:
            with self._reader.open_login(
                capabilities,
                self._clock.now(),
                expected_identity=expected_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                return self._exchange.provision(
                    capabilities,
                    authority_expectation(protected.snapshot),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError as error:
            return ClaudeExchangeFailure(
                claude_exchange_storage_failure(error.code)
            )

    def _read_profile(
        self,
        capabilities: ClaudeCapabilities,
        *,
        expected_identity: ProviderIdentity | None,
    ) -> ClaudeAuthoritySnapshot | ClaudeExchangeFailure:
        try:
            return self._reader.read(
                capabilities,
                self._clock.now(),
                expected_identity=expected_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError as error:
            return ClaudeExchangeFailure(
                claude_exchange_storage_failure(error.code)
            )

    def _verify_managed(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
    ) -> CredentialLoginResult:
        observed = self._read_profile(
            capabilities,
            expected_identity=subscription.provider_identity,
        )
        if isinstance(observed, ClaudeExchangeFailure):
            return exchange_failure(observed.kind)
        if not managed_authority_matches(account, subscription, observed):
            return exchange_failure(
                ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED
            )
        try:
            self._commits.promote_managed_identity(account)
        except PersistenceError:
            return migration_failure(
                ProviderFailureKind.UNREADABLE,
                "Saved Claude usage identity could not be verified.",
                action_required=False,
            )
        return CredentialLoginSuccess(account.label)

    def _current_account(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
    ) -> SavedAccount | ProviderFailure:
        account = self._store.read_saved(account_id)
        current_id = self._store.resolve_account_id(
            ProviderId.CLAUDE,
            label,
        )
        if (
            account is None
            or current_id != account_id
            or account.label != label
        ):
            return migration_failure(
                ProviderFailureKind.MISSING,
                f"No Claude account named '{label}'.",
            )
        return account


def _stored_expectation(
    credentials: ClaudeLoginCredentials,
    saved_identity: ProviderIdentity | None,
) -> ClaudeAuthorityExpectation | ProviderFailure:
    identity = credentials.identity
    if identity is None:
        return migration_failure(
            ProviderFailureKind.INCOMPLETE,
            "The saved Claude login has no verified provider identity.",
        )
    provider_identity = identity.provider_identity
    if saved_identity is not None and saved_identity != provider_identity:
        return migration_failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The saved Claude login identity does not match its account.",
        )
    refresh_expiry = credentials.refresh_expiry
    return ClaudeAuthorityExpectation(
        provider_identity=provider_identity,
        generation=claude_access_token_generation(credentials.access_token),
        access_expires_at=credentials.access_expiry.at,
        refresh_expires_at=(
            refresh_expiry.at
            if isinstance(refresh_expiry, KnownExpiry)
            else None
        ),
        scopes=credentials.scopes,
    )
