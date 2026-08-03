"""Dedicated prompt-toolkit dashboard process application."""

import os

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import ColorDepth

from sidekick_usages.cli.dashboard.input import DashboardInputController
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardApplicationResult,
)
from sidekick_usages.cli.dashboard.ports import DashboardSessionPort
from sidekick_usages.cli.dashboard.session import dashboard_cursor
from sidekick_usages.cli.dashboard.terminal import terminal_dimensions
from sidekick_usages.usage.presentation.dashboard.render.frame import (
    render_dashboard_layout,
)
from sidekick_usages.usage.presentation.dashboard.render.models import (
    DashboardRenderLayout,
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
        self._rendered = DashboardRenderLayout(
            masthead="",
            body="",
            status="",
            keys="",
            focused_body_line=None,
        )
        self._session_started = False
        masthead = FormattedTextControl(
            text=lambda: ANSI(self._rendered.masthead),
        )
        body = FormattedTextControl(
            text=lambda: ANSI(self._rendered.body),
            focusable=True,
            show_cursor=False,
            get_cursor_position=self._body_cursor_position,
        )
        status = FormattedTextControl(
            text=lambda: ANSI(self._rendered.status),
        )
        keys = FormattedTextControl(
            text=lambda: ANSI(self._rendered.keys),
        )
        application: Application[DashboardApplicationResult] = Application(
            layout=Layout(
                HSplit(
                    (
                        Window(
                            content=masthead,
                            height=lambda: self._fixed_height(
                                self._rendered.masthead
                            ),
                            wrap_lines=False,
                        ),
                        Window(
                            content=body,
                            wrap_lines=False,
                            always_hide_cursor=True,
                        ),
                        Window(
                            content=status,
                            height=lambda: self._fixed_height(
                                self._rendered.status
                            ),
                            wrap_lines=False,
                        ),
                        Window(
                            content=keys,
                            height=lambda: self._fixed_height(
                                self._rendered.keys
                            ),
                            wrap_lines=False,
                        ),
                    )
                ),
                focused_element=body,
            ),
            key_bindings=self._input.bindings,
            full_screen=False,
            erase_when_done=False,
            color_depth=(
                ColorDepth.DEPTH_24_BIT
                if self._color
                else ColorDepth.DEPTH_1_BIT
            ),
            before_render=self._prepare_render,
            after_render=self._start_session_after_render,
        )
        self._application = application
        self._session.bind_invalidator(self._application.invalidate)

    def run(self) -> DashboardApplicationResult:
        """Run with one terminal-restoring prompt-toolkit lifecycle."""
        try:
            try:
                return self._application.run()
            finally:
                self._session.close()
        except KeyboardInterrupt:
            return INTERRUPTED_EXIT_CODE

    def _prepare_render(
        self,
        _application: Application[DashboardApplicationResult],
    ) -> None:
        """Resolve one atomic view against current output dimensions."""
        view = self._session.view
        size = get_app().output.get_size()
        self._rendered = render_dashboard_layout(
            view.snapshot,
            dimensions=terminal_dimensions(size.columns, size.rows),
            cursor=dashboard_cursor(view),
            footer=view.footer,
            color=self._color,
        )

    def _start_session_after_render(
        self,
        _application: Application[DashboardApplicationResult],
    ) -> None:
        """Start runtime owners once after the cached frame is flushed."""
        if self._session_started:
            return
        self._session_started = True
        self._session.start()

    def _body_cursor_position(self) -> Point | None:
        """Expose one hidden body cursor for prompt-toolkit scrolling."""
        line = self._rendered.focused_body_line
        return None if line is None else Point(x=0, y=line)

    @staticmethod
    def _fixed_height(fragment: str) -> Dimension:
        """Return one exact preferred fragment height."""
        return Dimension.exact(max(1, fragment.count("\n")))
