"""First-selection Claude recovery without a manufactured source."""

from collections.abc import Callable

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import ProviderAuthState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeExchangeSuccess,
    native_authority_expectation,
)
from sidekick_usages.credentials.claude.exchange.service import (
    claude_native_propagation_proven,
    verified_claude_native_exchange,
)
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
    ActivationJournalTransaction,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities

type CommitTarget = Callable[
    [
        ActivationJournalTransaction,
        ActivationRecord,
        SavedAccount,
        ClaudeAuthoritySnapshot,
    ],
    SelectedAccountState,
]
type CommitInactive = Callable[
    [ActivationJournalTransaction, ActivationRecord],
    SelectedAccountState,
]


def recover_initial_activation(
    authorities: ClaudeActivationAuthorityCoordinator,
    journals: ActivationJournalStore,
    clock: Clock,
    record: ActivationRecord,
    target_account_id: SidekickAccountId,
    authority: ProviderMutationAuthority,
    commit_target: CommitTarget,
    commit_inactive: CommitInactive,
) -> SelectedAccountState:
    """Resolve a first selection without inventing source authority."""
    authority.account(target_account_id)
    transaction = journals.transaction(
        ProviderId.CLAUDE,
        (target_account_id,),
        authority,
    )
    try:
        target, target_authority = authorities.managed_account(
            target_account_id,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        target_capabilities = authorities.prepare_existing(target_account_id)
        native_capabilities = authorities.native_capabilities(
            target_capabilities
        )
        authorities.require_native_switch(native_capabilities)
        native = authorities.observe_native(native_capabilities)
        authorities.record_native_observation(native)
        if transaction.load().active != record:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        target_private = authorities.read_saved_private(
            target_capabilities,
            target_authority,
            target,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if (
            target_private.generation != record.target_authority_generation
            or target_authority.provider_identity
            != record.expected_target_identity
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        authorities.require_usable(
            target_private,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        )
        if native.state is ProviderAuthState.LOGGED_OUT:
            authorities.require_native_current(native_capabilities, native)
            resolved = commit_inactive(transaction, record)
        elif native.state is ProviderAuthState.ACTIVE:
            snapshot = native.snapshot
            if (
                snapshot is None
                or snapshot.provider_identity
                != target_authority.provider_identity
                or not _recovered_target_proven(
                    record,
                    target_private,
                    native_capabilities,
                    snapshot,
                )
            ):
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
            authorities.require_native_current(native_capabilities, native)
            resolved = commit_target(
                transaction,
                record,
                target,
                snapshot,
            )
        else:
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        authorities.record_selected_runtime(resolved)
        return resolved
    except ClaudeActivationError as error:
        transaction.require_reconciliation(
            record.operation_id,
            updated_at=clock.now(),
            failure_code=error.failure_code,
        )
        raise
    except (SourceChangedError, ManagedStateConflictError) as error:
        recovery_error = ClaudeActivationError(
            ClaudeActivationFailure.STATE_CHANGED
        )
        transaction.require_reconciliation(
            record.operation_id,
            updated_at=clock.now(),
            failure_code=recovery_error.failure_code,
        )
        raise recovery_error from error


def _recovered_target_proven(
    record: ActivationRecord,
    target_private: ClaudeAuthoritySnapshot,
    native_capabilities: ClaudeCapabilities,
    native: ClaudeAuthoritySnapshot,
) -> bool:
    """Prove target propagation from a journaled absent native file."""
    baseline = record.native_auth_baseline
    if (
        type(baseline) is not ProviderAuthObservation
        or baseline.state is not ProviderAuthState.LOGGED_OUT
    ):
        return False
    if not claude_native_propagation_proven(
        native_capabilities,
        None,
        native.modified_milliseconds,
        native_absent_before=True,
    ):
        return False
    result = verified_claude_native_exchange(
        native_authority_expectation(target_private, None),
        native,
    )
    return isinstance(result, ClaudeExchangeSuccess)
