"""Passive supervisor service-state reader."""

from pathlib import Path

from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.persistence.schema.service import decode_service_state
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)


class ServiceStateReader:
    """Read sanitized supervisor observations without mutable coordination."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Service-state path must be absolute.")
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_service_state,
        )

    def observe(self) -> ServiceState | None:
        """Passively read service state without lock-sidecar writes."""
        return self._load()

    def _load(self) -> ServiceState | None:
        snapshot = self._filesystem.read_opaque_private()
        return (
            None if snapshot is None else decode_service_state(snapshot.data)
        )
