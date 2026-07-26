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
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecycleState,
)

_POWERSHELL = (
    "powershell.exe",
    "-NoProfile",
    "-NonInteractive",
    "-Command",
)
_WSL_RESCUE_TASK_PATH = "\\"


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

    def cancel(self) -> None:
        """Interrupt one active WSL or systemd user command."""
        self._runner.cancel()

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
        try:
            service = self._systemd.status()
        except ServiceLifecycleError:
            service = ServiceBackendStatus.single(
                ServiceBackendId.SYSTEMD,
                ServiceLifecycleState.UNHEALTHY,
            )
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
        return ServiceBackendStatus(
            self.id,
            state,
            service.process,
            _rescue_configuration_state(rescue),
        )

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
        try:
            result = self._runner.run((*_POWERSHELL, self._status_script()))
        except ServiceLifecycleError:
            return ServiceLifecycleState.UNHEALTHY
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
                "$ErrorActionPreference = 'Stop'",
                "$currentUser = "
                "[System.Security.Principal.WindowsIdentity]"
                "::GetCurrent().Name",
                "if ([string]::IsNullOrWhiteSpace($currentUser)) { "
                "throw 'Current Windows user is unavailable.' }",
                "$action = New-ScheduledTaskAction "
                "-Execute 'wsl.exe' "
                f"-Argument {_powershell_literal(arguments)}",
                "$trigger = New-ScheduledTaskTrigger "
                "-AtLogOn -User $currentUser",
                "$trigger.Enabled = $true",
                "$principal = New-ScheduledTaskPrincipal "
                "-UserId $currentUser "
                "-LogonType Interactive "
                "-RunLevel Limited",
                "$settings = New-ScheduledTaskSettingsSet "
                "-StartWhenAvailable "
                "-MultipleInstances IgnoreNew "
                "-ExecutionTimeLimit (New-TimeSpan -Minutes 2)",
                "$settings.Hidden = $true",
                "$settings.Enabled = $true",
                "Register-ScheduledTask "
                f"-TaskName {_powershell_literal(WSL_RESCUE_TASK_NAME)} "
                f"-TaskPath {_powershell_literal(_WSL_RESCUE_TASK_PATH)} "
                "-Action $action "
                "-Trigger $trigger "
                "-Settings $settings "
                "-Principal $principal "
                f"-Description {_powershell_literal(description)} "
                "-Force | Out-Null",
            )
        )

    def _status_script(self) -> str:
        arguments = _powershell_literal(self._rescue_arguments())
        description = _powershell_literal(_rescue_description())
        return "\n".join(
            (
                "$ErrorActionPreference = 'Stop'",
                "$currentUser = "
                "[System.Security.Principal.WindowsIdentity]"
                "::GetCurrent().Name",
                "$tasks = @(Get-ScheduledTask | Where-Object {",
                "  $_.TaskName -ieq "
                f"{_powershell_literal(WSL_RESCUE_TASK_NAME)}",
                "})",
                "if ($tasks.Count -eq 0) {",
                f"  Write-Output '{WSL_RESCUE_ABSENT}'",
                "} elseif ($tasks.Count -ne 1) {",
                f"  Write-Output '{WSL_RESCUE_UNHEALTHY}'",
                "} else {",
                "  $task = $tasks[0]",
                "  $actions = @($task.Actions)",
                "  $triggers = @($task.Triggers)",
                "  $principals = @($task.Principal)",
                "  $settings = @($task.Settings)",
                "  $valid = "
                "-not [string]::IsNullOrWhiteSpace($currentUser) "
                f"-and $task.TaskName -ceq "
                f"{_powershell_literal(WSL_RESCUE_TASK_NAME)} "
                f"-and $task.Description -ceq {description} "
                f"-and $task.TaskPath -ceq "
                f"{_powershell_literal(_WSL_RESCUE_TASK_PATH)} "
                "-and $actions.Count -eq 1 "
                "-and $actions[0].Execute -ieq 'wsl.exe' "
                f"-and $actions[0].Arguments -ceq {arguments} "
                "-and $triggers.Count -eq 1 "
                "-and $triggers[0].CimClass.CimClassName "
                "-ceq 'MSFT_TaskLogonTrigger' "
                "-and $triggers[0].Enabled -eq $true "
                "-and $triggers[0].UserId -ieq $currentUser "
                "-and $principals.Count -eq 1 "
                "-and $principals[0].UserId -ieq $currentUser "
                "-and [string]$principals[0].LogonType "
                "-ceq 'Interactive' "
                "-and [string]$principals[0].RunLevel -ceq 'Limited' "
                "-and $settings.Count -eq 1 "
                "-and $settings[0].Enabled -eq $true "
                "-and $settings[0].StartWhenAvailable -eq $true "
                "-and [string]$settings[0].MultipleInstances "
                "-ceq 'IgnoreNew' "
                "-and $settings[0].Hidden -eq $true "
                "-and $settings[0].ExecutionTimeLimit -ceq 'PT2M'",
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
            f"-TaskPath {_powershell_literal(_WSL_RESCUE_TASK_PATH)} "
            "-Confirm:$false -ErrorAction SilentlyContinue"
        )


def _rescue_configuration_state(
    state: ServiceLifecycleState,
) -> ServiceComponentState:
    """Map the installed Windows task to configuration health."""
    if state is ServiceLifecycleState.INSTALLED:
        return ServiceComponentState.HEALTHY
    if state is ServiceLifecycleState.ABSENT:
        return ServiceComponentState.ABSENT
    return ServiceComponentState.UNHEALTHY


def _powershell_literal(value: str) -> str:
    if "\0" in value or "\r" in value or "\n" in value:
        raise ServiceLifecycleError(ServiceFailureCode.ARTIFACT_UNSAFE)
    return "'" + value.replace("'", "''") + "'"


def _rescue_description() -> str:
    return f"Sidekick WSL service rescue v{SERVICE_ARTIFACT_VERSION}"
