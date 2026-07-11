"""Typed command contexts and invocation-scoped production composition."""

import shlex
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

import click
import typer
from rich.console import Console

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials import CredentialService
from sidekick_usages.daemon import DaemonManager
from sidekick_usages.doctor import DoctorService
from sidekick_usages.heartbeat import HeartbeatProvider, HeartbeatService
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.account_store import (
    AccountStore,
    AccountStoreStateError,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
    PersistenceOperationResult,
    recovery_guidance,
    recovery_next_command,
)
from sidekick_usages.persistence.assessment import (
    doctor_exit_code as persistence_doctor_exit_code,
)
from sidekick_usages.persistence.errors import (
    ManagedFileReadError,
    PersistenceCode,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.migrations import (
    PermissionRepairOperationResult,
    PersistenceMigrationService,
)
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
    PersistenceMigrationStateError,
)
from sidekick_usages.persistence.migrations.location import (
    BlockedLocationSelection,
    LocationMigrationAssessment,
    LocationMigrationResult,
    ReadyLocationSelection,
    RuntimePersistenceSelection,
    blocked_location_assessment,
    is_blocked_location_selection,
    ready_location_assessment,
)
from sidekick_usages.persistence.v060 import ReleasedV060Verifier
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import (
    ClaudeActivity,
    ClaudeProvider,
    ClaudeSetupToken,
    discover_claude_config_dir,
)
from sidekick_usages.providers.codex import CodexActivity
from sidekick_usages.providers.codex.auth_migration import (
    CodexPrivateAuthMigrator,
)
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from sidekick_usages.update import UpdateService
from sidekick_usages.usage import (
    AccountTokenActivitySource,
    LocalTokenActivitySource,
    UsageCheckService,
)


class PersistenceCommands(Protocol):
    """Persistence operations exposed to explicit recovery commands."""

    def assess(self) -> PersistenceAssessment:
        """Return a passive assessment."""

    def assess_locations(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Return passive durable-state location evidence."""

    def mutation_preview(self) -> PersistenceAssessment:
        """Require scheduler quiescence and return a safe preview."""

    def location_migration_preview(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Require quiescence and return relocation evidence."""

    def permission_repair_preview(
        self,
    ) -> PersistenceAssessment | PersistenceCompositionFailure:
        """Return the bounded explicit permission-repair scope."""

    def read_accounts(self) -> tuple[Account, ...]:
        """Return a validated read-only account snapshot."""

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        """Migrate account authority or import the prototype."""

    def migrate_locations(self) -> LocationMigrationResult:
        """Relocate compatibility state to native application data."""

    def prepare_rollback(self) -> PersistenceOperationResult:
        """Prepare exact released-v0.6.0 compatibility."""

    def repair_permissions(self) -> PermissionRepairOperationResult:
        """Repair and verify released-layout permissions."""

    def full_reset(self) -> PersistenceAssessment:
        """Delete every Sidekick-owned credential artifact."""


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
    """Explicit persistence-recovery command context."""

    persistence: PersistenceCommands


@dataclass(frozen=True, slots=True)
class DoctorReady:
    """Doctor can inspect validated accounts and persistence state."""

    service: DoctorService
    assessment: LocationMigrationAssessment[ReadyLocationSelection]


@dataclass(frozen=True, slots=True)
class DoctorBlocked:
    """Doctor can render a safe blocking persistence assessment."""

    assessment: LocationMigrationAssessment[BlockedLocationSelection]


@dataclass(frozen=True, slots=True)
class DoctorFailed:
    """Doctor can render a bounded passive-composition failure."""

    failure: PersistenceCompositionFailure


type DoctorState = DoctorReady | DoctorBlocked | DoctorFailed


@dataclass(frozen=True, slots=True)
class DoctorContext:
    """Closed doctor-state context."""

    state: DoctorState


@dataclass(frozen=True, slots=True)
class DaemonContext:
    """Scheduler-management command context."""

    daemon: DaemonManager


@dataclass(frozen=True, slots=True)
class UpdateContext:
    """Self-update command context."""

    update: UpdateService


class _ApplicationCompositionError(Exception):
    """Carry one safe passive failure to the invocation adapter."""

    def __init__(self, failure: PersistenceCompositionFailure) -> None:
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
) -> PersistenceMigrationService:
    daemon = DaemonManager()
    return PersistenceMigrationService(
        paths,
        scheduler_assessor=daemon.assess_quiescence,
        private_auth_migrator=CodexPrivateAuthMigrator(),
        released_v060_verifier=ReleasedV060Verifier(),
    )


def _composition_failure(
    error: ManagedFileReadError
    | UnsafeManagedFileError
    | UnsupportedFilesystemError,
    safe_path: Path,
) -> PersistenceCompositionFailure:
    return PersistenceCompositionFailure(
        code=error.code,
        safe_path=safe_path,
        artifact_basename=error.artifact_basename,
        message=str(error),
        next_command=recovery_next_command(error.code),
        guidance=recovery_guidance(error.code),
    )


def _location_race_failure(
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> PersistenceCompositionFailure:
    code = PersistenceCode.SOURCE_CHANGED
    return PersistenceCompositionFailure(
        code=code,
        safe_path=assessment.source,
        artifact_basename=assessment.artifact_basename,
        message=(
            "Persistence locations changed while diagnostics were being read."
        ),
        next_command=("sidekick-usages", "doctor"),
        guidance=recovery_guidance(code),
    )


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
            runtime = persistence.runtime()
            private = runtime.private_credentials
            accounts = AccountStore(
                runtime.locations,
                orphaned_credentials_observer=private.observe,
                private_credentials=private,
            ).load()
            persistence.require_location_unchanged(runtime.assessment)
        except (
            ManagedFileReadError,
            UnsafeManagedFileError,
            UnsupportedFilesystemError,
        ) as error:
            raise _ApplicationCompositionError(
                _composition_failure(error, resolved_paths.accounts.canonical)
            ) from None
        credentials = CredentialService(
            accounts,
            http,
            provider_map,
            private,
            clock=resolved_clock,
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
        )
        heartbeat = HeartbeatService(
            accounts,
            http,
            heartbeat_map,
            clock=resolved_clock,
        )
        maintenance = TokenMaintenanceService(
            accounts,
            credentials,
            clock=resolved_clock,
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
            maintenance=maintenance,
            claude_setup_token=setup_token,
        )

    return _compose(build)


def compose_persistence_context(
    *,
    paths: ApplicationPaths | None = None,
) -> Composed[PersistenceContext]:
    """Compose explicit persistence mutation services only."""

    def build(_resources: ExitStack) -> PersistenceContext:
        resolved_paths = _resolved_paths(paths)
        return PersistenceContext(_persistence(resolved_paths))

    return _compose(build)


def _ready_doctor_state(
    persistence: PersistenceMigrationService,
    ready: LocationMigrationAssessment[ReadyLocationSelection],
    providers: dict[ProviderId, Provider],
    heartbeat_providers: dict[ProviderId, HeartbeatProvider],
    clock: Clock,
) -> DoctorState:
    try:
        accounts = persistence.read_accounts()
        persistence.require_location_unchanged(ready)
    except LocationMigrationStateError as error:
        if is_blocked_location_selection(error.assessment.selection):
            return DoctorBlocked(blocked_location_assessment(error.assessment))
        return DoctorFailed(_location_race_failure(error.assessment))
    except PersistenceMigrationStateError as error:
        return DoctorFailed(
            PersistenceCompositionFailure(
                code=error.code,
                safe_path=ready.source,
                artifact_basename=ready.artifact_basename,
                message=str(error),
                next_command=error.next_command,
                guidance=recovery_guidance(error.code),
            )
        )
    except (
        ManagedFileReadError,
        UnsafeManagedFileError,
        UnsupportedFilesystemError,
    ) as error:
        return DoctorFailed(_composition_failure(error, ready.source))
    service = DoctorService(accounts, providers, heartbeat_providers, clock)
    return DoctorReady(service, ready)


def compose_doctor_context(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
    providers: Mapping[ProviderId, Provider] | None = None,
    heartbeat_providers: Mapping[ProviderId, HeartbeatProvider] | None = None,
) -> Composed[DoctorContext]:
    """Compose one ready, blocked, or failed read-only doctor state."""

    def build(_resources: ExitStack) -> DoctorContext:
        resolved_paths = _resolved_paths(paths)
        resolved_clock = _resolved_clock(clock)
        provider_map, heartbeat_map = _provider_maps(
            resolved_clock,
            providers,
            heartbeat_providers,
        )
        try:
            persistence = _persistence(resolved_paths)
            assessment = persistence.assess_locations()
        except (
            ManagedFileReadError,
            UnsafeManagedFileError,
            UnsupportedFilesystemError,
        ) as error:
            return DoctorContext(
                DoctorFailed(
                    _composition_failure(
                        error,
                        resolved_paths.accounts.canonical,
                    )
                )
            )
        if is_blocked_location_selection(assessment.selection):
            return DoctorContext(
                DoctorBlocked(blocked_location_assessment(assessment))
            )
        ready = ready_location_assessment(assessment)
        return DoctorContext(
            _ready_doctor_state(
                persistence,
                ready,
                provider_map,
                heartbeat_map,
                resolved_clock,
            )
        )

    return _compose(build)


def compose_daemon_context() -> Composed[DaemonContext]:
    """Compose scheduler-management services only."""
    return _compose(lambda _resources: DaemonContext(DaemonManager()))


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
        except AccountStoreStateError as error:
            self._exit_assessment(error.assessment)
        except LocationMigrationStateError as error:
            self._exit_location_assessment(error.assessment)
        except _ApplicationCompositionError as error:
            self._exit_failure(error.failure)
        if not isinstance(value, AppContext):
            raise TypeError("Application composer returned the wrong context.")
        return value

    def require_persistence(self, ctx: click.Context) -> PersistenceContext:
        """Return the one persistence-recovery context."""
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
        """Return the one scheduler-management context."""
        value = self._daemon.require(ctx)
        if not isinstance(value, DaemonContext):
            raise TypeError("Daemon composer returned the wrong context.")
        return value

    def require_update(self, ctx: click.Context) -> UpdateContext:
        """Return the one self-update context."""
        value = self._update.require(ctx)
        if not isinstance(value, UpdateContext):
            raise TypeError("Update composer returned the wrong context.")
        return value

    def _exit_assessment(
        self,
        assessment: PersistenceAssessment,
    ) -> NoReturn:
        self.err_console.print(f"[red]{assessment.message}[/red]")
        if assessment.next_command is not None:
            self.err_console.print(
                "[dim]Next: " + shlex.join(assessment.next_command) + "[/dim]"
            )
        raise typer.Exit(code=persistence_doctor_exit_code(assessment.code))

    def _exit_failure(
        self,
        failure: PersistenceCompositionFailure,
    ) -> NoReturn:
        self.err_console.print(f"[red]{failure.message}[/red]")
        self.err_console.print(f"[dim]Path: {failure.safe_path}[/dim]")
        if failure.artifact_basename is not None:
            self.err_console.print(
                f"[dim]Artifact: {failure.artifact_basename}[/dim]"
            )
        if failure.guidance is not None:
            self.err_console.print(f"[dim]{failure.guidance}[/dim]")
        if failure.next_command is not None:
            self.err_console.print(
                "[dim]Next: " + shlex.join(failure.next_command) + "[/dim]"
            )
        raise typer.Exit(code=persistence_doctor_exit_code(failure.code))

    def _exit_location_assessment(
        self,
        assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
    ) -> NoReturn:
        self.err_console.print(
            "[red]Persistence locations require explicit diagnosis or "
            "migration.[/red]"
        )
        self.err_console.print(
            f"[dim]State: {assessment.selection.code.value}[/dim]"
        )
        self.err_console.print(f"[dim]Source: {assessment.source}[/dim]")
        self.err_console.print(
            f"[dim]Destination: {assessment.destination}[/dim]"
        )
        if assessment.next_command is not None:
            self.err_console.print(
                "[dim]Next: " + shlex.join(assessment.next_command) + "[/dim]"
            )
        raise typer.Exit(
            code=persistence_doctor_exit_code(
                LocationMigrationStateError(assessment).code
            )
        )


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


__all__ = [
    "AppContext",
    "Composed",
    "DaemonContext",
    "DoctorBlocked",
    "DoctorContext",
    "DoctorFailed",
    "DoctorReady",
    "DoctorState",
    "InvocationContext",
    "PersistenceCommands",
    "PersistenceContext",
    "UpdateContext",
    "compose_app_context",
    "compose_daemon_context",
    "compose_doctor_context",
    "compose_persistence_context",
    "compose_update_context",
    "initialize_invocation",
    "invocation_context",
]
