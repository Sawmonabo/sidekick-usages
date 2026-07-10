"""Account migration and rollback-preparation command group."""

import shlex
from typing import Annotated, NoReturn

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.text import Text

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
    PersistenceOperationResult,
    operation_exit_code,
)
from sidekick_usages.persistence.assessment import (
    doctor_exit_code as persistence_doctor_exit_code,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.migration_errors import (
    PersistenceMigrationStateError,
    PrototypeReimportRequiredError,
    SchedulerMutationBlockedError,
)


def render_preview(
    ctx: typer.Context,
    assessment: PersistenceAssessment | PersistenceCompositionFailure,
    *,
    title: str,
    detail: str | None = None,
) -> None:
    """Render bounded credential-free persistence mutation details."""
    if isinstance(assessment, PersistenceAssessment):
        generation = assessment.generation
        count = (
            str(assessment.account_count)
            if assessment.account_count is not None
            else "unknown"
        )
    else:
        generation = "unknown"
        count = "unknown"
    lines = [
        f"State: {assessment.code}",
        f"Generation: {generation}",
        f"Validated accounts: {count}",
        f"Path: {assessment.safe_path}",
    ]
    if assessment.artifact_basename is not None:
        lines.append(f"Artifact: {assessment.artifact_basename}")
    if detail is not None:
        lines.append(detail)
    invocation_context(ctx).console.print(
        Panel(
            Text("\n".join(lines)),
            border_style="yellow",
            title=f"[yellow]{title}[/yellow]",
            title_align="left",
        )
    )


def confirm_operation(ctx: typer.Context, yes: bool) -> None:
    """Require explicit confirmation unless non-interactive intent exists."""
    if yes:
        return
    invocation = invocation_context(ctx)
    if Confirm.ask("Continue?", default=False, console=invocation.console):
        return
    invocation.console.print("Cancelled.")
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def persistence_error_exit_code(error: PersistenceError) -> ExitCode:
    """Map every persistence failure away from successful exit."""
    try:
        code = operation_exit_code(error.code)
    except ValueError:
        code = persistence_doctor_exit_code(error.code)
    return ExitCode.MANUAL_ACTION if code is ExitCode.SUCCESS else code


def exit_persistence_error(
    ctx: typer.Context,
    error: SchedulerMutationBlockedError | PersistenceError,
) -> NoReturn:
    """Render one typed persistence failure with stable exit policy."""
    invocation = invocation_context(ctx)
    invocation.err_console.print(f"[red]{error}[/red]")
    if isinstance(error, SchedulerMutationBlockedError):
        for observation in error.assessment.observations:
            invocation.err_console.print(
                f"[dim]{observation.backend}: {observation.state} — "
                f"{observation.message}[/dim]"
            )
        raise typer.Exit(code=ExitCode.SCHEDULER_ERROR)
    if (
        isinstance(
            error,
            PersistenceMigrationStateError | PrototypeReimportRequiredError,
        )
        and error.next_command is not None
    ):
        invocation.err_console.print(
            "[dim]Next: " + shlex.join(error.next_command) + "[/dim]"
        )
    raise typer.Exit(code=persistence_error_exit_code(error))


def migrate_accounts_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
    reimport_prototype: Annotated[
        bool,
        typer.Option(
            "--reimport-prototype",
            help="Replace current state from a changed prototype.",
        ),
    ] = False,
) -> None:
    """Explicitly migrate account storage to the current schema."""
    invocation = invocation_context(ctx)
    service = invocation.require_persistence(ctx).persistence
    try:
        assessment = service.mutation_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    render_preview(
        ctx,
        assessment,
        title="Account migration",
        detail=(
            "Changed prototype replacement is explicitly enabled."
            if reimport_prototype
            else None
        ),
    )
    confirm_operation(ctx, yes)
    try:
        result = service.migrate_accounts(
            reimport_prototype=reimport_prototype
        )
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    invocation.console.print(
        f"[green]Migration complete.[/green] {result.message}"
    )


def prepare_rollback_cmd(
    ctx: typer.Context,
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="Released compatibility target (v0.6.0).",
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
) -> None:
    """Prepare exact compatibility with released version 0.6.0."""
    invocation = invocation_context(ctx)
    if target != "v0.6.0":
        invocation.err_console.print(
            "[red]Unsupported rollback target. Expected 'v0.6.0'.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    service = invocation.require_persistence(ctx).persistence
    try:
        assessment = service.mutation_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    render_preview(ctx, assessment, title="Prepare rollback to v0.6.0")
    confirm_operation(ctx, yes)
    try:
        result = service.prepare_rollback()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    _render_operation_success(ctx, "Rollback prepared", result)


def _render_operation_success(
    ctx: typer.Context,
    action: str,
    result: PersistenceOperationResult,
) -> None:
    invocation = invocation_context(ctx)
    invocation.console.print(f"[green]{action}.[/green] {result.message}")
    if result.artifact_basename is not None:
        invocation.console.print(
            f"[dim]Snapshot: {result.artifact_basename}[/dim]"
        )


def register(application: typer.Typer) -> None:
    """Create and register the migration command group."""
    migrate_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Migrate account storage or prepare release rollback.",
        rich_markup_mode="rich",
    )
    branded_command(migrate_app, "accounts")(migrate_accounts_cmd)
    branded_command(migrate_app, "prepare-rollback")(prepare_rollback_cmd)
    application.add_typer(migrate_app, name="migrate")


__all__ = [
    "confirm_operation",
    "exit_persistence_error",
    "persistence_error_exit_code",
    "register",
    "render_preview",
]
