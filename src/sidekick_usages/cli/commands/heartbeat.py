"""Heartbeat command group, fallback parsing, and presentation."""

import json
from typing import Annotated, NoReturn

import click
import typer

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.heartbeat import (
    HeartbeatOutcome,
    heartbeat_exit_code,
)
from sidekick_usages.heartbeat.render import (
    HeartbeatOutputChannel,
    build_heartbeat_status_rows,
    heartbeat_status_json,
    render_heartbeat_outcomes,
    render_heartbeat_status,
)


class _HeartbeatGroup(BrandedTyperGroup):
    """Treat an unknown heartbeat subcommand as an account label."""

    label_command_name = "run-label"

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if args and not args[0].startswith("-"):
                command = self.get_command(ctx, self.label_command_name)
                if command is not None:
                    return self.label_command_name, command, args
            raise


def _usage_error(ctx: typer.Context, message: str) -> NoReturn:
    """Render a heartbeat-command usage failure and stop."""
    invocation_context(ctx).err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=ExitCode.SYSTEM_ERROR)


def render_outcomes(
    ctx: typer.Context,
    outcomes: list[HeartbeatOutcome],
    *,
    quiet: bool,
) -> None:
    """Render typed heartbeat outcomes through invocation consoles."""
    invocation = invocation_context(ctx)
    for rendered in render_heartbeat_outcomes(outcomes, quiet=quiet):
        console = (
            invocation.err_console
            if rendered.channel is HeartbeatOutputChannel.STDERR
            else invocation.console
        )
        console.print(rendered.renderable)


def heartbeat_cmd(
    ctx: typer.Context,
    all_accounts: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Warm every enabled account with an inactive 5h window.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Only print accounts needing manual action.",
        ),
    ] = False,
    provider_id: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="With --all, filter to one provider.",
        ),
    ] = None,
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Heartbeat target to warm: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Warm inactive usage windows without changing token refresh policy."""
    if ctx.invoked_subcommand is not None:
        return
    args = list(ctx.args)
    label = args[0] if args else None
    if len(args) > 1:
        _usage_error(ctx, "Pass at most one account label.")
    if all_accounts:
        try:
            provider_filter = (
                ProviderId(provider_id) if provider_id is not None else None
            )
        except ValueError:
            _usage_error(ctx, f"Unknown provider {provider_id!r}.")
        outcomes = (
            invocation_context(ctx)
            .require_app(ctx)
            .heartbeat.heartbeat_all(
                provider_id=provider_filter,
                target_id=target_id,
            )
        )
        render_outcomes(ctx, outcomes, quiet=quiet)
        code = heartbeat_exit_code(outcomes)
        if code:
            raise typer.Exit(code=code)
        return
    if provider_id is not None:
        _usage_error(ctx, "--provider only applies with --all.")
    if quiet:
        _usage_error(ctx, "--quiet only applies with --all.")
    if label is None:
        _usage_error(ctx, "Pass an account label or use --all.")
    _run_label(ctx, label, target_id=target_id)


def heartbeat_label_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to warm.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Heartbeat target to warm: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Hidden target for the ``heartbeat <label>`` fallback parser."""
    _run_label(ctx, label, target_id=target_id)


def _run_label(
    ctx: typer.Context,
    label: str,
    *,
    target_id: str | None,
) -> None:
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    account = app_context.accounts.get(label)
    if account is None:
        invocation.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    outcome = app_context.heartbeat.heartbeat_account(
        account,
        require_enabled=False,
        target_id=target_id,
    )
    render_outcomes(ctx, [outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


def heartbeat_enable_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to enable.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Daemon target to enable: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Enable daemon heartbeat for one supported account."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    outcome = app_context.heartbeat.enable(
        app_context.accounts.get(label),
        target_id=target_id,
    )
    render_outcomes(ctx, [outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


def heartbeat_disable_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to disable.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Daemon target to disable: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Disable daemon heartbeat for one account."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    outcome = app_context.heartbeat.disable(
        app_context.accounts.get(label),
        target_id=target_id,
    )
    render_outcomes(ctx, [outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


def heartbeat_status_cmd(
    ctx: typer.Context,
    provider_id: Annotated[
        str | None,
        typer.Option("--provider", help="Filter to one provider."),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Filter to one account label."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Show heartbeat support and latest diagnostics."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    accounts = list(app_context.accounts)
    if provider_id is not None:
        accounts = [
            account
            for account in accounts
            if account.provider_id == provider_id
        ]
    if label is not None:
        accounts = [account for account in accounts if account.label == label]
    if not accounts:
        invocation.err_console.print("[yellow]No matching accounts.[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    rows = build_heartbeat_status_rows(
        accounts,
        app_context.heartbeat.support_labels(accounts),
    )
    if json_output:
        invocation.console.print(
            json.dumps(heartbeat_status_json(rows), indent=2),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    invocation.console.print(
        render_heartbeat_status(
            rows,
            width=invocation.console.size.width,
        )
    )


def register(application: typer.Typer) -> None:
    """Create and register the heartbeat group and its exact commands."""
    heartbeat_app = typer.Typer(
        cls=_HeartbeatGroup,
        help="Warm inactive usage windows for opted-in accounts.",
        rich_markup_mode="rich",
        invoke_without_command=True,
        context_settings={"allow_extra_args": True},
    )
    heartbeat_app.callback()(heartbeat_cmd)
    branded_command(heartbeat_app, "run-label", hidden=True)(
        heartbeat_label_cmd
    )
    branded_command(heartbeat_app, "enable")(heartbeat_enable_cmd)
    branded_command(heartbeat_app, "disable")(heartbeat_disable_cmd)
    branded_command(heartbeat_app, "status")(heartbeat_status_cmd)
    application.add_typer(heartbeat_app, name="heartbeat")


__all__ = ["register", "render_outcomes"]
