"""Concurrent lookup and cursor traces after cached first paint."""

import os
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import (
    Barrier,
    BrokenBarrierError,
    Event,
    Lock,
    get_native_id,
)

from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.fixtures import (
    EXPANDED_ACCOUNT_COUNT,
    REFERENCE_ACCOUNT_COUNT,
    REFERENCE_TIME,
    dashboard_snapshot,
    saved_accounts,
)
from dashboard_benchmark.models import (
    ChildTrace,
    LookupTaskIdentity,
    LookupTaskStart,
)
from dashboard_benchmark.render import cursor_render_p95
from dashboard_benchmark.unix.process import (
    all_children_reaped,
    peak_reaped_child_rss_bytes,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.models import TokenActivitySummary, UsageReport
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.http.client import HttpClient
from sidekick_usages.usage.activity import TokenActivityCollector
from sidekick_usages.usage.lookup.models import (
    AccountLookupReading,
    AccountMutationExchange,
    AccountMutationIntent,
    AccountMutationResult,
    CurrentUsageReading,
    LocalActivityReading,
)
from sidekick_usages.usage.lookup.service import AccountLookupService
from sidekick_usages.usage.lookup.wave import UsageLookupWave
from sidekick_usages.usage.lookup.worker.client import (
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
    resolve_usage_lookup_interpreter,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupModuleLaunchSpec,
)

SLOW_ORDINAL = 0
LOCAL_ACTIVITY_PROVIDER = ProviderId.CLAUDE
LOCAL_ACTIVITY_TOKEN_COUNT = 1_000_000
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


class BenchmarkWaveProbe:
    """Record one fully-started, process-free lookup-thread wave."""

    def __init__(self, task_count: int) -> None:
        self._all_started = Event()
        self._barrier = Barrier(
            task_count,
            action=self._all_started.set,
        )
        self._slow_release = Event()
        self._lock = Lock()
        self._starts: list[LookupTaskStart] = []

    @property
    def all_started(self) -> bool:
        """Return whether every task crossed the common start barrier."""
        return self._all_started.is_set()

    @property
    def task_starts(self) -> tuple[LookupTaskStart, ...]:
        """Return every typed task start in stable identity order."""
        with self._lock:
            return tuple(
                sorted(
                    self._starts,
                    key=lambda start: start.identity.encode(),
                )
            )

    def start(self, identity: LookupTaskIdentity) -> None:
        """Record one task and block it until every peer has started."""
        with self._lock:
            self._starts.append(
                LookupTaskStart(
                    identity,
                    os.getpid(),
                    get_native_id(),
                )
            )
        try:
            self._barrier.wait(timeout=LOOKUP_SYNCHRONIZATION_TIMEOUT_SECONDS)
        except BrokenBarrierError:
            raise DashboardBenchmarkError(
                "Lookup tasks did not begin in one bounded global wave."
            ) from None

    def release_slow_account(self) -> None:
        """Allow the deliberately blocked account to complete."""
        self._slow_release.set()

    def wait_for_slow_release(self) -> None:
        """Block the slow task until a fast account has been consumed."""
        if not self._slow_release.wait(
            timeout=LOOKUP_SYNCHRONIZATION_TIMEOUT_SECONDS
        ):
            raise DashboardBenchmarkError(
                "Blocked benchmark account was not released."
            )


class BenchmarkLookup(AccountLookupService):
    """Coordinate one real lookup wave without provider or credential I/O."""

    def __init__(self, probe: BenchmarkWaveProbe) -> None:
        self._probe = probe

    def lookup(
        self,
        account: SavedAccount,
        ordinal: int,
        reference_time: datetime,
        mutate: AccountMutationExchange,
    ) -> AccountLookupReading:
        """Return one current reading after the global start barrier."""
        del reference_time, mutate
        self._probe.start(LookupTaskIdentity.account(ordinal))
        if ordinal == SLOW_ORDINAL:
            self._probe.wait_for_slow_release()
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


class BenchmarkLocalActivitySource:
    """Return one synthetic Claude-local reading through the real collector."""

    provider_id = LOCAL_ACTIVITY_PROVIDER

    def __init__(self, probe: BenchmarkWaveProbe) -> None:
        self._probe = probe

    def read(self, reference_time: datetime) -> TokenActivitySummary:
        """Record the local task and return one correctly scoped summary."""
        self._probe.start(LookupTaskIdentity.local_activity(self.provider_id))
        return TokenActivitySummary(
            total_tokens=LOCAL_ACTIVITY_TOKEN_COUNT,
            scope=TokenActivityScope.LOCAL_INSTALLATION,
            since=reference_time.date(),
        )


class BenchmarkLookupLaunchPlanner(UsageLookupModuleLaunchPlanner):
    """Count exact production launch plans without replacing the launcher."""

    def __init__(
        self,
        interpreter: Path,
        source_environment: Mapping[str, str],
    ) -> None:
        super().__init__(interpreter, source_environment)
        self._launch_count = 0

    @property
    def launch_count(self) -> int:
        """Return exact calls through the production launch seam."""
        return self._launch_count

    def plan(self) -> UsageLookupModuleLaunchSpec:
        """Record and return one qualified production launch plan."""
        self._launch_count += 1
        return super().plan()


def _reject_mutation(
    intent: AccountMutationIntent,
) -> AccountMutationResult:
    del intent
    raise DashboardBenchmarkError(
        "Dashboard lookup benchmark requested a mutation."
    )


def _synthetic_lookup_trace() -> tuple[
    tuple[LookupTaskStart, ...],
    tuple[int, ...],
    bool,
]:
    reaped_child_rss_before = peak_reaped_child_rss_bytes()
    if not all_children_reaped() or reaped_child_rss_before != 0:
        raise DashboardBenchmarkError(
            "Lookup thread wave began with an unexpected child."
        )
    accounts = saved_accounts(REFERENCE_ACCOUNT_COUNT)
    task_count = len(accounts) + 1
    probe = BenchmarkWaveProbe(task_count)
    lookup = BenchmarkLookup(probe)
    with HttpClient() as http:
        activity = TokenActivityCollector(
            http,
            {LOCAL_ACTIVITY_PROVIDER: BenchmarkLocalActivitySource(probe)},
            {},
        )
        wave = UsageLookupWave(
            lookup,
            activity,
        )
        readings = wave.run(
            accounts,
            (LOCAL_ACTIVITY_PROVIDER,),
            REFERENCE_TIME,
            _reject_mutation,
        )
        completed: list[AccountLookupReading | LocalActivityReading] = []
        try:
            for reading in readings:
                completed.append(reading)
                if not isinstance(reading, AccountLookupReading):
                    continue
                if reading.ordinal == SLOW_ORDINAL:
                    raise DashboardBenchmarkError(
                        "A blocked account delayed a completed fast account."
                    )
                if not probe.all_started:
                    raise DashboardBenchmarkError(
                        "A lookup completed before every task had started."
                    )
                probe.release_slow_account()
                completed.extend(readings)
                break
            else:
                raise DashboardBenchmarkError(
                    "Lookup wave returned no saved-account completion."
                )
        finally:
            probe.release_slow_account()
        completion_ordinals = tuple(
            reading.ordinal
            for reading in completed
            if isinstance(reading, AccountLookupReading)
        )
        local_completions = tuple(
            reading
            for reading in completed
            if isinstance(reading, LocalActivityReading)
        )
        if sorted(completion_ordinals) != list(range(len(accounts))):
            raise DashboardBenchmarkError(
                "Lookup wave did not complete every saved account once."
            )
        if (
            len(local_completions) != 1
            or local_completions[0].provider_id is not LOCAL_ACTIVITY_PROVIDER
        ):
            raise DashboardBenchmarkError(
                "Lookup wave did not complete one Claude-local activity task."
            )
        process_free = (
            all_children_reaped()
            and peak_reaped_child_rss_bytes() == reaped_child_rss_before
        )
        return (
            probe.task_starts,
            completion_ordinals,
            process_free,
        )


def _isolated_worker_environment(root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, directory_name in WORKER_DIRECTORY_ENVIRONMENT:
        directory = root / directory_name
        directory.mkdir(mode=OWNER_DIRECTORY_MODE)
        environment[name] = str(directory)
    return environment


def _production_worker_trace() -> tuple[int, int, bool]:
    if not all_children_reaped():
        raise DashboardBenchmarkError(
            "Dashboard trace process owned an unexpected child."
        )
    with TemporaryDirectory(prefix=TEMPORARY_HOME_PREFIX) as temporary_home:
        environment = _isolated_worker_environment(
            Path(temporary_home).resolve()
        )
        planner = BenchmarkLookupLaunchPlanner(
            resolve_usage_lookup_interpreter(),
            environment,
        )
        result = UsageLookupWorkerClient(
            planner,
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
    return (
        planner.launch_count,
        peak_reaped_child_rss_bytes(),
        all_children_reaped(),
    )


def main() -> int:
    """Emit the bounded post-paint concurrency and cursor trace."""
    task_starts, completed, process_free = _synthetic_lookup_trace()
    (
        lookup_worker_launch_count,
        lookup_worker_rss,
        lookup_worker_reaped,
    ) = _production_worker_trace()
    trace = ChildTrace(
        process_id=os.getpid(),
        reference_cursor_p95_ns=cursor_render_p95(
            dashboard_snapshot(REFERENCE_ACCOUNT_COUNT)
        ),
        expanded_cursor_p95_ns=cursor_render_p95(
            dashboard_snapshot(EXPANDED_ACCOUNT_COUNT)
        ),
        task_starts=task_starts,
        completion_ordinals=completed,
        slow_ordinal=SLOW_ORDINAL,
        thread_wave_process_free=process_free,
        lookup_worker_launch_count=lookup_worker_launch_count,
        reaped_lookup_worker_ru_maxrss_bytes=lookup_worker_rss,
        lookup_worker_reaped=lookup_worker_reaped,
    )
    sys.stdout.write(trace.encode() + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
