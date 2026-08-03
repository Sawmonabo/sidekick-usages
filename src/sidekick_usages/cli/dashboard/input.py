"""Static prompt input for the dedicated dashboard process."""

from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.cli.dashboard.ports import DashboardSessionPort


class DashboardInputController:
    """Translate portable keys into nonblocking session transitions."""

    def __init__(self, session: DashboardSessionPort) -> None:
        self.bindings = KeyBindings()
        self._session = session
        self._bind()

    def _bind(self) -> None:
        self.bindings.add("up")(self._move_up)
        self.bindings.add("k")(self._move_up)
        self.bindings.add("down")(self._move_down)
        self.bindings.add("j")(self._move_down)
        self.bindings.add("tab")(self._next_provider)
        self.bindings.add("escape")(self._restore)
        self.bindings.add("enter")(self._select_account)
        self.bindings.add("r")(self._refresh)
        self.bindings.add("R")(self._refresh_all)
        self.bindings.add("?")(self._toggle_help)
        self.bindings.add("y")(self._approve)
        self.bindings.add("n")(self._refuse)
        self.bindings.add("q")(self._quit)
        self.bindings.add("c-c")(self._interrupt)

    def _move_up(self, _event: KeyPressEvent) -> None:
        self._session.move(DashboardMove.UP)

    def _move_down(self, _event: KeyPressEvent) -> None:
        self._session.move(DashboardMove.DOWN)

    def _next_provider(self, _event: KeyPressEvent) -> None:
        self._session.focus_next_provider()

    def _restore(self, _event: KeyPressEvent) -> None:
        self._session.restore()

    def _select_account(self, _event: KeyPressEvent) -> None:
        self._session.select_account()

    def _refresh(self, _event: KeyPressEvent) -> None:
        self._session.refresh_account()

    def _refresh_all(self, _event: KeyPressEvent) -> None:
        self._session.refresh_due_accounts()

    def _toggle_help(self, _event: KeyPressEvent) -> None:
        self._session.toggle_help()

    def _approve(self, _event: KeyPressEvent) -> None:
        self._session.confirm(True)

    def _refuse(self, _event: KeyPressEvent) -> None:
        self._session.confirm(False)

    @staticmethod
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit(result=0)

    @staticmethod
    def _interrupt(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())
