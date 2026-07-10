"""Release-check and self-update command adapters."""

from typing import Annotated

import typer

from sidekick_usages import __version__
from sidekick_usages.branding import update_status_line
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import ExitCode
from sidekick_usages.errors import ForbiddenError, UsageError
from sidekick_usages.update import (
    InstallMethod,
    UpdateCommandFailedError,
    UpdateToolMissingError,
    is_newer,
    manual_instructions,
)


def check_update_cmd(ctx: typer.Context) -> None:
    """Check whether a newer release is available on GitHub."""
    invocation = invocation_context(ctx)
    try:
        latest = invocation.require_update(ctx).update.latest_release()
    except ForbiddenError as error:
        invocation.err_console.print(
            "[yellow]GitHub rate limit reached; try again later.[/yellow]"
        )
        if error.api_message:
            invocation.err_console.print(f"[dim]{error.api_message}[/dim]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    except UsageError as error:
        invocation.err_console.print(f"[red]Could not check: {error}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    except ValueError as error:
        invocation.err_console.print(
            f"[red]Unexpected GitHub response: {error}[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    invocation.console.print(update_status_line())
    invocation.console.print()
    if is_newer(latest, __version__):
        invocation.console.print(
            f"[green]New version {latest} available[/green] "
            f"(currently {__version__}). "
            "Run [bold]sidekick-usages update[/bold] to upgrade."
        )
        return
    invocation.console.print(f"[dim]Up to date ({__version__}).[/dim]")


def update_cmd(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the upgrade command without running it.",
        ),
    ] = False,
) -> None:
    """Upgrade sidekick-usages to the latest release.

    Detects the install method from ``sys.executable`` and invokes
    the matching upgrade command. Refuses to guess when the install
    method can't be determined — falls back to manual instructions.
    """
    invocation = invocation_context(ctx)
    service = invocation.require_update(ctx).update
    if service.install_method() is InstallMethod.UNKNOWN:
        invocation.err_console.print(
            f"[yellow]{manual_instructions()}[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    command = service.upgrade_command()
    invocation.console.print(f"[dim]$ {' '.join(command)}[/dim]")
    if dry_run:
        return
    try:
        service.upgrade()
    except UpdateToolMissingError as error:
        invocation.err_console.print(
            f"[red]Upgrade tool {error.tool!r} not found on PATH.[/red] "
            f"Install {error.tool!r} and retry, or run a different "
            "upgrade path manually."
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error
    except UpdateCommandFailedError as error:
        raise typer.Exit(code=error.return_code) from error


def register(application: typer.Typer) -> None:
    """Register update commands exactly once."""
    branded_command(application, "check-update")(check_update_cmd)
    branded_command(application, "update")(update_cmd)


__all__ = ["register"]
