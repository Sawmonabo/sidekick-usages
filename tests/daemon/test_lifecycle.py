"""Load-bearing resident-service lifecycle contracts."""

import os
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages import __version__
from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import AccountLabel, ExitCode, ProviderId
from sidekick_usages.daemon.control.client import ControlClient
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
from sidekick_usages.daemon.lifecycle.ports import (
    ServiceBackend,
    ServiceLifecycleObserver,
)
from sidekick_usages.daemon.lifecycle.readiness import (
    RuntimeCleanup,
    SupervisorReadiness,
)
from sidekick_usages.daemon.models.lifecycle import (
    CommandResult,
    PlatformInfo,
    ServiceBackendStatus,
    ServiceLifecycleObservation,
    SupervisorHealth,
)
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    ControlEvent,
)
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceBackendId,
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecyclePhase,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    CompletionOutcome,
    EventKind,
)
from sidekick_usages.daemon.types.service import PackageVersion, ServicePhase
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from tests.fakes.daemon.lifecycle import (
    LifecycleCancellationProof,
    exercise_lifecycle_command_cancellation,
)
from tests.support.daemon import make_supervisor_health
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
)
from tests.support.platform import (
    MANAGED_RUNTIME_REASON,
    MANAGED_RUNTIME_SUPPORTED,
    REQUIRES_MANAGED_RUNTIME,
)
from tests.support.time import REFERENCE_TIME, FixedClock

_OWNER_FILE_MODE = 0o600
_READINESS_REQUEST_ID = RequestId("88888888-8888-4888-8888-888888888888")


class RecordingRunner(SystemCommandRunner):
    """Record native commands and return healthy synthetic status."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.rescue_output = WSL_RESCUE_INSTALLED
        self.rescue_status_failure: ServiceFailureCode | None = None
        self.systemd_status_failure: ServiceFailureCode | None = None

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        self.calls.append(argv)
        if argv[:3] == ("systemctl", "--user", "show"):
            if self.systemd_status_failure is not None:
                raise ServiceLifecycleError(self.systemd_status_failure)
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
            if self.rescue_status_failure is not None:
                raise ServiceLifecycleError(self.rescue_status_failure)
            return CommandResult(0, self.rescue_output + "\n", "")
        return CommandResult(0, "", "")


class ReadinessControlClient:
    """Complete synthetic local-control readiness without a real socket."""

    def handshake(self) -> AcceptedPayload:
        """Accept one compatible supervisor handshake."""
        return AcceptedPayload(None)

    def refresh_all(self) -> Iterator[ControlEvent]:
        """Complete one bounded maintenance-readiness request."""
        yield ControlEvent(
            protocol_version=PROTOCOL_VERSION,
            request_id=_READINESS_REQUEST_ID,
            kind=EventKind.COMPLETED,
            payload=CompletedPayload(None, CompletionOutcome.SUCCEEDED),
            package_version=__version__,
        )

    def close(self) -> None:
        """Close one synthetic observation boundary."""


class ReadyProviderCapabilities:
    """Record provider capability proofs without opening provider state."""

    def __init__(self) -> None:
        self.checked: list[ProviderId] = []

    def cancel(self) -> None:
        """Leave synthetic provider state unchanged."""

    def ready(self, provider_id: ProviderId) -> bool:
        """Record and approve one synthetic provider capability."""
        self.checked.append(provider_id)
        return True


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
        *,
        progress: ServiceLifecycleObserver,
    ) -> None:
        del progress
        providers = "+".join(provider_id.value for provider_id in provider_ids)
        self.events.append("ready" if not providers else f"ready:{providers}")

    def complete_maintenance_pass(
        self,
        progress: ServiceLifecycleObserver,
    ) -> None:
        del progress
        self.events.append("maintain")

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        self.events.append("health")
        return replace(
            make_supervisor_health(),
            backend=status.backend,
            process=status.process,
            rescue=status.rescue,
        )


class RecordingBackend:
    """Record one healthy backend lifecycle."""

    id = ServiceBackendId.SYSTEMD

    def __init__(
        self,
        events: list[str],
        *,
        backend_id: ServiceBackendId = ServiceBackendId.SYSTEMD,
        status_failure: ServiceFailureCode | None = None,
    ) -> None:
        self.events = events
        self.id = backend_id
        self.status_failure = status_failure

    def cancel(self) -> None:
        """Record backend command cancellation."""
        self.events.append("cancel-backend")

    def install(self, progress: ServiceLifecycleObserver) -> None:
        del progress
        self.events.append("install")

    def restart(self, progress: ServiceLifecycleObserver) -> None:
        del progress
        self.events.append("restart")

    def status(self) -> ServiceBackendStatus:
        self.events.append("status")
        if self.status_failure is not None:
            raise ServiceLifecycleError(self.status_failure)
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
        uid=tmp_path.parent.stat().st_uid,
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


def _exercise_wsl_health_failures(
    backend: ServiceBackend,
    manager: DaemonManager,
    runner: RecordingRunner,
    paths: ApplicationPaths,
) -> None:
    """Prove truthful WSL components and cancellation propagation."""
    failed_health = DaemonManager(
        RecordingBackend(
            [],
            backend_id=ServiceBackendId.WSL,
            status_failure=ServiceFailureCode.COMMAND_FAILED,
        ),
        ReadyLifecycle([]),
        RuntimeCleanup(paths),
    ).health()
    with pytest.raises(ValueError, match="explicit rescue state"):
        ServiceBackendStatus.single(
            ServiceBackendId.WSL,
            ServiceLifecycleState.UNHEALTHY,
        )
    runner.systemd_status_failure = ServiceFailureCode.CANCELLED
    with pytest.raises(ServiceLifecycleError) as systemd_cancelled:
        manager.health()
    runner.systemd_status_failure = None
    runner.rescue_status_failure = ServiceFailureCode.CANCELLED
    with pytest.raises(ServiceLifecycleError) as rescue_cancelled:
        backend.status()
    runner.rescue_status_failure = None
    assert (
        failed_health.process,
        failed_health.rescue,
        systemd_cancelled.value.code,
        rescue_cancelled.value.code,
    ) == (
        ServiceComponentState.UNHEALTHY,
        ServiceComponentState.UNHEALTHY,
        ServiceFailureCode.CANCELLED,
        ServiceFailureCode.CANCELLED,
    )


def _exercise_real_lifecycle_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    tuple[
        ServiceLifecycleState,
        ServiceLifecycleState,
        ServiceLifecycleState,
    ],
    tuple[ProviderId, ...],
    tuple[ServiceLifecycleObservation, ...],
]:
    """Run the real lifecycle stack against synthetic local boundaries."""
    root = tmp_path / "progress"
    home = root / "home"
    paths = replace(
        make_application_paths(root),
        systemd_user_service=(
            home / ".config" / "systemd" / "user" / "sidekick-usages.service"
        ),
    )
    account_store = make_account_store(
        root,
        (
            Account(
                label=AccountLabel("codex-stored"),
                credentials=CodexCredentials(
                    access_token="test-only-codex-secret",
                    account_id="account-stored",
                ),
            ),
        ),
    )
    state_store = ServiceStateStore(paths.service_state)
    degraded = ServiceState(
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
    state_store.save(degraded)
    assert not degraded.ready_for(broker_required=True)
    provider_capabilities = ReadyProviderCapabilities()
    platform_info = _platform(home, system="Linux")
    readiness = SupervisorReadiness(
        paths,
        FixedClock(),
        provider_readiness=provider_capabilities,
    )
    cleanup = RuntimeCleanup(paths)
    manager = DaemonManager(
        build_service_backend(
            platform_info,
            lambda: _supervisor_executable(root),
            paths,
            RecordingRunner(),
            ServiceArtifactStore(platform_info.home, platform_info.uid),
        ),
        readiness,
        cleanup,
    )
    connection_attempts = 0

    def connect_after_startup(_socket_path: Path) -> ReadinessControlClient:
        nonlocal connection_attempts
        connection_attempts += 1
        if connection_attempts == 1:
            raise FileNotFoundError
        return ReadinessControlClient()

    monkeypatch.setattr(
        ControlClient,
        "connect",
        staticmethod(connect_after_startup),
    )
    readiness.enroll_accounts()
    broker_manager = DaemonManager(
        RecordingBackend([]),
        readiness,
        cleanup,
    )
    unscoped = broker_manager.status()
    codex_scoped = broker_manager.status((ProviderId.CODEX,))
    stored_account = account_store.saved_accounts()[0]
    account_store.remove_saved(
        stored_account.account_id,
        expected=stored_account,
    )
    state_store.save(
        ServiceState(
            protocol_version=PROTOCOL_VERSION,
            package_version=PackageVersion(__version__),
            phase=ServicePhase.READY,
            revision=2,
            observed_at=REFERENCE_TIME,
            queue_recovered=True,
            journals_reconciled=True,
            broker_ready=True,
            active_workers=0,
        )
    )
    progress: list[ServiceLifecycleObservation] = []
    result = manager.install(
        (ProviderId.CODEX,),
        progress=progress.append,
    )
    return (
        (unscoped.state, codex_scoped.state, result.state),
        tuple(provider_capabilities.checked),
        tuple(progress),
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
    if (
        not MANAGED_RUNTIME_SUPPORTED
        and backend_id is not ServiceBackendId.FEATURE_DISABLED
    ):
        pytest.skip(MANAGED_RUNTIME_REASON)
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
        runner.systemd_status_failure = ServiceFailureCode.COMMAND_FAILED
        systemd_degraded = backend.status()
        runner.systemd_status_failure = None
        runner.rescue_status_failure = ServiceFailureCode.COMMAND_FAILED
        rescue_degraded = backend.status()
        runner.rescue_status_failure = None
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
        rescue, rescue_status = (
            next(
                argv[-1]
                for argv in runner.calls
                if argv[0] == "powershell.exe"
                and "Register-ScheduledTask" in argv[-1]
            ),
            next(
                argv[-1]
                for argv in runner.calls
                if argv[0] == "powershell.exe"
                and "Get-ScheduledTask" in argv[-1]
            ),
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
            (contract in rescue_status) is expected
            for contract, expected in (
                ("$tasks.Count -ne 1", True),
                (f"$task.TaskName -ceq '{WSL_RESCUE_TASK_NAME}'", True),
                ("$task.TaskPath -ceq '\\'", True),
                ("$triggers[0].Enabled -eq $true", True),
                ("function Resolve-AccountSid", True),
                ("SecurityIdentifier]::new($accountIdentity)", True),
                (
                    "[System.Security.Principal.NTAccount]$accountIdentity",
                    True,
                ),
                ("catch [System.ArgumentException]", True),
                ("$accountIdentity.Contains('\\')", True),
                ("$accountIdentity.Contains('@')", True),
                ("::GetCurrent().User.Value", True),
                ("$scheduler.GetFolder('\\').GetTask(", True),
                ("$definition.Task.Triggers.LogonTrigger.UserId", True),
                ("$definition.Task.Principals.Principal.UserId", True),
                ("$triggerSid -ceq $currentSid", True),
                ("$principals.Count -eq 1", True),
                ("$principalSid -ceq $currentSid", True),
                (
                    "[string]$principals[0].LogonType -ceq 'Interactive'",
                    True,
                ),
                (
                    "[string]$principals[0].RunLevel -ceq 'Limited'",
                    True,
                ),
                ("$settings.Count -eq 1", True),
                ("$settings[0].Enabled -eq $true", True),
                ("$settings[0].StartWhenAvailable -eq $true", True),
                (
                    "[string]$settings[0].MultipleInstances -ceq 'IgnoreNew'",
                    True,
                ),
                ("$settings[0].Hidden -eq $true", True),
                ("$settings[0].ExecutionTimeLimit -ceq 'PT2M'", True),
                ("$currentUser", False),
                (".UserId -ieq", False),
                ("catch {", False),
            )
        )
        _exercise_wsl_health_failures(backend, manager, runner, paths)


@REQUIRES_MANAGED_RUNTIME
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
    progress_states, provider_checks, progress = (
        _exercise_real_lifecycle_progress(
            tmp_path,
            monkeypatch,
        )
    )

    assert (
        progress_states,
        provider_checks,
        tuple(item.phase for item in progress),
        progress[-1].provider_id,
    ) == (
        (
            ServiceLifecycleState.READY,
            ServiceLifecycleState.READY,
            ServiceLifecycleState.READY,
        ),
        (ProviderId.CODEX, ProviderId.CODEX, ProviderId.CODEX),
        (
            ServiceLifecyclePhase.INSTALLING,
            ServiceLifecyclePhase.STARTING,
            ServiceLifecyclePhase.CONTROL_SOCKET,
            ServiceLifecyclePhase.DURABLE_RECOVERY,
            ServiceLifecyclePhase.PROVIDER_CAPABILITY,
            ServiceLifecyclePhase.MAINTENANCE_COMPLETED,
            ServiceLifecyclePhase.RESTARTING,
            ServiceLifecyclePhase.CONTROL_SOCKET,
            ServiceLifecyclePhase.DURABLE_RECOVERY,
            ServiceLifecyclePhase.PROVIDER_CAPABILITY,
        ),
        ProviderId.CODEX,
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
