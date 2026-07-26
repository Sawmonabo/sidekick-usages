"""Invocation-scoped production composition."""

import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from functools import partial
from pathlib import Path

from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.contexts.migration import ManagedAuthDaemonLifecycle
from sidekick_usages.cli.contexts.models import (
    AppContext,
    Composed,
    DaemonContext,
    DoctorContext,
    DoctorFailed,
    DoctorReady,
    InvocationComposers,
    MigrationContext,
    PersistenceContext,
    UpdateContext,
)
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountLifecyclePersistence,
)
from sidekick_usages.credentials.accounts.lifecycle.service import (
    AccountLifecycleCoordinator,
)
from sidekick_usages.credentials.authorities import credential_resolver_for
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
from sidekick_usages.credentials.managed.composition import (
    compose_managed_credential_factories,
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
from sidekick_usages.daemon.lifecycle.ports import ProviderCapabilityReadiness
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import (
    PersistenceError,
    PersistenceFilesystemError,
)
from sidekick_usages.persistence.models.status import (
    PersistenceFailure,
)
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.snapshots.activity.store import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
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
from sidekick_usages.providers.codex.app_server.executable import (
    resolve_codex_executable,
)
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


class ApplicationCompositionError(Exception):
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


def _build_daemon_manager(
    paths: ApplicationPaths,
    *,
    clock: Clock | None = None,
    provider_readiness: ProviderCapabilityReadiness | None = None,
) -> DaemonManager:
    """Compose lifecycle management with lazy Codex path qualification."""
    return build_daemon_manager(
        codex_executable=partial(resolve_codex_executable, os.environ),
        paths=paths,
        clock=clock,
        provider_readiness=provider_readiness,
    )


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
    manager = _build_daemon_manager(paths) if daemon is None else daemon
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
    environment: Mapping[str, str] | None = None,
) -> Composed[AppContext]:
    """Compose normal store-backed application services."""

    def build(resources: ExitStack) -> AppContext:
        resolved_paths = _resolved_paths(paths)
        resolved_clock = _resolved_clock(clock)
        resolved_environment = (
            os.environ if environment is None else environment
        )
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
            raise ApplicationCompositionError(
                _persistence_failure(error, resolved_paths.accounts)
            ) from None
        resolver = credential_resolver_for(
            accounts,
            private,
            managed_factories=compose_managed_credential_factories(
                resolved_paths,
                persistence,
                accounts,
                resolved_clock,
                resolved_environment,
            ),
        )
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
                    discover_claude_config_dir(resolved_environment)
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
        daemon = _build_daemon_manager(
            resolved_paths,
            clock=resolved_clock,
            provider_readiness=capability_service,
        )
        supervisor = daemon.health()
        persistence = _persistence(resolved_paths, daemon)
        try:
            status, saved_accounts = persistence.observe_accounts()
            refresh_status = persistence.refresh_status()
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
            _build_daemon_manager(_resolved_paths(paths))
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
        daemon = _build_daemon_manager(
            resolved_paths,
            clock=resolved_clock,
        )
        try:
            persistence = _persistence(resolved_paths, daemon)
            accounts = persistence.open_store()
        except PersistenceError as error:
            raise ApplicationCompositionError(
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
