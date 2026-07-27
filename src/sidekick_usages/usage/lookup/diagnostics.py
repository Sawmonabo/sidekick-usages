"""Classify and record one dashboard metrics-refresh attempt."""

from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    UsageSnapshotFailureKind,
)
from sidekick_usages.usage.lookup.models import (
    MAX_METRICS_REFRESH_ATTEMPTS,
    MetricsRefreshCode,
    MetricsRefreshFailureCode,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
    MetricsRefreshWriteState,
)
from sidekick_usages.usage.lookup.ports import (
    MetricsRefreshObservationSink,
)
from sidekick_usages.usage.lookup.worker.models import UsageLookupFailure

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
        self._recovery_stage: MetricsRefreshStage | None = None
        self._recovery_code: MetricsRefreshCode | None = None

    @property
    def retry_available(self) -> bool:
        """Return whether the single refresh retry remains unused."""
        return self._attempts < MAX_METRICS_REFRESH_ATTEMPTS

    def retry_worker(self, failure: UsageLookupFailure) -> None:
        """Spend the single retry on a proven recoverable worker failure."""
        if not failure.recoverable:
            raise ValueError("Lookup worker failure is not recoverable.")
        self._consume_retry(MetricsRefreshStage.WORKER, failure)

    def retry_snapshot(self) -> None:
        """Spend the single retry on a transient snapshot-load failure."""
        self._consume_retry(
            MetricsRefreshStage.SNAPSHOT_RELOAD,
            MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE,
        )

    def record_worker_failure(
        self,
        failure: UsageLookupFailure,
    ) -> bool:
        """Record the terminal worker cause and actual attempt count."""
        return self._record(
            MetricsRefreshOutcome.FAILED,
            stage=MetricsRefreshStage.WORKER,
            code=failure,
        )

    def record_snapshot_failure(self) -> bool:
        """Record a terminal snapshot-load failure."""
        return self._record(
            MetricsRefreshOutcome.FAILED,
            stage=MetricsRefreshStage.SNAPSHOT_RELOAD,
            code=MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE,
        )

    def record_result(
        self,
        *,
        usage_cache_issue: UsageSnapshotFailureKind | None,
        activity_cache_issue: ActivitySnapshotFailureKind | None,
        provider_failed: bool,
    ) -> bool:
        """Record cache, provider, recovery, or complete success."""
        cache_code = _cache_failure(
            usage_cache_issue,
            activity_cache_issue,
        )
        if cache_code is not None:
            return self._record(
                MetricsRefreshOutcome.PARTIAL,
                stage=MetricsRefreshStage.CACHE_READ,
                code=cache_code,
            )
        if provider_failed:
            return self._record(
                MetricsRefreshOutcome.PARTIAL,
                stage=MetricsRefreshStage.PROVIDER,
                code=MetricsRefreshFailureCode.PROVIDER_FAILURE,
            )
        if (
            self._recovery_stage is not None
            and self._recovery_code is not None
        ):
            return self._record(
                MetricsRefreshOutcome.RECOVERED,
                stage=self._recovery_stage,
                code=self._recovery_code,
            )
        return self._record(MetricsRefreshOutcome.SUCCEEDED)

    def _consume_retry(
        self,
        stage: MetricsRefreshStage,
        code: MetricsRefreshCode,
    ) -> None:
        if not self.retry_available:
            raise RuntimeError("Metrics refresh retry is already spent.")
        self._attempts += 1
        self._recovery_stage = stage
        self._recovery_code = code

    def _record(
        self,
        outcome: MetricsRefreshOutcome,
        *,
        stage: MetricsRefreshStage | None = None,
        code: MetricsRefreshCode | None = None,
    ) -> bool:
        return (
            self._sink.record(
                outcome,
                attempts=self._attempts,
                stage=stage,
                code=code,
            )
            is MetricsRefreshWriteState.UNAVAILABLE
        )


def _cache_failure(
    usage_issue: UsageSnapshotFailureKind | None,
    activity_issue: ActivitySnapshotFailureKind | None,
) -> MetricsRefreshFailureCode | None:
    if usage_issue is not None:
        return _USAGE_CACHE_FAILURE_CODES[usage_issue]
    if activity_issue is None:
        return None
    return _ACTIVITY_CACHE_FAILURE_CODES[activity_issue]
