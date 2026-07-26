"""Resident supervisor lifecycle command group."""

import typer

from sidekick_usages.branding.rich import brand_header
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.types.lifecycle import DaemonOperation
from sidekick_usages.errors import UsageError


def _run(
    ctx: typer.Context,
    operation: DaemonOperation,
) -> None:
    invocation = invocation_context(ctx)
    try:
        result = invocation.require_daemon(ctx).daemon.run(operation)
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


def install_cmd(ctx: typer.Context) -> None:
    """Install and verify the current user's resident supervisor."""
    _run(ctx, DaemonOperation.INSTALL)


def status_cmd(ctx: typer.Context) -> None:
    """Inspect the current user's resident supervisor."""
    _run(ctx, DaemonOperation.STATUS)


def uninstall_cmd(ctx: typer.Context) -> None:
    """Remove only the current user's Sidekick supervisor."""
    _run(ctx, DaemonOperation.UNINSTALL)


def register(application: typer.Typer) -> None:
    """Create and register the daemon command group."""
    daemon_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Install, inspect, or remove the resident account supervisor.",
        rich_markup_mode="rich",
    )
    branded_command(daemon_app, "install")(install_cmd)
    branded_command(daemon_app, "status")(status_cmd)
    branded_command(daemon_app, "uninstall")(uninstall_cmd)
    application.add_typer(daemon_app, name="daemon")
