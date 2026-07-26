"""Default invocation and typed usage-check command adapter."""

import typer
from rich.panel import Panel
from rich.text import Text

from sidekick_usages.branding import brand_header
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.dashboard import launch
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import ExitCode, ProviderId, highest_exit_code
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
)
from sidekick_usages.usage.models import activity_has_failure
from sidekick_usages.usage.presentation.dashboard.overview import (
    dashboard_overview,
)
from sidekick_usages.usage.presentation.overview import usage_overview


def run_default(ctx: typer.Context, *, interactive: bool) -> None:
    """Choose cached interactive launch or the stable one-shot workflow."""
    if not interactive or not launch.interactive_dashboard_supported():
        run(ctx)
        return
    invocation = invocation_context(ctx)
    runtime = invocation.require_dashboard()
    snapshot = runtime.snapshots.load(invocation.only)
    controller = DashboardController.start(snapshot)
    state = controller.state
    frame = launch.render_dashboard_frame(
        invocation.console,
        dashboard_overview(
            snapshot,
            width=invocation.console.size.width,
            cursor=DashboardCursor(
                focused_provider=state.focused_provider,
                account_id=state.account_id,
                external=state.external,
            ),
            footer=DashboardFooter(),
        ),
    )
    line_count = launch.present_dashboard_frame(invocation.console, frame)
    try:
        runtime.process.replace(invocation.only)
    except (launch.InteractiveDashboardLaunchError, OSError) as error:
        launch.restore_after_failed_replace(
            invocation.console,
            line_count,
        )
        if isinstance(error, launch.InteractiveDashboardLaunchError):
            raise
        raise launch.InteractiveDashboardLaunchError(
            launch.DASHBOARD_LAUNCH_FAILURE_MESSAGE
        ) from error


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
