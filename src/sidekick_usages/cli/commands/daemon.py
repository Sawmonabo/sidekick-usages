"""Scheduled-maintenance daemon command group."""

from typing import Annotated

import typer

from sidekick_usages.branding import brand_header
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.types.maintenance import DaemonOperation
from sidekick_usages.errors import UsageError

__all__ = ["register"]

_BACKEND_HELP = (
    "Scheduler backend: auto, systemd, cron, launchd, task-scheduler."
)


def _run(
    ctx: typer.Context,
    operation: DaemonOperation,
    backend: str,
) -> None:
    invocation = invocation_context(ctx)
    try:
        result = invocation.require_daemon(ctx).daemon.run(operation, backend)
    except (UsageError, ValueError) as error:
        invocation.err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=ExitCode.SCHEDULER_ERROR) from error
    style = "green" if result.exit_code is ExitCode.SUCCESS else "red"
    if (
        operation is DaemonOperation.STATUS
        and result.exit_code is ExitCode.SUCCESS
    ):
        invocation.console.print(
            brand_header(
                invocation.console.size.width,
                section="daemon status",
            )
        )
    invocation.console.print(
        f"[{style}]{result.backend}: {result.message}[/{style}]"
    )
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


def install_cmd(
    ctx: typer.Context,
    backend: Annotated[
        str,
        typer.Option("--backend", help=_BACKEND_HELP),
    ] = "auto",
) -> None:
    """Install scheduled saved-token refresh for the current user."""
    _run(ctx, DaemonOperation.INSTALL, backend)


def status_cmd(
    ctx: typer.Context,
    backend: Annotated[
        str,
        typer.Option("--backend", help=_BACKEND_HELP),
    ] = "auto",
) -> None:
    """Inspect scheduled saved-token refresh for the current user."""
    _run(ctx, DaemonOperation.STATUS, backend)


def uninstall_cmd(
    ctx: typer.Context,
    backend: Annotated[
        str,
        typer.Option("--backend", help=_BACKEND_HELP),
    ] = "auto",
) -> None:
    """Remove scheduled saved-token refresh for the current user."""
    _run(ctx, DaemonOperation.UNINSTALL, backend)


def register(application: typer.Typer) -> None:
    """Create and register the daemon command group."""
    daemon_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Install, inspect, or remove scheduled token refresh.",
        rich_markup_mode="rich",
    )
    branded_command(daemon_app, "install")(install_cmd)
    branded_command(daemon_app, "status")(status_cmd)
    branded_command(daemon_app, "uninstall")(uninstall_cmd)
    application.add_typer(daemon_app, name="daemon")
