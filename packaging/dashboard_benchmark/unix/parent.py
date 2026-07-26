"""Unix dashboard performance and concurrency release trace."""

import os
import platform
import selectors
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

from dashboard_benchmark.cache.paths import benchmark_application_paths
from dashboard_benchmark.command import (
    DASHBOARD_BENCHMARK_SUCCESS,
    execute,
)
from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.fixtures import (
    EXPANDED_ACCOUNT_COUNT,
    REFERENCE_ACCOUNT_COUNT,
    seed_cached_dashboard,
)
from dashboard_benchmark.models import (
    BenchmarkResult,
    ChildTrace,
    FirstPaintSignal,
    LookupTaskIdentity,
)
from dashboard_benchmark.unix.process import (
    BYTES_PER_MEBIBYTE,
    peak_reaped_child_rss_bytes,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.lookup.wave import USAGE_LOOKUP_MAX_WORKERS

TRACE_PROCESS_MODULE = "dashboard_benchmark.unix.child"
FIRST_PAINT_DEADLINE_SECONDS = 0.250
CURSOR_P95_TARGET_MILLISECONDS = 50.0
TRACE_PROCESS_RSS_CEILING_MIB = 96.0
LOOKUP_WORKER_RSS_CEILING_MIB = 96.0
EXPECTED_LOOKUP_WORKER_LAUNCH_COUNT = 1
TRACE_PROCESS_COMPLETION_TIMEOUT_SECONDS = 10.0
TRACE_PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
NANOSECONDS_PER_MILLISECOND = 1_000_000
TEMPORARY_CACHE_PREFIX = "sidekick-dashboard-cache-"


def _terminate_and_reap(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    process.terminate()
    try:
        process.wait(timeout=TRACE_PROCESS_CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _first_paint_line(
    process: subprocess.Popen[str],
    started_at: float,
) -> tuple[str, float]:
    if process.stdout is None:
        raise DashboardBenchmarkError(
            "Dashboard trace process stdout is unavailable."
        )
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        remaining = FIRST_PAINT_DEADLINE_SECONDS - (
            time.perf_counter() - started_at
        )
        events = selector.select(max(0.0, remaining))
        if not events:
            raise DashboardBenchmarkError(
                "Cached first paint exceeded the 250 ms deadline."
            )
        line = process.stdout.readline()
    finally:
        selector.close()
    elapsed_ms = (time.perf_counter() - started_at) * 1_000
    if not line:
        raise DashboardBenchmarkError(
            "Dashboard trace process exited before cached first paint."
        )
    return line, elapsed_ms


def _already_reaped(process_id: int) -> bool:
    try:
        os.waitpid(process_id, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def _expected_lookup_tasks() -> frozenset[LookupTaskIdentity]:
    return frozenset(
        (
            *(
                LookupTaskIdentity.account(ordinal)
                for ordinal in range(REFERENCE_ACCOUNT_COUNT)
            ),
            LookupTaskIdentity.local_activity(ProviderId.CLAUDE),
        )
    )


def _validate_lookup_trace(
    process_id: int,
    trace: ChildTrace,
) -> None:
    expected_ordinals = tuple(range(REFERENCE_ACCOUNT_COUNT))
    expected_tasks = _expected_lookup_tasks()
    if trace.process_id != process_id:
        raise DashboardBenchmarkError(
            "Dashboard trace forked a second child process."
        )
    task_identities = frozenset(start.identity for start in trace.task_starts)
    if task_identities != expected_tasks:
        raise DashboardBenchmarkError(
            "The global lookup wave did not start six accounts and one "
            "Claude-local activity task."
        )
    if any(
        start.process_id != trace.process_id for start in trace.task_starts
    ):
        raise DashboardBenchmarkError(
            "The lookup thread wave spawned a task process."
        )
    worker_threads = frozenset(start.thread_id for start in trace.task_starts)
    if (
        len(worker_threads) != len(expected_tasks)
        or len(worker_threads) > USAGE_LOOKUP_MAX_WORKERS
    ):
        raise DashboardBenchmarkError(
            "The current lookup population did not start concurrently "
            "inside the bounded worker cap."
        )
    if not trace.thread_wave_process_free:
        raise DashboardBenchmarkError(
            "The lookup thread wave created an operating-system child."
        )
    if (
        trace.completion_ordinals[0] == trace.slow_ordinal
        or trace.slow_ordinal not in trace.completion_ordinals
    ):
        raise DashboardBenchmarkError(
            "Fast lookup completion did not precede the blocked account."
        )
    if sorted(trace.completion_ordinals) != list(expected_ordinals):
        raise DashboardBenchmarkError(
            "The lookup wave did not complete every saved account once."
        )
    if trace.lookup_worker_launch_count != EXPECTED_LOOKUP_WORKER_LAUNCH_COUNT:
        raise DashboardBenchmarkError(
            "The dashboard did not launch exactly one global lookup worker."
        )


def _cursor_measurements(trace: ChildTrace) -> tuple[float, float]:
    reference_cursor_ms = (
        trace.reference_cursor_p95_ns / NANOSECONDS_PER_MILLISECOND
    )
    expanded_cursor_ms = (
        trace.expanded_cursor_p95_ns / NANOSECONDS_PER_MILLISECOND
    )
    if (
        reference_cursor_ms > CURSOR_P95_TARGET_MILLISECONDS
        or expanded_cursor_ms > CURSOR_P95_TARGET_MILLISECONDS
    ):
        raise DashboardBenchmarkError(
            "Cursor-to-render p95 exceeded the 50 ms target."
        )
    return reference_cursor_ms, expanded_cursor_ms


def _lookup_worker_rss(
    trace: ChildTrace,
    reaped_trace_process_ru_maxrss_mib: float,
    trace_process_reaped: bool,
) -> float:
    if not trace_process_reaped:
        raise DashboardBenchmarkError(
            "Dashboard trace process was not reaped."
        )
    if not trace.lookup_worker_reaped:
        raise DashboardBenchmarkError(
            "Production usage lookup worker was not reaped."
        )
    lookup_worker_rss_mib = (
        trace.reaped_lookup_worker_ru_maxrss_bytes / BYTES_PER_MEBIBYTE
    )
    if (
        reaped_trace_process_ru_maxrss_mib > TRACE_PROCESS_RSS_CEILING_MIB
        or lookup_worker_rss_mib > LOOKUP_WORKER_RSS_CEILING_MIB
    ):
        raise DashboardBenchmarkError(
            "Dashboard trace or lookup-worker RSS exceeded its 96 MiB "
            "release ceiling."
        )
    return lookup_worker_rss_mib


def _validate_trace(
    process_id: int,
    signal: FirstPaintSignal,
    trace: ChildTrace,
    first_paint_ms: float,
    reaped_trace_process_ru_maxrss_mib: float,
    trace_process_reaped: bool,
) -> BenchmarkResult:
    if signal.account_count != REFERENCE_ACCOUNT_COUNT:
        raise DashboardBenchmarkError(
            "First paint used the wrong account population."
        )
    _validate_lookup_trace(process_id, trace)
    reference_cursor_ms, expanded_cursor_ms = _cursor_measurements(trace)
    lookup_worker_rss_mib = _lookup_worker_rss(
        trace,
        reaped_trace_process_ru_maxrss_mib,
        trace_process_reaped,
    )
    return BenchmarkResult(
        first_paint_ms=first_paint_ms,
        reference_cursor_p95_ms=reference_cursor_ms,
        expanded_cursor_p95_ms=expanded_cursor_ms,
        reaped_trace_process_ru_maxrss_mib=(
            reaped_trace_process_ru_maxrss_mib
        ),
        trace_process_reaped=trace_process_reaped,
        reaped_lookup_worker_ru_maxrss_mib=lookup_worker_rss_mib,
        lookup_worker_reaped=trace.lookup_worker_reaped,
        trace=trace,
    )


def _run_trace(
    packaging_root: Path,
    cache_root: Path,
) -> BenchmarkResult:
    """Measure one fresh cached paint and its post-paint release trace."""
    started_at = time.perf_counter()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            TRACE_PROCESS_MODULE,
            str(cache_root),
        ],
        cwd=packaging_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        first_line, first_paint_ms = _first_paint_line(process, started_at)
        remaining, stderr = process.communicate(
            timeout=TRACE_PROCESS_COMPLETION_TIMEOUT_SECONDS
        )
    except BaseException:
        _terminate_and_reap(process)
        raise
    if process.returncode != 0:
        detail = stderr.strip() or "no child diagnostics"
        raise DashboardBenchmarkError(
            f"Dashboard trace process failed: {detail}"
        )
    remaining_lines = tuple(line for line in remaining.splitlines() if line)
    if len(remaining_lines) != 1:
        raise DashboardBenchmarkError(
            "Dashboard trace process emitted an invalid trace count."
        )
    signal = FirstPaintSignal.decode(first_line)
    trace = ChildTrace.decode(remaining_lines[0])
    return _validate_trace(
        process.pid,
        signal,
        trace,
        first_paint_ms,
        peak_reaped_child_rss_bytes() / BYTES_PER_MEBIBYTE,
        _already_reaped(process.pid),
    )


def _run() -> int:
    packaging_root = Path(__file__).resolve().parents[2]
    with TemporaryDirectory(prefix=TEMPORARY_CACHE_PREFIX) as raw:
        cache_root = Path(raw).resolve()
        paths = benchmark_application_paths(cache_root)
        seed_cached_dashboard(paths, REFERENCE_ACCOUNT_COUNT)
        result = _run_trace(packaging_root, cache_root)
    worker_thread_count = len(
        {start.thread_id for start in result.trace.task_starts}
    )
    task_starts = ";".join(
        start.encode() for start in result.trace.task_starts
    )
    sys.stdout.write(
        "\n".join(
            (
                DASHBOARD_BENCHMARK_SUCCESS,
                f"platform={platform.platform()}",
                f"machine={platform.machine()}",
                f"python={platform.python_version()}",
                f"reference_accounts={REFERENCE_ACCOUNT_COUNT}",
                f"expanded_accounts={EXPANDED_ACCOUNT_COUNT}",
                f"first_paint_ms={result.first_paint_ms:.3f}",
                (
                    "reference_cursor_p95_ms="
                    f"{result.reference_cursor_p95_ms:.3f}"
                ),
                (
                    "expanded_cursor_p95_ms="
                    f"{result.expanded_cursor_p95_ms:.3f}"
                ),
                (
                    "reaped_trace_process_ru_maxrss_mib="
                    f"{result.reaped_trace_process_ru_maxrss_mib:.3f}"
                ),
                (
                    "trace_process_rss_ceiling_mib="
                    f"{TRACE_PROCESS_RSS_CEILING_MIB:.3f}"
                ),
                (
                    "trace_process_reaped="
                    f"{str(result.trace_process_reaped).lower()}"
                ),
                (
                    "reaped_lookup_worker_ru_maxrss_mib="
                    f"{result.reaped_lookup_worker_ru_maxrss_mib:.3f}"
                ),
                (
                    "lookup_worker_rss_ceiling_mib="
                    f"{LOOKUP_WORKER_RSS_CEILING_MIB:.3f}"
                ),
                (
                    "lookup_worker_reaped="
                    f"{str(result.lookup_worker_reaped).lower()}"
                ),
                (
                    "lookup_worker_launch_count="
                    f"{result.trace.lookup_worker_launch_count}"
                ),
                f"lookup_task_starts={task_starts}",
                f"lookup_worker_threads={worker_thread_count}",
                f"lookup_worker_thread_cap={USAGE_LOOKUP_MAX_WORKERS}",
                (
                    "thread_wave_process_free="
                    f"{str(result.trace.thread_wave_process_free).lower()}"
                ),
                f"completion_ordinals={result.trace.completion_ordinals}",
            )
        )
        + "\n"
    )
    return 0


def main() -> int:
    """Run the bounded Unix dashboard release trace."""
    if len(sys.argv) != 1:
        raise DashboardBenchmarkError(
            "Unix dashboard benchmark accepts no arguments."
        )
    return _run()


if __name__ == "__main__":
    execute(main)
