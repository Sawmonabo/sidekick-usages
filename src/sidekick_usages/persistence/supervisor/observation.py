"""Durable provider runtime-auth observations."""

from pathlib import Path

from sidekick_usages.core.selection.models import ProviderAuthObservation
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schema.selection import (
    decode_runtime_auth_observation,
    encode_runtime_auth_observation,
)
from sidekick_usages.persistence.state.files import recover_state_file
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

_OBSERVATION_DIRECTORY = "runtime-observations"


class RuntimeAuthObservationStore:
    """Persist the newest credential-free runtime observation per provider."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("Runtime-observation root must be absolute.")
        self._root = root

    def load(
        self,
        provider_id: ProviderId,
    ) -> ProviderAuthObservation | None:
        """Return the latest provider observation without mutation."""
        filesystem = self._filesystem(provider_id)
        with PersistenceLock(filesystem).hold():
            snapshot = filesystem.read_opaque_private()
            return (
                None
                if snapshot is None
                else decode_runtime_auth_observation(snapshot.data)
            )

    def save(
        self,
        observation: ProviderAuthObservation,
    ) -> ProviderAuthObservation:
        """Atomically replace one provider's runtime observation."""
        filesystem = self._filesystem(observation.provider_id)
        payload = encode_runtime_auth_observation(observation)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot = filesystem.read_opaque_private()
            if snapshot is not None and snapshot.data == payload:
                return observation
            filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
        return observation

    def _filesystem(
        self,
        provider_id: ProviderId,
    ) -> ManagedStateFilesystem:
        return ManagedStateFilesystem(
            self._root / _OBSERVATION_DIRECTORY / f"{provider_id.value}.json",
            decode_runtime_auth_observation,
        )
