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
    ClaudeAssociationRequest,
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
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
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
    DashboardAccount,
    DashboardCursor,
    DashboardFooter,
    DashboardNavigationKind,
    DashboardRow,
    DashboardService,
    DashboardSnapshot,
    DashboardStatus,
    DashboardStatusKind,
)
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshFailureCode,
    MetricsRefreshSnapshotCode,
)
from sidekick_usages.usage.lookup.diagnostics.ports import (
    MetricsRefreshObservationSink,
)
from sidekick_usages.usage.lookup.diagnostics.tracker import (
    MetricsRefreshTracker,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupFailure,
    UsageLookupWorkerEvent,
    UsageLookupWorkerResult,
)

ACTION_QUEUE_CAPACITY = 1
DECISION_QUEUE_CAPACITY = 1
DASHBOARD_LOOKUP_THREAD_NAME = "sidekick-dashboard-lookup"
DASHBOARD_ACTION_THREAD_NAME = "sidekick-dashboard-actions"
ACTIVATION_QUEUED_MESSAGE = "Account change queued."
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
        self._metrics_refresh = metrics_refresh
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
        self._startup_reconciliation_failures: set[ProviderId] = set()
        self._startup_status: DashboardStatus | None = None
        self._outcomes: dict[
            SidekickAccountId,
            UsageLookupWorkerEvent,
        ] = {}
        self._lookup_terminal_succeeded = False
        self._deferred_lookup_status: DashboardStatus | None = None
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
                name=DASHBOARD_LOOKUP_THREAD_NAME,
            )
            self._action_thread = Thread(
                target=self._run_actions,
                name=DASHBOARD_ACTION_THREAD_NAME,
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
        self._navigate(lambda controller: controller.move(direction))

    def focus_next_provider(self) -> None:
        """Focus the next displayed provider at its verified anchor."""
        self._navigate(DashboardController.focus_next_provider)

    def restore(self) -> None:
        """Restore verified focus unless activation is in flight."""
        self._navigate(
            DashboardController.restore,
            blocked_by_activation=True,
        )

    def activate(self) -> ClaudeAssociationRequest | None:
        """Return association work or queue ordinary activation."""
        with self._view_lock:
            if self._view.action_in_flight:
                return None
            intent = self._controller().activate_or_repair()
            if intent is None:
                return None
            if isinstance(intent, ClaudeAssociationRequest):
                return intent
            if self._environment_conflict(intent):
                invalidate = self._invalidate
            else:
                invalidate = self._submit(intent, ACTIVATION_QUEUED_MESSAGE)
        invalidate()
        return None

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
        blocked_by_activation: bool = False,
    ) -> None:
        with self._view_lock:
            if blocked_by_activation and self._view.activation_in_flight:
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
            activation_in_flight=isinstance(
                intent,
                ActivateOrRepairIntent,
            ),
        )
        return self._invalidate

    def _environment_conflict(self, intent: ActivateOrRepairIntent) -> bool:
        if intent.provider_id is not ProviderId.CLAUDE:
            return False
        conflict = self._claude_environment_conflict
        if conflict is None:
            return False
        self._deferred_lookup_status = None
        keys = " ".join(claude_environment_conflict_keys(conflict))
        self._view = replace(
            self._view,
            footer=self._status_footer(
                DashboardStatusKind.ERROR,
                "This shell overrides Claude account selection. "
                f"Run: unset {keys}",
            ),
        )
        return True

    def _run_lookup(self) -> None:
        metrics_refresh = MetricsRefreshTracker(self._metrics_refresh)
        result = self._run_lookup_attempt()
        if (
            not self._stopping.is_set()
            and result.failure is not None
            and result.failure.recoverable
        ):
            metrics_refresh.retry_worker(
                result.failure,
                self._account_events(),
            )
            with self._view_lock:
                self._outcomes.clear()
            result = self._run_lookup_attempt()
        if self._stopping.is_set():
            return
        if not result.succeeded:
            failure = result.failure
            if failure is None:
                raise AssertionError("Failed lookup has no terminal cause.")
            self._publish_lookup_failure(
                diagnostic_unavailable=(
                    metrics_refresh.record_worker_failure(
                        failure,
                        self._account_events(),
                    )
                )
            )
            return
        self._publish_successful_lookup(metrics_refresh)

    def _run_lookup_attempt(self) -> UsageLookupWorkerResult:
        try:
            return self._lookup.run(self._observe_lookup)
        except OSError:
            return UsageLookupWorkerResult(
                (),
                UsageLookupFailure.LAUNCH_FAILED,
            )

    def _observe_lookup(self, event: UsageLookupWorkerEvent) -> None:
        if self._stopping.is_set() or not event.kind.is_account_completion:
            return
        with self._view_lock:
            if self._closed or event.account_id is None:
                return
            self._outcomes[event.account_id] = event

    def _publish_successful_lookup(
        self,
        metrics_refresh: MetricsRefreshTracker,
    ) -> None:
        with self._snapshot_lock:
            resolved_snapshot, snapshot_failure = self._load_lookup_snapshot()
            if snapshot_failure is not None and metrics_refresh.retry_snapshot(
                snapshot_failure
            ):
                resolved_snapshot, snapshot_failure = (
                    self._load_lookup_snapshot()
                )
            if (
                resolved_snapshot is not None
                and metrics_refresh.retry_cache_read(
                    usage_cache_issue=resolved_snapshot.usage_cache_issue,
                    activity_cache_issue=(
                        resolved_snapshot.activity_cache_issue
                    ),
                )
            ):
                retried_snapshot, retry_failure = self._load_lookup_snapshot()
                if retried_snapshot is not None:
                    resolved_snapshot = retried_snapshot
                else:
                    snapshot_failure = retry_failure
        if resolved_snapshot is None:
            if snapshot_failure is None:
                raise AssertionError("Missing snapshot has no failure cause.")
            self._publish_lookup_failure(
                diagnostic_unavailable=(
                    metrics_refresh.record_snapshot_failure(
                        snapshot_failure, self._account_events()
                    )
                )
            )
            return
        with self._view_lock:
            if self._closed:
                return
            account_events = tuple(self._outcomes.values())
            self._lookup_terminal_succeeded = True
            outcome_snapshot = self._outcome_view(resolved_snapshot)
            controller = self._controller().rebase(outcome_snapshot)
            self._view = replace(
                self._view,
                snapshot=controller.snapshot,
                controller=controller.state,
            )
            all_metrics_unavailable = (
                outcome_snapshot.all_saved_metrics_unavailable
            )
            invalidate = self._invalidate
        invalidate()
        if all_metrics_unavailable:
            self._publish_lookup_failure(
                diagnostic_unavailable=metrics_refresh.record_result(
                    usage_cache_issue=outcome_snapshot.usage_cache_issue,
                    activity_cache_issue=(
                        outcome_snapshot.activity_cache_issue
                    ),
                    account_events=account_events,
                    snapshot_failure=snapshot_failure,
                )
            )
            return
        metrics_refresh.record_result(
            usage_cache_issue=outcome_snapshot.usage_cache_issue,
            activity_cache_issue=outcome_snapshot.activity_cache_issue,
            account_events=account_events,
            snapshot_failure=snapshot_failure,
        )

    def _publish_lookup_failure(
        self,
        *,
        diagnostic_unavailable: bool = False,
    ) -> None:
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
        snapshot = self._outcome_view(self._view.snapshot)
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

    def _outcome_view(self, snapshot: DashboardSnapshot) -> DashboardSnapshot:
        if not self._outcomes:
            return snapshot
        return replace(
            snapshot,
            providers=tuple(
                replace(
                    provider,
                    rows=tuple(
                        self._overlay_lookup_row(row) for row in provider.rows
                    ),
                )
                for provider in snapshot.providers
            ),
        )

    def _overlay_lookup_row(self, row: DashboardRow) -> DashboardRow:
        if not isinstance(row, DashboardAccount):
            return row
        event = self._outcomes.get(row.account_id)
        has_observation = row.usage is not None or row.activity is not None
        if (
            event is not None
            and event.kind is UsageLookupEventKind.ACCOUNT_FAILED
        ):
            freshness = (
                MetricsFreshness.STALE
                if has_observation
                else MetricsFreshness.UNAVAILABLE
            )
        elif (
            event is None
            or event.kind is not UsageLookupEventKind.ACCOUNT_SUCCEEDED
            or not self._lookup_terminal_succeeded
            or not has_observation
        ):
            return row
        else:
            freshness = MetricsFreshness.FRESH
        return replace(row, metrics_freshness=freshness)

    def _account_events(self) -> tuple[UsageLookupWorkerEvent, ...]:
        with self._view_lock:
            return tuple(self._outcomes.values())

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

    def _load_lookup_snapshot(
        self,
    ) -> tuple[
        DashboardSnapshot | None,
        MetricsRefreshSnapshotCode | None,
    ]:
        try:
            return self._snapshots.load(self._only), None
        except OSError:
            return None, MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
        except PersistenceError as error:
            return None, error.code

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
                controller = controller.rebase(self._outcome_view(snapshot))
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
                    outcome_snapshot = self._outcome_view(snapshot)
                    controller = self._controller().rebase(outcome_snapshot)
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
                                outcome_snapshot,
                                restore_provider=intent.provider_id,
                            )
                            failed = True
                    if failed:
                        self._deferred_lookup_status = None
                    footer = (
                        self._status_footer(
                            DashboardStatusKind.ERROR,
                            self._action_failure_message(intent),
                        )
                        if failed
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
                        activation_in_flight=False,
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
        if isinstance(intent, ActivateOrRepairIntent | RefreshAccountIntent):
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
                else self._outcome_view(snapshot)
            )
            restore_provider = (
                intent.provider_id
                if isinstance(intent, ActivateOrRepairIntent)
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
                activation_in_flight=False,
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
        external=state.external,
    )
