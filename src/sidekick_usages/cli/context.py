"""Invocation state and lazy access to typed CLI contexts."""

from collections.abc import Callable
from typing import Never

import click
import typer
from rich.console import Console

from sidekick_usages.cli.contexts.composition import (
    ApplicationCompositionError,
    default_invocation_composers,
)
from sidekick_usages.cli.contexts.dashboard.runtime import (
    compose_dashboard_runtime,
)
from sidekick_usages.cli.contexts.models import (
    AppContext,
    Composed,
    DaemonContext,
    DoctorContext,
    InvocationComposers,
    MigrationContext,
    PersistenceContext,
    UpdateContext,
)
from sidekick_usages.cli.contexts.use import UseContext, compose_use_context
from sidekick_usages.cli.dashboard.models.runtime import DashboardRuntime
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    exit_code_for_persistence_code,
)
from sidekick_usages.persistence.models.status import PersistenceFailure


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
        except ApplicationCompositionError as error:
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
        except ApplicationCompositionError as error:
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
