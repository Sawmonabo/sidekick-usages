"""Atomic latest-observation storage for dashboard metrics refresh."""

from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.persistence.errors import (
    PersistenceError,
    PersistenceSchemaError,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.lookup.schema import (
    decode_metrics_refresh_observation,
    encode_metrics_refresh_observation,
)
from sidekick_usages.persistence.state.files import (
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.usage.lookup.models import (
    MetricsRefreshCode,
    MetricsRefreshDiagnostic,
    MetricsRefreshDiagnosticState,
    MetricsRefreshObservation,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
    MetricsRefreshWriteState,
)

METRICS_REFRESH_PATH_ERROR = (
    "Metrics-refresh observation path must be absolute."
)


class MetricsRefreshObservationRecorder:
    """Timestamp and persist sanitized dashboard refresh outcomes."""

    def __init__(
        self,
        store: MetricsRefreshObservationStore,
        clock: Clock,
    ) -> None:
        self._store = store
        self._clock = clock

    def record(
        self,
        outcome: MetricsRefreshOutcome,
        *,
        attempts: int,
        stage: MetricsRefreshStage | None = None,
        code: MetricsRefreshCode | None = None,
    ) -> MetricsRefreshWriteState:
        """Record one safe outcome without raising persistence failures."""
        return self._store.record(
            MetricsRefreshObservation(
                observed_at=self._clock.now(),
                outcome=outcome,
                attempts=attempts,
                stage=stage,
                code=code,
            )
        )


class MetricsRefreshObservationStore:
    """Persist and passively read the latest sanitized observation."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError(METRICS_REFRESH_PATH_ERROR)
        self.path = path
        self._filesystem = ManagedStateFilesystem(
            path,
            decode_metrics_refresh_observation,
        )
        self._lock = PersistenceLock(self._filesystem)

    def _observe(self) -> MetricsRefreshObservation | None:
        """Passively read the latest observation without lock mutation."""
        snapshot = self._filesystem.read_opaque_private()
        return (
            None
            if snapshot is None
            else decode_metrics_refresh_observation(snapshot.data)
        )

    def diagnostic(self) -> MetricsRefreshDiagnostic:
        """Return available, absent, or unavailable passive state."""
        try:
            observation = self._observe()
        except OSError, PersistenceError:
            return MetricsRefreshDiagnostic(
                state=MetricsRefreshDiagnosticState.UNAVAILABLE
            )
        if observation is None:
            return MetricsRefreshDiagnostic(
                state=MetricsRefreshDiagnosticState.ABSENT
            )
        return MetricsRefreshDiagnostic(
            state=MetricsRefreshDiagnosticState.AVAILABLE,
            observation=observation,
        )

    def record(
        self,
        observation: MetricsRefreshObservation,
    ) -> MetricsRefreshWriteState:
        """Save without allowing diagnostic failure to stop the dashboard."""
        try:
            self._save(observation)
        except OSError, PersistenceError:
            return MetricsRefreshWriteState.UNAVAILABLE
        return MetricsRefreshWriteState.SAVED

    def _save(
        self,
        observation: MetricsRefreshObservation,
    ) -> MetricsRefreshObservation:
        """Atomically retain the latest wall-clock observation."""
        payload = encode_metrics_refresh_observation(observation)
        with self._lock.hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            current = (
                None
                if snapshot is None
                else self._decode_existing(snapshot.data)
            )
            if current == observation:
                return observation
            if (
                current is not None
                and current.observed_at > observation.observed_at
            ):
                return current
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
        return observation

    @staticmethod
    def _decode_existing(
        payload: bytes,
    ) -> MetricsRefreshObservation | None:
        """Allow fresh validated telemetry to replace malformed telemetry."""
        try:
            return decode_metrics_refresh_observation(payload)
        except PersistenceSchemaError:
            return None
