"""Credential add and sole refresh command adapters."""

import sys
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.text import Text

from sidekick_usages.cli.commands.maintenance import run_refresh_all
from sidekick_usages.cli.context import AppContext, invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.token_input import TokenInput
from sidekick_usages.cli.validation import (
    validated_label,
    validated_provider,
)
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.credentials.models import (
    LocalCredentialSource,
    TokenCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.models import CodexLoginEvent


def exit_credential_failure(
    ctx: typer.Context,
    failure: ProviderFailure,
    *,
    prefix: str = "",
) -> NoReturn:
    """Render one safe credential failure and stop the command."""
    invocation_context(ctx).err_console.print(
        f"[red]{prefix}{failure.message}[/red]"
    )
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def render_codex_login_event(
    console: Console,
    event: CodexLoginEvent,
) -> None:
    """Render one ephemeral provider-controlled login step."""
    console.print("[cyan]Complete the official Codex sign-in:[/cyan]")
    console.print(Text(event.authorization_url))
    if event.user_code is not None:
        code = Text("Device code: ", style="cyan")
        code.append(event.user_code)
        console.print(code)


def _usage_error(ctx: typer.Context, message: str) -> NoReturn:
    """Render a credential-command usage failure and stop."""
    invocation_context(ctx).err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=ExitCode.SYSTEM_ERROR)


def _prompt_for_token(
    ctx: typer.Context,
    spec: TokenPromptSpec,
) -> str | None:
    invocation = invocation_context(ctx)
    if not sys.stdin.isatty():
        invocation.console.print(
            f"[dim]No local {spec.display_name} login "
            "found — reading token from stdin...[/dim]"
        )
    else:
        invocation.console.print(
            f"[dim]No local {spec.display_name} login found. "
            "Paste an OAuth token (input hidden), or press Ctrl-C "
            "to cancel.[/dim]"
        )
        if spec.setup_hint is not None:
            invocation.console.print(f"[dim]Tip: {spec.setup_hint}[/dim]")
    return TokenInput(spec.token_pattern, invocation.err_console).read()


def _refresh_managed_codex(
    ctx: typer.Context,
    app_context: AppContext,
    label: AccountLabel,
    *,
    replace_identity: bool,
) -> bool:
    """Run managed Codex repair when the label belongs to Codex."""
    account_id = app_context.accounts.resolve_account_id(
        ProviderId.CODEX,
        label,
    )
    if account_id is None:
        return False
    if (
        app_context.accounts.resolve_account_id(ProviderId.CLAUDE, label)
        is not None
    ):
        _usage_error(
            ctx,
            f"Account label '{label}' exists for both providers.",
        )
    if replace_identity:
        _usage_error(
            ctx,
            "--replace-identity applies only to setup-token-only "
            "Claude accounts.",
        )
    invocation = invocation_context(ctx)
    result = app_context.credentials.login_codex(
        label,
        device_auth=False,
        events=lambda event: render_codex_login_event(
            invocation.console,
            event,
        ),
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    message = Text("Managed Codex login ready for ", style="green")
    message.append(f"'{label}'.")
    invocation.console.print(message)
    return True


def add_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str,
        typer.Argument(help="Provider id (claude or codex)."),
    ],
    label: Annotated[
        str | None,
        typer.Option("--label", help="Override the auto-generated label."),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Paste a token instead of auto-detecting.",
        ),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option("--plan", help="Override the auto-detected plan tag."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite an existing label."),
    ] = False,
) -> None:
    """Save an account. Idempotent: same token reuses the entry.

    Auto-detects credentials from the local provider install when
    ``--token`` is omitted. Falls back to a hidden prompt (or stdin
    if piped) when no local login is found.
    """
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    provider_id = validated_provider(ctx, provider)
    prompt_spec = app_context.credentials.prompt_spec(provider_id)
    if isinstance(prompt_spec, ProviderFailure):
        exit_credential_failure(ctx, prompt_spec)
    source = (
        TokenCredentialSource(provider_id=provider_id, token=token)
        if token is not None
        else LocalCredentialSource(provider_id=provider_id)
    )
    target_label = validated_label(ctx, label) if label is not None else None
    result = app_context.credentials.save(
        source,
        label=target_label,
        plan=plan,
        force=force,
    )
    if (
        isinstance(result, ProviderFailure)
        and token is None
        and result.kind is ProviderFailureKind.MISSING
    ):
        prompted = _prompt_for_token(ctx, prompt_spec)
        if prompted is None:
            invocation.err_console.print(
                "[red]No valid token provided. Cancelled.[/red]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        source_name = "stdin" if not sys.stdin.isatty() else "prompt"
        invocation.console.print(
            f"[green]Got token from {source_name}.[/green]"
        )
        result = app_context.credentials.save(
            TokenCredentialSource(
                provider_id=provider_id,
                token=prompted,
            ),
            label=target_label,
            plan=plan,
            force=force,
        )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    action = "Saved" if result.created else "Updated"
    invocation.console.print(f"[green]{action} '{result.label}'.[/green]")
    if result.warning is not None:
        invocation.console.print(f"[yellow]Note: {result.warning}[/yellow]")


def refresh_cmd(
    ctx: typer.Context,
    label: Annotated[
        str | None,
        typer.Argument(help="Account label."),
    ] = None,
    all_accounts: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Refresh every due account from its owned authority.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Only print accounts needing manual action.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="With --all, refresh even if tokens are still fresh.",
        ),
    ] = False,
    replace_identity: Annotated[
        bool,
        typer.Option(
            "--replace-identity",
            help=(
                "Confirm the first subscription identity association for "
                "a setup-token-only Claude account."
            ),
        ),
    ] = False,
) -> None:
    """Repair one account or refresh all accounts from owned authorities.

    A provider label repairs its isolated managed profile through the
    official CLI. ``--all`` uses saved authorities only.
    """
    narrowed = _validate_refresh_args(
        ctx,
        label,
        all_accounts=all_accounts,
        quiet=quiet,
        force=force,
        replace_identity=replace_identity,
    )
    if all_accounts:
        run_refresh_all(ctx, quiet=quiet, force=force)
        return
    if narrowed is None:
        raise AssertionError("Refresh label validation failed.")
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    account_label = validated_label(ctx, narrowed)
    if _refresh_managed_codex(
        ctx,
        app_context,
        account_label,
        replace_identity=replace_identity,
    ):
        return
    account_id = app_context.accounts.resolve_account_id(
        ProviderId.CLAUDE,
        account_label,
    )
    if account_id is None:
        invocation.err_console.print(
            f"[yellow]No account named '{narrowed}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    result = app_context.credentials.login_claude(
        account_label,
        establish_identity=replace_identity,
        interactive=(
            sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()
        ),
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    invocation.console.print(
        f"[green]Managed Claude login ready for '{narrowed}'.[/green]"
    )


def _validate_refresh_args(
    ctx: typer.Context,
    label: str | None,
    *,
    all_accounts: bool,
    quiet: bool,
    force: bool,
    replace_identity: bool,
) -> str | None:
    if all_accounts:
        if label is not None:
            _usage_error(
                ctx,
                "--all cannot be combined with an account label.",
            )
        if replace_identity:
            _usage_error(
                ctx,
                "--replace-identity only applies to a label refresh.",
            )
        return None
    if label is None:
        _usage_error(ctx, "Pass an account label or use --all.")
    if quiet:
        _usage_error(ctx, "--quiet only applies with --all.")
    if force:
        _usage_error(ctx, "--force only applies with --all.")
    return label


def register(application: typer.Typer) -> None:
    """Register the sole add and refresh command owners."""
    branded_command(application, "add")(add_cmd)
    branded_command(application, "refresh")(refresh_cmd)
