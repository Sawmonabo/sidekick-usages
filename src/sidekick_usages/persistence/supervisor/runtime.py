"""Provider-neutral read guard for selected runtime authority."""

from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.locking import StoreLockedError
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore

_SELECTION_READ_LOCK_TIMEOUT_SECONDS = 0.0


class RuntimeStateReader:
    """Read provider runtime state under one mutation-safe snapshot."""

    def __init__(
        self,
        provider_id: ProviderId,
        selected: SelectedStateStore,
        journals: ActivationJournalStore,
        operations_root: Path,
    ) -> None:
        if not operations_root.is_absolute():
            raise ValueError("Durable-operation root must be absolute.")
        self._provider_id = provider_id
        self._selected = selected
        self._journals = journals
        self._operations_root = operations_root

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

    def _snapshot(
        self,
    ) -> tuple[SelectedAccountState | None, ActivationRecord | None]:
        try:
            with ProviderMutationLock(
                self._operations_root,
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
