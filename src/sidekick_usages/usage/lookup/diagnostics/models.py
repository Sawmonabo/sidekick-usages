"""Immutable dashboard metrics-refresh diagnostics."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.limits import MAX_ACCOUNTS
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.usage.lookup.worker.models import UsageLookupFailure
from sidekick_usages.usage.models import FetchFailureKind

MAX_METRICS_REFRESH_ATTEMPTS = 2
MAX_METRICS_REFRESH_CAUSES = MAX_ACCOUNTS + 3
RECOVERABLE_SNAPSHOT_PERSISTENCE_CODES = frozenset(
    {
        PersistenceCode.AUTHORITY_UNAVAILABLE,
        PersistenceCode.SOURCE_CHANGED,
        PersistenceCode.STORE_LOCKED,
        PersistenceCode.UNREADABLE,
    }
)

type MetricsRefreshCode = (
    UsageLookupFailure
    | FetchFailureKind
    | PersistenceCode
    | MetricsRefreshFailureCode
)
type MetricsRefreshSnapshotCode = PersistenceCode | MetricsRefreshFailureCode


class MetricsRefreshOutcome(StrEnum):
    """Closed outcomes for one dashboard metrics refresh."""

    SUCCEEDED = "succeeded"
    RECOVERED = "recovered"
    PARTIAL = "partial"
    FAILED = "failed"


class MetricsRefreshStage(StrEnum):
    """Closed metrics-refresh failure boundaries."""

    WORKER = "worker"
    ACCOUNT = "account"
    SNAPSHOT_RELOAD = "snapshot_reload"
    CACHE_READ = "cache_read"


class MetricsRefreshFailureCode(StrEnum):
    """Safe non-account reasons for an incomplete metrics refresh."""

    SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
    USAGE_READ = "usage_read"
    USAGE_MALFORMED = "usage_malformed"
    ACTIVITY_READ = "activity_read"
    ACTIVITY_MALFORMED = "activity_malformed"


class MetricsRefreshWriteState(StrEnum):
    """Outcome from the no-throw diagnostic sink."""

    SAVED = "saved"
    UNAVAILABLE = "unavailable"


class MetricsRefreshDiagnosticState(StrEnum):
    """Passive availability of the latest diagnostic artifact."""

    AVAILABLE = "available"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRefreshCause:
    """One bounded, secret-free refresh cause."""

    stage: MetricsRefreshStage
    code: MetricsRefreshCode
    provider_id: ProviderId | None = None
    account_id: SidekickAccountId | None = None

    def __post_init__(self) -> None:
        """Require detail owned by exactly one failure boundary."""
        account_cause = self.stage is MetricsRefreshStage.ACCOUNT
        if (self.provider_id is not None) is not account_cause:
            raise ValueError("Metrics refresh provider scope is invalid.")
        if (self.account_id is not None) is not account_cause:
            raise ValueError("Metrics refresh account scope is invalid.")
        if not _metrics_refresh_cause_is_valid(self.stage, self.code):
            raise ValueError("Metrics refresh cause is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRefreshObservation:
    """Latest sanitized dashboard metrics-refresh observation."""

    observed_at: datetime
    outcome: MetricsRefreshOutcome
    attempts: int
    retry_causes: tuple[MetricsRefreshCause, ...] = ()
    causes: tuple[MetricsRefreshCause, ...] = ()

    def __post_init__(self) -> None:
        """Normalize time and require one unambiguous outcome."""
        object.__setattr__(self, "observed_at", as_utc(self.observed_at))
        if not 1 <= self.attempts <= MAX_METRICS_REFRESH_ATTEMPTS:
            raise ValueError("Metrics refresh attempts are outside the limit.")
        _require_canonical_causes(self.retry_causes)
        _require_canonical_causes(self.causes)
        _require_metrics_refresh_contract(
            self.outcome,
            self.attempts,
            self.retry_causes,
            self.causes,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class MetricsRefreshDiagnostic:
    """Passive latest observation or its safe availability state."""

    state: MetricsRefreshDiagnosticState
    observation: MetricsRefreshObservation | None = None

    def __post_init__(self) -> None:
        """Require an observation exactly when it is available."""
        available = self.state is MetricsRefreshDiagnosticState.AVAILABLE
        if (self.observation is not None) is not available:
            raise ValueError("Metrics refresh diagnostic is inconsistent.")

    def scoped(
        self,
        provider_id: ProviderId | None,
        account_ids: frozenset[SidekickAccountId] | None = None,
    ) -> MetricsRefreshDiagnostic:
        """Retain global causes and the exact requested account scope."""
        if (
            provider_id is None and account_ids is None
        ) or self.observation is None:
            return self
        observation = self.observation
        retry_causes = _scoped_causes(
            observation.retry_causes,
            provider_id,
            account_ids,
        )
        causes = _scoped_causes(
            observation.causes,
            provider_id,
            account_ids,
        )
        if (
            retry_causes == observation.retry_causes
            and causes == observation.causes
        ):
            return self
        outcome = _scoped_outcome(retry_causes, causes)
        return MetricsRefreshDiagnostic(
            state=MetricsRefreshDiagnosticState.AVAILABLE,
            observation=MetricsRefreshObservation(
                observed_at=observation.observed_at,
                outcome=outcome,
                attempts=(MAX_METRICS_REFRESH_ATTEMPTS if retry_causes else 1),
                retry_causes=retry_causes,
                causes=causes,
            ),
        )


def canonical_metrics_refresh_causes(
    causes: tuple[MetricsRefreshCause, ...],
) -> tuple[MetricsRefreshCause, ...]:
    """Return deterministic, deduplicated diagnostic causes."""
    return tuple(sorted(set(causes), key=_metrics_refresh_cause_key))


def _metrics_refresh_cause_key(
    cause: MetricsRefreshCause,
) -> tuple[str, str, str, str]:
    return (
        cause.stage.value,
        "" if cause.provider_id is None else cause.provider_id.value,
        "" if cause.account_id is None else str(cause.account_id),
        cause.code.value,
    )


def _require_canonical_causes(
    causes: tuple[MetricsRefreshCause, ...],
) -> None:
    if len(causes) > MAX_METRICS_REFRESH_CAUSES:
        raise ValueError("Metrics refresh causes exceed the limit.")
    if causes != canonical_metrics_refresh_causes(causes):
        raise ValueError("Metrics refresh causes are not canonical.")


def _metrics_refresh_cause_is_valid(
    stage: MetricsRefreshStage,
    code: MetricsRefreshCode,
) -> bool:
    if stage is MetricsRefreshStage.WORKER:
        return isinstance(code, UsageLookupFailure)
    if stage is MetricsRefreshStage.ACCOUNT:
        return isinstance(code, FetchFailureKind)
    if stage is MetricsRefreshStage.SNAPSHOT_RELOAD:
        return isinstance(code, PersistenceCode) or (
            code is MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
        )
    return code in {
        MetricsRefreshFailureCode.USAGE_READ,
        MetricsRefreshFailureCode.USAGE_MALFORMED,
        MetricsRefreshFailureCode.ACTIVITY_READ,
        MetricsRefreshFailureCode.ACTIVITY_MALFORMED,
    }


def _require_metrics_refresh_contract(
    outcome: MetricsRefreshOutcome,
    attempts: int,
    retry_causes: tuple[MetricsRefreshCause, ...],
    causes: tuple[MetricsRefreshCause, ...],
) -> None:
    if attempts == 1 and retry_causes:
        raise ValueError("Single-attempt refresh cannot contain retry causes.")
    if attempts == MAX_METRICS_REFRESH_ATTEMPTS and not retry_causes:
        raise ValueError("Retried refresh requires its original causes.")
    _require_retry_causes(retry_causes)
    if outcome is MetricsRefreshOutcome.SUCCEEDED:
        if attempts != 1 or retry_causes or causes:
            raise ValueError("Successful metrics refresh is inconsistent.")
        return
    if outcome is MetricsRefreshOutcome.RECOVERED:
        if attempts != MAX_METRICS_REFRESH_ATTEMPTS or causes:
            raise ValueError("Recovered metrics refresh is inconsistent.")
        return
    if not causes:
        raise ValueError("Incomplete metrics refresh requires a cause.")
    failed = any(
        cause.stage
        in {
            MetricsRefreshStage.WORKER,
            MetricsRefreshStage.SNAPSHOT_RELOAD,
        }
        for cause in causes
    )
    if (outcome is MetricsRefreshOutcome.FAILED) is not failed:
        raise ValueError("Metrics refresh terminal outcome is inconsistent.")


def _require_retry_causes(
    causes: tuple[MetricsRefreshCause, ...],
) -> None:
    if not causes:
        return
    stages = {cause.stage for cause in causes}
    if MetricsRefreshStage.WORKER in stages:
        worker_causes = tuple(
            cause
            for cause in causes
            if cause.stage is MetricsRefreshStage.WORKER
        )
        if (
            len(worker_causes) == 1
            and stages
            <= {
                MetricsRefreshStage.ACCOUNT,
                MetricsRefreshStage.WORKER,
            }
            and isinstance(worker_causes[0].code, UsageLookupFailure)
            and worker_causes[0].code.recoverable
        ):
            return
    elif stages == {MetricsRefreshStage.CACHE_READ}:
        if any(
            cause.code
            in {
                MetricsRefreshFailureCode.USAGE_READ,
                MetricsRefreshFailureCode.ACTIVITY_READ,
            }
            for cause in causes
        ):
            return
    elif (
        stages == {MetricsRefreshStage.SNAPSHOT_RELOAD}
        and len(causes) == 1
        and (
            causes[0].code is MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
            or causes[0].code in RECOVERABLE_SNAPSHOT_PERSISTENCE_CODES
        )
    ):
        return
    raise ValueError("Metrics refresh retry causes are invalid.")


def _scoped_causes(
    causes: tuple[MetricsRefreshCause, ...],
    provider_id: ProviderId | None,
    account_ids: frozenset[SidekickAccountId] | None,
) -> tuple[MetricsRefreshCause, ...]:
    return tuple(
        cause
        for cause in causes
        if cause.provider_id is None
        or (
            (provider_id is None or cause.provider_id is provider_id)
            and (
                account_ids is None
                or (
                    cause.account_id is not None
                    and cause.account_id in account_ids
                )
            )
        )
    )


def _scoped_outcome(
    retry_causes: tuple[MetricsRefreshCause, ...],
    causes: tuple[MetricsRefreshCause, ...],
) -> MetricsRefreshOutcome:
    if any(
        cause.stage
        in {
            MetricsRefreshStage.WORKER,
            MetricsRefreshStage.SNAPSHOT_RELOAD,
        }
        for cause in causes
    ):
        return MetricsRefreshOutcome.FAILED
    if causes:
        return MetricsRefreshOutcome.PARTIAL
    if retry_causes:
        return MetricsRefreshOutcome.RECOVERED
    return MetricsRefreshOutcome.SUCCEEDED
