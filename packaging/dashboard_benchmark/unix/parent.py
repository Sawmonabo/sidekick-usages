"""Unix dashboard performance and concurrency release trace."""

import os
import platform
import selectors
import subprocess
import sys
import time
from pathlib import Path

from dashboard_benchmark.command import execute
from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.fixtures import (
    EXPANDED_ACCOUNT_COUNT,
    REFERENCE_ACCOUNT_COUNT,
)
from dashboard_benchmark.models import (
    BenchmarkResult,
    ChildTrace,
    FirstPaintSignal,
)
from dashboard_benchmark.unix.process import (
    BYTES_PER_MEBIBYTE,
    peak_reaped_child_rss_bytes,
)

TRACE_PROCESS_MODULE = "dashboard_benchmark.unix.child"
FIRST_PAINT_DEADLINE_SECONDS = 0.250
CURSOR_P95_TARGET_MILLISECONDS = 50.0
TRACE_PROCESS_COMPLETION_TIMEOUT_SECONDS = 10.0
TRACE_PROCESS_CLEANUP_TIMEOUT_SECONDS = 1.0
NANOSECONDS_PER_MILLISECOND = 1_000_000


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


def _validate_trace(
    process_id: int,
    signal: FirstPaintSignal,
    trace: ChildTrace,
    first_paint_ms: float,
    reaped_trace_process_ru_maxrss_mib: float,
    trace_process_reaped: bool,
) -> BenchmarkResult:
    expected_ordinals = tuple(range(REFERENCE_ACCOUNT_COUNT))
    if signal.account_count != REFERENCE_ACCOUNT_COUNT:
        raise DashboardBenchmarkError(
            "First paint used the wrong account population."
        )
    if trace.process_id != process_id:
        raise DashboardBenchmarkError(
            "Dashboard trace forked a second child process."
        )
    if sorted(trace.submitted_ordinals) != list(expected_ordinals):
        raise DashboardBenchmarkError(
            "Not every saved account began before the first completion."
        )
    if (
        trace.completion_ordinals[0] == trace.slow_ordinal
        or trace.slow_ordinal not in trace.completion_ordinals
    ):
        raise DashboardBenchmarkError(
            "Fast lookup completion did not precede the blocked account."
        )
    if trace.final_ordinals != expected_ordinals:
        raise DashboardBenchmarkError(
            "Lookup results lost deterministic saved-account order."
        )
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
    if not trace_process_reaped:
        raise DashboardBenchmarkError(
            "Dashboard trace process was not reaped."
        )
    if not trace.lookup_worker_reaped:
        raise DashboardBenchmarkError(
            "Production usage lookup worker was not reaped."
        )
    return BenchmarkResult(
        first_paint_ms=first_paint_ms,
        reference_cursor_p95_ms=reference_cursor_ms,
        expanded_cursor_p95_ms=expanded_cursor_ms,
        reaped_trace_process_ru_maxrss_mib=(
            reaped_trace_process_ru_maxrss_mib
        ),
        trace_process_reaped=trace_process_reaped,
        reaped_lookup_worker_ru_maxrss_mib=(
            trace.reaped_lookup_worker_ru_maxrss_bytes / BYTES_PER_MEBIBYTE
        ),
        lookup_worker_reaped=trace.lookup_worker_reaped,
        trace=trace,
    )


def _run() -> int:
    packaging_root = Path(__file__).resolve().parents[2]
    started_at = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-m", TRACE_PROCESS_MODULE],
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
    result = _validate_trace(
        process.pid,
        signal,
        trace,
        first_paint_ms,
        peak_reaped_child_rss_bytes() / BYTES_PER_MEBIBYTE,
        _already_reaped(process.pid),
    )
    sys.stdout.write(
        "\n".join(
            (
                "Dashboard benchmark passed.",
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
                    "trace_process_reaped="
                    f"{str(result.trace_process_reaped).lower()}"
                ),
                (
                    "reaped_lookup_worker_ru_maxrss_mib="
                    f"{result.reaped_lookup_worker_ru_maxrss_mib:.3f}"
                ),
                (
                    "lookup_worker_reaped="
                    f"{str(result.lookup_worker_reaped).lower()}"
                ),
                (f"submitted_ordinals={result.trace.submitted_ordinals}"),
                (f"completion_ordinals={result.trace.completion_ordinals}"),
                f"final_ordinals={result.trace.final_ordinals}",
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
