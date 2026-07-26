"""Passive provider-selection state reader."""

from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.persistence.filesystem.reader import PrivateDocumentReader
from sidekick_usages.persistence.models.selection import SelectedStateDocument
from sidekick_usages.persistence.schema.selection import decode_selected_state

SELECTED_STATE_PATH_ERROR = "Selected-state path must be absolute."


class SelectedStateReader(PrivateDocumentReader):
    """Read verified provider state without mutable coordination."""

    absolute_path_error = SELECTED_STATE_PATH_ERROR

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
