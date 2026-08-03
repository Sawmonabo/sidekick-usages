"""Shared protected-authority boundary for Claude activation."""

import os
from collections.abc import Callable, Mapping
from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
    ClaudeActivationRuntime,
    ClaudeNativeObservation,
)
from sidekick_usages.credentials.claude.authority.types import (
    ClaudeAuthorityReader,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeExchangeFailure,
    ClaudeExchangeSuccess,
    authority_expectation,
    native_authority_expectation,
)
from sidekick_usages.credentials.claude.exchange.service import (
    ClaudeOfficialLoginExchange,
    claude_native_login_baseline_available,
)
from sidekick_usages.credentials.claude.exchange.types import (
    ClaudeExchangeFailureKind,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_authority_matches,
    managed_login_authority,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.providers.claude.activation.service import (
    claude_environment_conflict,
    claude_native_switch_conflict,
)
from sidekick_usages.providers.claude.auth.proof.service import (
    same_claude_authority_proof,
)
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
from sidekick_usages.providers.claude.models import ClaudeNativeProfile

_EXCHANGE_FAILURES = {
    ClaudeExchangeFailureKind.TIMED_OUT: ClaudeActivationFailure.TIMED_OUT,
    ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED: (
        ClaudeActivationFailure.RECONCILIATION_REQUIRED
    ),
    ClaudeExchangeFailureKind.IDENTITY_MISMATCH: (
        ClaudeActivationFailure.RECONCILIATION_REQUIRED
    ),
}
_INACTIVE_NATIVE_STATES = {
    ClaudeProtectedStorageFailure.MISSING: ProviderAuthState.LOGGED_OUT,
    ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN: (
        ProviderAuthState.UNSUPPORTED
    ),
}
_RUNTIME_AUTH_STATES = {
    ProviderRuntimeState.SAVED_ACTIVE: ProviderAuthState.ACTIVE,
    ProviderRuntimeState.EXTERNAL_ACTIVE: ProviderAuthState.ACTIVE,
    ProviderRuntimeState.LOGGED_OUT: ProviderAuthState.LOGGED_OUT,
    ProviderRuntimeState.UNREADABLE: ProviderAuthState.UNREADABLE,
    ProviderRuntimeState.UNSUPPORTED: ProviderAuthState.UNSUPPORTED,
}


class ClaudeActivationAuthorityCoordinator:
    """Own strict Claude private/native reads and official mutations."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        profiles: PrivateCredentialTree,
        clock: Clock,
        *,
        capabilities: ClaudeProfileCapabilityFactory,
        runtime: ClaudeActivationRuntime | None = None,
    ) -> None:
        resolved_runtime = (
            ClaudeActivationRuntime() if runtime is None else runtime
        )
        self._store = store
        self._clock = clock
        self._environment = resolved_runtime.environment
        self._runner = resolved_runtime.runner
        self._remote_control_probe = resolved_runtime.remote_control_probe
        self._capabilities = capabilities
        self._managed_reader = ClaudeManagedAuthorityReader(paths, profiles)
        self._observations = RuntimeAuthObservationStore(
            paths.durable_operations
        )

    def saved_accounts(self) -> tuple[SavedAccount, ...]:
        """Return the current secret-free account index."""
        return self._store.saved_accounts()

    def managed_account(
        self,
        account_id: SidekickAccountId,
        failure: ClaudeActivationFailure,
    ) -> tuple[SavedAccount, ClaudeManagedLoginAuthority]:
        """Reopen one exact managed Claude subscription account."""
        account = self._store.read_saved(account_id)
        if account is None or account.provider_id is not ProviderId.CLAUDE:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return account, self.managed_authority(account, failure)

    @staticmethod
    def managed_authority(
        account: SavedAccount,
        failure: ClaudeActivationFailure,
    ) -> ClaudeManagedLoginAuthority:
        """Return one managed subscription authority or fail closed."""
        authority = account.authority
        if not isinstance(authority, ClaudeAccountAuthority) or not isinstance(
            authority.subscription,
            ClaudeManagedLoginAuthority,
        ):
            raise ClaudeActivationError(failure)
        return authority.subscription

    def prepare(
        self,
        account_id: SidekickAccountId,
    ) -> ClaudeCapabilities:
        """Prove one stable managed profile and exact Claude release."""
        try:
            return self._capabilities.managed(account_id)
        except ClaudeManagedError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.INCOMPATIBLE
            ) from None

    def prepare_existing(
        self,
        account_id: SidekickAccountId,
    ) -> ClaudeCapabilities:
        """Prove a managed profile without creating missing state."""
        try:
            return self._capabilities.existing_managed(account_id)
        except ClaudeManagedError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.INCOMPATIBLE
            ) from None

    def native_capabilities(
        self,
        managed: ClaudeCapabilities,
    ) -> ClaudeCapabilities:
        """Bind proven capabilities to the native default profile."""
        native = self.prepare_native()
        self.require_same_runtime(managed, native)
        return native

    def prepare_native(self) -> ClaudeCapabilities:
        """Prove and bind the supported native default Claude profile."""
        try:
            return self._capabilities.native(
                environment=self._source_environment()
            )
        except ClaudeManagedError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.INCOMPATIBLE
            ) from None

    def require_activation_environment(self) -> None:
        """Reject caller authentication that overrides native Claude."""
        conflict = claude_environment_conflict(self._source_environment())
        if conflict is not None:
            raise ClaudeActivationError(conflict)

    def require_native_switch(
        self,
        capabilities: ClaudeCapabilities,
    ) -> None:
        """Reject an unsafe new switch before native mutation."""
        conflict = claude_native_switch_conflict(
            capabilities,
            self._source_environment(),
            self._remote_control_probe,
        )
        if conflict is not None:
            raise ClaudeActivationError(conflict)

    @staticmethod
    def require_same_runtime(
        source: ClaudeCapabilities,
        target: ClaudeCapabilities,
    ) -> None:
        """Require both private profiles to use one proven Claude runtime."""
        if (
            source.executable != target.executable
            or source.platform is not target.platform
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.INCOMPATIBLE)

    def read_saved_private(
        self,
        capabilities: ClaudeCapabilities,
        authority: ClaudeManagedLoginAuthority,
        account: SavedAccount,
        failure: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        """Read one private profile and require its saved metadata."""
        observed = self.read_private(
            capabilities,
            authority.provider_identity,
            failure,
        )
        if not managed_authority_matches(account, authority, observed):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return observed

    def read_private(
        self,
        capabilities: ClaudeCapabilities,
        expected_identity: ProviderIdentity,
        failure: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        """Read one exact private identity through protected storage."""
        try:
            return self._managed_reader.read(
                capabilities,
                self._clock.now(),
                expected_identity=expected_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(failure) from None

    def reconcile_interrupted_source(
        self,
        account: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
    ) -> tuple[
        SavedAccount,
        ClaudeManagedLoginAuthority,
        ClaudeAuthoritySnapshot,
    ]:
        """Adopt only verified metadata from Sidekick's private source."""
        observed = self.read_private(
            capabilities,
            authority.provider_identity,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        self.require_usable(
            observed,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        if managed_authority_matches(account, authority, observed):
            return account, authority, observed
        candidate = self._updated_account(account, authority, observed)
        self._store.persist_state(candidate, expected=account)
        return (
            candidate,
            self.managed_authority(
                candidate,
                ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            ),
            observed,
        )

    def read_native(
        self,
        capabilities: ClaudeCapabilities,
        *,
        expected_identity: ProviderIdentity | None = None,
    ) -> ClaudeAuthoritySnapshot:
        """Read one exact native authority or reject unavailable storage."""
        try:
            return self._native_reader(capabilities).read(
                capabilities,
                self._clock.now(),
                expected_identity=expected_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.NATIVE_UNAVAILABLE
            ) from None

    def observe_native(
        self,
        capabilities: ClaudeCapabilities,
    ) -> ClaudeNativeObservation:
        """Return one strict active or inactive native observation."""
        try:
            snapshot = self._native_reader(capabilities).read(
                capabilities,
                self._clock.now(),
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError as error:
            return ClaudeNativeObservation(
                state=_INACTIVE_NATIVE_STATES.get(
                    error.code,
                    ProviderAuthState.UNREADABLE,
                ),
            )
        return ClaudeNativeObservation(
            state=ProviderAuthState.ACTIVE,
            snapshot=snapshot,
        )

    def record_native_observation(
        self,
        observed: ClaudeNativeObservation,
    ) -> ProviderAuthObservation:
        """Persist one credential-free native verification result."""
        snapshot = observed.snapshot
        return self._save_runtime_observation(
            observed.state,
            (None if snapshot is None else snapshot.provider_identity),
            None if snapshot is None else snapshot.generation,
        )

    def record_selected_runtime(
        self,
        selected: SelectedAccountState,
    ) -> None:
        """Persist one provider-verified selected runtime result."""
        if selected.provider_id is not ProviderId.CLAUDE:
            raise ValueError("Selected runtime is not Claude.")
        state = _RUNTIME_AUTH_STATES[selected.runtime_state]
        active = state is ProviderAuthState.ACTIVE
        self._save_runtime_observation(
            state,
            selected.provider_identity if active else None,
            selected.runtime_generation if active else None,
        )

    def require_native_current(
        self,
        capabilities: ClaudeCapabilities,
        expected: ClaudeNativeObservation,
    ) -> None:
        """Require one native observation to remain current on read-back."""
        observed = self.observe_native(capabilities)
        self.record_native_observation(observed)
        if observed != expected:
            raise ClaudeActivationError(ClaudeActivationFailure.NATIVE_CHANGED)

    def relate_native_account(
        self,
        native: ClaudeAuthoritySnapshot,
        reference_capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
    ) -> SavedAccount | None:
        """Relate one native identity to one verified managed account."""
        return self._relate_native_account(
            native,
            reference_capabilities,
            authority,
            self.saved_accounts(),
            self.prepare,
        )

    def relate_native_selection_account(
        self,
        native: ClaudeAuthoritySnapshot,
        reference_capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
        account_ids: tuple[SidekickAccountId, ...],
    ) -> SavedAccount | None:
        """Relate native truth only to existing journal account profiles."""
        accounts = tuple(
            account
            for account_id in dict.fromkeys(account_ids)
            if (account := self._store.read_saved(account_id)) is not None
            and account.provider_id is ProviderId.CLAUDE
        )
        return self._relate_native_account(
            native,
            reference_capabilities,
            authority,
            accounts,
            self.prepare_existing,
        )

    def _relate_native_account(
        self,
        native: ClaudeAuthoritySnapshot,
        reference_capabilities: ClaudeCapabilities,
        authority: ProviderMutationAuthority,
        accounts: tuple[SavedAccount, ...],
        prepare: Callable[[SidekickAccountId], ClaudeCapabilities],
    ) -> SavedAccount | None:
        """Relate native identity across one exact verified account set."""
        matches: list[tuple[SavedAccount, ClaudeAuthoritySnapshot]] = []
        for account in accounts:
            if account.provider_id is not ProviderId.CLAUDE:
                continue
            try:
                managed = self.managed_authority(
                    account,
                    ClaudeActivationFailure.TARGET_UNAVAILABLE,
                )
            except ClaudeActivationError:
                continue
            authority.account(account.account_id)
            capabilities = prepare(account.account_id)
            self.require_same_runtime(reference_capabilities, capabilities)
            private = self.read_saved_private(
                capabilities,
                managed,
                account,
                ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            )
            if private.provider_identity == native.provider_identity:
                matches.append((account, private))
        if len(matches) > 1:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        if not matches:
            return None
        account, private = matches[0]
        self.require_usable(
            private,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
        )
        return account

    def retain_source(
        self,
        source: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        source_capabilities: ClaudeCapabilities,
        native_capabilities: ClaudeCapabilities,
        native_source: ClaudeAuthoritySnapshot,
    ) -> SavedAccount:
        """Officially retain the exact native source in its private profile."""
        native_reader = self._native_reader(native_capabilities)
        try:
            with native_reader.open_login(
                native_capabilities,
                self._clock.now(),
                expected_identity=authority.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if protected.snapshot != native_source:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.NATIVE_CHANGED
                    )
                exchanged = self._official_exchange(
                    self._managed_reader
                ).provision(
                    source_capabilities,
                    authority_expectation(protected.snapshot),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.NATIVE_UNAVAILABLE
            ) from None
        retained = self.exchange_snapshot(
            exchanged,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        candidate = self._updated_account(source, authority, retained)
        self._store.persist_state(candidate, expected=source)
        return candidate

    def refresh_selected_native(
        self,
        account: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        capabilities: ClaudeCapabilities,
        expected: ClaudeAuthoritySnapshot,
    ) -> ClaudeAuthoritySnapshot:
        """Officially refresh one exact selected native authority."""
        self.require_usable(
            expected,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
        )
        if (
            self.managed_authority(
                account,
                ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            )
            != authority
            or expected.provider_identity != authority.provider_identity
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        reader = self._native_reader(capabilities)
        try:
            with reader.open_login(
                capabilities,
                self._clock.now(),
                expected_identity=authority.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if protected.snapshot != expected:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.NATIVE_CHANGED
                    )
                exchanged = self._official_exchange(reader).provision(
                    capabilities,
                    authority_expectation(protected.snapshot),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.NATIVE_UNAVAILABLE
            ) from None
        return self.exchange_snapshot(
            exchanged,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
        )

    def provision_native(
        self,
        private_capabilities: ClaudeCapabilities,
        private_snapshot: ClaudeAuthoritySnapshot,
        native_capabilities: ClaudeCapabilities,
        expected_native: ClaudeNativeObservation,
        failure: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        """Officially provision one private authority into native Claude."""
        self.require_native_current(
            native_capabilities,
            expected_native,
        )
        native_before = expected_native.snapshot
        modified_milliseconds = (
            None
            if native_before is None
            else native_before.modified_milliseconds
        )
        if not claude_native_login_baseline_available(
            native_capabilities,
            modified_milliseconds,
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        try:
            with self._managed_reader.open_login(
                private_capabilities,
                self._clock.now(),
                expected_identity=private_snapshot.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if protected.snapshot != private_snapshot:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.STATE_CHANGED
                    )
                exchanged = self._official_exchange(
                    self._native_reader(native_capabilities)
                ).provision(
                    native_capabilities,
                    native_authority_expectation(
                        private_snapshot,
                        modified_milliseconds,
                    ),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(failure) from None
        return self.exchange_snapshot(exchanged, failure)

    @staticmethod
    def require_usable(
        snapshot: ClaudeAuthoritySnapshot,
        failure: ClaudeActivationFailure,
    ) -> None:
        """Require refresh-capable protected Claude authority."""
        if snapshot.health is CredentialHealth.LOGIN_REQUIRED:
            raise ClaudeActivationError(failure)

    @staticmethod
    def exchange_snapshot(
        result: ClaudeExchangeSuccess | ClaudeExchangeFailure,
        unavailable: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        """Return verified provider output or one closed safe failure."""
        if isinstance(result, ClaudeExchangeSuccess):
            return result.snapshot
        failure = _EXCHANGE_FAILURES.get(result.kind, unavailable)
        raise ClaudeActivationError(failure)

    @staticmethod
    def require_same_native_proof(
        first: ClaudeAuthoritySnapshot,
        second: ClaudeAuthoritySnapshot,
        failure: ClaudeActivationFailure,
    ) -> None:
        """Require stable status, protected semantics, and ``mtimeMs``."""
        if (
            not same_claude_authority_proof(first, second)
            or first.modified_milliseconds != second.modified_milliseconds
        ):
            raise ClaudeActivationError(failure)

    def _source_environment(self) -> Mapping[str, str]:
        return os.environ if self._environment is None else self._environment

    def _save_runtime_observation(
        self,
        state: ProviderAuthState,
        provider_identity: ProviderIdentity | None,
        generation: AuthorityGeneration | None,
    ) -> ProviderAuthObservation:
        observation = ProviderAuthObservation(
            provider_id=ProviderId.CLAUDE,
            state=state,
            provider_identity=provider_identity,
            generation=generation,
            observed_at=self._clock.now(),
        )
        self._observations.save_native(observation)
        return observation

    def _native_reader(
        self,
        capabilities: ClaudeCapabilities,
    ) -> ClaudeNativeAuthorityReader:
        return ClaudeNativeAuthorityReader(
            self._require_native_profile(capabilities)
        )

    def _official_exchange(
        self,
        reader: ClaudeAuthorityReader,
    ) -> ClaudeOfficialLoginExchange:
        return ClaudeOfficialLoginExchange(
            reader,
            self._clock,
            environment=self._environment,
            runner=self._runner,
        )

    @staticmethod
    def _require_native_profile(
        capabilities: ClaudeCapabilities,
    ) -> ClaudeNativeProfile:
        profile = capabilities.profile
        if not isinstance(profile, ClaudeNativeProfile):
            raise ClaudeActivationError(ClaudeActivationFailure.INCOMPATIBLE)
        return profile

    def _updated_account(
        self,
        account: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        observed: ClaudeAuthoritySnapshot,
    ) -> SavedAccount:
        account_authority = account.authority
        if not isinstance(account_authority, ClaudeAccountAuthority):
            raise ClaudeActivationError(
                ClaudeActivationFailure.SOURCE_UNAVAILABLE
            )
        completed_at = self._clock.now()
        return replace(
            account,
            plan=observed.plan,
            authority=ClaudeAccountAuthority(
                setup_token=account_authority.setup_token,
                subscription=managed_login_authority(
                    observed,
                    authority.authority_id,
                    completed_at,
                ),
            ),
            credential_health=observed.health,
            last_refresh_at=completed_at,
            last_refresh_status=RefreshStatus.OK,
            last_refresh_error_code=None,
        )
