"""Dedicated prompt-toolkit dashboard process application."""

import shutil
from io import StringIO

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from rich.console import Console

from sidekick_usages.cli.dashboard.input import DashboardInputController
from sidekick_usages.cli.dashboard.ports import DashboardSessionPort
from sidekick_usages.cli.dashboard.session import dashboard_cursor
from sidekick_usages.usage.presentation.dashboard.overview import (
    dashboard_overview,
)

TERMINAL_FALLBACK = (80, 24)
INTERRUPTED_EXIT_CODE = 130


class InteractiveDashboardApplication:
    """Own one prompt-toolkit lifecycle around the shared Rich renderer."""

    def __init__(
        self,
        session: DashboardSessionPort,
    ) -> None:
        self._session = session
        self._input = DashboardInputController(session)
        control = FormattedTextControl(
            text=self._render,
            focusable=True,
            show_cursor=False,
        )
        self._application: Application[int] = Application(
            layout=Layout(
                Window(
                    content=control,
                    wrap_lines=False,
                    dont_extend_height=True,
                )
            ),
            key_bindings=self._input.bindings,
            full_screen=False,
            erase_when_done=False,
        )
        self._session.bind_invalidator(self._application.invalidate)

    def run(self) -> int:
        """Run with one terminal-restoring prompt-toolkit lifecycle."""
        try:
            try:
                return self._application.run(pre_run=self._session.start)
            finally:
                self._session.close()
        except KeyboardInterrupt:
            return INTERRUPTED_EXIT_CODE

    def _render(self) -> ANSI:
        view = self._session.view
        width = shutil.get_terminal_size(TERMINAL_FALLBACK).columns
        output = StringIO()
        console = Console(
            file=output,
            width=width,
            force_terminal=True,
            legacy_windows=False,
        )
        console.print(
            dashboard_overview(
                view.snapshot,
                width=width,
                cursor=dashboard_cursor(view),
                footer=view.footer,
            )
        )
        return ANSI(output.getvalue())
