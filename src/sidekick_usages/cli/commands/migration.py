"""Interactive managed-auth migration command."""

import sys
from typing import Annotated

import typer
from rich.text import Text

from sidekick_usages.cli.commands.credentials import render_codex_login_event
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode
from sidekick_usages.credentials.migration.models.managed_auth import (
    ManagedAuthPlan,
    ManagedAuthTarget,
)
from sidekick_usages.credentials.migration.types.managed_auth import (
    ManagedAuthOutcome,
)
from sidekick_usages.credentials.migration.types.service import (
    ManagedAuthServiceState,
)


def _render_plan(ctx: typer.Context, plan: ManagedAuthPlan) -> None:
    invocation = invocation_context(ctx)
    invocation.console.print("[bold]Managed authentication migration[/bold]")
    if not plan.targets:
        invocation.console.print("[dim]No saved accounts to migrate.[/dim]")
        return
    for target in plan.targets:
        line = Text("  ")
        line.append(target.provider_id.value, style="cyan")
        line.append(f" · {target.label} · {target.action.value}")
        invocation.console.print(line)
    invocation.console.print(
        "[dim]Uses official provider login only; no tokens are accepted "
        "by this command.[/dim]"
    )


def _approve_association(
    target: ManagedAuthTarget,
    *,
    assume_yes: bool,
) -> bool:
    if assume_yes:
        return True
    return typer.confirm(
        "Associate the next verified Claude subscription identity with "
        f"'{target.label}' while preserving its setup token?",
        default=False,
    )


def managed_auth_cmd(
    ctx: typer.Context,
    assume_yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Continue after the secret-safe preview.",
        ),
    ] = False,
    device_auth: Annotated[
        bool,
        typer.Option(
            "--device-auth",
            help="Use official Codex device authentication when login is due.",
        ),
    ] = False,
) -> None:
    """Migrate every saved account to verified managed authentication."""
    invocation = invocation_context(ctx)
    migration = invocation.require_migration(ctx).managed_auth
    plan = migration.plan()
    _render_plan(ctx, plan)
    if not plan.targets:
        return
    if not assume_yes and not typer.confirm(
        "Continue with managed authentication migration?",
        default=False,
    ):
        invocation.console.print("[yellow]Migration canceled.[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    report = migration.migrate(
        interactive=sys.stdin.isatty() and sys.stdout.isatty(),
        device_auth=device_auth,
        approve_claude_association=lambda target: _approve_association(
            target,
            assume_yes=assume_yes,
        ),
        codex_events=lambda event: render_codex_login_event(
            invocation.console,
            event,
        ),
    )
    if report.service.state is not ManagedAuthServiceState.READY:
        failure = Text("Service readiness failed: ", style="red")
        failure.append(report.service.message)
        invocation.err_console.print(failure)
        raise typer.Exit(code=report.service.exit_code)
    for result in report.accounts:
        style = (
            "green" if result.outcome is ManagedAuthOutcome.READY else "yellow"
        )
        line = Text(style=style)
        line.append(
            f"{result.provider_id.value} · {result.label} · "
            f"{result.outcome.value}: {result.message}"
        )
        invocation.console.print(line)
    if not report.complete:
        if all(
            result.outcome is ManagedAuthOutcome.READY
            for result in report.accounts
        ):
            message = (
                "The saved account set changed or final authority proof "
                "was incomplete. Rerun this command to resume."
            )
        else:
            message = (
                "Some accounts still require the actions shown above. "
                "Rerun this command to resume."
            )
        invocation.err_console.print(Text(message, style="yellow"))
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    invocation.console.print(
        "[green]All saved accounts have verified managed authorities.[/green]"
    )


def register(application: typer.Typer) -> None:
    """Register managed-auth migration without token arguments."""
    migration_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Migrate saved accounts to provider-managed authentication.",
        rich_markup_mode="rich",
    )
    branded_command(migration_app, "managed-auth")(managed_auth_cmd)
    application.add_typer(migration_app, name="migrate")
