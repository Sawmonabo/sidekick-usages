"""Claude setup-token command adapter."""

from typing import Annotated, assert_never

import typer

from sidekick_usages.cli.commands.accounts import validated_label
from sidekick_usages.cli.commands.credentials import exit_credential_failure
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.credentials import TokenCredentialSource
from sidekick_usages.providers.base import ProviderFailure
from sidekick_usages.providers.claude import (
    SetupTokenCapture,
    SetupTokenMissing,
    SetupTokenRejected,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)

_CLAUDE_ALIAS_HELP = (
    "Use `sidekick-usages claude setup-token`; this alias is removed in 0.9.0."
)


def _provider_id(ctx: typer.Context, value: str) -> ProviderId:
    try:
        provider_id = ProviderId(value)
    except ValueError:
        known = ", ".join(provider.value for provider in ProviderId)
        invocation_context(ctx).err_console.print(
            f"[red]Unknown provider {value!r}. Known: {known}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    if provider_id is not ProviderId.CLAUDE:
        invocation_context(ctx).err_console.print(
            "[red]Codex CLI doesn't expose a long-lived token generator. "
            "Run `codex login` then `sidekick-usages add codex`.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    return provider_id


def _capture_token(
    ctx: typer.Context,
    result: SetupTokenCapture,
) -> str | None:
    invocation = invocation_context(ctx)
    if isinstance(result, SetupTokenSuccess):
        return result.token
    if isinstance(result, SetupTokenTimedOut):
        invocation.err_console.print(
            "[red]`claude setup-token` timed out.[/red]"
        )
        return None
    if isinstance(result, SetupTokenMissing):
        invocation.err_console.print(
            "[red]Claude setup completed without returning a token.[/red]"
        )
        return None
    if isinstance(result, SetupTokenRejected):
        invocation.err_console.print(
            "[red]`claude setup-token` did not complete successfully "
            f"(exit {result.return_code}).[/red]"
        )
        return None
    if isinstance(result, SetupTokenUnreadable):
        invocation.err_console.print(
            "[red]`claude setup-token` could not be completed safely.[/red]"
        )
        return None
    assert_never(result)


def _run_setup_token(
    ctx: typer.Context,
    label: str | None,
    plan: str | None,
    force: bool,
) -> None:
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    invocation.err_console.print(
        "[dim]Running `claude setup-token` — complete the browser OAuth "
        "flow when it opens...[/dim]"
    )
    token = _capture_token(
        ctx,
        app_context.claude_setup_token.capture_setup_token(),
    )
    if token is None:
        invocation.err_console.print(
            "[red]Did not capture a token. Try again or run "
            "`sidekick-usages add claude` with --token.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    result = app_context.credentials.save(
        TokenCredentialSource(provider_id=ProviderId.CLAUDE, token=token),
        label=validated_label(ctx, label) if label is not None else None,
        plan=plan,
        force=force,
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    action = "Saved" if result.created else "Updated"
    invocation.console.print(f"[green]{action} '{result.label}'.[/green]")
    if result.warning is not None:
        invocation.console.print(f"[yellow]Note: {result.warning}[/yellow]")


def setup_token_cmd(
    ctx: typer.Context,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Override the auto-generated label.",
        ),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option("--plan", help="Override the plan tag."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing label.",
        ),
    ] = False,
) -> None:
    """Run Claude Code's long-lived token generator and save its token."""
    _run_setup_token(ctx, label, plan, force)


def setup_token_alias_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str,
        typer.Argument(help="Provider id (currently: claude)."),
    ],
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Override the auto-generated label.",
        ),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option("--plan", help="Override the plan tag."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing label.",
        ),
    ] = False,
) -> None:
    """Run the deprecated provider-positioned setup-token spelling."""
    _provider_id(ctx, provider)
    _run_setup_token(ctx, label, plan, force)


def register(application: typer.Typer) -> None:
    """Register the Claude group and its deprecated top-level alias."""
    claude_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Generate and save long-lived Claude Code credentials.",
        rich_markup_mode="rich",
    )
    branded_command(claude_app, "setup-token")(setup_token_cmd)
    application.add_typer(claude_app, name="claude")
    branded_command(
        application,
        "setup-token",
        help=_CLAUDE_ALIAS_HELP,
        deprecated=True,
    )(setup_token_alias_cmd)


__all__ = ["register"]
