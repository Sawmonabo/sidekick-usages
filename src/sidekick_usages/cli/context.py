"""Typed command contexts and invocation-scoped production composition."""

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

import click
import typer
from rich.console import Console

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.codex.coordinator import (
    CodexCredentialCoordinator,
)
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.daemon.lifecycle.manager import (
    DaemonManager,
    build_daemon_manager,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.doctor.service import DoctorService
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.activity_snapshots import (
    ActivitySnapshotStore,
)
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
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude.activity import (
    ClaudeActivity,
    discover_claude_config_dir,
)
from sidekick_usages.providers.claude.provider import (
    ClaudeProvider,
    ClaudeSetupToken,
)
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


@dataclass(frozen=True, slots=True)
class DaemonContext:
    """Scheduler-management command context."""

    daemon: DaemonManager


@dataclass(frozen=True, slots=True)
class UpdateContext:
    """Self-update command context."""

    update: UpdateService


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
        codex_coordinator = CodexCredentialCoordinator(
            accounts,
            private,
            clock=resolved_clock,
        )
        resolver = credential_resolver_for(accounts, private)
        refresh_coordinator = CredentialRefreshCoordinator(
            accounts,
            http,
            provider_map,
            refresh_transactions,
            clock=resolved_clock,
            codex=codex_coordinator,
            resolver=resolver,
        )
        credentials = CredentialService(
            accounts,
            http,
            provider_map,
            private,
            clock=resolved_clock,
            refresh_coordinator=refresh_coordinator,
            codex_coordinator=codex_coordinator,
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
            local_activity_sources=local_activity_map,
            account_activity_sources=account_activity_map,
            activity_snapshots=ActivitySnapshotStore(
                resolved_paths.activity_snapshots
            ),
            resolver=resolver,
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
        provider_map, heartbeat_map = _provider_maps(
            resolved_clock,
            providers,
            heartbeat_providers,
        )
        daemon = build_daemon_manager(
            paths=resolved_paths,
            clock=resolved_clock,
        )
        supervisor = daemon.health()
        persistence = _persistence(resolved_paths, daemon)
        try:
            accounts = persistence.open_store()
            status = persistence.status(accounts)
            refresh_status = persistence.refresh_status()
        except PersistenceError as error:
            return DoctorContext(
                DoctorFailed(
                    _persistence_failure(error, resolved_paths.accounts)
                ),
                supervisor,
            )
        return DoctorContext(
            DoctorReady(
                DoctorService(
                    tuple(accounts),
                    provider_map,
                    heartbeat_map,
                    resolved_clock,
                ),
                status,
                refresh_status,
            ),
            supervisor,
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
    """Lightweight presentation state and five lazy typed composers."""

    def __init__(
        self,
        *,
        console: Console | None = None,
        err_console: Console | None = None,
        app_composer: Callable[[], Composed[AppContext]] = (
            compose_app_context
        ),
        persistence_composer: Callable[[], Composed[PersistenceContext]] = (
            compose_persistence_context
        ),
        doctor_composer: Callable[[], Composed[DoctorContext]] = (
            compose_doctor_context
        ),
        daemon_composer: Callable[[], Composed[DaemonContext]] = (
            compose_daemon_context
        ),
        update_composer: Callable[[], Composed[UpdateContext]] = (
            compose_update_context
        ),
    ) -> None:
        self.console = console if console is not None else Console()
        self.err_console = (
            err_console if err_console is not None else Console(stderr=True)
        )
        self.only: ProviderId | None = None
        self._app = _LazyComposition(app_composer)
        self._persistence = _LazyComposition(persistence_composer)
        self._doctor = _LazyComposition(doctor_composer)
        self._daemon = _LazyComposition(daemon_composer)
        self._update = _LazyComposition(update_composer)

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

    def require_update(self, ctx: click.Context) -> UpdateContext:
        """Return the self-update context."""
        value = self._update.require(ctx)
        if not isinstance(value, UpdateContext):
            raise TypeError("Update composer returned the wrong context.")
        return value

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
