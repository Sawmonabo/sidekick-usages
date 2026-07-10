"""Reusable daemon backend architecture tests."""

from pathlib import Path

import pytest

from sidekick_usages.daemon import (
    CommandResult,
    DaemonManager,
    DaemonOperation,
    DaemonOperationResult,
    PlatformInfo,
    SystemCommandRunner,
    resolve_maintenance_command,
)
from sidekick_usages.errors import UsageError
from sidekick_usages.scheduler_quiescence import (
    CRON_BEGIN,
    CRON_END,
    SchedulerBackendId,
    SchedulerBackendState,
)


class RecordingRunner(SystemCommandRunner):
    """Command runner that records calls without touching the host."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        self.calls.append((argv, input_text))
        return CommandResult(returncode=0, stdout="", stderr="")


class RecordingDaemonManager(DaemonManager):
    """Daemon manager that records operation dispatch without side effects."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def install(self, backend: str = "auto") -> DaemonOperationResult:
        """Record an install dispatch."""
        self.calls.append(("install", backend))
        return DaemonOperationResult(backend, "installed")

    def status(self, backend: str = "auto") -> DaemonOperationResult:
        """Record a status dispatch."""
        self.calls.append(("status", backend))
        return DaemonOperationResult(backend, "healthy")

    def uninstall(self, backend: str = "auto") -> DaemonOperationResult:
        """Record an uninstall dispatch."""
        self.calls.append(("uninstall", backend))
        return DaemonOperationResult(backend, "removed")


class QuiescenceRunner(SystemCommandRunner):
    """Return scripted read-only results by scheduler backend."""

    def __init__(
        self,
        results: dict[SchedulerBackendId, CommandResult],
    ) -> None:
        self.results = results
        self.calls: list[SchedulerBackendId] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        del input_text
        backend = {
            "systemctl": SchedulerBackendId.SYSTEMD,
            "crontab": SchedulerBackendId.CRON,
            "launchctl": SchedulerBackendId.LAUNCHD,
            "powershell.exe": SchedulerBackendId.TASK_SCHEDULER,
        }[argv[0]]
        self.calls.append(backend)
        return self.results[backend]


def _platform(
    tmp_path: Path,
    *,
    system: str = "Linux",
    is_wsl: bool = False,
    has_user_systemd: bool = True,
) -> PlatformInfo:
    """Build a deterministic platform fixture."""
    return PlatformInfo(
        system=system,
        home=tmp_path,
        uid=501,
        is_wsl=is_wsl,
        wsl_distro="Ubuntu" if is_wsl else None,
        has_user_systemd=has_user_systemd,
    )


def _absent_scheduler_results() -> dict[SchedulerBackendId, CommandResult]:
    """Return successful native probes with no Sidekick schedule."""
    return {
        SchedulerBackendId.SYSTEMD: CommandResult(
            returncode=0,
            stdout=(
                "LoadState=not-found\nActiveState=inactive\nUnitFileState=\n"
            ),
            stderr="",
        ),
        SchedulerBackendId.CRON: CommandResult(0, "", ""),
        SchedulerBackendId.LAUNCHD: CommandResult(
            returncode=0,
            stdout="gui domain contains unrelated services",
            stderr="",
        ),
        SchedulerBackendId.TASK_SCHEDULER: CommandResult(
            returncode=0,
            stdout="sidekick-schedule-absent\n",
            stderr="",
        ),
    }


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (DaemonOperation.INSTALL, "installed"),
        (DaemonOperation.STATUS, "healthy"),
        (DaemonOperation.UNINSTALL, "removed"),
    ],
)
def test_daemon_manager_dispatches_exact_operation(
    operation: DaemonOperation,
    message: str,
) -> None:
    """Each supported operation dispatches once to its matching method."""
    manager = RecordingDaemonManager()

    result = manager.run(operation, "systemd")

    assert manager.calls == [(operation.value, "systemd")]
    assert result == DaemonOperationResult("systemd", message)


def test_invalid_daemon_operation_cannot_dispatch() -> None:
    """Invalid operation input cannot reach a scheduler mutation method."""
    manager = RecordingDaemonManager()

    with pytest.raises(UsageError) as exc_info:
        manager.run("destroy", "systemd")

    assert str(exc_info.value) == (
        "Unknown daemon operation 'destroy'. "
        "Expected one of: install, status, uninstall."
    )
    assert manager.calls == []


def test_wsl_task_scheduler_uses_hidden_windows_wrapper(
    tmp_path: Path,
) -> None:
    """WSL scheduled refresh runs through a Windows-local hidden wrapper."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "maintain", "--quiet"),
        platform_info=_platform(tmp_path, is_wsl=True),
        runner=runner,
    )

    result = manager.install("task-scheduler")

    assert result.backend == "task-scheduler"
    script = runner.calls[0][0][-1]
    assert "New-ScheduledTaskAction -Execute 'wscript.exe'" in script
    assert "//B //Nologo" in script
    assert "$env:LOCALAPPDATA" in script
    assert "Set-Content -Path $vbsPath" in script
    assert "Set-Content -Path $ps1Path" in script
    assert "wsl.exe" in script
    assert "'-d' 'Ubuntu'" in script
    assert "sidekick-usages maintain --quiet" in script
    assert "shell.Run(command, 0, True)" in script
    assert "WScript.Quit code" in script
    assert "refresh.out.log" in script
    assert "refresh.err.log" in script
    assert "New-ScheduledTaskAction -Execute 'wsl.exe'" not in script


def test_windows_task_scheduler_uses_hidden_windows_wrapper(
    tmp_path: Path,
) -> None:
    """Native Windows scheduled refresh also avoids direct console launch."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=(
            "C:\\Program Files\\sidekick\\sidekick-usages.exe",
            "maintain",
            "--quiet",
        ),
        platform_info=_platform(tmp_path, system="Windows"),
        runner=runner,
    )

    result = manager.install("task-scheduler")

    assert result.backend == "task-scheduler"
    script = runner.calls[0][0][-1]
    assert "New-ScheduledTaskAction -Execute 'wscript.exe'" in script
    assert "$env:LOCALAPPDATA" in script
    assert "Set-Content -Path $vbsPath" in script
    assert "Set-Content -Path $ps1Path" in script
    assert "sidekick-usages.exe" in script
    assert "shell.Run(command, 0, True)" in script
    assert "WScript.Quit code" in script
    assert "'maintain' '--quiet'" in script
    assert "refresh.out.log" in script
    assert "refresh.err.log" in script
    assert (
        "New-ScheduledTaskAction "
        "-Execute 'C:\\Program Files\\sidekick\\sidekick-usages.exe'"
    ) not in script


def test_task_scheduler_uninstall_removes_generated_launcher_artifacts(
    tmp_path: Path,
) -> None:
    """Task Scheduler uninstall removes generated wrappers, not logs."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "maintain", "--quiet"),
        platform_info=_platform(tmp_path, is_wsl=True),
        runner=runner,
    )

    result = manager.uninstall("task-scheduler")

    assert result.backend == "task-scheduler"
    script = runner.calls[0][0][-1]
    assert "Unregister-ScheduledTask" in script
    assert "refresh.vbs" in script
    assert "refresh.ps1" in script
    assert "refresh.out.log" not in script
    assert "refresh.err.log" not in script
    assert "Get-ChildItem -LiteralPath $daemonDir" in script
    assert "$remaining.Count -eq 0" in script
    assert "Remove-Item -LiteralPath $daemonDir" in script


def test_daemon_manager_auto_selects_wsl_task_scheduler(
    tmp_path: Path,
) -> None:
    """WSL defaults to Windows Task Scheduler so refresh can wake WSL."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "refresh", "--all", "--quiet"),
        platform_info=_platform(tmp_path, is_wsl=True),
        runner=runner,
    )

    result = manager.install("auto")

    assert result.backend == "task-scheduler"
    assert runner.calls
    argv, _ = runner.calls[0]
    assert argv[0] == "powershell.exe"
    assert "wscript.exe" in argv[-1]
    assert "refresh.vbs" in argv[-1]


def test_systemd_backend_writes_user_service_and_timer(
    tmp_path: Path,
) -> None:
    """Systemd backend writes reusable user-level unit files."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "maintain", "--quiet"),
        platform_info=_platform(tmp_path),
        runner=runner,
    )

    result = manager.install("systemd")

    assert result.backend == "systemd"
    service = (
        tmp_path
        / ".config"
        / "systemd"
        / "user"
        / "sidekick-usages-refresh.service"
    )
    timer = (
        tmp_path
        / ".config"
        / "systemd"
        / "user"
        / "sidekick-usages-refresh.timer"
    )
    assert "sidekick-usages maintain --quiet" in service.read_text()
    assert "OnUnitActiveSec=30m" in timer.read_text()
    assert runner.calls[-1][0] == (
        "systemctl",
        "--user",
        "enable",
        "--now",
        "sidekick-usages-refresh.timer",
    )


def test_launchd_backend_writes_launch_agent(tmp_path: Path) -> None:
    """Launchd backend is a reusable class with deterministic plist output."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "refresh", "--all", "--quiet"),
        platform_info=_platform(tmp_path, system="Darwin"),
        runner=runner,
    )

    result = manager.install("launchd")

    assert result.backend == "launchd"
    plist = (
        tmp_path
        / "Library"
        / "LaunchAgents"
        / "com.sidekick-usages.refresh.plist"
    )
    text = plist.read_text()
    assert "<integer>1800</integer>" in text
    assert "<string>sidekick-usages</string>" in text
    assert "<key>StandardOutPath</key>" in text
    assert "<key>StandardErrorPath</key>" in text
    assert runner.calls[0][0][:3] == ("launchctl", "bootstrap", "gui/501")


def test_default_maintenance_command_runs_maintain_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New daemon installs run the combined maintenance command."""
    monkeypatch.setattr(
        "sidekick_usages.daemon.shutil.which",
        lambda name: (
            "/usr/local/bin/sidekick-usages"
            if name == "sidekick-usages"
            else None
        ),
    )

    assert resolve_maintenance_command() == (
        "/usr/local/bin/sidekick-usages",
        "maintain",
        "--quiet",
    )


@pytest.mark.parametrize(
    ("system", "is_wsl", "expected"),
    [
        (
            "Linux",
            False,
            (SchedulerBackendId.SYSTEMD, SchedulerBackendId.CRON),
        ),
        (
            "Darwin",
            False,
            (SchedulerBackendId.LAUNCHD, SchedulerBackendId.CRON),
        ),
        ("Windows", False, (SchedulerBackendId.TASK_SCHEDULER,)),
        (
            "Linux",
            True,
            (
                SchedulerBackendId.SYSTEMD,
                SchedulerBackendId.CRON,
                SchedulerBackendId.TASK_SCHEDULER,
            ),
        ),
    ],
)
def test_quiescence_checks_every_coexisting_backend(
    tmp_path: Path,
    system: str,
    is_wsl: bool,
    expected: tuple[SchedulerBackendId, ...],
) -> None:
    runner = QuiescenceRunner(_absent_scheduler_results())
    manager = DaemonManager(
        platform_info=_platform(tmp_path, system=system, is_wsl=is_wsl),
        runner=runner,
    )

    assessment = manager.assess_quiescence()

    assert tuple(item.backend for item in assessment.observations) == expected
    assert tuple(item.state for item in assessment.observations) == (
        SchedulerBackendState.ABSENT,
    ) * len(expected)
    assert runner.calls == list(expected)
    assert assessment.quiescent is True
    assert assessment.write_blocked is False


def test_coexisting_installed_and_active_schedules_all_block(
    tmp_path: Path,
) -> None:
    results = _absent_scheduler_results()
    results[SchedulerBackendId.SYSTEMD] = CommandResult(
        returncode=0,
        stdout=(
            "ActiveState=active\nUnitFileState=enabled\nLoadState=loaded\n"
        ),
        stderr="",
    )
    results[SchedulerBackendId.CRON] = CommandResult(
        returncode=0,
        stdout=f"{CRON_BEGIN}\njob\n{CRON_END}\n",
        stderr="",
    )
    runner = QuiescenceRunner(results)
    manager = DaemonManager(
        platform_info=_platform(tmp_path),
        runner=runner,
    )

    assessment = manager.assess_quiescence()

    assert tuple(item.state for item in assessment.observations) == (
        SchedulerBackendState.INSTALLED,
        SchedulerBackendState.INSTALLED,
    )
    assert runner.calls == [
        SchedulerBackendId.SYSTEMD,
        SchedulerBackendId.CRON,
    ]
    assert assessment.write_blocked is True


def test_unassessable_wsl_backends_block_without_short_circuit_or_raw_errors(
    tmp_path: Path,
) -> None:
    raw_error = "synthetic native scheduler detail"
    results = {
        backend: CommandResult(7, "", raw_error)
        for backend in (
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendId.CRON,
            SchedulerBackendId.TASK_SCHEDULER,
        )
    }
    runner = QuiescenceRunner(results)
    manager = DaemonManager(
        platform_info=_platform(tmp_path, is_wsl=True),
        runner=runner,
    )

    first = manager.assess_quiescence()
    second = manager.assess_quiescence()

    expected_backends = [
        SchedulerBackendId.SYSTEMD,
        SchedulerBackendId.CRON,
        SchedulerBackendId.TASK_SCHEDULER,
    ]
    assert tuple(item.state for item in first.observations) == (
        SchedulerBackendState.UNASSESSABLE,
    ) * len(expected_backends)
    assert runner.calls == expected_backends * 2
    assert first == second
    assert first.write_blocked is True
    assert raw_error not in repr(first)
    assert {item.message for item in first.observations} == {
        "Sidekick schedule status could not be assessed."
    }
