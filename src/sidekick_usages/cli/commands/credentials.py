"""Credential add and sole refresh command adapters."""

import sys
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from sidekick_usages.cli.commands.accounts import validated_label
from sidekick_usages.cli.commands.maintenance import run_refresh_all
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.token_input import TokenInput
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.credentials import (
    LocalCredentialSource,
    TokenCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


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


def _usage_error(ctx: typer.Context, message: str) -> NoReturn:
    """Render a credential-command usage failure and stop."""
    invocation_context(ctx).err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=ExitCode.SYSTEM_ERROR)


def _provider_id(ctx: typer.Context, value: str) -> ProviderId:
    try:
        return ProviderId(value)
    except ValueError:
        known = ", ".join(provider.value for provider in ProviderId)
        invocation_context(ctx).err_console.print(
            f"[red]Unknown provider {value!r}. Known: {known}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None


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
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help=(
                "Read Codex credentials from this source CODEX_HOME, "
                "then copy them into sidekick's private credential bundle."
            ),
        ),
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
    provider_id = _provider_id(ctx, provider)
    prompt_spec = app_context.credentials.prompt_spec(provider_id)
    if isinstance(prompt_spec, ProviderFailure):
        exit_credential_failure(ctx, prompt_spec)
    if codex_home is not None and provider_id is not ProviderId.CODEX:
        _usage_error(
            ctx,
            "--codex-home can only be used with the codex provider.",
        )
    source = (
        TokenCredentialSource(provider_id=provider_id, token=token)
        if token is not None
        else LocalCredentialSource(
            provider_id=provider_id,
            credential_home=(
                codex_home.expanduser() if codex_home is not None else None
            ),
        )
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
            help="Refresh every due account using saved refresh tokens.",
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
    from_codex_home: Annotated[
        Path | None,
        typer.Option(
            "--from-codex-home",
            help=(
                "Read Codex credentials from this CODEX_HOME instead "
                "of the saved/default home."
            ),
        ),
    ] = None,
    replace_identity: Annotated[
        bool,
        typer.Option(
            "--replace-identity",
            help=(
                "Allow replacing the saved provider account id with the "
                "current local login."
            ),
        ),
    ] = False,
) -> None:
    """Replace a saved account's token with the local CLI login.

    With a label, reads the current login from the provider's local
    install and writes the new access token into that saved account.
    With ``--all``, uses only saved refresh tokens and never adopts
    the current global provider login.
    """
    narrowed = _validate_refresh_args(
        ctx,
        label,
        all_accounts=all_accounts,
        quiet=quiet,
        force=force,
        from_codex_home=from_codex_home,
        replace_identity=replace_identity,
    )
    if all_accounts:
        run_refresh_all(ctx, quiet=quiet, force=force)
        return
    if narrowed is None:
        raise AssertionError("Refresh label validation failed.")
    invocation = invocation_context(ctx)
    app_context = invocation.require_app(ctx)
    account = app_context.accounts.get(narrowed)
    if account is None:
        invocation.err_console.print(
            f"[yellow]No account named '{narrowed}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if (
        from_codex_home is not None
        and account.provider_id is not ProviderId.CODEX
    ):
        _usage_error(
            ctx,
            "--from-codex-home requires a saved Codex account.",
        )
    result = app_context.credentials.refresh_from_source(
        narrowed,
        LocalCredentialSource(
            provider_id=account.provider_id,
            credential_home=(
                from_codex_home.expanduser()
                if from_codex_home is not None
                else None
            ),
        ),
        replace_identity=replace_identity,
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    invocation.console.print(f"[green]Updated token for '{narrowed}'.[/green]")


def _validate_refresh_args(
    ctx: typer.Context,
    label: str | None,
    *,
    all_accounts: bool,
    quiet: bool,
    force: bool,
    from_codex_home: Path | None,
    replace_identity: bool,
) -> str | None:
    if all_accounts:
        if label is not None:
            _usage_error(
                ctx,
                "--all cannot be combined with an account label.",
            )
        if from_codex_home is not None:
            _usage_error(
                ctx,
                "--from-codex-home only applies to a label refresh.",
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


__all__ = ["exit_credential_failure", "register"]
