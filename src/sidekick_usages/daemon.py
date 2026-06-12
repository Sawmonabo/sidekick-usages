"""Reusable OS scheduler backends for token refresh maintenance."""

import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "sidekick-usages-refresh"
LAUNCHD_LABEL = "com.sidekick-usages.refresh"
CRON_BEGIN = "# sidekick-usages refresh begin"
CRON_END = "# sidekick-usages refresh end"
DAEMON_DIR_NAME = "sidekick-usages"
WINDOWS_DAEMON_SUBDIR = "sidekick-usages\\daemon"


@dataclass(frozen=True)
class CommandResult:
    """Completed system command result."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class DaemonOperationResult:
    """Result of an install/status/uninstall daemon operation."""

    backend: str
    message: str
    exit_code: int = 0


@dataclass(frozen=True)
class PlatformInfo:
    """Platform facts used by backend auto-detection."""

    system: str
    home: Path
    uid: int
    is_wsl: bool
    wsl_distro: str | None
    has_user_systemd: bool

    @classmethod
    def detect(cls) -> PlatformInfo:
        """Detect platform facts from the current process."""
        system = platform.system()
        return cls(
            system=system,
            home=Path.home(),
            uid=os.getuid() if hasattr(os, "getuid") else 0,
            is_wsl=_detect_wsl(),
            wsl_distro=os.environ.get("WSL_DISTRO_NAME"),
            has_user_systemd=_has_user_systemd(system),
        )


class SystemCommandRunner:
    """Small injectable wrapper around subprocess execution."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        input_text: str | None = None,
    ) -> CommandResult:
        """Run a command and capture text output."""
        completed = subprocess.run(
            list(argv),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class SchedulerBackend:
    """Base class for one user-level scheduler backend."""

    id = ""

    def __init__(
        self,
        command: tuple[str, ...],
        platform_info: PlatformInfo,
        runner: SystemCommandRunner,
    ) -> None:
        self.command = command
        self.platform_info = platform_info
        self.runner = runner

    def install(self) -> DaemonOperationResult:
        """Install or update the user-level scheduled refresh."""
        raise NotImplementedError

    def status(self) -> DaemonOperationResult:
        """Return scheduler status."""
        raise NotImplementedError

    def uninstall(self) -> DaemonOperationResult:
        """Remove the user-level scheduled refresh."""
        raise NotImplementedError


class SystemdBackend(SchedulerBackend):
    """Linux user-level systemd timer backend."""

    id = "systemd"

    @property
    def unit_dir(self) -> Path:
        """Return the user systemd unit directory."""
        return self.platform_info.home / ".config" / "systemd" / "user"

    @property
    def service_path(self) -> Path:
        """Return the service unit path."""
        return self.unit_dir / f"{SERVICE_NAME}.service"

    @property
    def timer_path(self) -> Path:
        """Return the timer unit path."""
        return self.unit_dir / f"{SERVICE_NAME}.timer"

    def install(self) -> DaemonOperationResult:
        """Write units and enable the timer."""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.service_path.write_text(self._service_text())
        self.timer_path.write_text(self._timer_text())
        self.runner.run(("systemctl", "--user", "daemon-reload"))
        result = self.runner.run(
            (
                "systemctl",
                "--user",
                "enable",
                "--now",
                f"{SERVICE_NAME}.timer",
            )
        )
        return _result_from_command(self.id, result, "installed systemd timer")

    def status(self) -> DaemonOperationResult:
        """Return systemd timer status."""
        result = self.runner.run(
            ("systemctl", "--user", "status", f"{SERVICE_NAME}.timer")
        )
        message = result.stdout or result.stderr or "systemd status checked"
        return DaemonOperationResult(self.id, message, _exit(result))

    def uninstall(self) -> DaemonOperationResult:
        """Disable and remove systemd units."""
        self.runner.run(
            (
                "systemctl",
                "--user",
                "disable",
                "--now",
                f"{SERVICE_NAME}.timer",
            )
        )
        self.service_path.unlink(missing_ok=True)
        self.timer_path.unlink(missing_ok=True)
        result = self.runner.run(("systemctl", "--user", "daemon-reload"))
        return _result_from_command(self.id, result, "removed systemd timer")

    def _service_text(self) -> str:
        """Build the service unit."""
        command = shlex.join(self.command)
        return (
            "[Unit]\n"
            "Description=Refresh sidekick-usages provider tokens\n\n"
            "[Service]\n"
            "Type=oneshot\n"
            f"ExecStart={command}\n"
        )

    @staticmethod
    def _timer_text() -> str:
        """Build the timer unit."""
        return (
            "[Unit]\n"
            "Description=Run sidekick-usages token refresh\n\n"
            "[Timer]\n"
            "OnBootSec=5m\n"
            "OnUnitActiveSec=30m\n"
            "RandomizedDelaySec=5m\n"
            "Persistent=true\n\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )


class CronBackend(SchedulerBackend):
    """Portable cron fallback backend."""

    id = "cron"

    def install(self) -> DaemonOperationResult:
        """Install or replace the marked crontab block."""
        current = self.runner.run(("crontab", "-l"))
        existing = current.stdout if current.returncode == 0 else ""
        updated = _replace_marked_block(existing, self._cron_block())
        result = self.runner.run(("crontab", "-"), input_text=updated)
        return _result_from_command(self.id, result, "installed cron entry")

    def status(self) -> DaemonOperationResult:
        """Return whether the marked crontab block exists."""
        result = self.runner.run(("crontab", "-l"))
        if result.returncode != 0:
            return DaemonOperationResult(
                self.id, "cron entry not installed", 1
            )
        installed = CRON_BEGIN in result.stdout and CRON_END in result.stdout
        message = "cron entry installed" if installed else "cron entry missing"
        return DaemonOperationResult(self.id, message, 0 if installed else 1)

    def uninstall(self) -> DaemonOperationResult:
        """Remove the marked crontab block."""
        current = self.runner.run(("crontab", "-l"))
        existing = current.stdout if current.returncode == 0 else ""
        updated = _remove_marked_block(existing)
        result = self.runner.run(("crontab", "-"), input_text=updated)
        return _result_from_command(self.id, result, "removed cron entry")

    def _cron_block(self) -> str:
        """Return the sidekick-usages crontab block."""
        return (
            f"{CRON_BEGIN}\n"
            f"*/30 * * * * {shlex.join(self.command)}\n"
            f"{CRON_END}\n"
        )


class LaunchdBackend(SchedulerBackend):
    """macOS LaunchAgent backend."""

    id = "launchd"

    @property
    def agent_dir(self) -> Path:
        """Return the user LaunchAgents directory."""
        return self.platform_info.home / "Library" / "LaunchAgents"

    @property
    def plist_path(self) -> Path:
        """Return the LaunchAgent plist path."""
        return self.agent_dir / f"{LAUNCHD_LABEL}.plist"

    @property
    def log_dir(self) -> Path:
        """Return the user LaunchAgent log directory."""
        return self.platform_info.home / "Library" / "Logs" / DAEMON_DIR_NAME

    def install(self) -> DaemonOperationResult:
        """Write and bootstrap the LaunchAgent."""
        self.agent_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_text(self._plist_text())
        target = f"gui/{self.platform_info.uid}"
        self.runner.run(
            ("launchctl", "bootstrap", target, str(self.plist_path))
        )
        self.runner.run(("launchctl", "enable", f"{target}/{LAUNCHD_LABEL}"))
        result = self.runner.run(
            ("launchctl", "kickstart", "-k", f"{target}/{LAUNCHD_LABEL}")
        )
        return _result_from_command(self.id, result, "installed launch agent")

    def status(self) -> DaemonOperationResult:
        """Return LaunchAgent status."""
        result = self.runner.run(
            (
                "launchctl",
                "print",
                f"gui/{self.platform_info.uid}/{LAUNCHD_LABEL}",
            )
        )
        message = result.stdout or result.stderr or "launchd status checked"
        return DaemonOperationResult(self.id, message, _exit(result))

    def uninstall(self) -> DaemonOperationResult:
        """Boot out and remove the LaunchAgent."""
        target = f"gui/{self.platform_info.uid}"
        self.runner.run(("launchctl", "bootout", target, str(self.plist_path)))
        self.plist_path.unlink(missing_ok=True)
        return DaemonOperationResult(self.id, "removed launch agent")

    def _plist_text(self) -> str:
        """Build the LaunchAgent plist."""
        args = "\n".join(
            f"    <string>{xml_escape(arg)}</string>" for arg in self.command
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "<dict>\n"
            "  <key>Label</key>\n"
            f"  <string>{LAUNCHD_LABEL}</string>\n"
            "  <key>ProgramArguments</key>\n"
            "  <array>\n"
            f"{args}\n"
            "  </array>\n"
            "  <key>StartInterval</key>\n"
            "  <integer>1800</integer>\n"
            "  <key>StandardOutPath</key>\n"
            f"  <string>{xml_escape(str(self.log_dir / 'refresh.out.log'))}</string>\n"
            "  <key>StandardErrorPath</key>\n"
            f"  <string>{xml_escape(str(self.log_dir / 'refresh.err.log'))}</string>\n"
            "  <key>RunAtLoad</key>\n"
            "  <true/>\n"
            "</dict>\n"
            "</plist>\n"
        )


class HiddenWindowsLauncher:
    """Generate silent Windows-side launcher artifacts for scheduled refresh."""

    def __init__(
        self,
        command: tuple[str, ...],
        platform_info: PlatformInfo,
    ) -> None:
        self.command = command
        self.platform_info = platform_info

    def install_preamble(self) -> str:
        """Return PowerShell that writes wrapper artifacts under LOCALAPPDATA."""
        return "\n".join(
            (
                "$daemonDir = Join-Path $env:LOCALAPPDATA "
                f"{ps_quote(WINDOWS_DAEMON_SUBDIR)}",
                "New-Item -ItemType Directory -Force -Path $daemonDir | Out-Null",
                "$vbsPath = Join-Path $daemonDir 'refresh.vbs'",
                "$ps1Path = Join-Path $daemonDir 'refresh.ps1'",
                "$outPath = Join-Path $daemonDir 'refresh.out.log'",
                "$errPath = Join-Path $daemonDir 'refresh.err.log'",
                "$vbs = " + ps_here_string(self._vbs_text()),
                "$ps1 = " + ps_here_string(self._ps1_text()),
                "Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII",
                "Set-Content -Path $ps1Path -Value $ps1 -Encoding UTF8",
            )
        )

    @staticmethod
    def action_script() -> str:
        """Return PowerShell that creates a hidden scheduled task action."""
        return (
            "$action = New-ScheduledTaskAction -Execute 'wscript.exe' "
            "-Argument ('//B //Nologo \"' + $vbsPath + '\"')"
        )

    @staticmethod
    def status_script() -> str:
        """Return PowerShell that reports wrapper and log paths."""
        return "\n".join(
            (
                "$daemonDir = Join-Path $env:LOCALAPPDATA "
                f"{ps_quote(WINDOWS_DAEMON_SUBDIR)}",
                "$vbsPath = Join-Path $daemonDir 'refresh.vbs'",
                "$outPath = Join-Path $daemonDir 'refresh.out.log'",
                "$errPath = Join-Path $daemonDir 'refresh.err.log'",
                "Get-ScheduledTask -TaskName "
                f"{ps_quote(SERVICE_NAME)} -ErrorAction Stop",
                "Get-ScheduledTaskInfo -TaskName "
                f"{ps_quote(SERVICE_NAME)} -ErrorAction Stop",
                "Write-Output ('WrapperPath: ' + $vbsPath)",
                "Write-Output ('StdoutPath: ' + $outPath)",
                "Write-Output ('StderrPath: ' + $errPath)",
            )
        )

    @staticmethod
    def uninstall_script() -> str:
        """Return PowerShell that removes task and generated launcher files."""
        return "\n".join(
            (
                "Unregister-ScheduledTask "
                f"-TaskName {ps_quote(SERVICE_NAME)} "
                "-Confirm:$false -ErrorAction SilentlyContinue",
                "$daemonDir = Join-Path $env:LOCALAPPDATA "
                f"{ps_quote(WINDOWS_DAEMON_SUBDIR)}",
                "$paths = @(",
                "  (Join-Path $daemonDir 'refresh.vbs'),",
                "  (Join-Path $daemonDir 'refresh.ps1')",
                ")",
                "foreach ($path in $paths) {",
                "  Remove-Item -LiteralPath $path "
                "-Force -ErrorAction SilentlyContinue",
                "}",
                "Remove-Item -LiteralPath $daemonDir "
                "-Force -ErrorAction SilentlyContinue",
            )
        )

    def _task_command(self) -> tuple[str, ...]:
        """Return the command argv executed by the PowerShell wrapper."""
        if self.platform_info.is_wsl:
            distro = self.platform_info.wsl_distro or "Ubuntu"
            return (
                "wsl.exe",
                "-d",
                distro,
                "--",
                "bash",
                "-lc",
                shlex.join(self.command),
            )
        return self.command

    @staticmethod
    def _vbs_text() -> str:
        """Return the VBScript shim that launches PowerShell hidden."""
        return "\n".join(
            (
                'Set shell = CreateObject("WScript.Shell")',
                'Set fso = CreateObject("Scripting.FileSystemObject")',
                "scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)",
                'scriptPath = fso.BuildPath(scriptDir, "refresh.ps1")',
                'command = "powershell.exe -NoProfile -ExecutionPolicy '
                'Bypass -File " & Chr(34) & scriptPath & Chr(34)',
                "code = shell.Run(command, 0, True)",
                "WScript.Quit code",
            )
        )

    def _ps1_text(self) -> str:
        """Return the PowerShell refresh wrapper with file logging."""
        command = " ".join(ps_quote(arg) for arg in self._task_command())
        return "\n".join(
            (
                "$ErrorActionPreference = 'Continue'",
                "$daemonDir = Split-Path -Parent $MyInvocation.MyCommand.Path",
                "$stdoutPath = Join-Path $daemonDir 'refresh.out.log'",
                "$stderrPath = Join-Path $daemonDir 'refresh.err.log'",
                f"& {command} >> $stdoutPath 2>> $stderrPath",
                "$code = if ($LASTEXITCODE -is [int]) { $LASTEXITCODE } "
                "else { 0 }",
                "exit $code",
            )
        )


class TaskSchedulerBackend(SchedulerBackend):
    """Windows Task Scheduler backend, including WSL launch mode."""

    id = "task-scheduler"

    @property
    def launcher(self) -> HiddenWindowsLauncher:
        """Return the silent Windows launcher helper."""
        return HiddenWindowsLauncher(self.command, self.platform_info)

    def install(self) -> DaemonOperationResult:
        """Register the scheduled task for the current user."""
        result = self.runner.run(self._powershell(self._install_script()))
        return _result_from_command(
            self.id,
            result,
            "installed scheduled task",
        )

    def status(self) -> DaemonOperationResult:
        """Return scheduled task status."""
        script = self.launcher.status_script()
        result = self.runner.run(self._powershell(script))
        message = result.stdout or result.stderr or "task status checked"
        return DaemonOperationResult(self.id, message, _exit(result))

    def uninstall(self) -> DaemonOperationResult:
        """Unregister the scheduled task."""
        script = self.launcher.uninstall_script()
        result = self.runner.run(self._powershell(script))
        return _result_from_command(
            self.id,
            result,
            "removed scheduled task",
        )

    def _install_script(self) -> str:
        """Build the PowerShell registration script."""
        return "\n".join(
            (
                self.launcher.install_preamble(),
                "$trigger = New-ScheduledTaskTrigger "
                "-Once -At (Get-Date).AddMinutes(5) "
                "-RepetitionInterval (New-TimeSpan -Minutes 30)",
                self.launcher.action_script(),
                "$settings = New-ScheduledTaskSettingsSet "
                "-MultipleInstances IgnoreNew",
                "$settings.Hidden = $true",
                "Register-ScheduledTask "
                f"-TaskName {ps_quote(SERVICE_NAME)} "
                "-Trigger $trigger -Action $action -Settings $settings "
                "-Description 'Silently refresh sidekick-usages provider tokens' "
                "-Force",
            )
        )

    @staticmethod
    def _powershell(script: str) -> tuple[str, ...]:
        """Return a PowerShell command argv."""
        return (
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        )


class DaemonManager:
    """Select and run reusable daemon scheduler backends."""

    def __init__(
        self,
        *,
        command: tuple[str, ...] | None = None,
        platform_info: PlatformInfo | None = None,
        runner: SystemCommandRunner | None = None,
    ) -> None:
        self.command = command or resolve_maintenance_command()
        self.platform_info = platform_info or PlatformInfo.detect()
        self.runner = runner or SystemCommandRunner()

    def install(self, backend: str = "auto") -> DaemonOperationResult:
        """Install the selected backend."""
        return self.backend(backend).install()

    def status(self, backend: str = "auto") -> DaemonOperationResult:
        """Return status for the selected backend."""
        return self.backend(backend).status()

    def uninstall(self, backend: str = "auto") -> DaemonOperationResult:
        """Uninstall the selected backend."""
        return self.backend(backend).uninstall()

    def backend(self, requested: str) -> SchedulerBackend:
        """Build a backend instance by name or auto-detection."""
        backend_id = (
            self.auto_backend_id() if requested == "auto" else requested
        )
        backend_type = {
            "systemd": SystemdBackend,
            "cron": CronBackend,
            "launchd": LaunchdBackend,
            "task-scheduler": TaskSchedulerBackend,
        }.get(backend_id)
        if backend_type is None:
            raise ValueError(f"Unknown daemon backend: {requested}")
        return backend_type(self.command, self.platform_info, self.runner)

    def auto_backend_id(self) -> str:
        """Choose the best scheduler backend for the current platform."""
        if self.platform_info.is_wsl:
            return "task-scheduler"
        if self.platform_info.system == "Windows":
            return "task-scheduler"
        if self.platform_info.system == "Darwin":
            return "launchd"
        if (
            self.platform_info.system == "Linux"
            and self.platform_info.has_user_systemd
        ):
            return "systemd"
        return "cron"


def resolve_maintenance_command() -> tuple[str, ...]:
    """Return the command schedulers should run periodically."""
    executable = shutil.which("sidekick-usages")
    if executable:
        return (executable, "maintain", "--quiet")
    return (
        sys.executable,
        "-m",
        "sidekick_usages",
        "maintain",
        "--quiet",
    )


def ps_quote(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def ps_here_string(value: str) -> str:
    """Quote a string as a PowerShell single-quoted here-string."""
    if "\n'@" in value or value.startswith("'@"):
        raise ValueError("PowerShell here-string terminator in value")
    return "@'\n" + value.rstrip() + "\n'@"


def xml_escape(value: str) -> str:
    """Escape text for launchd plist string elements."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _detect_wsl() -> bool:
    """Return whether this Linux process is running under WSL."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    version = Path("/proc/version")
    try:
        return "microsoft" in version.read_text().lower()
    except OSError:
        return False


def _has_user_systemd(system: str) -> bool:
    """Return whether user-level systemd appears usable."""
    if system != "Linux":
        return False
    return shutil.which("systemctl") is not None


def _result_from_command(
    backend: str,
    result: CommandResult,
    success_message: str,
) -> DaemonOperationResult:
    """Convert a system command result to a daemon result."""
    if result.returncode == 0:
        return DaemonOperationResult(backend, success_message)
    message = result.stderr or result.stdout or success_message
    return DaemonOperationResult(backend, message, 3)


def _exit(result: CommandResult) -> int:
    """Map a command return code to the scheduler error code."""
    return 0 if result.returncode == 0 else 3


def _replace_marked_block(text: str, block: str) -> str:
    """Replace or append the sidekick-usages marked crontab block."""
    without = _remove_marked_block(text).rstrip()
    if without:
        return f"{without}\n\n{block}"
    return block


def _remove_marked_block(text: str) -> str:
    """Remove the sidekick-usages marked crontab block."""
    start = text.find(CRON_BEGIN)
    end = text.find(CRON_END)
    if start == -1 or end == -1 or end < start:
        return text
    end += len(CRON_END)
    return (text[:start] + text[end:]).strip() + "\n"
