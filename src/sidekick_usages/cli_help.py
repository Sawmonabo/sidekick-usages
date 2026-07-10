"""Typer adapters that prepend shared branding to human-facing help."""

import shutil
from collections.abc import Callable
from typing import Any

import click
import typer
from rich.console import Console
from typer.core import TyperCommand, TyperGroup
from typer.models import CommandFunctionType

from sidekick_usages.branding import brand_header

_DEFAULT_HELP_WIDTH = 80


def _help_width(ctx: click.Context) -> int:
    """Resolve help width from public Click context and terminal settings."""
    width = ctx.terminal_width
    if width is None:
        width = shutil.get_terminal_size(
            fallback=(_DEFAULT_HELP_WIDTH, 24)
        ).columns
    if ctx.max_content_width is not None:
        width = min(width, ctx.max_content_width)
    return max(1, width)


class _BrandedHelpMixin:
    """Shared prelude used by Typer command and group help renderers."""

    @staticmethod
    def _print_brand(ctx: click.Context) -> None:
        """Print branding without initializing application state."""
        width = _help_width(ctx)
        console = Console(
            width=width,
            no_color=ctx.color is False,
        )
        console.print(brand_header(width))


class BrandedTyperCommand(_BrandedHelpMixin, TyperCommand):
    """Typer leaf command whose help starts with shared branding."""

    def format_help(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> None:
        """Print the shared header, then delegate to Typer's formatter."""
        self._print_brand(ctx)
        super().format_help(ctx, formatter)


class BrandedTyperGroup(_BrandedHelpMixin, TyperGroup):
    """Typer command group whose help starts with shared branding."""

    def format_help(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> None:
        """Print the shared header, then delegate to Typer's formatter."""
        self._print_brand(ctx)
        super().format_help(ctx, formatter)


class BrandedTyper(typer.Typer):
    """Typer application that brands leaf help without decorator repetition."""

    def command(
        self,
        name: str | None = None,
        *,
        cls: type[TyperCommand] | None = None,
        **kwargs: Any,
    ) -> Callable[[CommandFunctionType], CommandFunctionType]:
        """Register a command using the shared branded class by default."""
        return super().command(
            name,
            cls=cls or BrandedTyperCommand,
            **kwargs,
        )
