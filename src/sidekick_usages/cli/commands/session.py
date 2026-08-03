"""Explicit integrated-session command group."""

from typing import Annotated, Never

import typer

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.cli.session.models import (
    ShellEnrollmentStatus,
    ShellIntegrationError,
    ShellIntegrationResult,
    ShellKind,
)
from sidekick_usages.core.types import ExitCode


def _refuse_provider(ctx: typer.Context, provider: str) -> Never:
    invocation = invocation_context(ctx)
    invocation.err_console.print(
        f"[red]{provider} session integration is not available; "
        "the provider process was not started.[/red]"
    )
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def claude_cmd(
    ctx: typer.Context,
    claude_arguments: Annotated[
        list[str] | None,
        typer.Argument(
            help="Arguments passed unchanged to Claude Code.",
            metavar="CLAUDE_ARGUMENTS",
        ),
    ] = None,
) -> None:
    """Enter a coordinated Claude session when its host is qualified."""
    del claude_arguments
    _refuse_provider(ctx, "claude")


def codex_cmd(
    ctx: typer.Context,
    codex_arguments: Annotated[
        list[str] | None,
        typer.Argument(
            help="Arguments passed unchanged to Codex CLI.",
            metavar="CODEX_ARGUMENTS",
        ),
    ] = None,
) -> None:
    """Enter a coordinated Codex session when its relay is qualified."""
    del codex_arguments
    _refuse_provider(ctx, "codex")


def _render_shell_failure(
    ctx: typer.Context,
    error: ShellIntegrationError,
) -> Never:
    invocation = invocation_context(ctx)
    invocation.err_console.print(f"[red]{error}[/red]")
    if error.path is not None:
        invocation.err_console.print(f"[dim]Path: {error.path}[/dim]")
    if error.manual_range is not None:
        start, end = error.manual_range
        invocation.err_console.print(
            f"[dim]Manual removal range: lines {start}-{end}[/dim]"
        )
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def _render_result(ctx: typer.Context, result: ShellIntegrationResult) -> None:
    invocation = invocation_context(ctx)
    label = "Dry run" if result.dry_run else "Shell enrollment"
    outcome = "would change" if result.dry_run else "changed"
    if not result.changed:
        outcome = "already matches"
    invocation.console.print(f"[green]{label}: {outcome}.[/green]")
    for path in result.paths:
        invocation.console.print(f"[dim]Path: {path}[/dim]")
    for precondition in result.preconditions:
        invocation.console.print(f"[dim]Precondition: {precondition}[/dim]")
    for difference in result.diffs:
        invocation.console.print(difference, markup=False, highlight=False)


def _shell_change(
    ctx: typer.Context,
    shell: ShellKind | None,
    dry_run: bool,
    *,
    install: bool,
) -> None:
    enrollment = invocation_context(ctx).require_session().shell
    try:
        result = (
            enrollment.install(shell, dry_run=dry_run)
            if install
            else enrollment.uninstall(shell, dry_run=dry_run)
        )
    except ShellIntegrationError as error:
        _render_shell_failure(ctx, error)
    _render_result(ctx, result)


def shell_install_cmd(
    ctx: typer.Context,
    shell: Annotated[
        ShellKind | None,
        typer.Option("--shell", help="Shell to enroll."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print qualified paths and diffs without writing.",
        ),
    ] = False,
) -> None:
    """Install explicit Sidekick shell forwarding functions."""
    _shell_change(ctx, shell, dry_run, install=True)


def shell_uninstall_cmd(
    ctx: typer.Context,
    shell: Annotated[
        ShellKind | None,
        typer.Option("--shell", help="Shell enrollment to remove."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print qualified paths and diffs without writing.",
        ),
    ] = False,
) -> None:
    """Remove only exact Sidekick shell forwarding content."""
    _shell_change(ctx, shell, dry_run, install=False)


def _render_status(ctx: typer.Context, status: ShellEnrollmentStatus) -> None:
    invocation = invocation_context(ctx)
    invocation.console.print(f"{status.state.value}: {status.detail}")
    for path in status.paths:
        invocation.console.print(f"[dim]Path: {path}[/dim]")


def shell_status_cmd(
    ctx: typer.Context,
    shell: Annotated[
        ShellKind | None,
        typer.Option("--shell", help="Shell enrollment to inspect."),
    ] = None,
) -> None:
    """Inspect shell forwarding without reading provider credentials."""
    status = invocation_context(ctx).require_session().shell.status(shell)
    _render_status(ctx, status)


def register(application: typer.Typer) -> None:
    """Create and register explicit provider-session commands."""
    session_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Launch or enroll coordinated provider sessions.",
        rich_markup_mode="rich",
    )
    shell_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Install, inspect, or remove shell enrollment.",
        rich_markup_mode="rich",
    )
    branded_command(session_app, "claude")(claude_cmd)
    branded_command(session_app, "codex")(codex_cmd)
    branded_command(shell_app, "install")(shell_install_cmd)
    branded_command(shell_app, "uninstall")(shell_uninstall_cmd)
    branded_command(shell_app, "status")(shell_status_cmd)
    session_app.add_typer(shell_app, name="shell")
    application.add_typer(session_app, name="session")
