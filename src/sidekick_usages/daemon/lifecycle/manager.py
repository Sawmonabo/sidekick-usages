"""Cross-platform per-user supervisor lifecycle orchestration."""

from collections.abc import Callable
from pathlib import Path
from typing import assert_never

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.launchd import LaunchdBackend
from sidekick_usages.daemon.lifecycle.platform import (
    FeatureDisabledBackend,
    detect_platform_info,
    qualify_supervisor_executable,
    resolve_supervisor_executable,
)
from sidekick_usages.daemon.lifecycle.ports import (
    ServiceBackend,
    ServiceCleanup,
    ServiceReadiness,
)
from sidekick_usages.daemon.lifecycle.readiness import (
    RuntimeCleanup,
    SupervisorReadiness,
)
from sidekick_usages.daemon.lifecycle.systemd import SystemdBackend
from sidekick_usages.daemon.lifecycle.wsl import WslBackend
from sidekick_usages.daemon.models.lifecycle import (
    DaemonOperationResult,
    PlatformInfo,
    ServiceBackendStatus,
    SupervisorHealth,
)
from sidekick_usages.daemon.types.lifecycle import (
    DaemonOperation,
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecycleState,
)
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import ApplicationPaths, discover_application_paths

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

    def install(self) -> DaemonOperationResult:
        """Install, prove readiness, restart, and prove singleton health."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            self._readiness.enroll_accounts()
            self._backend.install()
            self._readiness.verify_ready()
            self._readiness.complete_maintenance_pass()
            self._backend.restart()
            self._readiness.verify_ready()
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

    def status(self) -> DaemonOperationResult:
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
            self._readiness.verify_ready()
        except ServiceLifecycleError as error:
            return self._failure(error)
        return DaemonOperationResult(
            self._backend.id,
            ServiceLifecycleState.READY,
            _READY_MESSAGE,
        )

    def restart(self) -> DaemonOperationResult:
        """Restart and prove readiness of one installed user service."""
        if self._feature_disabled:
            return self._feature_disabled_result()
        try:
            self._backend.restart()
            self._readiness.verify_ready()
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
        except ServiceLifecycleError:
            status = ServiceBackendStatus(
                self._backend.id,
                ServiceLifecycleState.UNHEALTHY,
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
        )


def build_service_backend(
    platform_info: PlatformInfo,
    supervisor_executable: Callable[[], Path],
    paths: ApplicationPaths,
    runner: SystemCommandRunner,
    artifacts: ServiceArtifactStore,
) -> ServiceBackend:
    """Build the one supported backend for explicit platform facts."""
    if platform_info.system == "Windows":
        return FeatureDisabledBackend()
    qualified_executable = qualify_supervisor_executable(
        supervisor_executable()
    )
    if platform_info.system == "Darwin":
        return LaunchdBackend(
            paths.launch_agent,
            qualified_executable,
            paths.service_logs,
            platform_info.uid,
            runner,
            artifacts,
        )
    if platform_info.system != "Linux" or not platform_info.has_user_systemd:
        return FeatureDisabledBackend()
    systemd = SystemdBackend(
        paths.systemd_user_service,
        qualified_executable,
        runner,
        artifacts,
    )
    if platform_info.is_wsl:
        return WslBackend(systemd, platform_info, runner)
    return systemd


def build_daemon_manager(
    *,
    paths: ApplicationPaths | None = None,
    clock: Clock | None = None,
) -> DaemonManager:
    """Compose lifecycle management without importing resident runtime."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_clock = SystemClock() if clock is None else clock
    platform_info = detect_platform_info()
    runner = SystemCommandRunner()
    backend = build_service_backend(
        platform_info,
        resolve_supervisor_executable,
        resolved_paths,
        runner,
        ServiceArtifactStore(platform_info.home, platform_info.uid),
    )
    return DaemonManager(
        backend,
        SupervisorReadiness(resolved_paths, resolved_clock),
        RuntimeCleanup(resolved_paths),
    )
