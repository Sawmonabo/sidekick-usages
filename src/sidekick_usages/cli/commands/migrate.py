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
from sidekick_usages.persistence.migrations.account_preview import (
    AccountMigrationPreview,
)
from sidekick_usages.persistence.migrations.credential_kinds import (
    CredentialMigrationPreflightError,
)
from sidekick_usages.persistence.migrations.errors import (
    PersistenceMigrationStateError,
    PrototypeReimportRequiredError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations.location import (
    ConflictSelection,
    LocationMigrationAssessment,
    RuntimePersistenceSelection,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationAssessment,
    PrivateAuthMigrationFailure,
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


def render_account_migration_preview(
    ctx: typer.Context,
    preview: AccountMigrationPreview,
    *,
    reimport_prototype: bool,
) -> None:
    """Render one secret-safe credential classification before mutation."""
    details: list[str] = []
    classification = preview.classification
    if classification is not None:
        details.extend(
            (
                f"Claude setup-token records: {classification.setup_count}",
                "Claude subscription-login records: "
                f"{classification.login_count}",
                "Refresh expiry unavailable: "
                f"{classification.refresh_expiry_unavailable_count}",
            )
        )
    if reimport_prototype:
        details.append("Changed prototype replacement is explicitly enabled.")
    render_preview(
        ctx,
        preview.assessment,
        title="Account migration",
        detail="\n".join(details) if details else None,
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
    if isinstance(error, CredentialMigrationPreflightError):
        for command in error.next_commands:
            invocation.err_console.print(
                "[dim]Next: " + shlex.join(command) + "[/dim]"
            )
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
        preview = service.account_migration_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    render_account_migration_preview(
        ctx,
        preview,
        reimport_prototype=reimport_prototype,
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


def render_location_preview(
    ctx: typer.Context,
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
    *,
    replace_conflicting_destination: bool,
) -> None:
    """Render one bounded credential-free location migration preview."""
    private = assessment.private_auth_summary
    if isinstance(private, PrivateAuthMigrationAssessment):
        private_detail = f"Private bundles to copy: {private.copies_required}"
    elif isinstance(private, PrivateAuthMigrationFailure):
        private_detail = f"Private auth: {private.code.value}"
    else:
        raise TypeError("Unknown private-auth migration summary.")
    lines = [
        f"State: {assessment.selection.code.value}",
        f"Source: {assessment.source}",
        f"Destination: {assessment.destination}",
        private_detail,
    ]
    if replace_conflicting_destination and isinstance(
        assessment.selection,
        ConflictSelection,
    ):
        lines.append(
            "The canonical destination will be replaced from compatibility."
        )
    for candidate in assessment.candidates:
        lines.append(
            f"{candidate.role.value}: {candidate.assessment.code.value}"
        )
    if assessment.artifact_basename is not None:
        lines.append(f"Artifact: {assessment.artifact_basename}")
    invocation_context(ctx).console.print(
        Panel(
            Text("\n".join(lines)),
            border_style="yellow",
            title="[yellow]Application-data migration[/yellow]",
            title_align="left",
        )
    )


def migrate_locations_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
    replace_conflicting_destination: Annotated[
        bool,
        typer.Option(
            "--replace-conflicting-destination",
            help=("Replace conflicting canonical state from compatibility."),
        ),
    ] = False,
) -> None:
    """Explicitly relocate compatibility data to native application data."""
    invocation = invocation_context(ctx)
    service = invocation.require_persistence(ctx).persistence
    try:
        assessment = service.location_migration_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    render_location_preview(
        ctx,
        assessment,
        replace_conflicting_destination=replace_conflicting_destination,
    )
    confirm_operation(ctx, yes)
    try:
        result = service.migrate_locations(
            replace_conflicting_destination=replace_conflicting_destination,
        )
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    invocation.console.print(
        "[green]Application-data migration complete.[/green] "
        f"State: {result.assessment.selection.code.value}."
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
        help=(
            "Migrate account or application-data storage and prepare rollback."
        ),
        rich_markup_mode="rich",
    )
    branded_command(migrate_app, "accounts")(migrate_accounts_cmd)
    branded_command(migrate_app, "locations")(migrate_locations_cmd)
    branded_command(migrate_app, "prepare-rollback")(prepare_rollback_cmd)
    application.add_typer(migrate_app, name="migrate")


__all__ = [
    "confirm_operation",
    "exit_persistence_error",
    "persistence_error_exit_code",
    "register",
    "render_account_migration_preview",
    "render_location_preview",
    "render_preview",
]
