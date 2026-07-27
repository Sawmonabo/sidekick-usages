"""Dedicated prompt-toolkit dashboard process application."""

import os
import sys

from prompt_toolkit import Application
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl

from sidekick_usages.cli.dashboard.input import DashboardInputController
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardApplicationResult,
)
from sidekick_usages.cli.dashboard.ports import DashboardSessionPort
from sidekick_usages.cli.dashboard.session import dashboard_cursor
from sidekick_usages.cli.dashboard.terminal import terminal_width
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard,
)
from sidekick_usages.usage.presentation.dashboard.render.style import (
    dashboard_color_enabled,
)

INTERRUPTED_EXIT_CODE = 130


class InteractiveDashboardApplication:
    """Own one prompt-toolkit lifecycle around the shared frame renderer."""

    def __init__(
        self,
        session: DashboardSessionPort,
    ) -> None:
        self._session = session
        self._input = DashboardInputController(session)
        self._color = dashboard_color_enabled(
            os.environ,
            terminal=True,
        )
        control = FormattedTextControl(
            text=self._render,
            focusable=True,
            show_cursor=False,
        )
        application: Application[DashboardApplicationResult] = Application(
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
        self._application = application
        self._session.bind_invalidator(self._application.invalidate)

    def run(self) -> DashboardApplicationResult:
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
        width = terminal_width(sys.stdout)
        return ANSI(
            render_dashboard(
                view.snapshot,
                width=width,
                cursor=dashboard_cursor(view),
                footer=view.footer,
                color=self._color,
            )
        )
