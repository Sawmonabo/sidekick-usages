"""Bounded real-process proof for lookup-worker cancellation."""

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages.usage.lookup.worker.client import (
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupWorkerResult,
)

BLOCKING_LOOKUP_SOURCE = "import time\ntime.sleep(5)\n"
LOOKUP_CANCEL_JOIN_SECONDS = 1.0
LOOKUP_CLEANUP_JOIN_SECONDS = 3.0
LOOKUP_PROCESS_WAIT_SECONDS = 0.5
LOOKUP_TERMINATION_GRACE_SECONDS = 0.2
LOOKUP_WORKER_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class LookupCancellationProof:
    """Product outcomes captured before unconditional test cleanup."""

    before_start_joined: bool
    before_start_results: tuple[UsageLookupWorkerResult, ...]
    before_start_process_count: int
    worker_started: bool
    active_joined: bool
    active_results: tuple[UsageLookupWorkerResult, ...]
    active_process_count: int
    active_reaped: bool


def exercise_lookup_worker_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> LookupCancellationProof:
    """Exercise missed-start and active-process cancellation safely."""
    real_popen = subprocess.Popen
    lookup_started = Event()
    lookup_processes: list[subprocess.Popen[bytes]] = []

    def start_blocking_lookup(
        _argv: list[str],
        *,
        close_fds: bool,
        env: dict[str, str],
        shell: bool,
        start_new_session: bool,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            [sys.executable, "-c", BLOCKING_LOOKUP_SOURCE],
            close_fds=close_fds,
            env=env,
            shell=shell,
            start_new_session=start_new_session,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        lookup_processes.append(process)
        lookup_started.set()
        return process

    lookup = UsageLookupWorkerClient(
        UsageLookupModuleLaunchPlanner(Path(sys.executable), {}),
        timeout_seconds=LOOKUP_WORKER_TIMEOUT_SECONDS,
        termination_grace_seconds=LOOKUP_TERMINATION_GRACE_SECONDS,
    )
    with monkeypatch.context() as lookup_patch:
        lookup_patch.setattr(subprocess, "Popen", start_blocking_lookup)
        before_start = _cancel_before_run(lookup, lookup_processes)
        active = _cancel_active_run(
            lookup,
            lookup_started,
            lookup_processes,
        )
    return LookupCancellationProof(
        before_start_joined=before_start[0],
        before_start_results=before_start[1],
        before_start_process_count=before_start[2],
        worker_started=active[0],
        active_joined=active[1],
        active_results=active[2],
        active_process_count=active[3],
        active_reaped=active[4],
    )


def _cancel_before_run(
    lookup: UsageLookupWorkerClient,
    lookup_processes: list[subprocess.Popen[bytes]],
) -> tuple[bool, tuple[UsageLookupWorkerResult, ...], int]:
    release_run = Event()
    results: list[UsageLookupWorkerResult] = []

    def run_after_release() -> None:
        release_run.wait(LOOKUP_CLEANUP_JOIN_SECONDS)
        results.append(lookup.run())

    thread = Thread(target=run_after_release)
    thread_started = False
    joined_before_cleanup = False
    result_snapshot: tuple[UsageLookupWorkerResult, ...] = ()
    process_count = 0
    try:
        thread.start()
        thread_started = True
        lookup.cancel()
        release_run.set()
        thread.join(LOOKUP_CANCEL_JOIN_SECONDS)
        joined_before_cleanup = not thread.is_alive()
        result_snapshot = tuple(results)
        process_count = len(lookup_processes)
    finally:
        release_run.set()
        if thread_started and thread.is_alive():
            lookup.cancel()
            thread.join(LOOKUP_CANCEL_JOIN_SECONDS)
        _cleanup_processes(lookup_processes)
        if thread_started:
            thread.join(LOOKUP_CLEANUP_JOIN_SECONDS)
    return joined_before_cleanup, result_snapshot, process_count


def _cancel_active_run(
    lookup: UsageLookupWorkerClient,
    lookup_started: Event,
    lookup_processes: list[subprocess.Popen[bytes]],
) -> tuple[
    bool,
    bool,
    tuple[UsageLookupWorkerResult, ...],
    int,
    bool,
]:
    results: list[UsageLookupWorkerResult] = []

    def run_lookup() -> None:
        results.append(lookup.run())

    thread = Thread(target=run_lookup)
    thread_started = False
    worker_started = False
    joined_before_cleanup = False
    result_snapshot: tuple[UsageLookupWorkerResult, ...] = ()
    process_count = 0
    reaped_before_cleanup = False
    try:
        thread.start()
        thread_started = True
        worker_started = lookup_started.wait(LOOKUP_CANCEL_JOIN_SECONDS)
        if worker_started:
            lookup.cancel()
            lookup.cancel()
        thread.join(LOOKUP_CANCEL_JOIN_SECONDS)
        joined_before_cleanup = not thread.is_alive()
        result_snapshot = tuple(results)
        process_count = len(lookup_processes)
        if joined_before_cleanup and process_count == 1:
            reaped_before_cleanup = _process_was_reaped(
                lookup_processes[0]
            )
    finally:
        lookup.cancel()
        if thread_started:
            thread.join(LOOKUP_CANCEL_JOIN_SECONDS)
        _cleanup_processes(lookup_processes)
        if thread_started:
            thread.join(LOOKUP_CLEANUP_JOIN_SECONDS)
    return (
        worker_started,
        joined_before_cleanup,
        result_snapshot,
        process_count,
        reaped_before_cleanup,
    )


def _process_was_reaped(process: subprocess.Popen[bytes]) -> bool:
    try:
        os.waitpid(process.pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return False


def _cleanup_processes(
    processes: list[subprocess.Popen[bytes]],
) -> None:
    for process in processes:
        if process.poll() is not None:
            continue
        process.terminate()
        try:
            process.wait(timeout=LOOKUP_PROCESS_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=LOOKUP_PROCESS_WAIT_SECONDS)
