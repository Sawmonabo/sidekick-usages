"""Crash-safe guided service-setup acknowledgement authority."""

from pathlib import Path

from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.setup.models import (
    ServiceSetupAcknowledgement,
)
from sidekick_usages.persistence.setup.schema import (
    decode_setup_acknowledgement,
    encode_setup_acknowledgement,
)
from sidekick_usages.persistence.state.files import recover_state_file
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation


class ServiceSetupAcknowledgementStore:
    """Persist the last explicitly approved control protocol generation."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError(
                "Service setup acknowledgement path must be absolute."
            )
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_setup_acknowledgement,
        )
        self._lock = PersistenceLock(self._filesystem)

    def matches(self, protocol_generation: int) -> bool:
        """Return whether the exact generation was previously approved."""
        expected = ServiceSetupAcknowledgement(protocol_generation)
        snapshot = self._filesystem.read_opaque_private()
        if snapshot is None:
            return False
        return decode_setup_acknowledgement(snapshot.data) == expected

    def acknowledge(
        self,
        protocol_generation: int,
    ) -> ServiceSetupAcknowledgement:
        """Atomically persist one explicitly approved generation."""
        acknowledgement = ServiceSetupAcknowledgement(protocol_generation)
        payload = encode_setup_acknowledgement(acknowledgement)
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            if snapshot is not None:
                current = decode_setup_acknowledgement(snapshot.data)
                if current == acknowledgement:
                    return acknowledgement
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
        return acknowledgement
