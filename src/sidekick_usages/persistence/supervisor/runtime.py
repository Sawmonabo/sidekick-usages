"""Provider-neutral read guard for selected runtime authority."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.locking import StoreLockedError
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore

_SELECTION_READ_LOCK_TIMEOUT_SECONDS = 0.0


class RuntimeStateReader:
    """Read provider runtime state under one mutation-safe snapshot."""

    def __init__(
        self,
        provider_id: ProviderId,
        selected: SelectedStateStore,
        journals: ActivationJournalStore,
        queue: OperationQueueStore,
        clock: Clock,
    ) -> None:
        self._provider_id = provider_id
        self._selected = selected
        self._journals = journals
        self._queue = queue
        self._clock = clock

    def current(self) -> SelectedAccountState | None:
        """Return the current state when no transition owns the provider."""
        selected, activation = self._snapshot()
        return None if activation is not None else selected

    def rollback_account_id(
        self,
        target_account_id: SidekickAccountId,
    ) -> SidekickAccountId | None:
        """Return the saved baseline account for one active transition."""
        _selected, activation = self._snapshot()
        if (
            activation is None
            or activation.target_account_id != target_account_id
        ):
            return None
        baseline = activation.selected_baseline
        if (
            baseline is None
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
        ):
            return None
        return baseline.account_id

    def native_reconciliation_pending(self) -> bool:
        """Return whether native-auth truth blocks selected rehydration."""
        operation = self._queue.get(
            self._provider_id,
            None,
            OperationKind.RECONCILE_NATIVE,
        )
        if operation is None:
            return False
        if operation.state in {
            OperationState.RUNNING,
            OperationState.RETRY_WAIT,
            OperationState.ACTION_REQUIRED,
        }:
            return True
        return operation.due_at <= self._clock.now()

    def native_auth_baseline(self) -> ProviderAuthObservation | None:
        """Return the active transition's exact native baseline."""
        _selected, activation = self._snapshot()
        return None if activation is None else activation.native_auth_baseline

    def _snapshot(
        self,
    ) -> tuple[SelectedAccountState | None, ActivationRecord | None]:
        try:
            with ProviderMutationLock(
                self._queue.root,
                self._provider_id,
                (),
                timeout_seconds=_SELECTION_READ_LOCK_TIMEOUT_SECONDS,
            ).hold():
                activation = self._journals.load(self._provider_id).active
                selected = self._selected.load(self._provider_id)
                return selected, activation
        except StoreLockedError:
            raise RuntimeError("Selected runtime is changing.") from None
        except PersistenceError:
            raise RuntimeError("Selected runtime is unavailable.") from None
