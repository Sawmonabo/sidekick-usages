"""Default invocation and typed usage-check command adapter."""

import typer
from rich.panel import Panel
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import ExitCode, ProviderId, highest_exit_code
from sidekick_usages.usage import activity_has_failure
from sidekick_usages.usage.render import usage_overview


def run(ctx: typer.Context) -> None:
    """Run and render the default typed usage workflow."""
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    result = app_context.usage.check(invocation.only)
    if not result.usages and not result.failures:
        print_no_accounts(ctx, invocation.only, branded=True)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    exit_code = ExitCode.MANUAL_ACTION if result.failures else ExitCode.SUCCESS
    if any(activity_has_failure(item) for item in result.activities):
        exit_code = highest_exit_code(exit_code, ExitCode.SYSTEM_ERROR)
    invocation.console.print(
        usage_overview(
            result,
            width=invocation.console.size.width,
        )
    )
    if exit_code:
        raise typer.Exit(code=exit_code)


def check_cmd(ctx: typer.Context) -> None:
    """Print usage for every saved account."""
    run(ctx)


def print_no_accounts(
    ctx: typer.Context,
    only: ProviderId | None,
    *,
    branded: bool = False,
) -> None:
    """Render the shared no-saved-accounts guidance."""
    invocation = invocation_context(ctx)
    if branded:
        invocation.err_console.print(
            brand_header(invocation.err_console.size.width)
        )
        invocation.err_console.print()
    scope = f" for {only}" if only is not None else ""
    invocation.err_console.print(
        Panel(
            Text.from_markup(
                f"No accounts saved{scope}.\n\n"
                "Run [bold]sidekick-usages add <provider>[/bold] "
                "after logging into the CLI."
            ),
            border_style="yellow",
            title="[yellow]Nothing to show[/yellow]",
            title_align="left",
        )
    )


def register(application: typer.Typer) -> None:
    """Register the usage command exactly once."""
    branded_command(application, "check")(check_cmd)


__all__ = ["register", "run"]
