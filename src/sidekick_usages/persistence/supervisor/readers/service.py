"""Passive supervisor service-state reader."""

from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.persistence.filesystem.reader import PrivateDocumentReader
from sidekick_usages.persistence.schema.service import decode_service_state

SERVICE_STATE_PATH_ERROR = "Service-state path must be absolute."


class ServiceStateReader(PrivateDocumentReader):
    """Read sanitized supervisor observations without mutable coordination."""

    absolute_path_error = SERVICE_STATE_PATH_ERROR

    def observe(self) -> ServiceState | None:
        """Passively read service state without lock-sidecar writes."""
        return self._load()

    def _load(self) -> ServiceState | None:
        snapshot = self._filesystem.read_opaque_private()
        return (
            None if snapshot is None else decode_service_state(snapshot.data)
        )
