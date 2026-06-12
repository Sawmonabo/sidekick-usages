"""Reusable daemon backend architecture tests."""

from pathlib import Path

from sidekick_usages.daemon import (
    CommandResult,
    DaemonManager,
    PlatformInfo,
    SystemCommandRunner,
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
    assert "wsl.exe" in argv[-1]
    assert "-d Ubuntu" in argv[-1]


def test_systemd_backend_writes_user_service_and_timer(
    tmp_path: Path,
) -> None:
    """Systemd backend writes reusable user-level unit files."""
    runner = RecordingRunner()
    manager = DaemonManager(
        command=("sidekick-usages", "refresh", "--all", "--quiet"),
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
    assert "sidekick-usages refresh --all --quiet" in service.read_text()
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
    assert runner.calls[0][0][:3] == ("launchctl", "bootstrap", "gui/501")
