"""Crash-safe supervisor service-state authority."""

from pathlib import Path

from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.schema.service import (
    decode_service_state,
    encode_service_state,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation


class ServiceStateStore:
    """Persist monotonic sanitized supervisor observations."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Service-state path must be absolute.")
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_service_state,
        )
        self._lock = PersistenceLock(self._filesystem)

    def load(self) -> ServiceState | None:
        """Load the latest service observation when present."""
        with self._lock.hold():
            return self._load()

    def observe(self) -> ServiceState | None:
        """Passively read service state without lock-sidecar writes."""
        return self._load()

    def _load(self) -> ServiceState | None:
        snapshot = self._filesystem.read_opaque_private()
        return (
            None if snapshot is None else decode_service_state(snapshot.data)
        )

    def save(self, state: ServiceState) -> ServiceState:
        """Commit exactly the next service-state revision."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            current = self._decode(snapshot)
            if current == state:
                return state
            expected_revision = 1 if current is None else current.revision + 1
            if state.revision != expected_revision:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            payload = encode_service_state(state)
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
            return state

    def recover(self) -> None:
        """Discard bounded interrupted writes and validate current state."""
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            self._load()

    def clear(self) -> None:
        """Delete the exact service-state authority when present."""
        with self._lock.hold():
            snapshot = self._filesystem.read_opaque_private()
            if snapshot is not None:
                self._filesystem.delete_opaque_private(snapshot.fingerprint)

    @staticmethod
    def _decode(snapshot: FileSnapshot | None) -> ServiceState | None:
        return (
            None if snapshot is None else decode_service_state(snapshot.data)
        )
