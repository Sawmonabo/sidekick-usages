"""Saved-account listing, CRUD, plan, and reset command adapters."""

from dataclasses import replace
from typing import Annotated

import typer
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from sidekick_usages.branding.rich import brand_header
from sidekick_usages.branding.theme import PROVIDER_COLORS
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.persistence import exit_persistence_failure
from sidekick_usages.cli.validation import (
    validated_label,
    validated_provider,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import (
    ExitCode,
    HeartbeatStatus,
    ProviderId,
    highest_exit_code,
)
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountProfileFailure,
    AccountRemovalFailure,
    AccountRemovalPartialFailure,
    AccountRemovalResult,
    AccountRemovalSuccess,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError


def _saved_account(
    ctx: typer.Context,
    store: AccountStore,
    label: str,
) -> SavedAccount:
    """Resolve one unambiguous label to its stable saved account."""
    invocation = invocation_context(ctx)
    account_label = validated_label(ctx, label)
    matches = tuple(
        account
        for account in store.saved_accounts()
        if account.label == account_label
    )
    if not matches:
        invocation.err_console.print(
            Text(f"No account labeled '{account_label}'.", style="red")
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if len(matches) > 1:
        providers = ", ".join(
            sorted(str(account.provider_id) for account in matches)
        )
        invocation.err_console.print(
            Text(
                f"Account label '{account_label}' matches multiple "
                f"providers ({providers}); this command will not guess.",
                style="red",
            )
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    try:
        current = store.read_saved(matches[0].account_id)
    except PersistenceError as error:
        exit_persistence_failure(ctx, error)
    if current is None or current.label != account_label:
        invocation.err_console.print(
            Text(
                f"Account '{account_label}' changed; run the command again.",
                style="red",
            )
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    return current


def _heartbeat_label(account: SavedAccount) -> str:
    """Return compact heartbeat state from secret-free saved metadata."""
    if account.last_heartbeat_status is HeartbeatStatus.FAILED:
        return "failed"
    return "on" if account.heartbeat_enabled else "off"


def _render_removal_issue(
    ctx: typer.Context,
    result: (
        AccountRemovalFailure
        | AccountRemovalPartialFailure
        | AccountProfileFailure
    ),
    account: SavedAccount | None,
) -> None:
    """Render one incomplete removal without hiding partial deletion."""
    invocation = invocation_context(ctx)
    if isinstance(result, AccountProfileFailure):
        identity = f"{result.provider_id} profile '{result.profile_basename}'"
    elif account is not None:
        identity = f"'{account.label}' ({account.provider_id})"
    else:
        identity = f"account {result.account_id}"
    if isinstance(result, AccountRemovalPartialFailure):
        invocation.err_console.print(
            Text(
                f"Removed {identity}, but cleanup is incomplete: "
                f"{result.message}",
                style="yellow",
            )
        )
        return
    invocation.err_console.print(
        Text(
            f"Could not remove {identity}: {result.message}",
            style="red",
        )
    )


def _render_removal_results(
    ctx: typer.Context,
    results: tuple[AccountRemovalResult, ...],
    targets: tuple[SavedAccount, ...],
) -> tuple[int, ExitCode]:
    """Render every incomplete result and return clean-removal totals."""
    target_map = {account.account_id: account for account in targets}
    removed = 0
    exit_code = ExitCode.SUCCESS
    for result in results:
        if isinstance(result, AccountRemovalSuccess):
            removed += 1
            continue
        _render_removal_issue(
            ctx,
            result,
            (
                None
                if isinstance(result, AccountProfileFailure)
                else target_map.get(result.account_id)
            ),
        )
        result_exit = (
            ExitCode.MANUAL_ACTION
            if result.action_required
            else ExitCode.SYSTEM_ERROR
        )
        exit_code = highest_exit_code(exit_code, result_exit)
    result_ids = {
        result.account_id
        for result in results
        if not isinstance(result, AccountProfileFailure)
    }
    if result_ids != set(target_map):
        invocation_context(ctx).err_console.print(
            Text(
                "Account removal did not return one result per target.",
                style="red",
            )
        )
        exit_code = highest_exit_code(exit_code, ExitCode.SYSTEM_ERROR)
    return removed, exit_code


def _reconcile_removals(
    ctx: typer.Context,
    *,
    ignored_account_id: SidekickAccountId | None = None,
) -> None:
    """Retry incomplete cleanup before another account mutation."""
    invocation = invocation_context(ctx)
    results = invocation.require_app(ctx).lifecycle.reconcile(
        excluding_account_id=ignored_account_id,
    )
    exit_code = ExitCode.SUCCESS
    for result in results:
        if isinstance(result, AccountRemovalSuccess):
            continue
        if (
            not isinstance(result, AccountProfileFailure)
            and result.account_id == ignored_account_id
        ):
            continue
        _render_removal_issue(ctx, result, None)
        result_exit = (
            ExitCode.MANUAL_ACTION
            if result.action_required
            else ExitCode.SYSTEM_ERROR
        )
        exit_code = highest_exit_code(exit_code, result_exit)
    if exit_code is not ExitCode.SUCCESS:
        raise typer.Exit(code=exit_code)


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
    accounts = app_context.accounts.saved_accounts()
    if not accounts:
        invocation.console.print("[dim](no accounts saved)[/dim]")
        return
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
    for account in accounts:
        provider_color = PROVIDER_COLORS.get(account.provider_id, "dim")
        plan = (
            Text(account.plan, style="dim")
            if account.plan == "unknown"
            else Text(account.plan)
        )
        table.add_row(
            Text(account.label),
            Text(account.provider_id, style=provider_color),
            plan,
            _heartbeat_label(account),
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
    app_context = invocation.require_app(ctx)
    account = _saved_account(ctx, app_context.accounts, label)
    _reconcile_removals(
        ctx,
        ignored_account_id=account.account_id,
    )
    result = app_context.lifecycle.remove(account.account_id)
    if isinstance(result, AccountRemovalSuccess):
        identity = (
            f"account {result.account_id}"
            if result.label is None
            else f"'{result.label}' ({result.provider_id})"
        )
        invocation.console.print(
            Text(
                f"Removed {identity}.",
                style="green",
            )
        )
        return
    _render_removal_issue(
        ctx,
        result,
        account,
    )
    raise typer.Exit(
        code=(
            ExitCode.MANUAL_ACTION
            if result.action_required
            else ExitCode.SYSTEM_ERROR
        )
    )


def rename_cmd(
    ctx: typer.Context,
    old: Annotated[str, typer.Argument(help="Existing label.")],
    new: Annotated[str, typer.Argument(help="New label.")],
) -> None:
    """Rename a saved account."""
    invocation = invocation_context(ctx)
    store = invocation.require_app(ctx).accounts
    _reconcile_removals(ctx)
    account = _saved_account(ctx, store, old)
    new_label = validated_label(ctx, new)
    try:
        renamed = store.rename_saved(
            account.account_id,
            new_label,
            expected=account,
        )
    except PersistenceError as error:
        exit_persistence_failure(ctx, error)
    invocation.console.print(
        Text(
            f"Renamed '{account.label}' → '{renamed.label}'.",
            style="green",
        )
    )


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
    _reconcile_removals(ctx)
    account = _saved_account(ctx, store, label)
    try:
        store.persist_state(
            replace(account, plan=value),
            expected=account,
        )
    except PersistenceError as error:
        exit_persistence_failure(ctx, error)
    invocation.console.print(Text(f"Set '{account.label}' plan to '{value}'."))


def _reset_accounts(
    ctx: typer.Context,
    provider_id: ProviderId | None,
    targets: tuple[SavedAccount, ...],
) -> None:
    """Retire one reset scope, then remove remaining global artifacts."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    results = (
        app_context.lifecycle.reset_all()
        if provider_id is None
        else app_context.lifecycle.reset_provider(provider_id)
    )
    removed, exit_code = _render_removal_results(ctx, results, targets)
    if exit_code is not ExitCode.SUCCESS:
        scope = (
            "account(s)"
            if provider_id is None
            else f"{provider_id} account(s)"
        )
        cleanup = (
            "; global credential cleanup was not run"
            if provider_id is None
            else ""
        )
        invocation.err_console.print(
            Text(
                f"Fully removed {removed} of {len(targets)} {scope}{cleanup}.",
                style="yellow",
            )
        )
        raise typer.Exit(code=exit_code)
    if provider_id is None:
        try:
            invocation.require_persistence(ctx).persistence.reset_all()
        except PersistenceError as error:
            exit_persistence_failure(ctx, error)
    scope = (
        "account(s)" if provider_id is None else f"{provider_id} account(s)"
    )
    suffix = (
        " and all managed credential artifacts" if provider_id is None else ""
    )
    invocation.console.print(
        Text(
            f"Cleared {removed} {scope}{suffix}.",
            style="green",
        )
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
    app_context = invocation.require_app(ctx)
    provider_id: ProviderId | None = None
    if provider is not None:
        provider_id = validated_provider(ctx, provider)
        targets = app_context.accounts.saved_accounts(provider_id)
        count = len(targets)
        scope = f"{count} {provider} account(s)"
    else:
        targets = app_context.accounts.saved_accounts()
        try:
            status = invocation.require_persistence(ctx).persistence.status()
        except PersistenceError as error:
            exit_persistence_failure(ctx, error)
        count = len(targets)
        scope = (
            f"{count} account(s) and all managed credential "
            f"artifacts at {status.path}"
        )
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
        _reset_accounts(ctx, provider_id, targets)
        return
    _reset_accounts(ctx, None, targets)


def register(application: typer.Typer) -> None:
    """Register each account command exactly once."""
    branded_command(application, "list")(list_cmd)
    branded_command(application, "remove")(remove_cmd)
    branded_command(application, "rename")(rename_cmd)
    branded_command(application, "set-plan")(set_plan_cmd)
    branded_command(application, "reset")(reset_cmd)
