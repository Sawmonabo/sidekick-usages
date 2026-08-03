"""Two-owner orchestration for one interactive dashboard process."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import replace
from pathlib import Path
from queue import Full, Queue
from threading import Event, Lock, Thread

from sidekick_usages.cli.dashboard.actions import DashboardActionExecutor
from sidekick_usages.cli.dashboard.controller import DashboardController
from sidekick_usages.cli.dashboard.lookup import DashboardLookupCoordinator
from sidekick_usages.cli.dashboard.models.controller import (
    DashboardIntent,
    DashboardMove,
    DashboardSelectionProof,
    DashboardSelectionRefusal,
    RefreshAccountIntent,
    SelectAccountIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardActionRequest,
    DashboardConfirmation,
    DashboardConfirmationKind,
    DashboardSessionView,
    DashboardStartupReconciliation,
    DashboardStartupReconciliationState,
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
from sidekick_usages.core.selection.models import SelectionResult
from sidekick_usages.core.selection.types import (
    SelectionCode,
    SelectionOutcome,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import (
    CompletedPayload,
    ControlActionTerminalPayload,
    FailedPayload,
)
from sidekick_usages.daemon.selection.models import SelectionStatus
from sidekick_usages.daemon.types.protocol import CompletionOutcome
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.usage.dashboard.models import (
    DashboardCursor,
    DashboardFooter,
    DashboardNavigationKind,
    DashboardService,
    DashboardSnapshot,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.lookup.diagnostics.ports import (
    MetricsRefreshObservationSink,
)

ACTION_QUEUE_CAPACITY = 1
DECISION_QUEUE_CAPACITY = 1
DASHBOARD_ACTION_THREAD_NAME = "sidekick-dashboard-actions"
SELECTION_QUEUED_MESSAGE = "Preparing account change…"
REFRESH_QUEUED_MESSAGE = "Account refresh queued."
REFRESH_ALL_QUEUED_MESSAGE = "Due-account refresh queued."
LOOKUP_FAILED_MESSAGE = (
    "Live metrics are unavailable. Run: sidekick-usages doctor"
)
LOOKUP_DIAGNOSTIC_UNAVAILABLE_MESSAGE = (
    " Diagnostic details could not be saved."
)
CACHE_RELOAD_ERROR_MESSAGE = (
    "The action completed, but cached state could not be reloaded."
)
STARTUP_RECONCILIATION_RETRY_MESSAGE = (
    "{provider} account verification is unavailable; retrying."
)
STARTUP_RECONCILIATION_FAILED_MESSAGE = (
    "{provider} account verification is unavailable; cached selection "
    "remains. Restart the dashboard to retry."
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
        metrics_refresh: MetricsRefreshObservationSink,
        connector: DashboardControlConnector,
        socket_path: Path,
        setup: GuidedServiceSetup,
    ) -> None:
        controller = DashboardController.start(snapshot)
        self._view = DashboardSessionView(
            snapshot=snapshot,
            controller=controller.state,
            footer=DashboardFooter(),
        )
        self._snapshots = snapshots
        self._only = only
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
        self._action_thread: Thread | None = None
        self._startup_reconciliation_failures: set[ProviderId] = set()
        self._startup_status: DashboardStatus | None = None
        self._deferred_lookup_status: DashboardStatus | None = None
        self._started = False
        self._closed = False
        self._action_executor = DashboardActionExecutor(
            connector=connector,
            socket_path=socket_path,
            setup=setup,
            sink=self,
        )
        self._lookup_coordinator = DashboardLookupCoordinator(
            snapshots=snapshots,
            only=only,
            worker=lookup,
            metrics_refresh=metrics_refresh,
            snapshot_lock=self._snapshot_lock,
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
            self._action_thread = Thread(
                target=self._run_actions,
                name=DASHBOARD_ACTION_THREAD_NAME,
            )
            action_thread = self._action_thread
        try:
            self._lookup_coordinator.start()
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
            action_thread = self._action_thread
        self._lookup_coordinator.close()
        self._action_executor.close()
        with suppress(Full):
            self._decisions.put_nowait(ServiceSetupDecision.REFUSED)
        with suppress(Full):
            self._actions.put_nowait(None)
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
        self._navigate(lambda controller: controller.move(direction))

    def focus_next_provider(self) -> None:
        """Focus the next displayed provider at its verified anchor."""
        self._navigate(DashboardController.focus_next_provider)

    def restore(self) -> None:
        """Restore verified focus unless selection is in flight."""
        self._navigate(
            DashboardController.restore,
            blocked_by_selection=True,
        )

    def select_account(self) -> None:
        """Queue coordinated selection or publish one typed refusal."""
        with self._view_lock:
            if self._view.action_in_flight:
                return
            intent = self._controller().select_account()
            if intent is None:
                return
            if isinstance(intent, DashboardSelectionRefusal):
                self._view = replace(
                    self._view,
                    footer=self._status_footer(
                        DashboardStatusKind.ERROR,
                        _selection_refusal_message(intent),
                    ),
                )
                invalidate = self._invalidate
            else:
                invalidate = self._submit(intent, SELECTION_QUEUED_MESSAGE)
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
        self._navigate(DashboardController.toggle_help)

    def _navigate(
        self,
        transition: Callable[[DashboardController], DashboardController],
        *,
        blocked_by_selection: bool = False,
    ) -> None:
        with self._view_lock:
            if blocked_by_selection and self._view.selection_in_flight:
                return
            controller = transition(self._controller())
            self._view = replace(
                self._view,
                snapshot=controller.snapshot,
                controller=controller.state,
                footer=self._navigation_footer(controller),
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
                footer=self._status_footer(
                    DashboardStatusKind.PROGRESS,
                    "Continuing account action."
                    if approved
                    else "Cancelling account action.",
                ),
            )
            invalidate = self._invalidate
        invalidate()

    def _controller(self) -> DashboardController:
        return DashboardController(
            snapshot=self._view.snapshot,
            state=self._view.controller,
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
            footer=self._status_footer(
                DashboardStatusKind.PROGRESS,
                message,
            ),
            action_in_flight=True,
            selection_in_flight=isinstance(
                intent,
                SelectAccountIntent,
            ),
        )
        return self._invalidate

    def publish_lookup_snapshot(
        self,
        snapshot: DashboardSnapshot,
    ) -> bool:
        """Rebase and publish one resolved live-lookup snapshot."""
        with self._view_lock:
            if self._closed:
                return False
            controller = self._controller().rebase(snapshot)
            self._view = replace(
                self._view,
                snapshot=controller.snapshot,
                controller=controller.state,
            )
            invalidate = self._invalidate
        invalidate()
        return True

    def publish_lookup_failure(
        self,
        *,
        diagnostic_unavailable: bool = False,
    ) -> None:
        """Rebase lookup outcomes and publish one terminal failure."""
        with self._view_lock:
            if self._closed:
                return
            self._publish_lookup_failure_locked(
                diagnostic_unavailable=diagnostic_unavailable
            )
            invalidate = self._invalidate
        invalidate()

    def _publish_lookup_failure_locked(
        self,
        *,
        diagnostic_unavailable: bool = False,
    ) -> None:
        snapshot = self._lookup_coordinator.apply(self._view.snapshot)
        controller = self._controller().rebase(snapshot)
        self._view = replace(
            self._view,
            snapshot=controller.snapshot,
            controller=controller.state,
        )
        if not self._view.snapshot.all_saved_metrics_unavailable:
            self._deferred_lookup_status = None
            return
        message = LOOKUP_FAILED_MESSAGE
        if diagnostic_unavailable:
            message += LOOKUP_DIAGNOSTIC_UNAVAILABLE_MESSAGE
        status = DashboardStatus(
            kind=DashboardStatusKind.ERROR,
            message=message,
        )
        self._deferred_lookup_status = status
        owner_active = self._view.action_in_flight or self._startup_owns()
        if not owner_active and self._view.footer.status is None:
            self._view = replace(
                self._view,
                footer=replace(self._view.footer, status=status),
            )

    def _startup_owns(self) -> bool:
        return (
            bool(self._startup_reconciliation_failures)
            and self._view.footer.status == self._startup_status
        )

    @contextmanager
    def _serialized_snapshot(self) -> Iterator[DashboardSnapshot | None]:
        """Serialize cache read and publication without blocking input."""
        with self._snapshot_lock:
            yield self._load_snapshot()

    def _load_snapshot(self) -> DashboardSnapshot | None:
        try:
            return self._snapshots.load(self._only)
        except OSError, PersistenceError:
            return None

    def _run_actions(self) -> None:
        self._action_executor.reconcile_startup(
            tuple(ProviderId) if self._only is None else (self._only,)
        )
        while not self._stopping.is_set():
            request = self._actions.get()
            if request is None or self._stopping.is_set():
                return
            self._action_executor.execute(request)

    def startup_reconciled(
        self,
        result: DashboardStartupReconciliation,
    ) -> None:
        """Rebase and publish one provider's passive read-back result."""
        with (
            self._serialized_snapshot() as snapshot,
            self._view_lock,
        ):
            if self._closed:
                return
            controller = self._controller()
            if snapshot is not None:
                controller = controller.rebase(
                    self._lookup_coordinator.apply(snapshot),
                    restore_provider=(
                        result.provider_id
                        if result.state
                        is DashboardStartupReconciliationState.VERIFIED
                        else None
                    ),
                )
            footer = self._view.footer
            if result.state is DashboardStartupReconciliationState.VERIFIED:
                self._startup_reconciliation_failures.discard(
                    result.provider_id
                )
                if (
                    not self._startup_reconciliation_failures
                    and footer.status == self._startup_status
                ):
                    footer = self._navigation_footer(
                        controller,
                        reveal_lookup=True,
                    )
                if not self._startup_reconciliation_failures:
                    self._startup_status = None
            else:
                self._startup_reconciliation_failures.add(result.provider_id)
                message_template = (
                    STARTUP_RECONCILIATION_FAILED_MESSAGE
                    if result.state
                    is DashboardStartupReconciliationState.UNAVAILABLE
                    else STARTUP_RECONCILIATION_RETRY_MESSAGE
                )
                message = message_template.format(
                    provider=result.provider_id.value.title()
                )
                self._startup_status = DashboardStatus(
                    kind=(
                        DashboardStatusKind.ERROR
                        if result.state
                        is DashboardStartupReconciliationState.UNAVAILABLE
                        else DashboardStatusKind.PROGRESS
                    ),
                    message=message,
                )
                footer = replace(footer, status=self._startup_status)
            self._view = replace(
                self._view,
                snapshot=controller.snapshot,
                controller=controller.state,
                footer=footer,
            )
            invalidate = self._invalidate
        invalidate()

    def action_completed(
        self,
        intent: DashboardIntent,
        terminal: ControlActionTerminalPayload,
    ) -> None:
        if self._stopping.is_set():
            return
        selection_result = (
            terminal if isinstance(terminal, SelectionResult) else None
        )
        already_selected = (
            isinstance(terminal, FailedPayload)
            and terminal.code == SelectionCode.ALREADY_SELECTED.value
        )
        selection_terminal = selection_result is not None or already_selected
        if isinstance(intent, SelectAccountIntent) and isinstance(
            terminal,
            FailedPayload,
        ) and not already_selected:
            self.action_error(
                intent,
                _selection_code_message(terminal.code),
            )
            return
        if selection_result is not None and (
            selection_result.outcome is not SelectionOutcome.READY
        ):
            self.action_error(
                intent,
                _selection_code_message(selection_result.safe_code.value),
            )
            return
        if not selection_terminal and (
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
                    outcome_snapshot = self._lookup_coordinator.apply(snapshot)
                    controller = self._controller().rebase(outcome_snapshot)
                    if isinstance(intent, SelectAccountIntent):
                        try:
                            controller = controller.selection_succeeded(
                                DashboardSelectionProof(
                                    provider_id=intent.provider_id,
                                    account_id=intent.account_id,
                                )
                            )
                        except ValueError:
                            controller = self._controller().rebase(
                                outcome_snapshot,
                                restore_provider=intent.provider_id,
                            )
                            failed = True
                    if failed:
                        self._deferred_lookup_status = None
                        footer = self._status_footer(
                            DashboardStatusKind.ERROR,
                            self._action_failure_message(intent),
                        )
                    else:
                        footer = (
                            self._status_footer(
                                DashboardStatusKind.PROGRESS,
                                _selection_ready_message(selection_result),
                            )
                            if selection_terminal
                            else self._navigation_footer(
                                controller,
                                reveal_lookup=True,
                            )
                        )
                    self._view = replace(
                        self._view,
                        snapshot=controller.snapshot,
                        controller=controller.state,
                        footer=footer,
                        action_in_flight=False,
                        selection_in_flight=False,
                        confirmation=None,
                    )
                    invalidate = self._invalidate
        invalidate()

    def action_failed(self, intent: DashboardIntent) -> None:
        self.action_error(intent, self._action_failure_message(intent))

    def action_error(self, intent: DashboardIntent, message: str) -> None:
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
        message = "Account action failed. Run sidekick-usages doctor"
        if isinstance(intent, SelectAccountIntent | RefreshAccountIntent):
            return f"{message} --provider {intent.provider_id.value}"
        return message

    def _action_error_transition(
        self,
        intent: DashboardIntent,
        message: str,
        snapshot: DashboardSnapshot | None,
    ) -> Callable[[], None]:
        with self._view_lock:
            if self._closed:
                return _discard_invalidation
            self._deferred_lookup_status = None
            controller = self._controller()
            source = (
                controller.snapshot
                if snapshot is None
                else self._lookup_coordinator.apply(snapshot)
            )
            restore_provider = (
                intent.provider_id
                if isinstance(intent, SelectAccountIntent)
                else None
            )
            controller = controller.rebase(
                source,
                restore_provider=restore_provider,
            )
            self._view = replace(
                self._view,
                snapshot=controller.snapshot,
                controller=controller.state,
                footer=self._status_footer(
                    DashboardStatusKind.ERROR,
                    message,
                ),
                action_in_flight=False,
                selection_in_flight=False,
                confirmation=None,
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
                footer=self._status_footer(
                    DashboardStatusKind.CONFIRMATION,
                    message,
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
                footer=self._status_footer(
                    DashboardStatusKind.PROGRESS,
                    message,
                ),
            )
            invalidate = self._invalidate
        invalidate()

    def publish_selection_status(
        self,
        provider_id: ProviderId,
        status: SelectionStatus | None,
    ) -> None:
        """Publish one canonical provider status without persisting it."""
        if status is not None and status.provider_id is not provider_id:
            raise ValueError("Dashboard selection provider does not match.")
        with self._view_lock:
            if self._closed:
                return
            snapshot = replace(
                self._view.snapshot,
                providers=tuple(
                    replace(
                        provider,
                        finalized_epoch=(
                            provider.finalized_epoch
                            if status is None
                            else status.finalized_epoch
                        ),
                        selection=status,
                    )
                    if provider.provider_id is provider_id
                    else provider
                    for provider in self._view.snapshot.providers
                ),
            )
            self._view = replace(
                self._view,
                snapshot=snapshot,
            )
            invalidate = self._invalidate
        invalidate()

    def _navigation_footer(
        self,
        controller: DashboardController,
        *,
        reveal_lookup: bool = False,
    ) -> DashboardFooter:
        if reveal_lookup:
            status = self._deferred_lookup_status
            self._deferred_lookup_status = None
        elif self._view.action_in_flight or self._startup_owns():
            status = self._view.footer.status
        else:
            status = None
            self._deferred_lookup_status = None
        navigation = DashboardNavigationKind.KEYS
        if controller.state.help_visible:
            navigation = DashboardNavigationKind.HELP
        return DashboardFooter(navigation=navigation, status=status)

    def _status_footer(
        self,
        kind: DashboardStatusKind,
        message: str,
    ) -> DashboardFooter:
        status = DashboardStatus(kind=kind, message=message)
        return replace(self._view.footer, status=status)


def dashboard_cursor(view: DashboardSessionView) -> DashboardCursor:
    """Project one atomic session view to the shared renderer cursor."""
    state = view.controller
    return DashboardCursor(
        focused_provider=state.focused_provider,
        account_id=state.account_id,
    )


def _selection_refusal_message(intent: DashboardSelectionRefusal) -> str:
    """Render one sanitized refusal without hiding the focused account."""
    return f"Saved account selection is unavailable: {intent.code.value}."


def _selection_code_message(code: str) -> str:
    """Render one sanitized coordinator refusal code visibly."""
    if code == "already_selected":
        return "This saved account is already selected."
    return f"Saved account selection is unavailable: {code}."


def _selection_ready_message(result: SelectionResult | None) -> str:
    """Render truthful participant readiness without claiming adoption."""
    if result is None:
        return _selection_code_message(
            SelectionCode.ALREADY_SELECTED.value
        )
    if result.ready_count == 0:
        return "Account ready; next requests use it."
    suffix = "session" if result.ready_count == 1 else "sessions"
    return (
        f"Account ready in {result.ready_count} {suffix}; "
        "next requests use it."
    )
