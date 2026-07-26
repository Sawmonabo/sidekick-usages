"""Passive provider-selection state reader."""

from pathlib import Path

from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.persistence.models.selection import SelectedStateDocument
from sidekick_usages.persistence.schema.selection import decode_selected_state
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)


class SelectedStateReader:
    """Read verified provider state without mutable coordination."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Selected-state path must be absolute.")
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_selected_state,
        )

    def observe_all(self) -> tuple[SelectedAccountState, ...]:
        """Passively read every provider state without lock-sidecar writes."""
        return self._load_document().states

    def _load_document(self) -> SelectedStateDocument:
        snapshot = self._filesystem.read_opaque_private()
        return (
            SelectedStateDocument()
            if snapshot is None
            else decode_selected_state(snapshot.data)
        )
