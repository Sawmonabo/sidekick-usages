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
    migration,
    permissions,
    session,
    updates,
    usage,
    use,
)
from sidekick_usages.cli.context import (
    InvocationContext,
    initialize_invocation,
)
from sidekick_usages.cli.help import BrandedTyperGroup
from sidekick_usages.cli.validation import validated_provider
from sidekick_usages.core.types import ExitCode
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.errors import (
    PersistenceError,
    exit_code_for_persistence_code,
)

PROGRAM_NAME = "sidekick-usages"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"{PROGRAM_NAME} {__version__}")
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
    no_interactive: Annotated[
        bool,
        typer.Option(
            "--no-interactive",
            help="Render once without reading terminal input.",
        ),
    ] = False,
) -> None:
    """Default invocation runs ``check`` if no subcommand is given."""
    del no_interactive, version
    invocation = initialize_invocation(ctx)
    invocation.only = (
        validated_provider(ctx, only) if only is not None else None
    )
    if ctx.invoked_subcommand is None:
        usage.run(ctx)


def create_app() -> typer.Typer:
    """Register the complete CLI without composing runtime services."""
    application = typer.Typer(
        name=PROGRAM_NAME,
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
    migration.register(application)
    doctor.register(application)
    permissions.register(application)
    daemon.register(application)
    updates.register(application)
    claude.register(application)
    codex.register(application)
    use.register(application)
    session.register(application)
    return application


def run(argv: list[str] | None = None) -> int:
    """Run the CLI and translate typed boundary failures to process codes."""
    try:
        result: object = create_app()(
            args=argv,
            prog_name=PROGRAM_NAME,
            standalone_mode=False,
        )
        return result if isinstance(result, int) else 0
    except typer.Exit as error:
        exit_code = (
            ExitCode.SUCCESS if error.exit_code is None else error.exit_code
        )
        return int(exit_code)
    except PersistenceError as error:
        InvocationContext().err_console.print(f"[red]{error}[/red]")
        return int(exit_code_for_persistence_code(error.code))
    except UsageError as error:
        InvocationContext().err_console.print(f"[red]{error}[/red]")
        return int(ExitCode.MANUAL_ACTION)
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130
