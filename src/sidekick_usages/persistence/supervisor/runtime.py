"""Provider-neutral read guard for selected runtime authority."""

from pathlib import Path

from sidekick_usages.core.selection.models import SelectedAccountState
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


class SelectedRuntimeReader:
    """Read one selection only while no activation journal is active."""

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
        try:
            with ProviderMutationLock(
                self._operations_root,
                self._provider_id,
                (),
                timeout_seconds=_SELECTION_READ_LOCK_TIMEOUT_SECONDS,
            ).hold():
                if self._journals.load(self._provider_id).active is not None:
                    return None
                return self._selected.load(self._provider_id)
        except StoreLockedError:
            raise RuntimeError("Selected runtime is changing.") from None
        except PersistenceError:
            raise RuntimeError("Selected runtime is unavailable.") from None
