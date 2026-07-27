"""Typed values owned by CLI invocation composition."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field

from sidekick_usages.credentials.accounts.lifecycle.service import (
    AccountLifecycleCoordinator,
)
from sidekick_usages.credentials.capabilities.ports import (
    ProviderCapabilityEvidenceSource,
)
from sidekick_usages.credentials.migration.managed_auth.service import (
    ManagedAuthMigrationCoordinator,
)
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.daemon.lifecycle.manager import DaemonManager
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.providers.claude.types import ClaudeSetupToken
from sidekick_usages.update import UpdateService
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshDiagnostic,
)
from sidekick_usages.usage.service import UsageCheckService

type DoctorState = DoctorReady | DoctorFailed


@dataclass(slots=True)
class Composed[T]:
    """Own one fully composed value and its transferred resources."""

    value: T
    _resources: ExitStack = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Close transferred resources at most once."""
        if self._closed:
            return
        self._closed = True
        self._resources.close()


@dataclass(frozen=True, slots=True)
class AppContext:
    """Strict services available to normal application commands."""

    accounts: AccountStore
    usage: UsageCheckService
    credentials: CredentialService
    lifecycle: AccountLifecycleCoordinator
    heartbeat: HeartbeatService
    maintenance: TokenMaintenanceService
    claude_setup_token: ClaudeSetupToken


@dataclass(frozen=True, slots=True)
class PersistenceContext:
    """Explicit current persistence administration context."""

    persistence: PersistenceService


@dataclass(frozen=True, slots=True)
class DoctorReady:
    """Doctor can inspect validated accounts and persistence state."""

    service: DoctorService
    persistence: PersistenceStatus
    refresh_state: CredentialRefreshState


@dataclass(frozen=True, slots=True)
class DoctorFailed:
    """Doctor can render one bounded persistence failure."""

    failure: PersistenceFailure


@dataclass(frozen=True, slots=True)
class DoctorContext:
    """Closed doctor-state context."""

    state: DoctorState
    supervisor: SupervisorHealth
    capabilities: ProviderCapabilityEvidenceSource
    metrics_refresh: MetricsRefreshDiagnostic


@dataclass(frozen=True, slots=True)
class DaemonContext:
    """Scheduler-management command context."""

    daemon: DaemonManager


@dataclass(frozen=True, slots=True)
class MigrationContext:
    """Interactive managed-auth migration context."""

    managed_auth: ManagedAuthMigrationCoordinator


@dataclass(frozen=True, slots=True)
class UpdateContext:
    """Self-update command context."""

    update: UpdateService


@dataclass(frozen=True, slots=True, kw_only=True)
class InvocationComposers:
    """Typed lazy composers configured as one cohesive dependency."""

    application: Callable[[], Composed[AppContext]]
    persistence: Callable[[], Composed[PersistenceContext]]
    doctor: Callable[[], Composed[DoctorContext]]
    daemon: Callable[[], Composed[DaemonContext]]
    migration: Callable[[], Composed[MigrationContext]]
    update: Callable[[], Composed[UpdateContext]]
