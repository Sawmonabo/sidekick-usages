"""Explicit Sidekick-owned permission-repair command group."""

from typing import Annotated

import typer
from rich.panel import Panel
from rich.prompt import Confirm

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.cli.persistence import exit_persistence_failure
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.scheduler_quiescence import (
    SchedulerMutationBlockedError,
)


def repair_permissions_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
) -> None:
    """Repair Sidekick-owned account and credential permissions."""
    invocation = invocation_context(ctx)
    service = invocation.require_persistence(ctx).persistence
    invocation.console.print(
        Panel(
            "This will repair permissions only within Sidekick's current "
            "account and credential roots at "
            f"{service.paths.accounts.parent}.",
            border_style="yellow",
            title="[yellow]Permission repair[/yellow]",
            title_align="left",
        )
    )
    if not yes and not Confirm.ask(
        "Continue?",
        default=False,
        console=invocation.console,
    ):
        invocation.console.print("Cancelled.")
        raise typer.Exit()
    try:
        result = service.repair_permissions()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_failure(ctx, error)
    invocation.console.print(
        "[green]Permissions repaired and verified.[/green]"
    )
    invocation.console.print(
        "[dim]Application parent changed: "
        f"{'yes' if result.repair.account_parent_repaired else 'no'}; "
        "private directories changed: "
        f"{result.repair.directories_repaired}; private files changed: "
        f"{result.repair.files_repaired}; validated accounts: "
        f"{result.status.account_count}.[/dim]"
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
