"""Classify and record one dashboard metrics-refresh attempt."""

from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    UsageSnapshotFailureKind,
)
from sidekick_usages.usage.lookup.diagnostics.models import (
    MAX_METRICS_REFRESH_ATTEMPTS,
    RECOVERABLE_SNAPSHOT_PERSISTENCE_CODES,
    MetricsRefreshCause,
    MetricsRefreshFailureCode,
    MetricsRefreshOutcome,
    MetricsRefreshSnapshotCode,
    MetricsRefreshStage,
    MetricsRefreshWriteState,
    canonical_metrics_refresh_causes,
)
from sidekick_usages.usage.lookup.diagnostics.ports import (
    MetricsRefreshObservationSink,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupFailure,
    UsageLookupWorkerEvent,
)

_ACTIVITY_CACHE_FAILURE_CODES = {
    ActivitySnapshotFailureKind.READ: (
        MetricsRefreshFailureCode.ACTIVITY_READ
    ),
    ActivitySnapshotFailureKind.MALFORMED: (
        MetricsRefreshFailureCode.ACTIVITY_MALFORMED
    ),
}
_USAGE_CACHE_FAILURE_CODES = {
    UsageSnapshotFailureKind.READ: MetricsRefreshFailureCode.USAGE_READ,
    UsageSnapshotFailureKind.MALFORMED: (
        MetricsRefreshFailureCode.USAGE_MALFORMED
    ),
}


class MetricsRefreshTracker:
    """Own one shared retry budget and terminal diagnostic outcome."""

    def __init__(self, sink: MetricsRefreshObservationSink) -> None:
        self._sink = sink
        self._attempts = 1
        self._retry_causes: tuple[MetricsRefreshCause, ...] = ()

    @property
    def retry_available(self) -> bool:
        """Return whether the single refresh retry remains unused."""
        return self._attempts < MAX_METRICS_REFRESH_ATTEMPTS

    def retry_worker(
        self,
        failure: UsageLookupFailure,
        account_events: tuple[UsageLookupWorkerEvent, ...],
    ) -> None:
        """Spend the single retry on a proven recoverable worker failure."""
        if not failure.recoverable:
            raise ValueError("Lookup worker failure is not recoverable.")
        self._consume_retry(
            (
                *_account_causes(account_events),
                MetricsRefreshCause(
                    stage=MetricsRefreshStage.WORKER,
                    code=failure,
                ),
            )
        )

    def retry_snapshot(self, code: MetricsRefreshSnapshotCode) -> bool:
        """Spend the single retry on a recoverable snapshot-load failure."""
        if (
            code is not MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
            and code not in RECOVERABLE_SNAPSHOT_PERSISTENCE_CODES
        ):
            return False
        if not self.retry_available:
            return False
        self._consume_retry((_snapshot_cause(code),))
        return True

    def retry_cache_read(
        self,
        *,
        usage_cache_issue: UsageSnapshotFailureKind | None,
        activity_cache_issue: ActivitySnapshotFailureKind | None,
    ) -> bool:
        """Spend one retry on all returned cache-read failures."""
        retry_causes = _cache_causes(
            usage_cache_issue,
            activity_cache_issue,
        )
        retryable = any(
            cause.code
            in {
                MetricsRefreshFailureCode.USAGE_READ,
                MetricsRefreshFailureCode.ACTIVITY_READ,
            }
            for cause in retry_causes
        )
        if not retryable or not self.retry_available:
            return False
        self._consume_retry(retry_causes)
        return True

    def record_worker_failure(
        self,
        failure: UsageLookupFailure,
        account_events: tuple[UsageLookupWorkerEvent, ...],
    ) -> bool:
        """Record a terminal worker cause and completed account failures."""
        causes = (
            *_account_causes(account_events),
            MetricsRefreshCause(
                stage=MetricsRefreshStage.WORKER,
                code=failure,
            ),
        )
        return self._record(
            MetricsRefreshOutcome.FAILED,
            causes=causes,
        )

    def record_snapshot_failure(
        self,
        code: MetricsRefreshSnapshotCode,
        account_events: tuple[UsageLookupWorkerEvent, ...],
    ) -> bool:
        """Record a terminal snapshot cause and account failures."""
        causes = (
            *_account_causes(account_events),
            _snapshot_cause(code),
        )
        return self._record(
            MetricsRefreshOutcome.FAILED,
            causes=causes,
        )

    def record_result(
        self,
        *,
        usage_cache_issue: UsageSnapshotFailureKind | None,
        activity_cache_issue: ActivitySnapshotFailureKind | None,
        account_events: tuple[UsageLookupWorkerEvent, ...],
        snapshot_failure: MetricsRefreshSnapshotCode | None = None,
    ) -> bool:
        """Record all cache, account, recovery, or success causes."""
        causes = _cache_causes(
            usage_cache_issue,
            activity_cache_issue,
        ) + _account_causes(account_events)
        if snapshot_failure is not None:
            causes += (_snapshot_cause(snapshot_failure),)
        if causes:
            return self._record(
                (
                    MetricsRefreshOutcome.FAILED
                    if snapshot_failure is not None
                    else MetricsRefreshOutcome.PARTIAL
                ),
                causes=causes,
            )
        if self._retry_causes:
            return self._record(MetricsRefreshOutcome.RECOVERED)
        return self._record(MetricsRefreshOutcome.SUCCEEDED)

    def _consume_retry(
        self,
        causes: tuple[MetricsRefreshCause, ...],
    ) -> None:
        if not self.retry_available:
            raise RuntimeError("Metrics refresh retry is already spent.")
        self._attempts += 1
        self._retry_causes = canonical_metrics_refresh_causes(causes)

    def _record(
        self,
        outcome: MetricsRefreshOutcome,
        *,
        causes: tuple[MetricsRefreshCause, ...] = (),
    ) -> bool:
        return (
            self._sink.record(
                outcome,
                attempts=self._attempts,
                retry_causes=self._retry_causes,
                causes=canonical_metrics_refresh_causes(causes),
            )
            is MetricsRefreshWriteState.UNAVAILABLE
        )


def _cache_causes(
    usage_issue: UsageSnapshotFailureKind | None,
    activity_issue: ActivitySnapshotFailureKind | None,
) -> tuple[MetricsRefreshCause, ...]:
    causes: list[MetricsRefreshCause] = []
    if usage_issue is not None:
        causes.append(
            MetricsRefreshCause(
                stage=MetricsRefreshStage.CACHE_READ,
                code=_USAGE_CACHE_FAILURE_CODES[usage_issue],
            )
        )
    if activity_issue is not None:
        causes.append(
            MetricsRefreshCause(
                stage=MetricsRefreshStage.CACHE_READ,
                code=_ACTIVITY_CACHE_FAILURE_CODES[activity_issue],
            )
        )
    return tuple(causes)


def _snapshot_cause(
    code: MetricsRefreshSnapshotCode,
) -> MetricsRefreshCause:
    return MetricsRefreshCause(
        stage=MetricsRefreshStage.SNAPSHOT_RELOAD,
        code=code,
    )


def _account_causes(
    events: tuple[UsageLookupWorkerEvent, ...],
) -> tuple[MetricsRefreshCause, ...]:
    causes: list[MetricsRefreshCause] = []
    for event in events:
        if event.kind is not UsageLookupEventKind.ACCOUNT_FAILED:
            continue
        if (
            event.account_id is None
            or event.provider_id is None
            or event.fetch_failure is None
        ):
            raise AssertionError("Account failure event is incomplete.")
        causes.append(
            MetricsRefreshCause(
                stage=MetricsRefreshStage.ACCOUNT,
                code=event.fetch_failure,
                provider_id=event.provider_id,
                account_id=event.account_id,
            )
        )
    return canonical_metrics_refresh_causes(tuple(causes))
