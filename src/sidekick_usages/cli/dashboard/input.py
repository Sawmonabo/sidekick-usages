"""Static prompt input for the dedicated dashboard process."""

from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import DashboardMove
from sidekick_usages.cli.dashboard.ports import (
    DashboardLookupPort,
    DashboardSupervisorPort,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
    DashboardFooterKind,
)

ACTIVATION_QUEUED_MESSAGE = "Account change queued."
REFRESH_QUEUED_MESSAGE = "Account refresh queued."
REFRESH_ALL_QUEUED_MESSAGE = "Due-account refresh queued."


class DashboardInputController:
    """Translate portable keys into local state or typed queued intents."""

    def __init__(
        self,
        controller: DashboardController,
        *,
        lookup: DashboardLookupPort | None = None,
        supervisor: DashboardSupervisorPort | None = None,
    ) -> None:
        self.controller = controller
        self.footer = DashboardFooter()
        self.bindings = KeyBindings()
        self._lookup = lookup
        self._supervisor = supervisor
        self._bind()

    @property
    def cursor(self) -> DashboardCursor:
        """Project the current controller focus for the shared renderer."""
        state = self.controller.state
        return DashboardCursor(
            focused_provider=state.focused_provider,
            account_id=state.account_id,
            external=state.external,
        )

    def _bind(self) -> None:
        self.bindings.add("up")(self._move_up)
        self.bindings.add("k")(self._move_up)
        self.bindings.add("down")(self._move_down)
        self.bindings.add("j")(self._move_down)
        self.bindings.add("tab")(self._next_provider)
        self.bindings.add("escape")(self._restore)
        self.bindings.add("enter")(self._activate)
        self.bindings.add("r")(self._refresh)
        self.bindings.add("R")(self._refresh_all)
        self.bindings.add("?")(self._toggle_help)
        self.bindings.add("q")(self._quit)
        self.bindings.add("c-c")(self._interrupt)

    def _move_up(self, event: KeyPressEvent) -> None:
        self.controller = self.controller.move(DashboardMove.UP)
        self._keys_footer()
        event.app.invalidate()

    def _move_down(self, event: KeyPressEvent) -> None:
        self.controller = self.controller.move(DashboardMove.DOWN)
        self._keys_footer()
        event.app.invalidate()

    def _next_provider(self, event: KeyPressEvent) -> None:
        self.controller = self.controller.focus_next_provider()
        self._keys_footer()
        event.app.invalidate()

    def _restore(self, event: KeyPressEvent) -> None:
        self.controller = self.controller.restore()
        self._keys_footer()
        event.app.invalidate()

    def _activate(self, event: KeyPressEvent) -> None:
        intent = self.controller.activate_or_repair()
        if intent is None or self._supervisor is None:
            return
        self._supervisor.enqueue(intent)
        self.footer = DashboardFooter(
            kind=DashboardFooterKind.PROGRESS,
            message=ACTIVATION_QUEUED_MESSAGE,
        )
        event.app.invalidate()

    def _refresh(self, event: KeyPressEvent) -> None:
        intent = self.controller.refresh_account()
        if intent is None or self._lookup is None:
            return
        self._lookup.enqueue(intent)
        self.footer = DashboardFooter(
            kind=DashboardFooterKind.PROGRESS,
            message=REFRESH_QUEUED_MESSAGE,
        )
        event.app.invalidate()

    def _refresh_all(self, event: KeyPressEvent) -> None:
        intent = self.controller.refresh_due_accounts()
        if intent is None or self._lookup is None:
            return
        self._lookup.enqueue(intent)
        self.footer = DashboardFooter(
            kind=DashboardFooterKind.PROGRESS,
            message=REFRESH_ALL_QUEUED_MESSAGE,
        )
        event.app.invalidate()

    def _toggle_help(self, event: KeyPressEvent) -> None:
        self.controller = self.controller.toggle_help()
        self.footer = DashboardFooter(
            kind=(
                DashboardFooterKind.HELP
                if self.controller.state.help_visible
                else DashboardFooterKind.KEYS
            )
        )
        event.app.invalidate()

    @staticmethod
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit(result=0)

    @staticmethod
    def _interrupt(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    def _keys_footer(self) -> None:
        self.footer = DashboardFooter(
            kind=(
                DashboardFooterKind.HELP
                if self.controller.state.help_visible
                else DashboardFooterKind.KEYS
            )
        )
