"""Load-bearing resident-service lifecycle contracts."""

import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.control.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.constants import (
    WSL_RESCUE_ABSENT,
    WSL_RESCUE_INSTALLED,
    WSL_RESCUE_TASK_NAME,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.manager import (
    DaemonManager,
    build_service_backend,
)
from sidekick_usages.daemon.lifecycle.readiness import (
    RuntimeCleanup,
    SupervisorReadiness,
)
from sidekick_usages.daemon.models.lifecycle import (
    CommandResult,
    PlatformInfo,
    ServiceBackendStatus,
    SupervisorHealth,
)
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceBackendId,
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.service import PackageVersion, ServicePhase
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from tests.fakes.daemon.lifecycle import (
    LifecycleCancellationProof,
    exercise_lifecycle_command_cancellation,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
    make_supervisor_health,
)

_OWNER_FILE_MODE = 0o600


class RecordingRunner(SystemCommandRunner):
    """Record native commands and return healthy synthetic status."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.rescue_output = WSL_RESCUE_INSTALLED
        self.rescue_status_fails = False
        self.systemd_status_fails = False

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        if argv[:3] == ("systemctl", "--user", "show"):
            if self.systemd_status_fails:
                raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
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
            if self.rescue_status_fails:
                raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
            return CommandResult(0, self.rescue_output + "\n", "")
        return CommandResult(0, "", "")


class ReadyLifecycle:
    """Record the exact readiness sequence without provider activity."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def cancel(self) -> None:
        """Record readiness cancellation."""
        self.events.append("cancel-readiness")

    def enroll_accounts(self) -> None:
        self.events.append("enroll")

    def verify_ready(
        self,
        provider_ids: ProviderReadinessScope = (),
    ) -> None:
        providers = "+".join(
            provider_id.value for provider_id in provider_ids
        )
        self.events.append("ready" if not providers else f"ready:{providers}")

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

    def cancel(self) -> None:
        """Record backend command cancellation."""
        self.events.append("cancel-backend")

    def install(self) -> None:
        self.events.append("install")

    def restart(self) -> None:
        self.events.append("restart")

    def status(self) -> ServiceBackendStatus:
        self.events.append("status")
        return ServiceBackendStatus.single(
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


def _state_tree_snapshot(
    root: Path,
) -> tuple[tuple[str, bytes | None, int], ...]:
    """Capture every synthetic state path, payload, and modification time."""
    return tuple(
        (
            "." if path == root else str(path.relative_to(root)),
            path.read_bytes() if path.is_file() else None,
            path.stat().st_mtime_ns,
        )
        for path in (root, *sorted(root.rglob("*")))
    )


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
        lambda: executable,
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

    assert (result.state, result.exit_code) == (
        ServiceLifecycleState.READY,
        ExitCode.SUCCESS,
    )
    status = backend.status()
    assert (status.process, status.rescue) == (
        ServiceComponentState.HEALTHY,
        (
            ServiceComponentState.HEALTHY
            if backend_id is ServiceBackendId.WSL
            else ServiceComponentState.NOT_REQUIRED
        ),
    )
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
        runner.systemd_status_fails = True
        systemd_degraded = backend.status()
        runner.systemd_status_fails = False
        runner.rescue_status_fails = True
        rescue_degraded = backend.status()
        runner.rescue_status_fails = False
        runner.rescue_output = WSL_RESCUE_ABSENT
        degraded = backend.status()
        assert (
            systemd_degraded,
            rescue_degraded,
            degraded,
        ) == (
            ServiceBackendStatus(
                ServiceBackendId.WSL,
                ServiceLifecycleState.UNHEALTHY,
                ServiceComponentState.UNHEALTHY,
                ServiceComponentState.HEALTHY,
            ),
            ServiceBackendStatus(
                ServiceBackendId.WSL,
                ServiceLifecycleState.UNHEALTHY,
                ServiceComponentState.HEALTHY,
                ServiceComponentState.UNHEALTHY,
            ),
            ServiceBackendStatus(
                ServiceBackendId.WSL,
                ServiceLifecycleState.UNHEALTHY,
                ServiceComponentState.HEALTHY,
                ServiceComponentState.ABSENT,
            ),
        )
        rescue = next(
            argv[-1]
            for argv in runner.calls
            if argv[0] == "powershell.exe"
            and "Register-ScheduledTask" in argv[-1]
        )
        rescue_status = next(
            argv[-1]
            for argv in runner.calls
            if argv[0] == "powershell.exe" and "Get-ScheduledTask" in argv[-1]
        )
        assert all(
            contract in rescue
            for contract in (
                "New-ScheduledTaskTrigger -AtLogOn -User $currentUser",
                "New-ScheduledTaskPrincipal -UserId $currentUser",
                "-LogonType Interactive",
                "-RunLevel Limited",
                "$trigger.Enabled = $true",
                "$settings.Enabled = $true",
                "-TaskPath '\\'",
                "-Principal $principal",
                "Sidekick-Distro",
                "sidekick-user",
                "systemctl",
                "--user",
                "start",
            )
        )
        assert all(
            forbidden not in rescue.lower()
            for forbidden in ("maintain", "refresh", "token")
        )
        assert all(
            contract in rescue_status
            for contract in (
                "$tasks.Count -ne 1",
                f"$task.TaskName -ceq '{WSL_RESCUE_TASK_NAME}'",
                "$task.TaskPath -ceq '\\'",
                "$triggers[0].Enabled -eq $true",
                "$triggers[0].UserId -ieq $currentUser",
                "$principals.Count -eq 1",
                "$principals[0].UserId -ieq $currentUser",
                "[string]$principals[0].LogonType -ceq 'Interactive'",
                "[string]$principals[0].RunLevel -ceq 'Limited'",
                "$settings.Count -eq 1",
                "$settings[0].Enabled -eq $true",
                "$settings[0].StartWhenAvailable -eq $true",
                "[string]$settings[0].MultipleInstances -ceq 'IgnoreNew'",
                "$settings[0].Hidden -eq $true",
                "$settings[0].ExecutionTimeLimit -ceq 'PT2M'",
            )
        )


def test_lifecycle_is_idempotent_cancellable_and_preserves_user_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle work is idempotent, bounded, and ownership-safe."""
    passive_root = tmp_path / "passive-state"
    passive_paths = make_application_paths(passive_root)
    passive_state = ServiceStateStore(passive_paths.service_state)
    passive_state.save(
        ServiceState(
            protocol_version=PROTOCOL_VERSION,
            package_version=PackageVersion(__version__),
            phase=ServicePhase.READY,
            revision=1,
            observed_at=REFERENCE_TIME,
            queue_recovered=True,
            journals_reconciled=True,
            broker_ready=True,
            active_workers=0,
        )
    )
    passive_state.path.with_name(f"{passive_state.path.name}.lock").unlink()
    passive_manager = DaemonManager(
        RecordingBackend([]),
        SupervisorReadiness(passive_paths, FixedClock()),
        RuntimeCleanup(passive_paths),
    )
    state_before = _state_tree_snapshot(passive_root)

    passive_health = passive_manager.health()

    assert (
        passive_health.queue,
        passive_health.journal,
        _state_tree_snapshot(passive_root),
    ) == (
        ServiceComponentState.HEALTHY,
        ServiceComponentState.HEALTHY,
        state_before,
    )
    assert (
        ServiceBackendStatus.single(
            ServiceBackendId.SYSTEMD,
            ServiceLifecycleState.INSTALLED,
        ).process
        is ServiceComponentState.UNHEALTHY
    )
    paths = make_application_paths(tmp_path / "state")
    _write_user_state_sentinels(paths, tmp_path)
    _write_service_state_sentinels(paths)
    events: list[str] = []
    runner = SystemCommandRunner()
    manager = DaemonManager(
        RecordingBackend(events),
        ReadyLifecycle(events),
        RuntimeCleanup(paths),
    )

    first = manager.install()
    second = manager.install()
    restarted = manager.restart()
    status = manager.status()
    claude_status = manager.status((ProviderId.CLAUDE,))
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
        "restart",
        "ready",
        "status",
        "status",
        "ready",
        "status",
        "ready:claude",
        "status",
        "health",
        "uninstall",
    ]
    assert first.state is ServiceLifecycleState.READY
    assert second.state is ServiceLifecycleState.READY
    assert restarted.state is ServiceLifecycleState.READY
    assert status.state is ServiceLifecycleState.READY
    assert claude_status.state is ServiceLifecycleState.READY
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
    if os.name == "posix":
        assert exercise_lifecycle_command_cancellation(
            runner,
            monkeypatch,
        ) == LifecycleCancellationProof(
            owner_joined=True,
            failures=(ServiceFailureCode.CANCELLED,),
            launch_options=((False, True),),
            process_count=1,
            process_group_reaped=True,
        )
    broker_degraded = ServiceState(
        protocol_version=PROTOCOL_VERSION,
        package_version=PackageVersion(__version__),
        phase=ServicePhase.DEGRADED,
        revision=1,
        observed_at=REFERENCE_TIME,
        queue_recovered=True,
        journals_reconciled=True,
        broker_ready=False,
        active_workers=0,
        failure_code=ServiceFailureCode.CODEX_BROKER_UNAVAILABLE.value,
    )
    assert broker_degraded.ready_for((ProviderId.CLAUDE,))
    assert not broker_degraded.ready_for((ProviderId.CODEX,))
    assert not broker_degraded.ready_for(
        (ProviderId.CLAUDE, ProviderId.CODEX)
    )
    manager.cancel()
    assert events[-2:] == ["cancel-backend", "cancel-readiness"]


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
