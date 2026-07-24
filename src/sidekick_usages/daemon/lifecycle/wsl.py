"""WSL logon rescue layered over the Linux user service."""

import subprocess

from sidekick_usages.daemon.lifecycle.commands import SystemCommandRunner
from sidekick_usages.daemon.lifecycle.constants import (
    SERVICE_ARTIFACT_VERSION,
    SYSTEMD_SERVICE_NAME,
    WSL_RESCUE_ABSENT,
    WSL_RESCUE_INSTALLED,
    WSL_RESCUE_TASK_NAME,
    WSL_RESCUE_UNHEALTHY,
)
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.systemd import SystemdBackend
from sidekick_usages.daemon.models.lifecycle import (
    PlatformInfo,
    ServiceBackendStatus,
)
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceFailureCode,
    ServiceLifecycleState,
)

_POWERSHELL = (
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
)


class WslBackend:
    """One Linux user service plus one non-maintenance Windows rescue."""

    id = ServiceBackendId.WSL

    def __init__(
        self,
        systemd: SystemdBackend,
        platform_info: PlatformInfo,
        runner: SystemCommandRunner,
    ) -> None:
        if not platform_info.is_wsl or platform_info.wsl_distro is None:
            raise ValueError("WSL backend requires an explicit distribution.")
        self._systemd = systemd
        self._platform = platform_info
        self._runner = runner

    def install(self) -> None:
        """Install the Linux service and current-user logon rescue."""
        self._systemd.install()
        if (
            self._runner.run((*_POWERSHELL, self._install_script())).returncode
            != 0
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)

    def restart(self) -> None:
        """Restart only the resident Linux service."""
        self._systemd.restart()

    def status(self) -> ServiceBackendStatus:
        """Require both the Linux service and Windows rescue."""
        service = self._systemd.status()
        rescue = self._rescue_status()
        if (
            service.state is ServiceLifecycleState.ABSENT
            and rescue is ServiceLifecycleState.ABSENT
        ):
            state = ServiceLifecycleState.ABSENT
        elif (
            service.state is ServiceLifecycleState.READY
            and rescue is ServiceLifecycleState.INSTALLED
        ):
            state = ServiceLifecycleState.READY
        else:
            state = ServiceLifecycleState.UNHEALTHY
        return ServiceBackendStatus(self.id, state)

    def uninstall(self) -> None:
        """Remove the rescue task, then the Linux user service."""
        if (
            self._runner.run(
                (*_POWERSHELL, self._uninstall_script())
            ).returncode
            != 0
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        if self._rescue_status() is not ServiceLifecycleState.ABSENT:
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        self._systemd.uninstall()

    def _rescue_status(self) -> ServiceLifecycleState:
        result = self._runner.run((*_POWERSHELL, self._status_script()))
        if result.returncode != 0:
            return ServiceLifecycleState.UNHEALTHY
        output = result.stdout.strip()
        if output == WSL_RESCUE_INSTALLED:
            return ServiceLifecycleState.INSTALLED
        if output == WSL_RESCUE_ABSENT:
            return ServiceLifecycleState.ABSENT
        return ServiceLifecycleState.UNHEALTHY

    def _install_script(self) -> str:
        arguments = self._rescue_arguments()
        description = _rescue_description()
        return "\n".join(
            (
                "$action = New-ScheduledTaskAction "
                "-Execute 'wsl.exe' "
                f"-Argument {_powershell_literal(arguments)}",
                "$trigger = New-ScheduledTaskTrigger "
                "-AtLogOn -User $env:USERNAME",
                "$settings = New-ScheduledTaskSettingsSet "
                "-StartWhenAvailable "
                "-MultipleInstances IgnoreNew "
                "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)",
                "$settings.Hidden = $true",
                "Register-ScheduledTask "
                f"-TaskName {_powershell_literal(WSL_RESCUE_TASK_NAME)} "
                "-Action $action "
                "-Trigger $trigger "
                "-Settings $settings "
                f"-Description {_powershell_literal(description)} "
                "-Force | Out-Null",
            )
        )

    def _status_script(self) -> str:
        arguments = _powershell_literal(self._rescue_arguments())
        description = _powershell_literal(_rescue_description())
        return "\n".join(
            (
                "$task = Get-ScheduledTask "
                f"-TaskName {_powershell_literal(WSL_RESCUE_TASK_NAME)} "
                "-ErrorAction SilentlyContinue",
                "if ($null -eq $task) {",
                f"  Write-Output '{WSL_RESCUE_ABSENT}'",
                "} else {",
                "  $actions = @($task.Actions)",
                "  $triggers = @($task.Triggers)",
                "  $valid = "
                f"$task.Description -ceq {description} "
                "-and $actions.Count -eq 1 "
                "-and $actions[0].Execute -ieq 'wsl.exe' "
                f"-and $actions[0].Arguments -ceq {arguments} "
                "-and $triggers.Count -eq 1 "
                "-and $triggers[0].CimClass.CimClassName "
                "-eq 'MSFT_TaskLogonTrigger'",
                "  if ($valid) {",
                f"    Write-Output '{WSL_RESCUE_INSTALLED}'",
                "  } else {",
                f"    Write-Output '{WSL_RESCUE_UNHEALTHY}'",
                "  }",
                "}",
            )
        )

    def _rescue_arguments(self) -> str:
        distribution = self._platform.wsl_distro
        if distribution is None:
            raise ServiceLifecycleError(ServiceFailureCode.ARTIFACT_UNSAFE)
        return subprocess.list2cmdline(
            [
                "--distribution",
                distribution,
                "--user",
                self._platform.user_name,
                "--exec",
                "systemctl",
                "--user",
                "start",
                SYSTEMD_SERVICE_NAME,
            ]
        )

    @staticmethod
    def _uninstall_script() -> str:
        return (
            "Unregister-ScheduledTask "
            f"-TaskName {_powershell_literal(WSL_RESCUE_TASK_NAME)} "
            "-Confirm:$false -ErrorAction SilentlyContinue"
        )


def _powershell_literal(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise ServiceLifecycleError(ServiceFailureCode.ARTIFACT_UNSAFE)
    return "'" + value.replace("'", "''") + "'"


def _rescue_description() -> str:
    return f"Sidekick WSL service rescue v{SERVICE_ARTIFACT_VERSION}"
