"""Saved-account listing, CRUD, plan, and reset command adapters."""

from typing import Annotated

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding import PROVIDER_COLORS, brand_header
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.persistence import exit_persistence_failure
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.scheduler_quiescence import (
    SchedulerMutationBlockedError,
)

_MIN_TOKEN_LENGTH_FOR_MASKING = 30


def validated_label(ctx: typer.Context, value: str) -> AccountLabel:
    """Validate one label and translate failure to CLI output."""
    try:
        return AccountLabel(value)
    except ValueError as error:
        invocation_context(ctx).err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error


def _masked_token(token: str) -> str:
    if len(token) <= _MIN_TOKEN_LENGTH_FOR_MASKING:
        return "(missing)"
    return token[:18] + "…" + token[-6:]


def list_cmd(ctx: typer.Context) -> None:
    """List every saved account."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    invocation.console.print(
        brand_header(
            invocation.console.size.width,
            section="saved accounts",
        )
    )
    accounts = list(app_context.accounts)
    if not accounts:
        invocation.console.print("[dim](no accounts saved)[/dim]")
        return
    support_labels = app_context.heartbeat.support_labels(accounts)
    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        pad_edge=False,
    )
    table.add_column("Label", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Plan", no_wrap=True)
    table.add_column("Heartbeat", no_wrap=True)
    table.add_column("Token", no_wrap=True, style="dim")
    for account in accounts:
        provider_color = PROVIDER_COLORS.get(account.provider_id, "dim")
        plan = (
            Text(account.plan, style="dim")
            if account.plan == "unknown"
            else Text(account.plan)
        )
        table.add_row(
            account.label,
            Text(account.provider_id, style=provider_color),
            plan,
            support_labels[account.label],
            _masked_token(account.access_token),
        )
    invocation.console.print(table)
    invocation.console.print(
        f"\n[dim]Config: {app_context.accounts.path}[/dim]"
    )


def remove_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to delete.")],
) -> None:
    """Delete a saved account."""
    invocation = invocation_context(ctx)
    if not invocation.require_app(ctx).accounts.remove(label):
        invocation.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    invocation.console.print(f"[green]Removed '{label}'.[/green]")


def rename_cmd(
    ctx: typer.Context,
    old: Annotated[str, typer.Argument(help="Existing label.")],
    new: Annotated[str, typer.Argument(help="New label.")],
) -> None:
    """Rename a saved account."""
    invocation = invocation_context(ctx)
    if not invocation.require_app(ctx).accounts.rename(old, new):
        invocation.err_console.print(
            f"[yellow]Cannot rename: '{old}' is missing or "
            f"'{new}' already exists.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    invocation.console.print(f"[green]Renamed '{old}' → '{new}'.[/green]")


def set_plan_cmd(ctx: typer.Context, label: str, plan: str) -> None:
    """Manually set an account's plan tag.

    For credentials the usage API cannot introspect (e.g. inference-
    only Claude tokens), this is the supported way to correct the
    plan chip.
    """
    invocation = invocation_context(ctx)
    value = plan.strip().lower()
    if not value:
        invocation.err_console.print("[red]Plan must not be empty.[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    store = invocation.require_app(ctx).accounts
    account = store.get(label)
    if account is None:
        invocation.err_console.print(
            f"[red]No account labeled '{label}'.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    account.plan = value
    store.persist(account)
    invocation.console.print(
        f"Set [bold]{label}[/bold] plan to [bold]{value}[/bold]."
    )


def _reset_provider(
    ctx: typer.Context,
    provider_id: ProviderId,
) -> None:
    invocation = invocation_context(ctx)
    try:
        cleared = invocation.require_app(ctx).accounts.reset_provider(
            provider_id
        )
    except PersistenceError as error:
        exit_persistence_failure(ctx, error)
    invocation.console.print(
        f"[green]Cleared {cleared} {provider_id} account(s).[/green]"
    )


def reset_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option("-y", "--yes", help="Skip confirmation prompt."),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Only reset one provider's accounts.",
        ),
    ] = None,
) -> None:
    """Delete saved accounts (all, or one provider).

    Prompts for confirmation unless ``--yes`` is passed.
    """
    invocation = invocation_context(ctx)
    provider_id: ProviderId | None = None
    validated_count: int | None = None
    if provider is not None:
        try:
            provider_id = ProviderId(provider)
        except ValueError:
            invocation.err_console.print(
                f"[red]Unknown provider {provider!r}.[/red]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
        targets = invocation.require_app(ctx).accounts.filter_by_provider(
            provider_id
        )
        count = len(targets)
        scope = f"{count} {provider} account(s)"
    else:
        persistence = invocation.require_persistence(ctx).persistence
        try:
            status = persistence.status()
        except (SchedulerMutationBlockedError, PersistenceError) as error:
            exit_persistence_failure(ctx, error)
        validated_count = status.account_count
        count = status.account_count
        scope = (
            f"{count} validated account(s) and all managed credential "
            f"artifacts at {status.path}"
        )
    if count == 0 and provider_id is not None:
        invocation.console.print("[dim]Nothing to reset.[/dim]")
        return
    if not yes:
        invocation.console.print(
            Panel(
                f"This will delete {scope}.",
                border_style="yellow",
                title="[yellow]Confirm reset[/yellow]",
                title_align="left",
            )
        )
        if not Confirm.ask(
            "Continue?",
            default=False,
            console=invocation.console,
        ):
            invocation.console.print("Cancelled.")
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if provider_id is not None:
        _reset_provider(ctx, provider_id)
        return
    try:
        invocation.require_persistence(ctx).persistence.reset_all()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        exit_persistence_failure(ctx, error)
    if validated_count is None:
        invocation.console.print(
            "[green]Cleared all managed account and credential "
            "artifacts.[/green]"
        )
    else:
        invocation.console.print(
            f"[green]Cleared {count} account(s) and removed "
            "config file.[/green]"
        )


def register(application: typer.Typer) -> None:
    """Register each account command exactly once."""
    branded_command(application, "list")(list_cmd)
    branded_command(application, "remove")(remove_cmd)
    branded_command(application, "rename")(rename_cmd)
    branded_command(application, "set-plan")(set_plan_cmd)
    branded_command(application, "reset")(reset_cmd)


__all__ = ["register", "validated_label"]
