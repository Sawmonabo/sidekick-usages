"""Explicit Sidekick-owned permission-repair command group."""

from typing import Annotated

import typer

from sidekick_usages.cli.commands.migrate import (
    confirm_operation,
    exit_persistence_error,
    render_preview,
)
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.migration_errors import (
    SchedulerMutationBlockedError,
)


def repair_permissions_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
) -> None:
    """Repair a validated released-layout permission boundary."""
    invocation = invocation_context(ctx)
    service = invocation.require_persistence(ctx).persistence
    try:
        preview = service.permission_repair_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    render_preview(ctx, preview, title="Permission repair")
    confirm_operation(ctx, yes)
    try:
        result = service.repair_permissions()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_error(ctx, error)
    invocation.console.print(
        "[green]Permissions repaired.[/green] " + result.assessment.message
    )
    invocation.console.print(
        "[dim]Application parent changed: "
        f"{'yes' if result.repair.account_parent_repaired else 'no'}; "
        "private directories changed: "
        f"{result.repair.directories_repaired}; private files changed: "
        f"{result.repair.files_repaired}.[/dim]"
    )


def register(application: typer.Typer) -> None:
    """Create and register the permissions command group."""
    permissions_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Inspect or repair Sidekick-owned permissions.",
        rich_markup_mode="rich",
    )
    branded_command(permissions_app, "repair")(repair_permissions_cmd)
    application.add_typer(permissions_app, name="permissions")


__all__ = ["register"]
