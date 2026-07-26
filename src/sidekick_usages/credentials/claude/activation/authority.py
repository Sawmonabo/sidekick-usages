"""Shared protected-authority boundary for Claude activation."""

import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.types import ProviderAuthState
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
)
from sidekick_usages.credentials.claude.exchange.service import (
    ClaudeOfficialLoginExchange,
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
    prepare_claude_managed_profile,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.claude.activation.service import (
    claude_environment_conflict,
    claude_native_switch_conflict,
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
from sidekick_usages.providers.claude.credentials import native_claude_profile
from sidekick_usages.providers.claude.environment import (
    CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY,
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


class ClaudeActivationAuthorityCoordinator:
    """Own strict Claude private/native reads and official mutations."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        profiles: PrivateCredentialTree,
        clock: Clock,
        *,
        runtime: ClaudeActivationRuntime | None = None,
    ) -> None:
        resolved_runtime = (
            ClaudeActivationRuntime() if runtime is None else runtime
        )
        self._paths = paths
        self._store = store
        self._profiles = profiles
        self._clock = clock
        self._environment = resolved_runtime.environment
        self._host = resolved_runtime.host
        self._runner = resolved_runtime.runner
        self._foreground_probe = resolved_runtime.foreground_probe
        self._managed_reader = ClaudeManagedAuthorityReader(paths, profiles)

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
            return prepare_claude_managed_profile(
                self._paths,
                self._profiles,
                account_id,
                environment=self._environment,
                host=self._host,
                runner=self._runner,
            )
        except ClaudeManagedError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.INCOMPATIBLE
            ) from None

    def native_capabilities(
        self,
        managed: ClaudeCapabilities,
    ) -> ClaudeCapabilities:
        """Bind proven capabilities to the native default profile."""
        if (
            self._environment is not None
            and CLAUDE_CONFIG_DIR_ENVIRONMENT_KEY in self._environment
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.INCOMPATIBLE)
        try:
            profile = self._resolve_native_profile()
        except ValueError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.INCOMPATIBLE
            ) from None
        return ClaudeCapabilities(
            managed.executable,
            profile,
            managed.platform,
        )

    def require_activation_environment(self) -> None:
        """Reject caller authentication that overrides native Claude."""
        conflict = claude_environment_conflict(self._source_environment())
        if conflict is not None:
            raise ClaudeActivationError(conflict)

    def require_native_switch(
        self,
        capabilities: ClaudeCapabilities,
        *,
        allow_remote_control_disconnect: bool,
    ) -> None:
        """Reject an unsafe new switch before native mutation."""
        conflict = claude_native_switch_conflict(
            capabilities,
            self._source_environment(),
            self._foreground_probe,
            allow_remote_control_disconnect=allow_remote_control_disconnect,
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
        if self.observe_native(native_capabilities) != expected_native:
            raise ClaudeActivationError(ClaudeActivationFailure.NATIVE_CHANGED)
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
                    authority_expectation(private_snapshot),
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

    def _resolve_native_profile(self) -> ClaudeNativeProfile:
        home = self._source_environment().get("HOME")
        if home is None or not home:
            raise ValueError("Claude native profile path is unavailable.")
        home_path = Path(home)
        if not home_path.is_absolute() or ".." in home_path.parts:
            raise ValueError("Claude native profile path is unavailable.")
        return native_claude_profile(
            credential_home=home_path / ".claude",
            environment={},
        )

    def _source_environment(self) -> Mapping[str, str]:
        return os.environ if self._environment is None else self._environment

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
