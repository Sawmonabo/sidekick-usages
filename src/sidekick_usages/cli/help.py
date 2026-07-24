"""Typer adapters that prepend shared branding to human-facing help."""

import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import click
import typer
from rich.console import Console
from typer import rich_utils
from typer.core import TyperCommand, TyperGroup
from typer.models import CommandFunctionType

from sidekick_usages.branding import brand_header

_DEFAULT_HELP_WIDTH = 80


def _help_width(ctx: click.Context) -> int:
    """Resolve help width from public Click and terminal settings."""
    width = ctx.terminal_width
    if width is None:
        width = shutil.get_terminal_size(
            fallback=(_DEFAULT_HELP_WIDTH, 24)
        ).columns
    if ctx.max_content_width is not None:
        width = min(width, ctx.max_content_width)
    return max(1, width)


@contextmanager
def _typer_help_width(width: int) -> Iterator[None]:
    """Apply one width to Typer's Rich help console for this render."""
    previous_width = rich_utils.MAX_WIDTH
    rich_utils.MAX_WIDTH = width
    try:
        yield
    finally:
        rich_utils.MAX_WIDTH = previous_width


class _BrandedHelpMixin:
    """Shared prelude used by Typer command and group help renderers."""

    @staticmethod
    def _print_brand(
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> int:
        """Print branding and return the shared help width."""
        width = _help_width(ctx)
        ctx.terminal_width = width
        formatter.width = width
        console = Console(width=width, no_color=ctx.color is False)
        console.print(brand_header(width))
        return width


class BrandedTyperCommand(_BrandedHelpMixin, TyperCommand):
    """Typer leaf command whose help starts with shared branding."""

    def format_help(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> None:
        """Print the shared header, then delegate to Typer."""
        width = self._print_brand(ctx, formatter)
        with _typer_help_width(width):
            super().format_help(ctx, formatter)


class BrandedTyperGroup(_BrandedHelpMixin, TyperGroup):
    """Typer command group whose help starts with shared branding."""

    def format_help(
        self,
        ctx: click.Context,
        formatter: click.HelpFormatter,
    ) -> None:
        """Print the shared header, then delegate to Typer."""
        width = self._print_brand(ctx, formatter)
        with _typer_help_width(width):
            super().format_help(ctx, formatter)


def branded_command(
    application: typer.Typer,
    name: str | None = None,
    *,
    help: str | None = None,
    hidden: bool = False,
    deprecated: bool = False,
) -> Callable[[CommandFunctionType], CommandFunctionType]:
    """Return a decorator using the shared branded command class."""
    return application.command(
        name,
        cls=BrandedTyperCommand,
        help=help,
        hidden=hidden,
        deprecated=deprecated,
    )
