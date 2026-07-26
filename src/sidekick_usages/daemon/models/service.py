"""Persisted resident service state models."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceFailureCode,
)
from sidekick_usages.daemon.types.protocol import MAX_PROTOCOL_VERSION
from sidekick_usages.daemon.types.service import (
    PackageVersion,
    ServicePhase,
)

_MAX_ACTIVE_WORKERS = 64


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceState:
    """Persisted no-secret resident service observation."""

    protocol_version: int
    package_version: PackageVersion
    phase: ServicePhase
    revision: int
    observed_at: datetime
    queue_recovered: bool
    journals_reconciled: bool
    broker_ready: bool
    active_workers: int
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Validate bounded counters and readiness invariants."""
        if (
            type(self.protocol_version) is not int
            or not 1 <= self.protocol_version <= MAX_PROTOCOL_VERSION
        ):
            raise ValueError("Service protocol version is invalid.")
        if (
            type(self.revision) is not int
            or self.revision < 0
            or self.revision > (1 << 63) - 1
        ):
            raise ValueError("Service revision is invalid.")
        if (
            type(self.active_workers) is not int
            or not 0 <= self.active_workers <= _MAX_ACTIVE_WORKERS
        ):
            raise ValueError("Active worker count is invalid.")
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        code = safe_outcome_code(self.failure_code)
        if self.phase is ServicePhase.READY and (
            not self.queue_recovered
            or not self.journals_reconciled
            or not self.broker_ready
            or code is not None
        ):
            raise ValueError(
                "Ready service state requires recovered resident state."
            )
        object.__setattr__(self, "failure_code", code)

    def ready_for(self, provider_ids: ProviderReadinessScope) -> bool:
        """Allow broker-only degradation solely for Claude-scoped work."""
        if self.phase is ServicePhase.READY:
            return True
        return (
            self.phase is ServicePhase.DEGRADED
            and bool(provider_ids)
            and ProviderId.CODEX not in provider_ids
            and self.queue_recovered
            and self.journals_reconciled
            and self.failure_code
            == ServiceFailureCode.CODEX_BROKER_UNAVAILABLE.value
        )
