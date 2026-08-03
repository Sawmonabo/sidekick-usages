"""Linux and WSL systemd user-service integration."""

from collections.abc import Callable
from pathlib import Path

from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.constants import (
    SERVICE_ARTIFACT_VERSION,
    SYSTEMD_SERVICE_NAME,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.ports import ServiceLifecycleObserver
from sidekick_usages.daemon.models.lifecycle import (
    ServiceArtifact,
    ServiceBackendStatus,
    ServiceLaunchCommand,
    ServiceLifecycleObservation,
)
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecyclePhase,
    ServiceLifecycleState,
)

_SYSTEMCTL = ("systemctl", "--user")
_STATUS_PROPERTIES = (
    "--property=LoadState",
    "--property=ActiveState",
    "--property=SubState",
    "--property=UnitFileState",
)


class SystemdBackend:
    """One resident systemd service owned by the current Linux user."""

    id = ServiceBackendId.SYSTEMD

    def __init__(
        self,
        artifact_path: Path,
        launch_command: Callable[[], ServiceLaunchCommand],
        runner: SystemCommandRunner,
        artifacts: ServiceArtifactStore,
    ) -> None:
        self._artifact_path = artifact_path
        self._launch_command = launch_command
        self._runner = runner
        self._artifacts = artifacts

    def cancel(self) -> None:
        """Interrupt one active systemd user command."""
        self._runner.cancel()

    def install(self, progress: ServiceLifecycleObserver) -> None:
        """Publish, reload, and enable the resident user service."""
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.INSTALLING))
        self._publish()
        self._require_success((*_SYSTEMCTL, "enable", SYSTEMD_SERVICE_NAME))
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.STARTING))
        self._restart()

    def restart(self, progress: ServiceLifecycleObserver) -> None:
        """Republish and restart the current resident user service."""
        progress(ServiceLifecycleObservation(ServiceLifecyclePhase.RESTARTING))
        self._publish()
        self._restart()

    def _publish(self) -> None:
        self._artifacts.write(
            ServiceArtifact(
                self._artifact_path,
                _service_payload(self._launch_command()),
            )
        )
        self._require_success((*_SYSTEMCTL, "daemon-reload"))

    def _restart(self) -> None:
        self._require_success((*_SYSTEMCTL, "restart", SYSTEMD_SERVICE_NAME))

    def status(self) -> ServiceBackendStatus:
        """Return strict installed/running systemd state."""
        artifact_exists = self._artifacts.exists(self._artifact_path)
        result = self._runner.run(
            (
                *_SYSTEMCTL,
                "show",
                *_STATUS_PROPERTIES,
                SYSTEMD_SERVICE_NAME,
            )
        )
        if result.returncode != 0:
            return ServiceBackendStatus.single(
                self.id,
                ServiceLifecycleState.UNHEALTHY,
            )
        properties = _properties(result.stdout)
        if properties is None:
            return ServiceBackendStatus.single(
                self.id,
                ServiceLifecycleState.UNHEALTHY,
            )
        if not artifact_exists and properties["LoadState"] == "not-found":
            return ServiceBackendStatus.single(
                self.id,
                ServiceLifecycleState.ABSENT,
            )
        ready = (
            artifact_exists
            and properties["LoadState"] == "loaded"
            and properties["ActiveState"] == "active"
            and properties["SubState"] == "running"
            and properties["UnitFileState"] == "enabled"
        )
        return ServiceBackendStatus.single(
            self.id,
            (
                ServiceLifecycleState.READY
                if ready
                else ServiceLifecycleState.UNHEALTHY
            ),
        )

    def uninstall(self) -> None:
        """Stop and remove only the Sidekick systemd service."""
        if self.status().state is not ServiceLifecycleState.ABSENT:
            self._require_success(
                (
                    *_SYSTEMCTL,
                    "disable",
                    "--now",
                    SYSTEMD_SERVICE_NAME,
                )
            )
        self._artifacts.delete(self._artifact_path)
        self._require_success((*_SYSTEMCTL, "daemon-reload"))
        self._runner.run((*_SYSTEMCTL, "reset-failed", SYSTEMD_SERVICE_NAME))

    def _require_success(self, argv: tuple[str, ...]) -> None:
        if self._runner.run(argv).returncode != 0:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)


def _service_payload(command: ServiceLaunchCommand) -> bytes:
    arguments = " ".join(
        f'"{_systemd_value(value)}"'
        for value in (str(command.program), *command.arguments)
    )
    return (
        f"# sidekick-service-version={SERVICE_ARTIFACT_VERSION}\n"
        "[Unit]\n"
        "Description=Sidekick Usages account supervisor\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={arguments}\n"
        "Restart=on-failure\n"
        "RestartSec=5s\n"
        "TimeoutStopSec=15s\n"
        "# The official Codex daemon outlives supervisor replacement.\n"
        "KillMode=process\n"
        "NoNewPrivileges=true\n"
        "UMask=0077\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def _systemd_value(value: str) -> str:
    if "\0" in value or "\n" in value or "\r" in value:
        raise ServiceLifecycleError(ServiceFailureCode.ARTIFACT_UNSAFE)
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")


def _properties(payload: str) -> dict[str, str] | None:
    expected = {
        "LoadState",
        "ActiveState",
        "SubState",
        "UnitFileState",
    }
    values: dict[str, str] = {}
    for line in payload.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in values:
            return None
        values[key] = value
    return values if set(values) == expected else None
