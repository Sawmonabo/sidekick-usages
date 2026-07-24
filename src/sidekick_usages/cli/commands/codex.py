"""Codex login and credential-export command adapters."""

from pathlib import Path
from typing import Annotated

import typer

from sidekick_usages.cli.commands.accounts import validated_label
from sidekick_usages.cli.commands.credentials import exit_credential_failure
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.providers.base import ProviderFailure

__all__ = ["register"]


def codex_login_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to update.")],
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help=(
                "Advanced: run login against this source CODEX_HOME "
                "before importing a private sidekick copy."
            ),
        ),
    ] = None,
    device_auth: Annotated[
        bool,
        typer.Option(
            "--device-auth",
            help="Use Codex CLI device authentication.",
        ),
    ] = False,
    replace_identity: Annotated[
        bool,
        typer.Option(
            "--replace-identity",
            help=(
                "Allow replacing the saved provider account id with the "
                "login from this Codex home."
            ),
        ),
    ] = False,
) -> None:
    """Run ``codex login`` and import a private sidekick auth copy."""
    invocation = invocation_context(ctx)
    result = invocation.require_app(ctx).credentials.login_codex(
        validated_label(ctx, label),
        source_home=(
            codex_home.expanduser() if codex_home is not None else None
        ),
        device_auth=device_auth,
        replace_identity=replace_identity,
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    invocation.console.print(
        f"[green]Updated Codex login for '{label}'.[/green]"
    )


def codex_export_cmd(
    ctx: typer.Context,
    label: Annotated[
        str,
        typer.Argument(help="Saved Codex account label."),
    ],
    codex_home: Annotated[
        Path,
        typer.Option(
            "--codex-home",
            help="Target isolated Codex CODEX_HOME.",
        ),
    ],
    source_codex_home: Annotated[
        Path | None,
        typer.Option(
            "--source-codex-home",
            help=(
                "Optional source CODEX_HOME whose auth.json belongs to "
                "this account."
            ),
        ),
    ] = None,
) -> None:
    """Export a saved Codex account into a file-backed Codex home."""
    invocation = invocation_context(ctx)
    exported = invocation.require_app(ctx).credentials.export_codex(
        label,
        codex_home,
        source_home=source_codex_home,
    )
    if isinstance(exported, ProviderFailure):
        exit_credential_failure(
            ctx,
            exported,
            prefix=f"Cannot export '{label}': ",
        )
    invocation.console.print(
        f"[green]Exported '{label}' to Codex home "
        f"{exported.target_home}.[/green]"
    )


def register(application: typer.Typer) -> None:
    """Register the Codex credential command group."""
    codex_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Manage saved Codex CLI login credentials.",
        rich_markup_mode="rich",
    )
    branded_command(codex_app, "login")(codex_login_cmd)
    branded_command(codex_app, "export")(codex_export_cmd)
    application.add_typer(codex_app, name="codex")
