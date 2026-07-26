"""Two-owner orchestration for one interactive dashboard process."""

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread

from sidekick_usages.cli.dashboard.actions import DashboardActionExecutor
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardActivationProof,
    DashboardIntent,
    DashboardMove,
    RefreshAccountIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardActionRequest,
    DashboardConfirmation,
    DashboardConfirmationKind,
    DashboardSessionView,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardControlConnector,
    DashboardLookupWorker,
    DashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import (
    CompletedPayload,
    ControlActionTerminalPayload,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.providers.claude.activation.service import (
    claude_environment_conflict,
    claude_environment_conflict_keys,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
    DashboardFooterKind,
    DashboardService,
    DashboardSnapshot,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupWorkerEvent,
)

ACTION_QUEUE_CAPACITY = 1
DECISION_QUEUE_CAPACITY = 1
ACTIVATION_QUEUED_MESSAGE = "Account change queued."
REFRESH_QUEUED_MESSAGE = "Account refresh queued."
REFRESH_ALL_QUEUED_MESSAGE = "Due-account refresh queued."
LOOKUP_STARTED_MESSAGE = "Refreshing account metrics."
LOOKUP_PROGRESS_MESSAGE = "Updated account metrics."
LOOKUP_FAILED_MESSAGE = "Live metrics refresh failed; cached metrics remain."
CACHE_RELOAD_ERROR_MESSAGE = (
    "The action completed, but cached state could not be reloaded."
)


def _discard_invalidation() -> None:
    """Discard redraws before prompt-toolkit binds its application."""


class InteractiveDashboardSession:
    """Own one atomic view, one lookup owner, and one action owner."""

    def __init__(
        self,
        snapshot: DashboardSnapshot,
        *,
        snapshots: DashboardSnapshotSource,
        only: ProviderId | None,
        lookup: DashboardLookupWorker,
        connector: DashboardControlConnector,
        socket_path: Path,
        setup: GuidedServiceSetup,
        environment: Mapping[str, str],
    ) -> None:
        controller = DashboardController.start(snapshot)
        self._view = DashboardSessionView(
            snapshot=snapshot,
            controller=controller.state,
            footer=DashboardFooter(),
        )
        self._snapshots = snapshots
        self._only = only
        self._lookup = lookup
        self._claude_environment_conflict = claude_environment_conflict(
            dict(environment)
        )
        self._view_lock = Lock()
        self._snapshot_lock = Lock()
        self._actions: Queue[DashboardActionRequest | None] = Queue(
            ACTION_QUEUE_CAPACITY
        )
        self._decisions: Queue[ServiceSetupDecision] = Queue(
            DECISION_QUEUE_CAPACITY
        )
        self._stopping = Event()
        self._invalidate = _discard_invalidation
        self._lookup_thread: Thread | None = None
        self._action_thread: Thread | None = None
        self._started = False
        self._closed = False
        self._action_executor = DashboardActionExecutor(
            connector=connector,
            socket_path=socket_path,
            setup=setup,
            sink=self,
        )

    @property
    def view(self) -> DashboardSessionView:
        """Return one internally consistent immutable dashboard view."""
        with self._view_lock:
            return self._view

    @property
    def service(self) -> DashboardService:
        """Return the latest cached service hint."""
        return self.view.snapshot.service

    @property
    def stopping(self) -> bool:
        """Return whether the dashboard is closing."""
        return self._stopping.is_set()

    def bind_invalidator(self, invalidate: Callable[[], None]) -> None:
        """Bind the thread-safe prompt-toolkit invalidation callback."""
        with self._view_lock:
            if self._started:
                raise RuntimeError("Dashboard invalidation is already active.")
            self._invalidate = invalidate

    def start(self) -> None:
        """Start exactly one lookup owner and one action owner."""
        with self._view_lock:
            if self._closed:
                raise RuntimeError("The dashboard session is closed.")
            if self._started:
                return
            self._started = True
            self._lookup_thread = Thread(
                target=self._run_lookup,
                name="sidekick-dashboard-lookup",
            )
            self._action_thread = Thread(
                target=self._run_actions,
                name="sidekick-dashboard-actions",
            )
            lookup_thread = self._lookup_thread
            action_thread = self._action_thread
        try:
            lookup_thread.start()
            action_thread.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Cancel lookup work, stop observation, and join both owners."""
        with self._view_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            lookup_thread = self._lookup_thread
            action_thread = self._action_thread
        self._lookup.cancel()
        self._action_executor.close()
        with suppress(Full):
            self._decisions.put_nowait(ServiceSetupDecision.REFUSED)
        with suppress(Full):
            self._actions.put_nowait(None)
        self._join_owner(lookup_thread)
        self._join_owner(action_thread)

    @staticmethod
    def _join_owner(thread: Thread | None) -> None:
        """Join a launched owner and ignore only a never-started thread."""
        if thread is None:
            return
        try:
            thread.join()
        except RuntimeError:
            if thread.ident is not None:
                raise

    def move(self, direction: DashboardMove) -> None:
        """Move the preview cursor without changing verified state."""
        with self._view_lock:
            controller = self._controller().move(direction)
            self._set_controller(
                controller,
                self._navigation_footer(controller),
            )
            invalidate = self._invalidate
        invalidate()

    def focus_next_provider(self) -> None:
        """Focus the next displayed provider at its verified anchor."""
        with self._view_lock:
            controller = self._controller().focus_next_provider()
            self._set_controller(
                controller,
                self._navigation_footer(controller),
            )
            invalidate = self._invalidate
        invalidate()

    def restore(self) -> None:
        """Restore verified focus unless activation is in flight."""
        with self._view_lock:
            if self._view.activation_in_flight:
                return
            controller = self._controller().restore()
            self._set_controller(
                controller,
                self._navigation_footer(controller),
            )
            invalidate = self._invalidate
        invalidate()

    def activate(self) -> None:
        """Queue one account activation without blocking input."""
        with self._view_lock:
            if self._view.action_in_flight:
                return
            intent = self._controller().activate_or_repair()
            if intent is None:
                return
            if self._environment_conflict(intent):
                invalidate = self._invalidate
            else:
                invalidate = self._submit(
                    intent,
                    ACTIVATION_QUEUED_MESSAGE,
                )
        invalidate()

    def refresh_account(self) -> None:
        """Queue one account refresh without blocking input."""
        with self._view_lock:
            if self._view.action_in_flight:
                return
            intent = self._controller().refresh_account()
            if intent is None:
                return
            invalidate = self._submit(intent, REFRESH_QUEUED_MESSAGE)
        invalidate()

    def refresh_due_accounts(self) -> None:
        """Queue one global maintenance request without blocking input."""
        with self._view_lock:
            if self._view.action_in_flight:
                return
            intent = self._controller().refresh_due_accounts()
            if intent is None:
                return
            invalidate = self._submit(intent, REFRESH_ALL_QUEUED_MESSAGE)
        invalidate()

    def toggle_help(self) -> None:
        """Toggle bounded keyboard guidance."""
        with self._view_lock:
            controller = self._controller().toggle_help()
            self._set_controller(
                controller,
                self._navigation_footer(controller),
            )
            invalidate = self._invalidate
        invalidate()

    def confirm(self, approved: bool) -> None:
        """Resolve the currently displayed typed confirmation once."""
        with self._view_lock:
            if self._view.confirmation is None:
                return
            decision = (
                ServiceSetupDecision.APPROVED
                if approved
                else ServiceSetupDecision.REFUSED
            )
            try:
                self._decisions.put_nowait(decision)
            except Full:
                return
            self._view = replace(
                self._view,
                confirmation=None,
                footer=self._progress_footer(
                    "Continuing account action."
                    if approved
                    else "Cancelling account action."
                ),
            )
            invalidate = self._invalidate
        invalidate()

    def _controller(self) -> DashboardController:
        return DashboardController(
            snapshot=self._view.snapshot,
            state=self._view.controller,
        )

    def _set_controller(
        self,
        controller: DashboardController,
        footer: DashboardFooter,
        *,
        action_in_flight: bool | None = None,
        activation_in_flight: bool | None = None,
        clear_confirmation: bool = False,
    ) -> None:
        current = self._view
        self._view = DashboardSessionView(
            snapshot=controller.snapshot,
            controller=controller.state,
            footer=footer,
            action_in_flight=(
                current.action_in_flight
                if action_in_flight is None
                else action_in_flight
            ),
            activation_in_flight=(
                current.activation_in_flight
                if activation_in_flight is None
                else activation_in_flight
            ),
            confirmation=(
                None if clear_confirmation else current.confirmation
            ),
        )

    def _submit(
        self,
        intent: DashboardIntent,
        message: str,
    ) -> Callable[[], None]:
        request = DashboardActionRequest(intent=intent)
        try:
            self._actions.put_nowait(request)
        except Full:
            return _discard_invalidation
        self._view = replace(
            self._view,
            footer=self._progress_footer(message),
            action_in_flight=True,
            activation_in_flight=isinstance(
                intent,
                ActivateOrRepairIntent,
            ),
        )
        return self._invalidate

    def _environment_conflict(
        self,
        intent: ActivateOrRepairIntent,
    ) -> bool:
        if intent.provider_id is not ProviderId.CLAUDE:
            return False
        conflict = self._claude_environment_conflict
        if conflict is None:
            return False
        keys = " ".join(claude_environment_conflict_keys(conflict))
        self._view = replace(
            self._view,
            footer=self._error_footer(
                "This shell overrides Claude account selection. "
                f"Run: unset {keys}"
            ),
        )
        return True

    def _run_lookup(self) -> None:
        self._lookup_notice(LOOKUP_STARTED_MESSAGE)
        try:
            result = self._lookup.run(self._observe_lookup)
        except OSError:
            if self._stopping.is_set():
                return
            self._publish_lookup_snapshot(
                LOOKUP_FAILED_MESSAGE,
                failed=True,
            )
            return
        if self._stopping.is_set():
            return
        self._publish_lookup_snapshot(
            (
                LOOKUP_PROGRESS_MESSAGE
                if result.succeeded
                else LOOKUP_FAILED_MESSAGE
            ),
            failed=not result.succeeded,
        )

    def _observe_lookup(self, event: UsageLookupWorkerEvent) -> None:
        if (
            self._stopping.is_set()
            or event.kind is not UsageLookupEventKind.ACCOUNT_COMPLETED
        ):
            return
        self._publish_lookup_snapshot(LOOKUP_PROGRESS_MESSAGE)

    def _lookup_notice(self, message: str, *, failed: bool = False) -> None:
        with self._view_lock:
            if self._closed or self._view.action_in_flight:
                return
            self._view = replace(
                self._view,
                footer=(
                    self._error_footer(message)
                    if failed
                    else self._progress_footer(message)
                ),
            )
            invalidate = self._invalidate
        invalidate()

    def _publish_lookup_snapshot(
        self,
        message: str,
        failed: bool = False,
    ) -> None:
        with (
            self._serialized_snapshot() as snapshot,
            self._view_lock,
        ):
            if self._closed:
                return
            controller = self._controller()
            if snapshot is not None:
                controller = controller.rebase(snapshot)
            footer = self._view.footer
            if not self._view.action_in_flight:
                footer = (
                    self._error_footer(message)
                    if failed or snapshot is None
                    else self._progress_footer(message)
                )
            self._set_controller(
                controller,
                footer,
            )
            invalidate = self._invalidate
        invalidate()

    @contextmanager
    def _serialized_snapshot(
        self,
    ) -> Iterator[DashboardSnapshot | None]:
        """Serialize cache read and publication without blocking input."""
        with self._snapshot_lock:
            yield self._load_snapshot()

    def _load_snapshot(self) -> DashboardSnapshot | None:
        try:
            return self._snapshots.load(self._only)
        except PersistenceError:
            return None

    def _run_actions(self) -> None:
        while not self._stopping.is_set():
            request = self._actions.get()
            if request is None or self._stopping.is_set():
                return
            self._action_executor.execute(request)

    def action_completed(
        self,
        intent: DashboardIntent,
        terminal: ControlActionTerminalPayload,
    ) -> None:
        if self._stopping.is_set():
            return
        if (
            not isinstance(terminal, CompletedPayload)
            or terminal.outcome is CompletionOutcome.CANCELLED
        ):
            self.action_failed(intent)
            return
        with self._serialized_snapshot() as snapshot:
            if snapshot is None:
                invalidate = self._action_error_transition(
                    intent,
                    CACHE_RELOAD_ERROR_MESSAGE,
                    None,
                )
            else:
                failed = False
                with self._view_lock:
                    if self._closed:
                        return
                    controller = self._controller().rebase(snapshot)
                    if isinstance(intent, ActivateOrRepairIntent):
                        try:
                            controller = controller.activation_succeeded(
                                DashboardActivationProof(
                                    provider_id=intent.provider_id,
                                    account_id=intent.account_id,
                                )
                            )
                        except ValueError:
                            controller = self._controller().rebase(
                                snapshot,
                                restore_provider=intent.provider_id,
                            )
                            failed = True
                    footer = (
                        self._error_footer(
                            self._action_failure_message(intent)
                        )
                        if failed
                        else self._idle_footer(controller)
                    )
                    self._set_controller(
                        controller,
                        footer,
                        action_in_flight=False,
                        activation_in_flight=False,
                        clear_confirmation=True,
                    )
                    invalidate = self._invalidate
        invalidate()

    def action_failed(self, intent: DashboardIntent) -> None:
        if self._stopping.is_set():
            return
        self.action_error(intent, self._action_failure_message(intent))

    def action_error(
        self,
        intent: DashboardIntent,
        message: str,
    ) -> None:
        if self._stopping.is_set():
            return
        with self._serialized_snapshot() as snapshot:
            invalidate = self._action_error_transition(
                intent,
                message,
                snapshot,
            )
        invalidate()

    @staticmethod
    def _action_failure_message(intent: DashboardIntent) -> str:
        provider_id = (
            intent.provider_id
            if isinstance(
                intent,
                ActivateOrRepairIntent | RefreshAccountIntent,
            )
            else None
        )
        return (
            "Account action failed. Run sidekick-usages doctor"
            if provider_id is None
            else (
                "Account action failed. Run sidekick-usages doctor "
                f"--provider {provider_id.value}"
            )
        )

    def _action_error_transition(
        self,
        intent: DashboardIntent,
        message: str,
        snapshot: DashboardSnapshot | None,
    ) -> Callable[[], None]:
        with self._view_lock:
            if self._closed:
                return _discard_invalidation
            controller = self._controller()
            controller = controller.rebase(
                controller.snapshot if snapshot is None else snapshot,
                restore_provider=(
                    intent.provider_id
                    if isinstance(intent, ActivateOrRepairIntent)
                    else None
                ),
            )
            self._set_controller(
                controller,
                self._error_footer(message),
                action_in_flight=False,
                activation_in_flight=False,
                clear_confirmation=True,
            )
            return self._invalidate

    def request_confirmation(
        self,
        kind: DashboardConfirmationKind,
        message: str,
    ) -> ServiceSetupDecision:
        with self._view_lock:
            if self._closed:
                return ServiceSetupDecision.REFUSED
            self._view = replace(
                self._view,
                footer=DashboardFooter(
                    kind=DashboardFooterKind.CONFIRMATION,
                    message=message,
                ),
                confirmation=DashboardConfirmation(kind=kind),
            )
            invalidate = self._invalidate
        invalidate()
        return self._decisions.get()

    def publish_progress(self, message: str) -> None:
        with self._view_lock:
            if self._closed:
                return
            self._view = replace(
                self._view,
                footer=self._progress_footer(message),
            )
            invalidate = self._invalidate
        invalidate()

    def _navigation_footer(
        self,
        controller: DashboardController,
    ) -> DashboardFooter:
        if self._view.action_in_flight:
            return self._view.footer
        return self._idle_footer(controller)

    @staticmethod
    def _idle_footer(
        controller: DashboardController,
    ) -> DashboardFooter:
        return DashboardFooter(
            kind=(
                DashboardFooterKind.HELP
                if controller.state.help_visible
                else DashboardFooterKind.KEYS
            )
        )

    @staticmethod
    def _progress_footer(message: str) -> DashboardFooter:
        return DashboardFooter(
            kind=DashboardFooterKind.PROGRESS,
            message=message,
        )

    @staticmethod
    def _error_footer(message: str) -> DashboardFooter:
        return DashboardFooter(
            kind=DashboardFooterKind.ERROR,
            message=message,
        )


def dashboard_cursor(view: DashboardSessionView) -> DashboardCursor:
    """Project one atomic session view to the shared renderer cursor."""
    state = view.controller
    return DashboardCursor(
        focused_provider=state.focused_provider,
        account_id=state.account_id,
        external=state.external,
    )
