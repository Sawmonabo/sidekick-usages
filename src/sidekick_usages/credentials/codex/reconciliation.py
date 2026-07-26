"""Reconcile effective Codex authentication with saved authorities."""

from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.selection.models import (
    NativeReconciliationResult,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    same_provider_auth_authority,
    same_selected_runtime_authority,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.home import (
    CodexManagedAuthReader,
)
from sidekick_usages.credentials.codex.models import (
    require_managed_codex_authority,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.base import ProviderFailure


class CodexNativeReconciliationError(RuntimeError):
    """One sanitized native-auth reconciliation failure."""

    def __init__(self, code: str, *, action_required: bool) -> None:
        self.code = code
        self.action_required = action_required
        super().__init__(code)


class CodexNativeReconciliationService:
    """Relate one proven runtime observation without importing credentials."""

    def __init__(
        self,
        accounts: AccountStore,
        managed_auth: CodexManagedAuthReader,
        journals: ActivationJournalStore,
        selected: SelectedStateStore,
        clock: Clock,
    ) -> None:
        self._accounts = accounts
        self._managed_auth = managed_auth
        self._journals = journals
        self._selected = selected
        self._clock = clock

    def reconcile(
        self,
        observation: ProviderAuthObservation,
        authority: ProviderMutationAuthority,
    ) -> NativeReconciliationResult:
        """Persist the newest observed native selection under provider lock."""
        authority.require(ProviderId.CODEX)
        baseline = self._selected.load(ProviderId.CODEX)
        selected = self._reconcile(observation, authority, baseline)
        return NativeReconciliationResult(
            selected,
            not same_selected_runtime_authority(baseline, selected),
        )

    def _reconcile(
        self,
        observation: ProviderAuthObservation,
        authority: ProviderMutationAuthority,
        baseline: SelectedAccountState | None,
    ) -> SelectedAccountState | None:
        """Apply one native relation after capturing its selected baseline."""
        candidate = self._candidate(observation, authority)
        journal = self._journals.load(ProviderId.CODEX).active
        if journal is not None:
            if same_provider_auth_authority(
                observation,
                journal.native_auth_baseline,
            ):
                return baseline
            if (
                candidate.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                and candidate.account_id == journal.target_account_id
            ):
                return baseline
            if candidate.runtime_state is ProviderRuntimeState.UNREADABLE:
                raise CodexNativeReconciliationError(
                    "native_auth_unreadable",
                    action_required=True,
                )
            transaction = self._journals.transaction(
                ProviderId.CODEX,
                tuple(sorted(journal.account_ids)),
                authority,
            )
            if candidate.runtime_state is ProviderRuntimeState.SAVED_ACTIVE:
                baseline = journal.selected_baseline
                if (
                    baseline is None
                    or candidate.account_id != baseline.account_id
                    or candidate.provider_identity
                    != baseline.provider_identity
                ):
                    raise CodexNativeReconciliationError(
                        "native_auth_changed",
                        action_required=True,
                    )
                rollback = replace(
                    candidate,
                    outcome=ActivationOutcome.ROLLED_BACK,
                )
                transaction.commit_rollback(
                    journal.operation_id,
                    rollback,
                    self._selected,
                    updated_at=self._clock.now(),
                )
                return rollback
            transaction.commit_external(
                journal.operation_id,
                candidate,
                self._selected,
                updated_at=self._clock.now(),
            )
            return candidate
        if same_selected_runtime_authority(baseline, candidate):
            return baseline
        return self._selected.compare_and_swap(candidate, expected=baseline)

    def _candidate(
        self,
        observation: ProviderAuthObservation,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        if observation.provider_id is not ProviderId.CODEX:
            raise CodexNativeReconciliationError(
                "native_observation_invalid",
                action_required=True,
            )
        if observation.state is ProviderAuthState.ACTIVE:
            return self._active_candidate(observation, authority)
        runtime_state, outcome = {
            ProviderAuthState.LOGGED_OUT: (
                ProviderRuntimeState.LOGGED_OUT,
                ActivationOutcome.LOGGED_OUT,
            ),
            ProviderAuthState.UNREADABLE: (
                ProviderRuntimeState.UNREADABLE,
                ActivationOutcome.RECONCILIATION_REQUIRED,
            ),
            ProviderAuthState.UNSUPPORTED: (
                ProviderRuntimeState.UNSUPPORTED,
                ActivationOutcome.UNSUPPORTED,
            ),
        }[observation.state]
        return SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=runtime_state,
            account_id=None,
            provider_identity=None,
            runtime_generation=None,
            verified_at=observation.observed_at,
            outcome=outcome,
        )

    def _active_candidate(
        self,
        observation: ProviderAuthObservation,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState:
        identity = observation.provider_identity
        generation = observation.generation
        if identity is None or generation is None:
            raise CodexNativeReconciliationError(
                "native_observation_invalid",
                action_required=True,
            )
        matches: list[tuple[SavedAccount, CodexManagedAuthority]] = []
        for account in self._accounts.saved_accounts():
            if account.provider_id is not ProviderId.CODEX:
                continue
            try:
                managed = require_managed_codex_authority(account)
            except ValueError:
                continue
            if managed.provider_identity == identity:
                matches.append((account, managed))
        if len(matches) > 1:
            raise CodexNativeReconciliationError(
                "native_identity_ambiguous",
                action_required=True,
            )
        if not matches:
            return self._external_candidate(observation)
        account, managed = matches[0]
        authority.account(account.account_id)
        matched = self._managed_auth.matches_observation(
            account.account_id,
            observation,
        )
        if isinstance(matched, ProviderFailure):
            raise CodexNativeReconciliationError(
                f"managed_auth_{matched.kind.value}",
                action_required=matched.action_required,
            )
        if not matched:
            return self._external_candidate(observation)
        return SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=account.account_id,
            provider_identity=managed.provider_identity,
            runtime_generation=managed.generation,
            verified_at=observation.observed_at,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )

    @staticmethod
    def _external_candidate(
        observation: ProviderAuthObservation,
    ) -> SelectedAccountState:
        identity = observation.provider_identity
        generation = observation.generation
        if identity is None or generation is None:
            raise CodexNativeReconciliationError(
                "native_observation_invalid",
                action_required=True,
            )
        return SelectedAccountState(
            provider_id=ProviderId.CODEX,
            runtime_state=ProviderRuntimeState.EXTERNAL_ACTIVE,
            account_id=None,
            provider_identity=identity,
            runtime_generation=generation,
            verified_at=observation.observed_at,
            outcome=ActivationOutcome.EXTERNAL_RECONCILED,
        )
