"""Durable provider-selected state keyed by stable account ID."""

from pathlib import Path

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.artifacts import AuthorityExpectation
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.selection import SelectedStateDocument
from sidekick_usages.persistence.schema.selection import (
    decode_selected_state,
    encode_selected_state,
)
from sidekick_usages.persistence.state_files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
    recover_state_file,
)
from sidekick_usages.persistence.state_filesystem import (
    ManagedStateFilesystem,
)

__all__ = ["SelectedStateStore"]


class SelectedStateStore:
    """Persist the last verified runtime state independently per provider."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Selected-state path must be absolute.")
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_selected_state,
        )
        self._lock = PersistenceLock(self._filesystem)

    def load(
        self,
        provider_id: ProviderId,
    ) -> SelectedAccountState | None:
        """Load one provider's last verified state without mutation."""
        return self._load_document().get(provider_id)

    def load_all(self) -> tuple[SelectedAccountState, ...]:
        """Load every provider state in deterministic provider order."""
        return self._load_document().states

    def save(self, state: SelectedAccountState) -> SelectedAccountState:
        """Atomically replace one provider state without changing the other."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = (
                SelectedStateDocument()
                if snapshot is None
                else decode_selected_state(snapshot.data)
            )
            states = {
                current.provider_id: current for current in document.states
            }
            states[state.provider_id] = state
            candidate = SelectedStateDocument(tuple(states.values()))
            payload = encode_selected_state(candidate)
            if snapshot is not None and snapshot.data == payload:
                return state
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
            return state

    def remove_account(self, account_id: SidekickAccountId) -> bool:
        """Remove stale references or reject a currently selected account."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            if snapshot is None:
                return False
            document = decode_selected_state(snapshot.data)
            if any(
                state.account_id == account_id
                and state.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                for state in document.states
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.SELECTED_ACCOUNT
                )
            retained = tuple(
                state
                for state in document.states
                if state.account_id != account_id
            )
            if len(retained) == len(document.states):
                return False
            payload = encode_selected_state(SelectedStateDocument(retained))
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=snapshot.fingerprint,
            )
            return True

    def recover(self) -> None:
        """Discard any bounded interrupted write candidate."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            self._load_document()

    def _load_document(self) -> SelectedStateDocument:
        snapshot = self._filesystem.read_opaque_private()
        return (
            SelectedStateDocument()
            if snapshot is None
            else decode_selected_state(snapshot.data)
        )
