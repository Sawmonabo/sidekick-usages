"""macOS per-user LaunchAgent integration."""

import html
from pathlib import Path

from sidekick_usages.daemon.lifecycle.artifacts import ServiceArtifactStore
from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.constants import (
    LAUNCH_AGENT_LABEL,
    SERVICE_ARTIFACT_VERSION,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import (
    ServiceArtifact,
    ServiceBackendStatus,
)
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecycleState,
)


class LaunchdBackend:
    """One resident LaunchAgent in the current GUI login context."""

    id = ServiceBackendId.LAUNCHD

    def __init__(
        self,
        artifact_path: Path,
        executable: Path,
        log_root: Path,
        uid: int,
        runner: SystemCommandRunner,
        artifacts: ServiceArtifactStore,
    ) -> None:
        self._artifact_path = artifact_path
        self._executable = executable
        self._log_root = log_root
        self._uid = uid
        self._runner = runner
        self._artifacts = artifacts

    def cancel(self) -> None:
        """Interrupt one active launchd user command."""
        self._runner.cancel()

    @property
    def _domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def _target(self) -> str:
        return f"{self._domain}/{LAUNCH_AGENT_LABEL}"

    def install(self) -> None:
        """Publish and start one exact per-user LaunchAgent."""
        self._artifacts.ensure_directory(self._log_root)
        self._artifacts.write(
            ServiceArtifact(
                self._artifact_path,
                _plist_payload(self._executable, self._log_root),
            )
        )
        self._runner.run(("launchctl", "bootout", self._target))
        self._require_success(("launchctl", "enable", self._target))
        self._require_success(
            (
                "launchctl",
                "bootstrap",
                self._domain,
                str(self._artifact_path),
            )
        )
        self._require_success(("launchctl", "kickstart", "-k", self._target))

    def restart(self) -> None:
        """Restart the exact installed LaunchAgent."""
        self._require_success(("launchctl", "kickstart", "-k", self._target))

    def status(self) -> ServiceBackendStatus:
        """Return strict installed/running LaunchAgent state."""
        artifact_exists = self._artifacts.exists(self._artifact_path)
        result = self._runner.run(("launchctl", "print", self._target))
        if result.returncode != 0:
            return ServiceBackendStatus.single(
                self.id,
                (
                    ServiceLifecycleState.INSTALLED
                    if artifact_exists
                    else ServiceLifecycleState.ABSENT
                ),
            )
        ready = artifact_exists and "state = running" in result.stdout
        return ServiceBackendStatus.single(
            self.id,
            (
                ServiceLifecycleState.READY
                if ready
                else ServiceLifecycleState.UNHEALTHY
            ),
        )

    def uninstall(self) -> None:
        """Boot out and remove only the Sidekick LaunchAgent."""
        self._runner.run(("launchctl", "bootout", self._target))
        if (
            self._runner.run(("launchctl", "print", self._target)).returncode
            == 0
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        self._artifacts.delete(self._artifact_path)

    def _require_success(self, argv: tuple[str, ...]) -> None:
        if self._runner.run(argv).returncode != 0:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)


def _plist_payload(executable: Path, log_root: Path) -> bytes:
    program = html.escape(str(executable), quote=True)
    stdout = html.escape(str(log_root / "supervisor.out.log"), quote=True)
    stderr = html.escape(str(log_root / "supervisor.err.log"), quote=True)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        f"<!-- sidekick-service-version={SERVICE_ARTIFACT_VERSION} -->\n"
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{program}</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <dict>\n"
        "    <key>SuccessfulExit</key>\n"
        "    <false/>\n"
        "  </dict>\n"
        "  <key>ProcessType</key>\n"
        "  <string>Background</string>\n"
        "  <key>ThrottleInterval</key>\n"
        "  <integer>5</integer>\n"
        "  <key>Umask</key>\n"
        "  <integer>63</integer>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{stdout}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{stderr}</string>\n"
        "</dict>\n"
        "</plist>\n"
    ).encode()
