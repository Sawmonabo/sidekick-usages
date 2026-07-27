"""Managed Claude private-profile maintenance coordination."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSubscriptionAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import (
    KnownExpiry,
    classify_expiry,
    refresh_due,
)
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeExchangeFailure,
    ClaudeExchangeSuccess,
    authority_expectation,
)
from sidekick_usages.credentials.claude.exchange.service import (
    ClaudeOfficialLoginExchange,
)
from sidekick_usages.credentials.claude.exchange.types import (
    ClaudeExchangeFailureKind,
)
from sidekick_usages.credentials.claude.lifetime import (
    CLAUDE_REFRESH_MARGIN,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_authority_matches,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    ClaudeManagedAuthorityResult,
    ClaudeVerifiedAuthorityExchange,
    require_managed_claude_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    OperationAuthorityLock,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_ACTIVATION_FAILURE_OUTCOMES = {
    ClaudeActivationFailure.INCOMPATIBLE: ClaudeManagedOutcome.INCOMPATIBLE,
    ClaudeActivationFailure.NATIVE_CHANGED: (
        ClaudeManagedOutcome.RECONCILIATION_REQUIRED
    ),
    ClaudeActivationFailure.NATIVE_UNAVAILABLE: (
        ClaudeManagedOutcome.UNREADABLE
    ),
    ClaudeActivationFailure.RECONCILIATION_REQUIRED: (
        ClaudeManagedOutcome.RECONCILIATION_REQUIRED
    ),
    ClaudeActivationFailure.SOURCE_UNAVAILABLE: (
        ClaudeManagedOutcome.UNREADABLE
    ),
    ClaudeActivationFailure.STATE_CHANGED: ClaudeManagedOutcome.STATE_CHANGED,
    ClaudeActivationFailure.TARGET_UNAVAILABLE: (
        ClaudeManagedOutcome.UNREADABLE
    ),
    ClaudeActivationFailure.TIMED_OUT: ClaudeManagedOutcome.TIMED_OUT,
}


class ClaudeManagedAuthorityCoordinator:
    """Read and refresh one stable provider-owned Claude profile at a time."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        profiles: PrivateCredentialTree,
        selected: SelectedStateStore,
        activation: ClaudeActivationAuthorityCoordinator,
        capabilities: ClaudeProfileCapabilityFactory,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> None:
        self._paths = paths
        self._store = store
        self._selected = selected
        self._activation = activation
        self._capabilities = capabilities
        self._clock = clock
        self._environment = environment
        self._runner = runner
        self._reader = ClaudeManagedAuthorityReader(paths, profiles)
        self._exchange = ClaudeOfficialLoginExchange(
            self._reader,
            clock,
            environment=environment,
            runner=runner,
        )

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
        selected = self._selected.load(ProviderId.CLAUDE)
        private = self._operate_private(
            account,
            subscription,
            forced=forced,
        )
        if (
            selected is None
            or selected.account_id != account.account_id
            or private.outcome is ClaudeManagedOutcome.STATE_CHANGED
        ):
            return private
        current = private.account
        return self._operate_selected(
            current,
            require_managed_claude_authority(current),
            selected,
            private,
            forced=forced,
        )

    def _operate_private(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        *,
        forced: bool,
    ) -> ClaudeManagedAuthorityResult:
        try:
            capabilities = self._capabilities.managed(account.account_id)
        except ClaudeManagedError:
            return self._persist_failure(
                account,
                ClaudeManagedOutcome.INCOMPATIBLE,
            )
        prepared = self._prepare_exchange(
            account,
            subscription,
            capabilities,
            forced=forced,
        )
        if isinstance(prepared, ClaudeManagedAuthorityResult):
            return prepared
        return self._persist_private_exchange(prepared)

    def _operate_selected(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        selected: SelectedAccountState,
        private: ClaudeManagedAuthorityResult,
        *,
        forced: bool,
    ) -> ClaudeManagedAuthorityResult:
        prepared = self._prepare_selected(
            account,
            subscription,
            selected,
        )
        if isinstance(prepared, ClaudeManagedAuthorityResult):
            return prepared
        capabilities, before = prepared
        reference_time = self._clock.now()
        if not _refresh_required(before, reference_time, forced=forced):
            return self._persist_selected_proof(
                private,
                selected,
                before,
            )
        try:
            after = self._activation.refresh_selected_native(
                account,
                subscription,
                capabilities,
                before,
            )
        except ClaudeActivationError as error:
            return self._persist_failure(
                account,
                _activation_outcome(error),
            )
        return self._persist_selected_proof(
            private,
            selected,
            after,
        )

    def _prepare_selected(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        selected: SelectedAccountState,
    ) -> (
        tuple[ClaudeCapabilities, ClaudeAuthoritySnapshot]
        | ClaudeManagedAuthorityResult
    ):
        outcome: ClaudeManagedOutcome | None = None
        if not _selected_authority_matches(account, subscription, selected):
            outcome = ClaudeManagedOutcome.RECONCILIATION_REQUIRED
        else:
            try:
                capabilities = self._capabilities.native(
                    environment=self._environment
                )
                before = self._activation.read_native(
                    capabilities,
                    expected_identity=subscription.provider_identity,
                )
            except ClaudeManagedError:
                outcome = ClaudeManagedOutcome.INCOMPATIBLE
            except ClaudeActivationError as error:
                outcome = _activation_outcome(error)
            else:
                if (
                    before.provider_identity != subscription.provider_identity
                    or before.generation != selected.runtime_generation
                ):
                    outcome = ClaudeManagedOutcome.RECONCILIATION_REQUIRED
                elif before.health is CredentialHealth.LOGIN_REQUIRED:
                    outcome = ClaudeManagedOutcome.LOGIN_REQUIRED
                else:
                    return capabilities, before
        if outcome is None:
            raise AssertionError("Selected Claude outcome is incomplete.")
        return self._persist_failure(account, outcome)

    def _prepare_exchange(
        self,
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
        *,
        forced: bool,
    ) -> ClaudeVerifiedAuthorityExchange | ClaudeManagedAuthorityResult:
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
                if not managed_authority_matches(
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
                if not _refresh_required(
                    before,
                    reference_time,
                    forced=forced,
                ):
                    return self._result(
                        account,
                        ClaudeManagedOutcome.HEALTHY,
                    )
                exchanged = self._exchange.provision(
                    capabilities,
                    authority_expectation(before),
                    protected.refresh_token,
                )
                if isinstance(exchanged, ClaudeExchangeFailure):
                    return self._persist_failure(
                        account,
                        _exchange_outcome(exchanged.kind),
                    )
                if not isinstance(exchanged, ClaudeExchangeSuccess):
                    raise AssertionError("Claude exchange result is invalid.")
                return ClaudeVerifiedAuthorityExchange(
                    account,
                    before,
                    exchanged.snapshot,
                )
        except ClaudeProtectedStorageError as error:
            return self._persist_failure(
                account,
                _storage_outcome(error.code),
            )

    def _persist_private_exchange(
        self,
        exchange: ClaudeVerifiedAuthorityExchange,
    ) -> ClaudeManagedAuthorityResult:
        source = exchange.source
        completed_at = self._clock.now()
        candidate = self._refreshed_account(exchange, completed_at)
        try:
            self._store.persist_state(candidate, expected=source)
        except SourceChangedError:
            return self._result(
                source,
                ClaudeManagedOutcome.STATE_CHANGED,
            )
        return self._result(candidate, ClaudeManagedOutcome.HEALTHY)

    def _persist_selected_proof(
        self,
        private: ClaudeManagedAuthorityResult,
        selected: SelectedAccountState,
        snapshot: ClaudeAuthoritySnapshot,
    ) -> ClaudeManagedAuthorityResult:
        completed_at = self._clock.now()
        updated = replace(
            selected,
            provider_identity=snapshot.provider_identity,
            runtime_generation=snapshot.generation,
            verified_at=completed_at,
        )
        try:
            self._selected.compare_and_swap(updated, expected=selected)
        except ManagedStateConflictError:
            return self._result(
                private.account,
                ClaudeManagedOutcome.STATE_CHANGED,
            )
        return private

    @staticmethod
    def _refreshed_account(
        exchange: ClaudeVerifiedAuthorityExchange,
        completed_at: datetime,
    ) -> SavedAccount:
        source = exchange.source
        current = require_managed_claude_authority(source)
        authority = source.authority
        if not isinstance(authority, ClaudeAccountAuthority):
            raise ValueError("Managed Claude authority changed type.")
        return replace(
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
        if account.provider_id is not ProviderId.CLAUDE or not isinstance(
            authority, ClaudeAccountAuthority
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


def _storage_outcome(
    failure: ClaudeProtectedStorageFailure,
) -> ClaudeManagedOutcome:
    if failure is ClaudeProtectedStorageFailure.MISSING:
        return ClaudeManagedOutcome.LOGIN_REQUIRED
    if failure in {
        ClaudeProtectedStorageFailure.IDENTITY_MISMATCH,
        ClaudeProtectedStorageFailure.PROOF_CHANGED,
    }:
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


def _activation_outcome(
    error: ClaudeActivationError,
) -> ClaudeManagedOutcome:
    """Map a secret-safe native-authority failure into maintenance state."""
    if not isinstance(error.failure, ClaudeActivationFailure):
        return ClaudeManagedOutcome.INCOMPATIBLE
    return _ACTIVATION_FAILURE_OUTCOMES[error.failure]


def _selected_authority_matches(
    account: SavedAccount,
    authority: ClaudeManagedLoginAuthority,
    selected: SelectedAccountState,
) -> bool:
    """Require selected state to name this exact saved identity."""
    return (
        selected.provider_id is ProviderId.CLAUDE
        and selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and selected.account_id == account.account_id
        and selected.provider_identity == authority.provider_identity
    )


def _refresh_required(
    snapshot: ClaudeAuthoritySnapshot,
    reference_time: datetime,
    *,
    forced: bool,
) -> bool:
    """Return whether one verified subscription needs official login."""
    return forced or refresh_due(
        classify_expiry(
            KnownExpiry(snapshot.access_expires_at),
            now=reference_time,
        ),
        now=reference_time,
        margin=CLAUDE_REFRESH_MARGIN,
    )


def _exchange_outcome(
    failure: ClaudeExchangeFailureKind,
) -> ClaudeManagedOutcome:
    if failure is ClaudeExchangeFailureKind.UNCHANGED:
        return ClaudeManagedOutcome.UNCHANGED
    if failure is ClaudeExchangeFailureKind.TIMED_OUT:
        return ClaudeManagedOutcome.TIMED_OUT
    if failure is ClaudeExchangeFailureKind.INCOMPATIBLE:
        return ClaudeManagedOutcome.INCOMPATIBLE
    if failure in {
        ClaudeExchangeFailureKind.LOGIN_FAILED,
        ClaudeExchangeFailureKind.TRANSIENT,
    }:
        return ClaudeManagedOutcome.TRANSIENT
    return ClaudeManagedOutcome.RECONCILIATION_REQUIRED


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
