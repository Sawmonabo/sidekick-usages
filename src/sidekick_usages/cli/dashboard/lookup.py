"""Serialized live-lookup lifecycle for one interactive dashboard."""

from dataclasses import dataclass, replace
from threading import Event, Lock, Thread
from typing import Protocol

from sidekick_usages.cli.dashboard.ports import (
    DashboardLookupWorker,
    DashboardSnapshotSource,
)
from sidekick_usages.core.accounts.types import (
    MetricsFreshness,
    SidekickAccountId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardRow,
    DashboardSnapshot,
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
    usage_lookup_failure_is_recoverable,
)

DASHBOARD_LOOKUP_THREAD_NAME = "sidekick-dashboard-lookup"


class DashboardLookupSink(Protocol):
    """Publish lookup results without owning interactive presentation."""

    def publish_lookup_snapshot(
        self,
        snapshot: DashboardSnapshot,
    ) -> bool:
        """Publish one resolved snapshot and report whether it was accepted."""
        ...

    def publish_lookup_failure(
        self,
        *,
        diagnostic_unavailable: bool = False,
    ) -> None:
        """Publish one terminal lookup failure."""
        ...


@dataclass(frozen=True, slots=True)
class _DashboardLookupOverlay:
    """Immutable account outcomes applied to later cached snapshots."""

    outcomes: tuple[UsageLookupWorkerEvent, ...] = ()
    terminal_succeeded: bool = False


class DashboardLookupCoordinator:
    """Own lookup execution, retry diagnostics, and immutable overlays."""

    def __init__(
        self,
        *,
        snapshots: DashboardSnapshotSource,
        only: ProviderId | None,
        worker: DashboardLookupWorker,
        metrics_refresh: MetricsRefreshObservationSink,
        snapshot_lock: Lock,
        sink: DashboardLookupSink,
    ) -> None:
        self._snapshots = snapshots
        self._only = only
        self._worker = worker
        self._metrics_refresh = metrics_refresh
        self._snapshot_lock = snapshot_lock
        self._sink = sink
        self._state_lock = Lock()
        self._stopping = Event()
        self._overlay = _DashboardLookupOverlay()
        self._thread: Thread | None = None
        self._started = False
        self._closed = False

    def start(self) -> None:
        """Start exactly one isolated lookup owner."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("The dashboard lookup is closed.")
            if self._started:
                return
            self._started = True
            self._thread = Thread(
                target=self._run_lookup,
                name=DASHBOARD_LOOKUP_THREAD_NAME,
            )
            thread = self._thread
        try:
            thread.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Cancel and join the lookup owner exactly once."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._stopping.set()
            thread = self._thread
        self._worker.cancel()
        self._join_owner(thread)

    def apply(self, snapshot: DashboardSnapshot) -> DashboardSnapshot:
        """Apply the latest immutable lookup outcomes to cached state."""
        with self._state_lock:
            overlay = self._overlay
        if not overlay.outcomes:
            return snapshot
        outcomes = {
            event.account_id: event
            for event in overlay.outcomes
            if event.account_id is not None
        }
        return replace(
            snapshot,
            providers=tuple(
                replace(
                    provider,
                    rows=tuple(
                        self._overlay_row(row, outcomes, overlay)
                        for row in provider.rows
                    ),
                )
                for provider in snapshot.providers
            ),
        )

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

    def _run_lookup(self) -> None:
        metrics_refresh = MetricsRefreshTracker(self._metrics_refresh)
        result = self._run_lookup_attempt()
        if (
            not self._stopping.is_set()
            and result.failure is not None
            and usage_lookup_failure_is_recoverable(result.failure)
        ):
            metrics_refresh.retry_worker(
                result.failure,
                self._account_events(),
            )
            with self._state_lock:
                self._overlay = _DashboardLookupOverlay()
            result = self._run_lookup_attempt()
        if self._stopping.is_set():
            return
        if not result.succeeded:
            failure = result.failure
            if failure is None:
                raise AssertionError("Failed lookup has no terminal cause.")
            self._sink.publish_lookup_failure(
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
            return self._worker.run(self._observe_lookup)
        except OSError:
            return UsageLookupWorkerResult(
                (),
                UsageLookupFailure.LAUNCH_FAILED,
            )

    def _observe_lookup(self, event: UsageLookupWorkerEvent) -> None:
        if self._stopping.is_set() or not event.kind.is_account_completion:
            return
        if event.account_id is None:
            return
        with self._state_lock:
            if self._closed:
                return
            retained = tuple(
                outcome
                for outcome in self._overlay.outcomes
                if outcome.account_id != event.account_id
            )
            self._overlay = replace(
                self._overlay,
                outcomes=(*retained, event),
            )

    def _publish_successful_lookup(
        self,
        metrics_refresh: MetricsRefreshTracker,
    ) -> None:
        with self._snapshot_lock:
            resolved_snapshot, snapshot_failure = (
                self._load_lookup_snapshot()
            )
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
                retried_snapshot, retry_failure = (
                    self._load_lookup_snapshot()
                )
                if retried_snapshot is not None:
                    resolved_snapshot = retried_snapshot
                else:
                    snapshot_failure = retry_failure
        if resolved_snapshot is None:
            if snapshot_failure is None:
                raise AssertionError("Missing snapshot has no failure cause.")
            self._sink.publish_lookup_failure(
                diagnostic_unavailable=(
                    metrics_refresh.record_snapshot_failure(
                        snapshot_failure,
                        self._account_events(),
                    )
                )
            )
            return
        with self._state_lock:
            self._overlay = replace(
                self._overlay,
                terminal_succeeded=True,
            )
        outcome_snapshot = self.apply(resolved_snapshot)
        account_events = self._account_events()
        if not self._sink.publish_lookup_snapshot(outcome_snapshot):
            return
        if outcome_snapshot.all_saved_metrics_unavailable:
            self._sink.publish_lookup_failure(
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

    def _account_events(self) -> tuple[UsageLookupWorkerEvent, ...]:
        with self._state_lock:
            return self._overlay.outcomes

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

    @staticmethod
    def _overlay_row(
        row: DashboardRow,
        outcomes: dict[SidekickAccountId, UsageLookupWorkerEvent],
        overlay: _DashboardLookupOverlay,
    ) -> DashboardRow:
        if not isinstance(row, DashboardAccount):
            return row
        event = outcomes.get(row.account_id)
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
            or not overlay.terminal_succeeded
            or not has_observation
        ):
            return row
        else:
            freshness = MetricsFreshness.FRESH
        return replace(row, metrics_freshness=freshness)
