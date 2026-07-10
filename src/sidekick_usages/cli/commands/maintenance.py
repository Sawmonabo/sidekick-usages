"""Scheduled refresh-plus-heartbeat maintenance command adapter."""

from typing import Annotated

import typer

from sidekick_usages.cli.commands.heartbeat import render_outcomes
from sidekick_usages.cli.commands.usage import print_no_accounts
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import (
    ExitCode,
    RefreshStatus,
    highest_exit_code,
)
from sidekick_usages.heartbeat import heartbeat_exit_code
from sidekick_usages.maintenance import RefreshOutcome, refresh_exit_code


def _render_refresh_outcomes(
    ctx: typer.Context,
    outcomes: list[RefreshOutcome],
    *,
    quiet: bool,
) -> None:
    invocation = invocation_context(ctx)
    for outcome in outcomes:
        if quiet and outcome.exit_code is ExitCode.SUCCESS:
            continue
        if outcome.status is RefreshStatus.OK:
            invocation.console.print(
                f"[green]{outcome.label}: refreshed[/green]"
            )
        elif outcome.status is RefreshStatus.FAILED:
            invocation.console.print(
                f"[red]{outcome.label}: {outcome.message}[/red]"
            )
        elif not quiet:
            invocation.console.print(
                f"[dim]{outcome.label}: skipped ({outcome.message})[/dim]"
            )


def run_refresh_all(
    ctx: typer.Context,
    *,
    quiet: bool,
    force: bool,
) -> None:
    """Run scheduler-safe refresh for the sole ``refresh --all`` path."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    if not list(app_context.accounts):
        print_no_accounts(ctx, None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    outcomes = app_context.maintenance.refresh_all(force=force)
    _render_refresh_outcomes(ctx, outcomes, quiet=quiet)
    code = refresh_exit_code(outcomes)
    if code:
        raise typer.Exit(code=code)


def maintain_cmd(
    ctx: typer.Context,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Only print manual-action failures."),
    ] = False,
) -> None:
    """Run scheduler-safe token refresh, then opted-in heartbeat."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    if not list(app_context.accounts):
        print_no_accounts(ctx, None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    refresh_outcomes = app_context.maintenance.refresh_all()
    _render_refresh_outcomes(ctx, refresh_outcomes, quiet=quiet)
    heartbeat_outcomes = app_context.heartbeat.heartbeat_all()
    render_outcomes(ctx, heartbeat_outcomes, quiet=quiet)
    code = highest_exit_code(
        refresh_exit_code(refresh_outcomes),
        heartbeat_exit_code(heartbeat_outcomes),
    )
    if code:
        raise typer.Exit(code=code)


def register(application: typer.Typer) -> None:
    """Register the maintenance command exactly once."""
    branded_command(application, "maintain")(maintain_cmd)


__all__ = ["register", "run_refresh_all"]
