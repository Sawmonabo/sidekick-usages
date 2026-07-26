"""Typed command contexts and invocation-scoped production composition."""

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import click
import typer
from rich.console import Console

from sidekick_usages.cli.contexts.dashboard import (
    CachedDashboardSnapshotSource,
    compose_dashboard_runtime,
)
from sidekick_usages.cli.contexts.migration import ManagedAuthDaemonLifecycle
from sidekick_usages.cli.contexts.use import UseContext, compose_use_context
from sidekick_usages.cli.dashboard.models.runtime import DashboardRuntime
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountLifecyclePersistence,
)
from sidekick_usages.credentials.accounts.lifecycle.service import (
    AccountLifecycleCoordinator,
)
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.capabilities.ports import (
    ProviderCapabilityEvidenceSource,
)
from sidekick_usages.credentials.capabilities.service import (
    build_provider_capability_service,
)
from sidekick_usages.credentials.claude.managed.migration.service import (
    ClaudeManagedMigrationCoordinator,
)
from sidekick_usages.credentials.claude.setup.service import (
    ClaudeSetupTokenCoordinator,
)
from sidekick_usages.credentials.codex.migration import (
    CodexAuthMigrationCoordinator,
)
from sidekick_usages.credentials.migration.managed_auth.service import (
    ManagedAuthMigrationCoordinator,
)
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.daemon.lifecycle.manager import (
    DaemonManager,
    build_daemon_manager,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
)
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import (
    PersistenceError,
    PersistenceFilesystemError,
    exit_code_for_persistence_code,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
    PersistenceStatus,
)
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.snapshots.activity import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLocks,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude.activity import (
    ClaudeActivity,
    discover_claude_config_dir,
)
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.claude.types import ClaudeSetupToken
from sidekick_usages.providers.codex.activity import CodexActivity
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from sidekick_usages.update import UpdateService
from sidekick_usages.usage.activity import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
)
from sidekick_usages.usage.lookup.service import AccountCredentialAccess
from sidekick_usages.usage.ports import UsagePersistence
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


class _ApplicationCompositionError(Exception):
    """Carry one safe persistence failure to the invocation adapter."""

    def __init__(self, failure: PersistenceFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


def _compose[T](builder: Callable[[ExitStack], T]) -> Composed[T]:
    """Build and transfer resources without losing cleanup failures."""
    resources = ExitStack()
    try:
        value = builder(resources)
    except BaseException as construction_error:
        try:
            resources.close()
        except BaseException as cleanup_error:
            raise BaseExceptionGroup(
                "Composition and cleanup both failed.",
                [construction_error, cleanup_error],
            ) from None
        raise
    return Composed(value, resources.pop_all())


def _resolved_paths(paths: ApplicationPaths | None) -> ApplicationPaths:
    return discover_application_paths() if paths is None else paths


def _resolved_clock(clock: Clock | None) -> Clock:
    return SystemClock() if clock is None else clock


def _provider_maps(
    clock: Clock,
    providers: Mapping[ProviderId, Provider] | None,
    heartbeat_providers: Mapping[ProviderId, HeartbeatProvider] | None,
) -> tuple[
    dict[ProviderId, Provider],
    dict[ProviderId, HeartbeatProvider],
]:
    provider_map = (
        build_provider_registry(clock)
        if providers is None
        else dict(providers)
    )
    heartbeat_map = (
        build_heartbeat_registry(provider_map)
        if heartbeat_providers is None
        else dict(heartbeat_providers)
    )
    return provider_map, heartbeat_map


def _persistence(
    paths: ApplicationPaths,
    daemon: DaemonManager | None = None,
) -> PersistenceService:
    manager = build_daemon_manager(paths=paths) if daemon is None else daemon
    return PersistenceService(
        paths,
        maintenance_quiescent=manager.quiescent,
    )


def _persistence_failure(
    error: PersistenceError,
    path: Path,
) -> PersistenceFailure:
    artifact = (
        error.artifact_basename
        if isinstance(error, PersistenceFilesystemError)
        else None
    )
    return PersistenceFailure(error.code, path, str(error), artifact)


def compose_app_context(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
    providers: Mapping[ProviderId, Provider] | None = None,
    heartbeat_providers: Mapping[ProviderId, HeartbeatProvider] | None = None,
    local_activity_sources: Mapping[
        ProviderId,
        LocalTokenActivitySource,
    ]
    | None = None,
    account_activity_sources: Mapping[
        ProviderId,
        AccountTokenActivitySource,
    ]
    | None = None,
    claude_setup_token: ClaudeSetupToken | None = None,
) -> Composed[AppContext]:
    """Compose normal store-backed application services."""

    def build(resources: ExitStack) -> AppContext:
        resolved_paths = _resolved_paths(paths)
        resolved_clock = _resolved_clock(clock)
        provider_map, heartbeat_map = _provider_maps(
            resolved_clock,
            providers,
            heartbeat_providers,
        )
        http = resources.enter_context(HttpClient(clock=resolved_clock))
        try:
            persistence = _persistence(resolved_paths)
            accounts = persistence.open_store()
            private = persistence.private_credentials
            refresh_transactions = CredentialRefreshTransactions(
                accounts,
                resolved_paths.credential_refresh,
            )
            with refresh_transactions.hold_lifecycle():
                refresh_transactions.recover()
        except PersistenceError as error:
            raise _ApplicationCompositionError(
                _persistence_failure(error, resolved_paths.accounts)
            ) from None
        resolver = credential_resolver_for(accounts, private)
        refresh_coordinator = CredentialRefreshCoordinator(
            accounts,
            http,
            provider_map,
            refresh_transactions,
            clock=resolved_clock,
            resolver=resolver,
        )
        usage_snapshots = UsageSnapshotStore(resolved_paths.usage_snapshots)
        credentials = CredentialService(
            accounts,
            http,
            provider_map,
            refresh_coordinator=refresh_coordinator,
            codex_auth_migration=CodexAuthMigrationCoordinator(
                resolved_paths,
                accounts,
                persistence.managed_codex_profiles,
                resolved_clock,
            ),
            claude_auth_migration=ClaudeManagedMigrationCoordinator(
                resolved_paths,
                accounts,
                resolver,
                persistence.managed_claude_profiles,
                usage_snapshots,
                resolved_clock,
            ),
            claude_setup_tokens=ClaudeSetupTokenCoordinator(
                accounts,
                resolved_clock,
            ),
        )
        lifecycle = AccountLifecycleCoordinator(
            resolved_paths,
            AccountLifecyclePersistence(
                accounts=accounts,
                operations=OperationQueueStore(
                    resolved_paths.durable_operations
                ),
                activations=ActivationJournalStore(
                    resolved_paths.activation_journals,
                    resolved_paths.durable_operations,
                ),
                selected=SelectedStateStore(resolved_paths.selected_state),
                claude_profiles=persistence.managed_claude_profiles,
                codex_profiles=persistence.managed_codex_profiles,
            ),
        )
        if local_activity_sources is None:
            local_activity_map: dict[
                ProviderId,
                LocalTokenActivitySource,
            ] = {}
            if ProviderId.CLAUDE in provider_map:
                local_activity_map[ProviderId.CLAUDE] = ClaudeActivity(
                    discover_claude_config_dir()
                )
        else:
            local_activity_map = dict(local_activity_sources)
        if account_activity_sources is None:
            account_activity_map: dict[
                ProviderId,
                AccountTokenActivitySource,
            ] = {}
            if ProviderId.CODEX in provider_map:
                account_activity_map[ProviderId.CODEX] = CodexActivity()
        else:
            account_activity_map = dict(account_activity_sources)
        usage = UsageCheckService(
            accounts,
            http,
            provider_map,
            credentials,
            clock=resolved_clock,
            credential_access=AccountCredentialAccess(
                resolver,
                OperationAuthorityLocks(resolved_paths.durable_operations),
            ),
            local_activity_sources=local_activity_map,
            account_activity_sources=account_activity_map,
            persistence=UsagePersistence(
                activity=ActivitySnapshotStore(
                    resolved_paths.activity_snapshots
                ),
                usage=usage_snapshots,
            ),
        )
        heartbeat = HeartbeatService(
            accounts,
            http,
            heartbeat_map,
            clock=resolved_clock,
            resolver=resolver,
        )
        setup_token = (
            ClaudeProvider(resolved_clock)
            if claude_setup_token is None
            else claude_setup_token
        )
        return AppContext(
            accounts=accounts,
            usage=usage,
            credentials=credentials,
            lifecycle=lifecycle,
            heartbeat=heartbeat,
            maintenance=TokenMaintenanceService(
                accounts,
                credentials,
                clock=resolved_clock,
            ),
            claude_setup_token=setup_token,
        )

    return _compose(build)


def compose_persistence_context(
    *,
    paths: ApplicationPaths | None = None,
) -> Composed[PersistenceContext]:
    """Compose current persistence administration."""

    def build(_resources: ExitStack) -> PersistenceContext:
        return PersistenceContext(_persistence(_resolved_paths(paths)))

    return _compose(build)


def compose_doctor_context(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
    providers: Mapping[ProviderId, Provider] | None = None,
    heartbeat_providers: Mapping[ProviderId, HeartbeatProvider] | None = None,
) -> Composed[DoctorContext]:
    """Compose one ready or failed read-only doctor state."""

    def build(_resources: ExitStack) -> DoctorContext:
        resolved_paths = _resolved_paths(paths)
        resolved_clock = _resolved_clock(clock)
        _provider_map, heartbeat_map = _provider_maps(
            resolved_clock,
            providers,
            heartbeat_providers,
        )
        capability_service = build_provider_capability_service(resolved_paths)
        daemon = build_daemon_manager(
            paths=resolved_paths,
            clock=resolved_clock,
            provider_readiness=capability_service,
        )
        supervisor = daemon.health()
        persistence = _persistence(resolved_paths, daemon)
        try:
            accounts = persistence.open_store()
            status = persistence.status(accounts)
            refresh_status = persistence.refresh_status()
            saved_accounts = accounts.saved_accounts()
            selected_states = SelectedStateStore(
                resolved_paths.selected_state
            ).observe_all()
            operations = OperationQueueStore(
                resolved_paths.durable_operations
            ).observe()
            activations = ActivationJournalStore(
                resolved_paths.activation_journals,
                resolved_paths.durable_operations,
            ).observe_all()
            dashboard = CachedDashboardSnapshotSource(
                resolved_paths,
                resolved_clock,
            ).load(None)
        except PersistenceError as error:
            return DoctorContext(
                DoctorFailed(
                    _persistence_failure(error, resolved_paths.accounts)
                ),
                supervisor,
                capability_service,
            )
        try:
            runtime = DoctorRuntimeService(
                saved_accounts,
                dashboard,
                selected_states,
                operations,
                activations,
            )
        except ValueError:
            failure = PersistenceFailure(
                PersistenceCode.INVALID_SCHEMA,
                resolved_paths.durable_operations,
                "Supervisor state does not match the saved accounts.",
                None,
            )
            return DoctorContext(
                DoctorFailed(failure),
                supervisor,
                capability_service,
            )
        return DoctorContext(
            DoctorReady(
                DoctorService(
                    saved_accounts,
                    capability_service,
                    heartbeat_map.keys(),
                    resolved_clock,
                    runtime,
                ),
                status,
                refresh_status,
            ),
            supervisor,
            capability_service,
        )

    return _compose(build)


def compose_daemon_context(
    *,
    paths: ApplicationPaths | None = None,
) -> Composed[DaemonContext]:
    """Compose resident-service lifecycle management only."""
    return _compose(
        lambda _resources: DaemonContext(
            build_daemon_manager(paths=_resolved_paths(paths))
        )
    )


def compose_migration_context(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
) -> Composed[MigrationContext]:
    """Compose managed-auth migration without HTTP or usage services."""

    def build(_resources: ExitStack) -> MigrationContext:
        resolved_paths = _resolved_paths(paths)
        resolved_clock = _resolved_clock(clock)
        daemon = build_daemon_manager(
            paths=resolved_paths,
            clock=resolved_clock,
        )
        try:
            persistence = _persistence(resolved_paths, daemon)
            accounts = persistence.open_store()
        except PersistenceError as error:
            raise _ApplicationCompositionError(
                _persistence_failure(error, resolved_paths.accounts)
            ) from None
        resolver = credential_resolver_for(
            accounts,
            persistence.private_credentials,
        )
        return MigrationContext(
            ManagedAuthMigrationCoordinator(
                accounts,
                ManagedAuthDaemonLifecycle(daemon),
                resolved_clock,
                CodexAuthMigrationCoordinator(
                    resolved_paths,
                    accounts,
                    persistence.managed_codex_profiles,
                    resolved_clock,
                ),
                ClaudeManagedMigrationCoordinator(
                    resolved_paths,
                    accounts,
                    resolver,
                    persistence.managed_claude_profiles,
                    UsageSnapshotStore(resolved_paths.usage_snapshots),
                    resolved_clock,
                ),
            )
        )

    return _compose(build)


def compose_update_context(
    *,
    clock: Clock | None = None,
) -> Composed[UpdateContext]:
    """Compose update services and their owned HTTP resource."""

    def build(resources: ExitStack) -> UpdateContext:
        resolved_clock = _resolved_clock(clock)
        http = resources.enter_context(HttpClient(clock=resolved_clock))
        return UpdateContext(UpdateService(http))

    return _compose(build)


def default_invocation_composers() -> InvocationComposers:
    """Return the complete production lazy-composition graph."""
    return InvocationComposers(
        application=compose_app_context,
        persistence=compose_persistence_context,
        doctor=compose_doctor_context,
        daemon=compose_daemon_context,
        migration=compose_migration_context,
        update=compose_update_context,
    )


class _LazyComposition[T]:
    """Cache one typed composition owner and register its close once."""

    def __init__(self, composer: Callable[[], Composed[T]]) -> None:
        self._composer = composer
        self._owner: Composed[T] | None = None

    def require(self, ctx: click.Context) -> T:
        """Compose once, register one root cleanup, and return the value."""
        owner = self._owner
        if owner is None:
            owner = self._composer()
            self._owner = owner
            ctx.find_root().call_on_close(owner.close)
        return owner.value


class InvocationContext:
    """Lightweight presentation state and lazy typed composers."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        err_console: Console | None = None,
        composers: InvocationComposers | None = None,
        dashboard_composer: Callable[
            [],
            DashboardRuntime,
        ] = compose_dashboard_runtime,
        use_composer: Callable[[], UseContext] = compose_use_context,
    ) -> None:
        resolved_composers = (
            default_invocation_composers() if composers is None else composers
        )
        self.console = console if console is not None else Console()
        self.err_console = (
            err_console if err_console is not None else Console(stderr=True)
        )
        self.only: ProviderId | None = None
        self._app = _LazyComposition(resolved_composers.application)
        self._persistence = _LazyComposition(resolved_composers.persistence)
        self._doctor = _LazyComposition(resolved_composers.doctor)
        self._daemon = _LazyComposition(resolved_composers.daemon)
        self._migration = _LazyComposition(resolved_composers.migration)
        self._update = _LazyComposition(resolved_composers.update)
        self._dashboard_composer = dashboard_composer
        self._dashboard: DashboardRuntime | None = None
        self._use_composer = use_composer
        self._use: UseContext | None = None

    def require_app(self, ctx: click.Context) -> AppContext:
        """Return the one normal application context."""
        try:
            value = self._app.require(ctx)
        except _ApplicationCompositionError as error:
            self._exit_failure(error.failure)
        if not isinstance(value, AppContext):
            raise TypeError("Application composer returned the wrong context.")
        return value

    def require_persistence(self, ctx: click.Context) -> PersistenceContext:
        """Return current persistence administration."""
        value = self._persistence.require(ctx)
        if not isinstance(value, PersistenceContext):
            raise TypeError("Persistence composer returned the wrong context.")
        return value

    def require_doctor(self, ctx: click.Context) -> DoctorContext:
        """Return the one read-only doctor context."""
        value = self._doctor.require(ctx)
        if not isinstance(value, DoctorContext):
            raise TypeError("Doctor composer returned the wrong context.")
        return value

    def require_daemon(self, ctx: click.Context) -> DaemonContext:
        """Return the resident-service lifecycle context."""
        value = self._daemon.require(ctx)
        if not isinstance(value, DaemonContext):
            raise TypeError("Daemon composer returned the wrong context.")
        return value

    def require_migration(self, ctx: click.Context) -> MigrationContext:
        """Return managed-auth migration coordination."""
        try:
            value = self._migration.require(ctx)
        except _ApplicationCompositionError as error:
            self._exit_failure(error.failure)
        if not isinstance(value, MigrationContext):
            raise TypeError("Migration composer returned the wrong context.")
        return value

    def require_update(self, ctx: click.Context) -> UpdateContext:
        """Return the self-update context."""
        value = self._update.require(ctx)
        if not isinstance(value, UpdateContext):
            raise TypeError("Update composer returned the wrong context.")
        return value

    def require_dashboard(self) -> DashboardRuntime:
        """Compose the passive dashboard boundary at most once."""
        dashboard = self._dashboard
        if dashboard is None:
            dashboard = self._dashboard_composer()
            self._dashboard = dashboard
        return dashboard

    def require_use(self) -> UseContext:
        """Compose the secret-free selection boundary at most once."""
        use = self._use
        if use is None:
            use = self._use_composer()
            self._use = use
        return use

    def _exit_failure(self, failure: PersistenceFailure) -> Never:
        self.err_console.print(f"[red]{failure.message}[/red]")
        self.err_console.print(f"[dim]Path: {failure.path}[/dim]")
        if failure.artifact_basename is not None:
            self.err_console.print(
                f"[dim]Artifact: {failure.artifact_basename}[/dim]"
            )
        raise typer.Exit(code=exit_code_for_persistence_code(failure.code))


def initialize_invocation(ctx: click.Context) -> InvocationContext:
    """Install production invocation state or validate injected state."""
    if ctx.obj is None:
        ctx.obj = InvocationContext()
    if not isinstance(ctx.obj, InvocationContext):
        raise TypeError("Expected an InvocationContext in ctx.obj.")
    return ctx.obj


def invocation_context(ctx: click.Context) -> InvocationContext:
    """Return explicitly initialized invocation state."""
    if not isinstance(ctx.obj, InvocationContext):
        raise TypeError("Expected an InvocationContext in ctx.obj.")
    return ctx.obj
