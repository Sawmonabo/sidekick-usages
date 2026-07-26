"""Shared validation for CLI-owned scalar inputs."""

from typing import Never

import typer

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
)


def exit_usage_error(ctx: typer.Context, message: str) -> Never:
    """Render one invalid command combination and stop."""
    invocation_context(ctx).err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=ExitCode.SYSTEM_ERROR)


def validated_label(ctx: typer.Context, value: str) -> AccountLabel:
    """Validate one label and translate failure to CLI output."""
    try:
        return AccountLabel(value)
    except ValueError as error:
        invocation_context(ctx).err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error


def validated_provider(ctx: typer.Context, value: str) -> ProviderId:
    """Validate one supported provider and render the closed vocabulary."""
    try:
        return ProviderId(value)
    except ValueError:
        known = ", ".join(provider.value for provider in ProviderId)
        invocation_context(ctx).err_console.print(
            f"[red]Unknown provider {value!r}. Known: {known}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
