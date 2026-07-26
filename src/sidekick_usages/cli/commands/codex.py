"""Official managed Codex login command adapter."""

from typing import Annotated

import typer
from rich.text import Text

from sidekick_usages.cli.commands.credentials import (
    exit_credential_failure,
    render_codex_login_event,
)
from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.help import BrandedTyperGroup, branded_command
from sidekick_usages.cli.validation import validated_label
from sidekick_usages.providers.base import ProviderFailure


def codex_login_cmd(
    ctx: typer.Context,
    label: Annotated[str, typer.Argument(help="Account label to update.")],
    device_auth: Annotated[
        bool,
        typer.Option(
            "--device-auth",
            help="Use Codex CLI device authentication.",
        ),
    ] = False,
) -> None:
    """Authenticate a saved account in its final managed Codex home."""
    invocation = invocation_context(ctx)
    account_label = validated_label(ctx, label)
    result = invocation.require_app(ctx).credentials.login_codex(
        account_label,
        device_auth=device_auth,
        events=lambda event: render_codex_login_event(
            invocation.console,
            event,
        ),
    )
    if isinstance(result, ProviderFailure):
        exit_credential_failure(ctx, result)
    message = Text("Managed Codex login ready for ", style="green")
    message.append(f"'{account_label}'.")
    invocation.console.print(message)


def register(application: typer.Typer) -> None:
    """Register the Codex credential command group."""
    codex_app = typer.Typer(
        cls=BrandedTyperGroup,
        help="Manage saved Codex CLI login credentials.",
        rich_markup_mode="rich",
    )
    branded_command(codex_app, "login")(codex_login_cmd)
    application.add_typer(codex_app, name="codex")
