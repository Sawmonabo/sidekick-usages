"""Managed Claude private-profile maintenance coordination."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSubscriptionAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import (
    KnownExpiry,
    classify_expiry,
    refresh_due,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.lifetime import (
    CLAUDE_REFRESH_MARGIN,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    ClaudeManagedAuthorityResult,
    ClaudeOfficialLoginAttempt,
    ClaudeVerifiedAuthorityExchange,
    require_managed_claude_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.credentials.claude.managed.profile import (
    prepare_claude_managed_profile,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    OperationAuthorityLock,
)
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.environment import (
    claude_refresh_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.login.service import (
    run_official_claude_login,
    verify_official_claude_login_status,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.managed.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.models import ClaudeExecutable
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner


class ClaudeManagedAuthorityCoordinator:
    """Read and refresh one stable provider-owned Claude profile at a time."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        profiles: PrivateCredentialTree,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
        host: HostPlatform | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> None:
        self._paths = paths
        self._store = store
        self._profiles = profiles
        self._clock = clock
        self._environment = environment
        self._host = host
        self._runner = runner
        self._reader = ClaudeManagedAuthorityReader(paths, profiles)

    def maintain(
        self,
        account_id: SidekickAccountId,
    ) -> ClaudeManagedAuthorityResult:
        """Maintain one due private authority under its account lock."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.maintain_with_authority(account_id, authority)

    def refresh(
        self,
        account_id: SidekickAccountId,
    ) -> ClaudeManagedAuthorityResult:
        """Force one private authority through official Claude login."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.refresh_with_authority(account_id, authority)

    def maintain_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> ClaudeManagedAuthorityResult:
        """Refresh one due private authority or verify fixed lifetime."""
        return self._operate(account_id, authority, forced=False)

    def refresh_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> ClaudeManagedAuthorityResult:
        """Force official login while one worker owns this account."""
        return self._operate(account_id, authority, forced=True)

    def _operate(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        *,
        forced: bool,
    ) -> ClaudeManagedAuthorityResult:
        authority.require(account_id)
        account = self._saved_account(account_id)
        subscription = self._subscription(account)
        if subscription is None:
            outcome = (
                ClaudeManagedOutcome.LOGIN_REQUIRED
                if forced
                else ClaudeManagedOutcome.FIXED_LIFETIME
            )
            return self._result(account, outcome)
        if not isinstance(subscription, ClaudeManagedLoginAuthority):
            return self._persist_failure(
                account,
                ClaudeManagedOutcome.INCOMPATIBLE,
            )
        return self._operate_managed(
            account,
            subscription,
            forced=forced,
        )

    def _operate_managed(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        *,
        forced: bool,
    ) -> ClaudeManagedAuthorityResult:
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
            return self._persist_failure(
                account,
                ClaudeManagedOutcome.INCOMPATIBLE,
            )
        prepared = self._prepare_attempt(
            account,
            subscription,
            capabilities,
            forced=forced,
        )
        if isinstance(prepared, ClaudeManagedAuthorityResult):
            return prepared
        return self._verify_attempt(
            account,
            subscription,
            capabilities,
            prepared,
        )

    def _prepare_attempt(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
        *,
        forced: bool,
    ) -> ClaudeOfficialLoginAttempt | ClaudeManagedAuthorityResult:
        reference_time = self._clock.now()
        try:
            with self._reader.open_login(
                capabilities,
                reference_time,
                expected_identity=subscription.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                before = protected.snapshot
                if not _saved_authority_matches(
                    account,
                    subscription,
                    before,
                ):
                    return self._persist_failure(
                        account,
                        ClaudeManagedOutcome.RECONCILIATION_REQUIRED,
                    )
                if before.health is CredentialHealth.LOGIN_REQUIRED:
                    return self._persist_failure(
                        account,
                        ClaudeManagedOutcome.LOGIN_REQUIRED,
                    )
                if not forced and not refresh_due(
                    classify_expiry(
                        KnownExpiry(before.access_expires_at),
                        now=reference_time,
                    ),
                    now=reference_time,
                    margin=CLAUDE_REFRESH_MARGIN,
                ):
                    return self._result(
                        account,
                        ClaudeManagedOutcome.HEALTHY,
                    )
                return ClaudeOfficialLoginAttempt(
                    before,
                    self._run_login(
                        capabilities.profile.config_directory,
                        capabilities.executable,
                        protected.refresh_token,
                        protected.scopes,
                    ),
                )
        except ClaudeProtectedStorageError as error:
            return self._persist_failure(
                account,
                _storage_outcome(error.code),
            )

    def _verify_attempt(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
        attempt: ClaudeOfficialLoginAttempt,
    ) -> ClaudeManagedAuthorityResult:
        outcome = attempt.outcome
        if outcome is None:
            try:
                verify_official_claude_login_status(
                    capabilities.executable,
                    self._environment,
                    capabilities.profile.config_directory,
                    capabilities.profile.config_directory,
                    capabilities.profile.config_directory,
                    runner=self._runner,
                )
            except ClaudeManagedError:
                outcome = ClaudeManagedOutcome.RECONCILIATION_REQUIRED
        try:
            after = self._reader.read(
                capabilities,
                self._clock.now(),
                expected_identity=subscription.provider_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError:
            return self._persist_failure(
                account,
                ClaudeManagedOutcome.RECONCILIATION_REQUIRED,
            )
        if outcome is not None:
            verified_outcome = (
                outcome
                if after == attempt.before
                else ClaudeManagedOutcome.RECONCILIATION_REQUIRED
            )
            return self._persist_failure(account, verified_outcome)
        invalid = _invalid_transition(attempt.before, after)
        if invalid is not None:
            return self._persist_failure(account, invalid)
        return self._persist_exchange(
            ClaudeVerifiedAuthorityExchange(
                account,
                attempt.before,
                after,
            )
        )

    def _run_login(
        self,
        config_directory: Path,
        executable: ClaudeExecutable,
        refresh_token: str,
        scopes: tuple[str, ...],
    ) -> ClaudeManagedOutcome | None:
        environment: dict[str, str] = {}
        try:
            environment.update(
                claude_refresh_environment(
                    self._environment,
                    process_home=config_directory,
                    config_directory=config_directory,
                    refresh_token=refresh_token,
                    scopes=scopes,
                )
            )
            result = run_official_claude_login(
                executable,
                environment,
                config_directory,
                runner=self._runner,
            )
        except ClaudeProcessError:
            return ClaudeManagedOutcome.INCOMPATIBLE
        except ClaudeManagedError as error:
            return (
                ClaudeManagedOutcome.TIMED_OUT
                if error.code
                is ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT
                else ClaudeManagedOutcome.TRANSIENT
            )
        finally:
            environment.clear()
        return (
            None
            if result is ClaudeOfficialLoginResult.SUCCEEDED
            else ClaudeManagedOutcome.TRANSIENT
        )

    def _persist_exchange(
        self,
        exchange: ClaudeVerifiedAuthorityExchange,
    ) -> ClaudeManagedAuthorityResult:
        source = exchange.source
        current = require_managed_claude_authority(source)
        completed_at = self._clock.now()
        authority = source.authority
        if not isinstance(authority, ClaudeAccountAuthority):
            raise ValueError("Managed Claude authority changed type.")
        candidate = replace(
            source,
            plan=exchange.after.plan,
            authority=ClaudeAccountAuthority(
                setup_token=authority.setup_token,
                subscription=managed_login_authority(
                    exchange.after,
                    current.authority_id,
                    completed_at,
                ),
            ),
            credential_health=CredentialHealth.HEALTHY,
            last_refresh_at=completed_at,
            last_refresh_status=RefreshStatus.OK,
            last_refresh_error_code=None,
        )
        try:
            self._store.persist_state(candidate, expected=source)
        except SourceChangedError:
            return self._result(
                source,
                ClaudeManagedOutcome.STATE_CHANGED,
            )
        return self._result(candidate, ClaudeManagedOutcome.HEALTHY)

    def _persist_failure(
        self,
        account: SavedAccount,
        outcome: ClaudeManagedOutcome,
    ) -> ClaudeManagedAuthorityResult:
        candidate = replace(
            account,
            credential_health=_failure_health(account, outcome),
            last_refresh_at=self._clock.now(),
            last_refresh_status=RefreshStatus.FAILED,
            last_refresh_error_code=outcome.failure_code,
        )
        try:
            self._store.persist_state(candidate, expected=account)
        except SourceChangedError:
            return self._result(
                account,
                ClaudeManagedOutcome.STATE_CHANGED,
            )
        return self._result(candidate, outcome)

    @staticmethod
    def _subscription(
        account: SavedAccount,
    ) -> ClaudeSubscriptionAuthority | None:
        authority = account.authority
        if (
            account.provider_id is not ProviderId.CLAUDE
            or not isinstance(authority, ClaudeAccountAuthority)
        ):
            raise ValueError("Account is not owned by Claude.")
        return authority.subscription

    def _saved_account(self, account_id: SidekickAccountId) -> SavedAccount:
        account = self._store.read_saved(account_id)
        if account is None:
            raise ValueError("Managed Claude account no longer exists.")
        return account

    @staticmethod
    def _result(
        account: SavedAccount,
        outcome: ClaudeManagedOutcome,
    ) -> ClaudeManagedAuthorityResult:
        return ClaudeManagedAuthorityResult(outcome, account)


def _saved_authority_matches(
    account: SavedAccount,
    authority: ClaudeManagedLoginAuthority,
    snapshot: ClaudeAuthoritySnapshot,
) -> bool:
    return (
        account.plan == snapshot.plan
        and authority.provider_identity == snapshot.provider_identity
        and authority.generation == snapshot.generation
        and authority.access_expires_at == snapshot.access_expires_at
        and authority.refresh_expires_at == snapshot.refresh_expires_at
        and authority.executable_version == snapshot.executable_version
    )


def _invalid_transition(
    before: ClaudeAuthoritySnapshot,
    after: ClaudeAuthoritySnapshot,
) -> ClaudeManagedOutcome | None:
    if after.generation == before.generation:
        return ClaudeManagedOutcome.UNCHANGED
    if (
        after.profile != before.profile
        or after.provider_identity != before.provider_identity
        or frozenset(after.scopes) != frozenset(before.scopes)
        or after.access_expires_at <= before.access_expires_at
        or (
            before.refresh_expires_at is not None
            and (
                after.refresh_expires_at is None
                or after.refresh_expires_at < before.refresh_expires_at
            )
        )
        or after.health is not CredentialHealth.HEALTHY
        or after.action is not CredentialAction.NONE
    ):
        return ClaudeManagedOutcome.RECONCILIATION_REQUIRED
    return None


def _storage_outcome(
    failure: ClaudeProtectedStorageFailure,
) -> ClaudeManagedOutcome:
    if failure is ClaudeProtectedStorageFailure.MISSING:
        return ClaudeManagedOutcome.LOGIN_REQUIRED
    if failure is ClaudeProtectedStorageFailure.IDENTITY_MISMATCH:
        return ClaudeManagedOutcome.RECONCILIATION_REQUIRED
    if failure in {
        ClaudeProtectedStorageFailure.KEYCHAIN_ACCESS_DENIED,
        ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED,
        ClaudeProtectedStorageFailure.UNREADABLE,
    }:
        return ClaudeManagedOutcome.UNREADABLE
    if failure is ClaudeProtectedStorageFailure.MALFORMED:
        return ClaudeManagedOutcome.MALFORMED
    return ClaudeManagedOutcome.INCOMPATIBLE


def _failure_health(
    account: SavedAccount,
    outcome: ClaudeManagedOutcome,
) -> CredentialHealth:
    if outcome is ClaudeManagedOutcome.LOGIN_REQUIRED:
        return CredentialHealth.LOGIN_REQUIRED
    if outcome is ClaudeManagedOutcome.INCOMPATIBLE:
        return CredentialHealth.UNSUPPORTED
    if outcome is ClaudeManagedOutcome.MALFORMED:
        return CredentialHealth.MALFORMED
    if outcome is ClaudeManagedOutcome.RECONCILIATION_REQUIRED:
        return CredentialHealth.RECONCILIATION_REQUIRED
    if outcome is ClaudeManagedOutcome.UNREADABLE:
        return CredentialHealth.UNREADABLE
    return account.credential_health
