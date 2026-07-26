"""Concurrent lookup and cursor traces after cached first paint."""

import os
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, BrokenBarrierError, Event, Lock

from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.fixtures import (
    EXPANDED_ACCOUNT_COUNT,
    REFERENCE_ACCOUNT_COUNT,
    REFERENCE_TIME,
    dashboard_snapshot,
    saved_accounts,
)
from dashboard_benchmark.models import ChildTrace
from dashboard_benchmark.render import cursor_render_p95
from dashboard_benchmark.unix.process import (
    all_children_reaped,
    peak_reaped_child_rss_bytes,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.models import UsageReport
from sidekick_usages.http.client import HttpClient
from sidekick_usages.usage.activity import TokenActivityCollector
from sidekick_usages.usage.lookup.models import (
    AccountLookupReading,
    AccountMutationExchange,
    AccountMutationIntent,
    AccountMutationResult,
    CurrentUsageReading,
)
from sidekick_usages.usage.lookup.service import AccountLookupService
from sidekick_usages.usage.lookup.wave import UsageLookupWave
from sidekick_usages.usage.lookup.worker.client import (
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
    resolve_usage_lookup_interpreter,
)

SLOW_ORDINAL = 0
LOOKUP_SYNCHRONIZATION_TIMEOUT_SECONDS = 2.0
LOOKUP_WORKER_TIMEOUT_SECONDS = 10.0
LOOKUP_WORKER_TERMINATION_GRACE_SECONDS = 1.0
OWNER_DIRECTORY_MODE = 0o700
TEMPORARY_HOME_PREFIX = "sidekick-dashboard-benchmark-"
WORKER_DIRECTORY_ENVIRONMENT = (
    ("HOME", "home"),
    ("XDG_CONFIG_HOME", "config"),
    ("XDG_DATA_HOME", "data"),
    ("XDG_RUNTIME_DIR", "runtime"),
)


class BenchmarkLookup(AccountLookupService):
    """Coordinate one real lookup wave without provider or credential I/O."""

    def __init__(self, account_count: int) -> None:
        self._barrier = Barrier(account_count)
        self._slow_release = Event()
        self._lock = Lock()
        self._started: list[int] = []

    @property
    def submitted_ordinals(self) -> tuple[int, ...]:
        """Return every account that reached the typed lookup seam."""
        with self._lock:
            return tuple(self._started)

    def release_slow_account(self) -> None:
        """Allow the deliberately blocked account to complete."""
        self._slow_release.set()

    def lookup(
        self,
        account: SavedAccount,
        ordinal: int,
        reference_time: datetime,
        mutate: AccountMutationExchange,
    ) -> AccountLookupReading:
        """Return one current reading after the global start barrier."""
        del reference_time, mutate
        with self._lock:
            self._started.append(ordinal)
        try:
            self._barrier.wait(timeout=LOOKUP_SYNCHRONIZATION_TIMEOUT_SECONDS)
        except BrokenBarrierError:
            raise DashboardBenchmarkError(
                "Saved accounts did not begin in one bounded lookup wave."
            ) from None
        if ordinal == SLOW_ORDINAL and not self._slow_release.wait(
            timeout=LOOKUP_SYNCHRONIZATION_TIMEOUT_SECONDS
        ):
            raise DashboardBenchmarkError(
                "Blocked benchmark account was not released."
            )
        return AccountLookupReading(
            ordinal=ordinal,
            account_id=account.account_id,
            label=account.label,
            provider_id=account.provider_id,
            usage=CurrentUsageReading(
                plan=account.plan,
                report=UsageReport(plan=account.plan),
            ),
            failure=None,
            activity=None,
            activity_eligible=False,
        )


def _reject_mutation(
    intent: AccountMutationIntent,
) -> AccountMutationResult:
    del intent
    raise DashboardBenchmarkError(
        "Dashboard lookup benchmark requested a mutation."
    )


def _synthetic_lookup_trace() -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
]:
    accounts = saved_accounts(REFERENCE_ACCOUNT_COUNT)
    lookup = BenchmarkLookup(len(accounts))
    with HttpClient() as http:
        wave = UsageLookupWave(
            lookup,
            TokenActivityCollector(http, {}, {}),
        )
        readings = wave.run(
            accounts,
            (),
            REFERENCE_TIME,
            _reject_mutation,
        )
        try:
            first = next(readings)
            if not isinstance(first, AccountLookupReading):
                raise DashboardBenchmarkError(
                    "Lookup wave yielded unexpected local activity."
                )
            if first.ordinal == SLOW_ORDINAL:
                raise DashboardBenchmarkError(
                    "A blocked account delayed a completed fast account."
                )
            lookup.release_slow_account()
            rest = tuple(readings)
        finally:
            lookup.release_slow_account()
        completed = (first, *rest)
        if any(
            not isinstance(reading, AccountLookupReading)
            for reading in completed
        ):
            raise DashboardBenchmarkError(
                "Lookup wave yielded unexpected local activity."
            )
        by_account = {
            reading.account_id: reading
            for reading in completed
            if isinstance(reading, AccountLookupReading)
        }
        final_ordinals = tuple(
            by_account[account.account_id].ordinal for account in accounts
        )
        return (
            lookup.submitted_ordinals,
            tuple(
                reading.ordinal
                for reading in completed
                if isinstance(reading, AccountLookupReading)
            ),
            final_ordinals,
        )


def _isolated_worker_environment(root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, directory_name in WORKER_DIRECTORY_ENVIRONMENT:
        directory = root / directory_name
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        environment[name] = str(directory)
    return environment


def _production_worker_trace() -> tuple[int, bool]:
    if not all_children_reaped():
        raise DashboardBenchmarkError(
            "Dashboard trace process owned an unexpected child."
        )
    with TemporaryDirectory(prefix=TEMPORARY_HOME_PREFIX) as temporary_home:
        environment = _isolated_worker_environment(
            Path(temporary_home).resolve()
        )
        result = UsageLookupWorkerClient(
            UsageLookupModuleLaunchPlanner(
                resolve_usage_lookup_interpreter(),
                environment,
            ),
            timeout_seconds=LOOKUP_WORKER_TIMEOUT_SECONDS,
            termination_grace_seconds=(
                LOOKUP_WORKER_TERMINATION_GRACE_SECONDS
            ),
        ).run()
    if not result.succeeded:
        failure = "unknown" if result.failure is None else result.failure.value
        raise DashboardBenchmarkError(
            f"Production usage lookup worker failed: {failure}."
        )
    if result.completed_account_ids:
        raise DashboardBenchmarkError(
            "Empty benchmark home returned saved-account completions."
        )
    return peak_reaped_child_rss_bytes(), all_children_reaped()


def main() -> int:
    """Emit the bounded post-paint concurrency and cursor trace."""
    submitted, completed, final = _synthetic_lookup_trace()
    lookup_worker_rss, lookup_worker_reaped = _production_worker_trace()
    trace = ChildTrace(
        process_id=os.getpid(),
        reference_cursor_p95_ns=cursor_render_p95(
            dashboard_snapshot(REFERENCE_ACCOUNT_COUNT)
        ),
        expanded_cursor_p95_ns=cursor_render_p95(
            dashboard_snapshot(EXPANDED_ACCOUNT_COUNT)
        ),
        submitted_ordinals=submitted,
        completion_ordinals=completed,
        final_ordinals=final,
        slow_ordinal=SLOW_ORDINAL,
        reaped_lookup_worker_ru_maxrss_bytes=lookup_worker_rss,
        lookup_worker_reaped=lookup_worker_reaped,
    )
    sys.stdout.write(trace.encode() + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
