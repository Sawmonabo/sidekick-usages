"""Cross-platform per-user supervisor lifecycle orchestration."""

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import assert_never

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.constants import (
    CLAUDE_LAUNCHER_OPTION,
    CODEX_LAUNCHER_OPTION,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.platform.launchd import LaunchdBackend
from sidekick_usages.daemon.lifecycle.platform.selection import (
    FeatureDisabledBackend,
    detect_platform_info,
    qualify_supervisor_executable,
    resolve_supervisor_executable,
)
from sidekick_usages.daemon.lifecycle.platform.systemd import SystemdBackend
from sidekick_usages.daemon.lifecycle.platform.wsl import WslBackend
from sidekick_usages.daemon.lifecycle.ports import (
    ProviderCapabilityReadiness,
    ServiceBackend,
    ServiceCleanup,
    ServiceLifecycleObserver,
    ServiceReadiness,
    discard_service_lifecycle_observation,
)
from sidekick_usages.daemon.lifecycle.readiness import (
    RuntimeCleanup,
    SupervisorReadiness,
)
from sidekick_usages.daemon.models.lifecycle import (
    DaemonOperationResult,
    PlatformInfo,
    ServiceBackendStatus,
    ServiceLaunchCommand,
    SupervisorHealth,
)
from sidekick_usages.daemon.types.lifecycle import (
    DaemonOperation,
    ProviderReadinessScope,
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecycleState,
)
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.platform.errors import ExecutableQualificationError
from sidekick_usages.platform.executable import qualify_executable
from sidekick_usages.platform.types import ExecutableFailure

_FEATURE_DISABLED_MESSAGE = (
    "Resident account supervision is disabled on native Windows."
)
_ABSENT_MESSAGE = "The Sidekick user service is not installed."
_READY_MESSAGE = "The Sidekick user service is ready."
_REMOVED_MESSAGE = "The Sidekick user service was removed."


class DaemonManager:
    """Run one exact per-user service lifecycle."""

    def __init__(
        self,
        backend: ServiceBackend,
        readiness: ServiceReadiness,
        cleanup: ServiceCleanup,
    ) -> None:
        self._backend = backend
        self._readiness = readiness
        self._cleanup = cleanup

    def cancel(self) -> None:
        """Interrupt lifecycle observation without provider mutation."""
        self._backend.cancel()
        self._readiness.cancel()

    def run(self, operation: DaemonOperation | str) -> DaemonOperationResult:
        """Run one supported lifecycle operation."""
        try:
            operation_id = DaemonOperation(operation)
        except ValueError as error:
            expected = ", ".join(item.value for item in DaemonOperation)
            raise UsageError(
                f"Unknown daemon operation {operation!r}. "
                f"Expected one of: {expected}."
            ) from error
        match operation_id:
            case DaemonOperation.INSTALL:
                return self.install()
            case DaemonOperation.STATUS:
                return self.status()
            case DaemonOperation.UNINSTALL:
                return self.uninstall()
        assert_never(operation_id)

    def install(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Install, prove readiness, restart, and prove singleton health."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            self._readiness.enroll_accounts()
            self._backend.install(progress)
            self._readiness.wait_until_ready(
                provider_ids,
                progress=progress,
            )
            self._readiness.complete_maintenance_pass(progress)
            self._backend.restart(progress)
            self._readiness.wait_until_ready(
                provider_ids,
                progress=progress,
            )
            status = self._backend.status()
            if status.state is not ServiceLifecycleState.READY:
                raise ServiceLifecycleError(
                    ServiceFailureCode.SERVICE_UNHEALTHY
                )
        except ServiceLifecycleError as error:
            return self._failure(error)
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.READY,
            _READY_MESSAGE,
        )

    def status(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Verify native service state and resident protocol readiness."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            status = self._backend.status()
            if status.state is ServiceLifecycleState.ABSENT:
                return DaemonOperationResult(
                    self._backend.id,
                    status.state,
                    _ABSENT_MESSAGE,
                    ExitCode.MANUAL_ACTION,
                )
            if status.state is not ServiceLifecycleState.READY:
                raise ServiceLifecycleError(
                    ServiceFailureCode.SERVICE_UNHEALTHY
                )
            self._readiness.verify_ready(
                provider_ids,
                progress=progress,
            )
        except ServiceLifecycleError as error:
            return self._failure(error)
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.READY,
            _READY_MESSAGE,
        )

    def restart(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> DaemonOperationResult:
        """Restart and prove readiness of one installed user service."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            self._backend.restart(progress)
            self._readiness.wait_until_ready(
                provider_ids,
                progress=progress,
            )
            status = self._backend.status()
            if status.state is not ServiceLifecycleState.READY:
                raise ServiceLifecycleError(
                    ServiceFailureCode.SERVICE_UNHEALTHY
                )
        except ServiceLifecycleError as error:
            return self._failure(error)
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.READY,
            _READY_MESSAGE,
        )

    def uninstall(self) -> DaemonOperationResult:
        """Remove only the platform service and its transient state."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            self._backend.uninstall()
            self._cleanup.clear()
        except ServiceLifecycleError as error:
            return self._failure(error)
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.ABSENT,
            _REMOVED_MESSAGE,
        )

    def health(self) -> SupervisorHealth:
        """Inspect independent supervisor components without mutation."""
        try:
            status = self._backend.status()
        except ServiceLifecycleError as error:
            if error.code is ServiceFailureCode.CANCELLED:
                raise
            status = ServiceBackendStatus.observation_failed(
                self._backend.id,
            )
        return self._readiness.health(status)

    def quiescent(self) -> bool:
        """Return whether no supported Sidekick user service is installed."""
        try:
            state = self._backend.status().state
        except ServiceLifecycleError:
            return False
        return state in {
            ServiceLifecycleState.ABSENT,
            ServiceLifecycleState.FEATURE_DISABLED,
        }

    @property
    def _feature_disabled(self) -> bool:
        return self._backend.id is ServiceBackendId.FEATURE_DISABLED

    def _feature_disabled_result(self) -> DaemonOperationResult:
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.FEATURE_DISABLED,
            _FEATURE_DISABLED_MESSAGE,
            ExitCode.MANUAL_ACTION,
        )

    def _failure(
        self,
        error: ServiceLifecycleError,
    ) -> DaemonOperationResult:
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.UNHEALTHY,
            str(error),
            ExitCode.SCHEDULER_ERROR,
            failure_code=error.code,
            failure_provider_id=error.provider_id,
        )


def build_service_backend(
    platform_info: PlatformInfo,
    launch_command: Callable[[], ServiceLaunchCommand],
    paths: ApplicationPaths,
    runner: SystemCommandRunner,
    artifacts: ServiceArtifactStore,
) -> ServiceBackend:
    """Build the one supported backend for explicit platform facts."""
    if platform_info.system == "Windows":
        return FeatureDisabledBackend()
    if platform_info.system == "Darwin":
        return LaunchdBackend(
            paths.launch_agent,
            launch_command,
            paths.service_logs,
            platform_info.uid,
            runner,
            artifacts,
        )
    if platform_info.system != "Linux" or not platform_info.has_user_systemd:
        return FeatureDisabledBackend()
    systemd = SystemdBackend(
        paths.systemd_user_service,
        launch_command,
        runner,
        artifacts,
    )
    if platform_info.is_wsl:
        return WslBackend(systemd, platform_info, runner)
    return systemd


def build_daemon_manager(
    *,
    claude_launcher: Callable[[], Path],
    codex_launcher: Callable[[], Path],
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
    provider_readiness: ProviderCapabilityReadiness | None = None,
) -> DaemonManager:
    """Compose lifecycle management without importing resident runtime."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_clock = SystemClock() if clock is None else clock
    platform_info = detect_platform_info()
    runner = SystemCommandRunner()
    readiness = SupervisorReadiness(
        resolved_paths,
        resolved_clock,
        provider_readiness=provider_readiness,
    )
    backend = build_service_backend(
        platform_info,
        partial(
            build_service_launch_command,
            resolve_supervisor_executable,
            claude_launcher,
            codex_launcher,
        ),
        resolved_paths,
        runner,
        ServiceArtifactStore(platform_info.home, platform_info.uid),
    )
    return DaemonManager(
        backend,
        readiness,
        RuntimeCleanup(resolved_paths),
    )


def build_service_launch_command(
    supervisor_executable: Callable[[], Path],
    claude_launcher: Callable[[], Path],
    codex_launcher: Callable[[], Path],
) -> ServiceLaunchCommand:
    """Resolve the exact secret-free command for one service publication."""
    supervisor = qualify_supervisor_executable(supervisor_executable())
    arguments = (
        *_service_launcher_arguments(
            CLAUDE_LAUNCHER_OPTION,
            claude_launcher,
        ),
        *_service_launcher_arguments(
            CODEX_LAUNCHER_OPTION,
            codex_launcher,
        ),
    )
    return ServiceLaunchCommand(supervisor, arguments)


def _service_launcher_arguments(
    option: str,
    launcher: Callable[[], Path],
) -> tuple[str, ...]:
    """Resolve one optional qualified provider launcher argument."""
    try:
        path = launcher()
        qualify_executable(path)
    except ExecutableQualificationError as error:
        if error.code is ExecutableFailure.MISSING:
            return ()
        raise ServiceLifecycleError(
            ServiceFailureCode.EXECUTABLE_UNAVAILABLE
        ) from None
    return option, str(path)
