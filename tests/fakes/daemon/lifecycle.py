"""Bounded resident-service lifecycle cancellation proof."""

import subprocess
import sys
from dataclasses import dataclass
from threading import Event, Thread

import pytest

from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode
from sidekick_usages.platform.process import SubprocessProcessGroup

COMMAND_JOIN_SECONDS = 2.0
BLOCKING_COMMAND_SOURCE = "import time; time.sleep(30)"


@dataclass(frozen=True, slots=True)
class LifecycleCancellationProof:
    """Outcomes retained after unconditional native-process cleanup."""

    owner_joined: bool
    failures: tuple[ServiceFailureCode, ...]
    launch_options: tuple[tuple[bool, bool], ...]
    process_count: int
    process_group_reaped: bool


def exercise_lifecycle_command_cancellation(
    runner: SystemCommandRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> LifecycleCancellationProof:
    """Cancel one active isolated command through its owning runner."""
    real_popen = subprocess.Popen
    command_started = Event()
    command_finished = Event()
    processes: list[subprocess.Popen[bytes]] = []
    failures: list[ServiceFailureCode] = []
    launch_options: list[tuple[bool, bool]] = []

    def start_command(
        argv: list[str],
        *,
        close_fds: bool,
        shell: bool,
        start_new_session: bool,
        stdin: int,
        stdout: int,
        stderr: int,
    ) -> subprocess.Popen[bytes]:
        process = real_popen(
            argv,
            close_fds=close_fds,
            shell=shell,
            start_new_session=start_new_session,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
        processes.append(process)
        launch_options.append((shell, start_new_session))
        command_started.set()
        return process

    def run_command() -> None:
        try:
            runner.run(
                (
                    sys.executable,
                    "-c",
                    BLOCKING_COMMAND_SOURCE,
                )
            )
        except ServiceLifecycleError as error:
            failures.append(error.code)
        finally:
            command_finished.set()

    monkeypatch.setattr(subprocess, "Popen", start_command)
    command_thread = Thread(target=run_command)
    command_thread.start()
    try:
        if not command_started.wait(COMMAND_JOIN_SECONDS):
            raise AssertionError("Native lifecycle command did not start.")
        runner.cancel()
        finished = command_finished.wait(COMMAND_JOIN_SECONDS)
        command_thread.join(COMMAND_JOIN_SECONDS)
    finally:
        runner.cancel()
        command_thread.join(COMMAND_JOIN_SECONDS)
    process_group_reaped = (
        len(processes) == 1
        and not SubprocessProcessGroup(processes[0]).group_alive()
    )
    return LifecycleCancellationProof(
        owner_joined=finished and not command_thread.is_alive(),
        failures=tuple(failures),
        launch_options=tuple(launch_options),
        process_count=len(processes),
        process_group_reaped=process_group_reaped,
    )
