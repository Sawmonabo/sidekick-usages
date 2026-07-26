"""Journaled official Claude native-account activation."""

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
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ProviderAuthObservation,
    SelectedAccountState,
    activation_account_ids,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
    ClaudeActivationRuntime,
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
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
    ActivationJournalTransaction,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
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


class ClaudeActivationService:
    """Retain the source and officially activate one verified target."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        profiles: PrivateCredentialTree,
        journals: ActivationJournalStore,
        selected: SelectedStateStore,
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
        self._journals = journals
        self._selected = selected
        self._clock = clock
        self._environment = resolved_runtime.environment
        self._host = resolved_runtime.host
        self._runner = resolved_runtime.runner
        self._managed_reader = ClaudeManagedAuthorityReader(paths, profiles)

    def activate(
        self,
        operation_id: OperationId,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        """Retain the native source, activate the target, and commit proof."""
        authority.require(ProviderId.CLAUDE)
        baseline = self._selected.load(ProviderId.CLAUDE)
        source_account_id = self._source_account_id(
            baseline,
            target_account_id,
        )
        authority.account(source_account_id)
        authority.account(target_account_id)
        source, source_authority = self._managed_account(
            source_account_id,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        target, target_authority = self._managed_account(
            target_account_id,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        source_capabilities = self._prepare(source_account_id)
        target_capabilities = self._prepare(target_account_id)
        self._require_same_runtime(
            source_capabilities,
            target_capabilities,
        )
        native_capabilities = self._native_capabilities(target_capabilities)
        self._read_private(
            source_capabilities,
            source_authority,
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        target_private = self._read_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if target_private.health is CredentialHealth.LOGIN_REQUIRED:
            raise ClaudeActivationError(
                ClaudeActivationFailure.TARGET_UNAVAILABLE
            )
        native_source = self._read_native(native_capabilities)
        native_baseline = self._relate_source(
            baseline,
            source,
            source_authority,
            native_source,
        )
        transaction = self._transaction(
            baseline,
            target_account_id,
            authority,
        )
        now = self._clock.now()
        record = transaction.begin(
            ActivationRecord(
                provider_id=ProviderId.CLAUDE,
                operation_id=operation_id,
                selected_baseline=baseline,
                native_auth_baseline=native_baseline,
                target_account_id=target_account_id,
                expected_target_identity=(target_authority.provider_identity),
                target_authority_generation=target_private.generation,
                phase=ActivationPhase.PREPARED,
                started_at=now,
                updated_at=now,
            )
        )
        try:
            retained_source = self._retain_source(
                source,
                source_authority,
                source_capabilities,
                native_capabilities,
                native_source,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.OUTGOING_RETAINED,
                updated_at=self._clock.now(),
            )
            activated_target = self._activate_target(
                target_capabilities,
                target_private,
                native_capabilities,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.TARGET_ACTIVATED,
                updated_at=self._clock.now(),
            )
            self._prove_final_authorities(
                retained_source,
                source_authority,
                source_capabilities,
                target,
                target_authority,
                target_capabilities,
                target_private,
                native_capabilities,
                activated_target,
            )
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.PROVIDER_PROOF_VERIFIED,
                updated_at=self._clock.now(),
                verified_runtime_generation=activated_target.generation,
            )
            selected = SelectedAccountState(
                provider_id=ProviderId.CLAUDE,
                runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                account_id=target.account_id,
                provider_identity=activated_target.provider_identity,
                runtime_generation=activated_target.generation,
                verified_at=self._clock.now(),
                outcome=ActivationOutcome.VERIFIED,
            )
            transaction.commit_verified(
                record.operation_id,
                selected,
                self._selected,
                updated_at=self._clock.now(),
            )
        except ClaudeActivationError as error:
            self._require_reconciliation(transaction, record, error)
            raise
        except (
            SourceChangedError,
            ManagedStateConflictError,
        ) as error:
            activation_error = ClaudeActivationError(
                ClaudeActivationFailure.STATE_CHANGED
            )
            self._require_reconciliation(
                transaction,
                record,
                activation_error,
            )
            raise activation_error from error
        return selected

    def _retain_source(
        self,
        source: SavedAccount,
        source_authority: ClaudeManagedLoginAuthority,
        source_capabilities: ClaudeCapabilities,
        native_capabilities: ClaudeCapabilities,
        native_source: ClaudeAuthoritySnapshot,
    ) -> SavedAccount:
        native_profile = self._require_native_profile(native_capabilities)
        native_reader = ClaudeNativeAuthorityReader(native_profile)
        try:
            with native_reader.open_login(
                native_capabilities,
                self._clock.now(),
                expected_identity=source_authority.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if protected.snapshot != native_source:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.NATIVE_CHANGED
                    )
                exchanged = ClaudeOfficialLoginExchange(
                    self._managed_reader,
                    self._clock,
                    environment=self._environment,
                    runner=self._runner,
                ).provision(
                    source_capabilities,
                    authority_expectation(protected.snapshot),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.NATIVE_UNAVAILABLE
            ) from None
        retained = self._exchange_snapshot(
            exchanged,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        candidate = self._updated_source(
            source,
            source_authority,
            retained,
        )
        self._store.persist_state(candidate, expected=source)
        return candidate

    def _activate_target(
        self,
        target_capabilities: ClaudeCapabilities,
        target_private: ClaudeAuthoritySnapshot,
        native_capabilities: ClaudeCapabilities,
    ) -> ClaudeAuthoritySnapshot:
        native_profile = self._require_native_profile(native_capabilities)
        try:
            with self._managed_reader.open_login(
                target_capabilities,
                self._clock.now(),
                expected_identity=target_private.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if protected.snapshot != target_private:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.STATE_CHANGED
                    )
                exchanged = ClaudeOfficialLoginExchange(
                    ClaudeNativeAuthorityReader(native_profile),
                    self._clock,
                    environment=self._environment,
                    runner=self._runner,
                ).provision(
                    native_capabilities,
                    authority_expectation(target_private),
                    protected.refresh_token,
                )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(
                ClaudeActivationFailure.TARGET_UNAVAILABLE
            ) from None
        return self._exchange_snapshot(
            exchanged,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )

    def _prove_final_authorities(
        self,
        source: SavedAccount,
        original_source_authority: ClaudeManagedLoginAuthority,
        source_capabilities: ClaudeCapabilities,
        target: SavedAccount,
        target_authority: ClaudeManagedLoginAuthority,
        target_capabilities: ClaudeCapabilities,
        target_private: ClaudeAuthoritySnapshot,
        native_capabilities: ClaudeCapabilities,
        activated_target: ClaudeAuthoritySnapshot,
    ) -> None:
        source_authority = self._managed_authority(
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        if (
            source_authority.authority_id
            != original_source_authority.authority_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        source_proof = self._read_private(
            source_capabilities,
            source_authority,
            source,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
        )
        if source_proof.generation != source_authority.generation:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        target_proof = self._read_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if target_proof != target_private:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        native_proof = self._read_native(
            native_capabilities,
            expected_identity=activated_target.provider_identity,
        )
        if (
            native_proof.provider_identity
            != activated_target.provider_identity
            or native_proof.generation != activated_target.generation
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )

    def _read_private(
        self,
        capabilities: ClaudeCapabilities,
        authority: ClaudeManagedLoginAuthority,
        account: SavedAccount,
        failure: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        try:
            observed = self._managed_reader.read(
                capabilities,
                self._clock.now(),
                expected_identity=authority.provider_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError:
            raise ClaudeActivationError(failure) from None
        if not managed_authority_matches(account, authority, observed):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return observed

    def _read_native(
        self,
        capabilities: ClaudeCapabilities,
        *,
        expected_identity: ProviderIdentity | None = None,
    ) -> ClaudeAuthoritySnapshot:
        reader = ClaudeNativeAuthorityReader(
            self._require_native_profile(capabilities)
        )
        try:
            return reader.read(
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

    def _prepare(
        self,
        account_id: SidekickAccountId,
    ) -> ClaudeCapabilities:
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

    def _native_capabilities(
        self,
        managed: ClaudeCapabilities,
    ) -> ClaudeCapabilities:
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

    def _resolve_native_profile(self) -> ClaudeNativeProfile:
        if self._environment is None:
            return native_claude_profile(environment={})
        home = self._environment.get("HOME")
        if home is None or not home:
            raise ValueError("Claude native profile path is unavailable.")
        home_path = Path(home)
        if not home_path.is_absolute() or ".." in home_path.parts:
            raise ValueError("Claude native profile path is unavailable.")
        return native_claude_profile(
            credential_home=home_path / ".claude",
            environment={},
        )

    @staticmethod
    def _require_native_profile(
        capabilities: ClaudeCapabilities,
    ) -> ClaudeNativeProfile:
        profile = capabilities.profile
        if not isinstance(profile, ClaudeNativeProfile):
            raise ClaudeActivationError(ClaudeActivationFailure.INCOMPATIBLE)
        return profile

    def _managed_account(
        self,
        account_id: SidekickAccountId,
        failure: ClaudeActivationFailure,
    ) -> tuple[SavedAccount, ClaudeManagedLoginAuthority]:
        account = self._store.read_saved(account_id)
        if account is None or account.provider_id is not ProviderId.CLAUDE:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return account, self._managed_authority(account, failure)

    @staticmethod
    def _managed_authority(
        account: SavedAccount,
        failure: ClaudeActivationFailure,
    ) -> ClaudeManagedLoginAuthority:
        authority = account.authority
        if not isinstance(authority, ClaudeAccountAuthority) or not isinstance(
            authority.subscription,
            ClaudeManagedLoginAuthority,
        ):
            raise ClaudeActivationError(failure)
        return authority.subscription

    @staticmethod
    def _source_account_id(
        baseline: SelectedAccountState | None,
        target_account_id: SidekickAccountId,
    ) -> SidekickAccountId:
        if (
            baseline is None
            or baseline.provider_id is not ProviderId.CLAUDE
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or baseline.account_id is None
            or baseline.account_id == target_account_id
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return baseline.account_id

    @staticmethod
    def _require_same_runtime(
        source: ClaudeCapabilities,
        target: ClaudeCapabilities,
    ) -> None:
        if (
            source.executable != target.executable
            or source.platform is not target.platform
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.INCOMPATIBLE)

    def _relate_source(
        self,
        baseline: SelectedAccountState | None,
        source: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        native: ClaudeAuthoritySnapshot,
    ) -> ProviderAuthObservation:
        if (
            baseline is None
            or baseline.account_id != source.account_id
            or baseline.provider_identity != authority.provider_identity
            or native.provider_identity != authority.provider_identity
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.NATIVE_CHANGED)
        if native.health is CredentialHealth.LOGIN_REQUIRED:
            raise ClaudeActivationError(
                ClaudeActivationFailure.NATIVE_UNAVAILABLE
            )
        return ProviderAuthObservation(
            provider_id=ProviderId.CLAUDE,
            state=ProviderAuthState.ACTIVE,
            provider_identity=native.provider_identity,
            generation=native.generation,
            observed_at=self._clock.now(),
        )

    def _transaction(
        self,
        baseline: SelectedAccountState | None,
        target_account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> ActivationJournalTransaction:
        transaction = self._journals.transaction(
            ProviderId.CLAUDE,
            tuple(
                sorted(
                    activation_account_ids(
                        baseline,
                        target_account_id,
                    )
                )
            ),
            authority,
        )
        if transaction.load().active is not None:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return transaction

    def _updated_source(
        self,
        source: SavedAccount,
        authority: ClaudeManagedLoginAuthority,
        observed: ClaudeAuthoritySnapshot,
    ) -> SavedAccount:
        account_authority = source.authority
        if not isinstance(account_authority, ClaudeAccountAuthority):
            raise ClaudeActivationError(
                ClaudeActivationFailure.SOURCE_UNAVAILABLE
            )
        completed_at = self._clock.now()
        return replace(
            source,
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

    @staticmethod
    def _exchange_snapshot(
        result: ClaudeExchangeSuccess | ClaudeExchangeFailure,
        unavailable: ClaudeActivationFailure,
    ) -> ClaudeAuthoritySnapshot:
        if isinstance(result, ClaudeExchangeSuccess):
            return result.snapshot
        failure = _EXCHANGE_FAILURES.get(result.kind, unavailable)
        raise ClaudeActivationError(failure)

    def _require_reconciliation(
        self,
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        error: ClaudeActivationError,
    ) -> None:
        active = transaction.load().active
        if (
            active is None
            or active.operation_id != record.operation_id
            or active.phase is ActivationPhase.RECONCILIATION_REQUIRED
            or active.phase.terminal
        ):
            return
        transaction.advance(
            active.operation_id,
            ActivationPhase.RECONCILIATION_REQUIRED,
            updated_at=self._clock.now(),
            verified_runtime_generation=active.verified_runtime_generation,
            failure_code=error.failure_code,
        )
