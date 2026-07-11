"""Typer application assembly and process-boundary execution."""

import sys
from typing import Annotated

import typer

from sidekick_usages import __version__
from sidekick_usages.cli.commands import (
    accounts,
    claude,
    codex,
    credentials,
    daemon,
    doctor,
    heartbeat,
    maintenance,
    migrate,
    permissions,
    updates,
    usage,
)
from sidekick_usages.cli.commands.migrate import persistence_error_exit_code
from sidekick_usages.cli.context import (
    InvocationContext,
    initialize_invocation,
)
from sidekick_usages.cli.help import BrandedTyperGroup
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.errors import PersistenceError


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"sidekick-usages {__version__}")
        raise typer.Exit()


def _main(
    ctx: typer.Context,
    only: Annotated[
        str | None,
        typer.Option(
            "--only",
            help="Filter to one provider's accounts.",
            metavar="PROVIDER",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Default invocation runs ``check`` if no subcommand is given."""
    del version
    invocation = initialize_invocation(ctx)
    try:
        invocation.only = ProviderId(only) if only is not None else None
    except ValueError:
        invocation.err_console.print(
            f"[red]Unknown provider {only!r}. Known: "
            + ", ".join(provider.value for provider in ProviderId)
            + ".[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    if ctx.invoked_subcommand is None:
        usage.run(ctx)


def create_app() -> typer.Typer:
    """Register the complete CLI without composing runtime services."""
    application = typer.Typer(
        name="sidekick-usages",
        cls=BrandedTyperGroup,
        rich_markup_mode="rich",
        no_args_is_help=False,
        pretty_exceptions_show_locals=False,
        add_completion=False,
        context_settings={"help_option_names": ["-h", "--help"]},
    )
    application.callback(invoke_without_command=True)(_main)
    usage.register(application)
    accounts.register(application)
    credentials.register(application)
    heartbeat.register(application)
    maintenance.register(application)
    doctor.register(application)
    migrate.register(application)
    permissions.register(application)
    daemon.register(application)
    updates.register(application)
    claude.register(application)
    codex.register(application)
    return application


app = create_app()


def run(argv: list[str] | None = None) -> int:
    """Run the CLI and translate typed boundary failures to process codes."""
    try:
        result: object = app(args=argv, standalone_mode=False)
        return result if isinstance(result, int) else 0
    except typer.Exit as error:
        exit_code = (
            ExitCode.SUCCESS if error.exit_code is None else error.exit_code
        )
        return int(exit_code)
    except PersistenceError as error:
        InvocationContext().err_console.print(f"[red]{error}[/red]")
        return int(persistence_error_exit_code(error))
    except UsageError as error:
        InvocationContext().err_console.print(f"[red]{error}[/red]")
        return int(ExitCode.MANUAL_ACTION)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


__all__ = ["app", "create_app", "run"]
