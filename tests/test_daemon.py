"""Load-bearing resident-service lifecycle contracts."""

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.manager import (
    DaemonManager,
    build_service_backend,
)
from sidekick_usages.daemon.lifecycle.readiness import RuntimeCleanup
from sidekick_usages.daemon.models.lifecycle import (
    CommandResult,
    PlatformInfo,
    ServiceBackendStatus,
    SupervisorHealth,
)
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
    ServiceLifecycleState,
)
from sidekick_usages.paths import ApplicationPaths
from tests.test_support import (
    make_application_paths,
    make_supervisor_health,
)

_OWNER_FILE_MODE = 0o600


class RecordingRunner(SystemCommandRunner):
    """Record native commands and return healthy synthetic status."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        if argv[:3] == ("systemctl", "--user", "show"):
            return CommandResult(
                0,
                (
                    "LoadState=loaded\n"
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "UnitFileState=enabled\n"
                ),
                "",
            )
        if argv[:2] == ("launchctl", "print"):
            return CommandResult(0, "state = running\n", "")
        if argv[0] == "powershell.exe" and "Get-ScheduledTask" in argv[-1]:
            return CommandResult(0, "sidekick-rescue-installed\n", "")
        return CommandResult(0, "", "")


class ReadyLifecycle:
    """Record the exact readiness sequence without provider activity."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def enroll_accounts(self) -> None:
        self.events.append("enroll")

    def verify_ready(self) -> None:
        self.events.append("ready")

    def complete_maintenance_pass(self) -> None:
        self.events.append("maintain")

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        self.events.append("health")
        return replace(
            make_supervisor_health(),
            backend=status.backend,
        )


class RecordingBackend:
    """Record one healthy backend lifecycle."""

    id = ServiceBackendId.SYSTEMD

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def install(self) -> None:
        self.events.append("install")

    def restart(self) -> None:
        self.events.append("restart")

    def status(self) -> ServiceBackendStatus:
        self.events.append("status")
        return ServiceBackendStatus(
            self.id,
            ServiceLifecycleState.READY,
        )

    def uninstall(self) -> None:
        self.events.append("uninstall")


def _platform(
    tmp_path: Path,
    *,
    system: str,
    is_wsl: bool = False,
) -> PlatformInfo:
    return PlatformInfo(
        system=system,
        home=tmp_path,
        uid=os.geteuid(),
        user_name="sidekick-user",
        is_wsl=is_wsl,
        wsl_distro="Sidekick-Distro" if is_wsl else None,
        has_user_systemd=system == "Linux",
    )


def _supervisor_executable(tmp_path: Path) -> Path:
    executable = tmp_path / "bin" / "sidekick-usages-supervisor"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


@pytest.mark.parametrize(
    ("system", "is_wsl", "backend_id"),
    [
        ("Linux", False, ServiceBackendId.SYSTEMD),
        ("Linux", True, ServiceBackendId.WSL),
        ("Darwin", False, ServiceBackendId.LAUNCHD),
        ("Windows", False, ServiceBackendId.FEATURE_DISABLED),
    ],
    ids=("linux", "wsl", "macos", "native-windows"),
)
def test_service_artifacts_are_user_scoped_resident_and_secret_free(
    tmp_path: Path,
    system: str,
    is_wsl: bool,
    backend_id: ServiceBackendId,
) -> None:
    """Each supported OS gets one exact resident-service contract."""
    home = tmp_path / "home"
    paths = replace(
        make_application_paths(tmp_path / "state"),
        service_logs=home / "Library" / "Logs" / "sidekick-usages",
        systemd_user_service=(
            home / ".config" / "systemd" / "user" / "sidekick-usages.service"
        ),
        launch_agent=(
            home
            / "Library"
            / "LaunchAgents"
            / "com.sidekick-usages.supervisor.plist"
        ),
    )
    platform_info = _platform(
        home,
        system=system,
        is_wsl=is_wsl,
    )
    runner = RecordingRunner()
    executable = _supervisor_executable(tmp_path)
    backend = build_service_backend(
        platform_info,
        executable,
        paths,
        runner,
        ServiceArtifactStore(platform_info.home, platform_info.uid),
    )
    manager = DaemonManager(
        backend,
        ReadyLifecycle([]),
        RuntimeCleanup(paths),
    )

    result = manager.install()

    assert result.backend is backend_id
    if backend_id is ServiceBackendId.FEATURE_DISABLED:
        assert result.state is ServiceLifecycleState.FEATURE_DISABLED
        assert result.exit_code is ExitCode.MANUAL_ACTION
        assert runner.calls == []
        return

    assert result.state is ServiceLifecycleState.READY
    assert result.exit_code is ExitCode.SUCCESS
    artifact_text = _service_artifact(platform_info, backend_id).read_text(
        encoding="utf-8"
    )
    assert str(executable) in artifact_text
    assert "sidekick-usages-supervisor" in artifact_text
    assert "maintain" not in artifact_text
    assert "refresh" not in artifact_text
    assert "token" not in artifact_text.lower()
    assert (
        stat.S_IMODE(
            _service_artifact(platform_info, backend_id).stat().st_mode
        )
        == _OWNER_FILE_MODE
    )

    if backend_id in {ServiceBackendId.SYSTEMD, ServiceBackendId.WSL}:
        assert "Restart=on-failure" in artifact_text
        assert "WantedBy=default.target" in artifact_text
        assert (
            not _service_artifact(
                platform_info,
                backend_id,
            )
            .with_suffix(".timer")
            .exists()
        )
    if backend_id is ServiceBackendId.LAUNCHD:
        assert "<key>RunAtLoad</key>" in artifact_text
        assert "<key>SuccessfulExit</key>" in artifact_text
        assert "<false/>" in artifact_text
        assert "StartInterval" not in artifact_text
    if backend_id is ServiceBackendId.WSL:
        rescue = next(
            argv[-1]
            for argv in runner.calls
            if argv[0] == "powershell.exe"
            and "Register-ScheduledTask" in argv[-1]
        )
        assert (
            "New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME" in rescue
        )
        assert "Sidekick-Distro" in rescue
        assert "sidekick-user" in rescue
        assert "systemctl" in rescue
        assert "--user" in rescue
        assert "start" in rescue
        assert "maintain" not in rescue
        assert "refresh" not in rescue
        assert "token" not in rescue.lower()


def test_lifecycle_is_idempotent_and_uninstall_preserves_user_state(
    tmp_path: Path,
) -> None:
    """Install, restart, status, and cleanup touch only service ownership."""
    paths = make_application_paths(tmp_path / "state")
    _write_user_state_sentinels(paths, tmp_path)
    _write_service_state_sentinels(paths)
    events: list[str] = []
    manager = DaemonManager(
        RecordingBackend(events),
        ReadyLifecycle(events),
        RuntimeCleanup(paths),
    )

    first = manager.install()
    second = manager.install()
    status = manager.status()
    health = manager.health()
    removed = manager.uninstall()

    install_sequence = [
        "enroll",
        "install",
        "ready",
        "maintain",
        "restart",
        "ready",
        "status",
    ]
    assert events == [
        *install_sequence,
        *install_sequence,
        "status",
        "ready",
        "status",
        "health",
        "uninstall",
    ]
    assert first.state is ServiceLifecycleState.READY
    assert second.state is ServiceLifecycleState.READY
    assert status.state is ServiceLifecycleState.READY
    assert health.process is ServiceComponentState.HEALTHY
    assert removed.state is ServiceLifecycleState.ABSENT
    assert paths.service_state.exists() is False
    assert paths.service_logs.exists() is False
    assert paths.runtime_directory.exists() is False
    assert paths.accounts.read_text(encoding="utf-8") == "account-index"
    assert paths.activity_snapshots.read_text(encoding="utf-8") == "metrics"
    assert (paths.private_credentials / "authority").read_text(
        encoding="utf-8"
    ) == "credential"
    assert (tmp_path / "native-provider-login").read_text(
        encoding="utf-8"
    ) == "provider"


def _service_artifact(
    platform_info: PlatformInfo,
    backend_id: ServiceBackendId,
) -> Path:
    if backend_id is ServiceBackendId.LAUNCHD:
        return (
            platform_info.home
            / "Library"
            / "LaunchAgents"
            / "com.sidekick-usages.supervisor.plist"
        )
    return (
        platform_info.home
        / ".config"
        / "systemd"
        / "user"
        / "sidekick-usages.service"
    )


def _write_user_state_sentinels(
    paths: ApplicationPaths,
    tmp_path: Path,
) -> None:
    paths.accounts.parent.mkdir(mode=0o700, parents=True)
    paths.accounts.write_text("account-index", encoding="utf-8")
    paths.activity_snapshots.write_text("metrics", encoding="utf-8")
    paths.private_credentials.mkdir()
    (paths.private_credentials / "authority").write_text(
        "credential",
        encoding="utf-8",
    )
    (tmp_path / "native-provider-login").write_text(
        "provider",
        encoding="utf-8",
    )


def _write_service_state_sentinels(paths: ApplicationPaths) -> None:
    paths.service_state.write_text("transient", encoding="utf-8")
    paths.service_state.chmod(_OWNER_FILE_MODE)
    paths.service_logs.mkdir(mode=0o700)
    log = paths.service_logs / "supervisor.jsonl"
    log.write_text(
        "transient",
        encoding="utf-8",
    )
    log.chmod(_OWNER_FILE_MODE)
    paths.runtime_directory.mkdir(mode=0o700)
