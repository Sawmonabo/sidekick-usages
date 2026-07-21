"""Read-only all-backend scheduler quiescence assessment."""

import enum
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

SERVICE_NAME = "sidekick-usages-refresh"
LAUNCHD_LABEL = "com.sidekick-usages.refresh"
CRON_BEGIN = "# sidekick-usages refresh begin"
CRON_END = "# sidekick-usages refresh end"

_TASK_INSTALLED_SENTINEL = "sidekick-schedule-installed"
_TASK_ABSENT_SENTINEL = "sidekick-schedule-absent"


class SchedulerBackendId(enum.StrEnum):
    """Scheduler backends that can own a Sidekick schedule."""

    SYSTEMD = "systemd"
    CRON = "cron"
    LAUNCHD = "launchd"
    TASK_SCHEDULER = "task-scheduler"


class SchedulerBackendState(enum.StrEnum):
    """Read-only installation state of one scheduler backend."""

    ABSENT = "absent"
    INSTALLED = "installed"
    UNASSESSABLE = "unassessable"


@dataclass(frozen=True, slots=True)
class SchedulerProbeResult:
    """Internal result from one injected read-only command probe."""

    returncode: int
    stdout: str
    stderr: str


type SchedulerProbe = Callable[[tuple[str, ...]], SchedulerProbeResult]


@dataclass(frozen=True, slots=True)
class SchedulerBackendObservation:
    """Safe read-only state for one scheduler backend.

    :ivar backend: Backend that was inspected.
    :ivar state: Closed state used by persistence mutation policy.
    :ivar message: Fixed Sidekick-authored summary without native errors.
    """

    backend: SchedulerBackendId
    state: SchedulerBackendState
    message: str

    @property
    def blocks_mutation(self) -> bool:
        """Return whether this observation prevents persistence mutation."""
        return self.state is not SchedulerBackendState.ABSENT


@dataclass(frozen=True, slots=True)
class SchedulerQuiescenceAssessment:
    """Complete scheduler state required before persistence mutation.

    :ivar observations: Every coexisting backend in deterministic order.
    """

    observations: tuple[SchedulerBackendObservation, ...]

    @property
    def quiescent(self) -> bool:
        """Return whether every applicable backend is safely absent."""
        return bool(self.observations) and not any(
            observation.blocks_mutation for observation in self.observations
        )

    @property
    def write_blocked(self) -> bool:
        """Return whether persistence mutation must remain blocked."""
        return not self.quiescent


def assess_scheduler_quiescence(
    *,
    system: str,
    home: Path,
    uid: int,
    is_wsl: bool,
    has_user_systemd: bool,
    probe: SchedulerProbe,
) -> SchedulerQuiescenceAssessment:
    """Inspect every scheduler backend that can coexist on this host."""
    observations: list[SchedulerBackendObservation] = []
    for backend in _applicable_backends(system, is_wsl):
        match backend:
            case SchedulerBackendId.SYSTEMD:
                observation = _observe_systemd(
                    home,
                    has_user_systemd,
                    probe,
                )
            case SchedulerBackendId.CRON:
                observation = _observe_cron(probe)
            case SchedulerBackendId.LAUNCHD:
                observation = _observe_launchd(home, uid, probe)
            case SchedulerBackendId.TASK_SCHEDULER:
                observation = _observe_task_scheduler(probe)
            case _ as unreachable:
                assert_never(unreachable)
        observations.append(observation)
    return SchedulerQuiescenceAssessment(tuple(observations))


def powershell_command(script: str) -> tuple[str, ...]:
    """Return a PowerShell command argv for scheduler operations."""
    return (
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    )


def _applicable_backends(
    system: str,
    is_wsl: bool,
) -> tuple[SchedulerBackendId, ...]:
    """Return all coexisting backends in deterministic order."""
    if is_wsl:
        return (
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendId.CRON,
            SchedulerBackendId.TASK_SCHEDULER,
        )
    match system:
        case "Linux":
            return (SchedulerBackendId.SYSTEMD, SchedulerBackendId.CRON)
        case "Darwin":
            return (SchedulerBackendId.LAUNCHD, SchedulerBackendId.CRON)
        case "Windows":
            return (SchedulerBackendId.TASK_SCHEDULER,)
        case _:
            return ()


def _observe_systemd(
    home: Path,
    has_user_systemd: bool,
    probe: SchedulerProbe,
) -> SchedulerBackendObservation:
    """Inspect owned unit files and loaded systemd state."""
    unit_dir = home / ".config" / "systemd" / "user"
    for suffix in ("service", "timer"):
        try:
            (unit_dir / f"{SERVICE_NAME}.{suffix}").lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return _observation(
                SchedulerBackendId.SYSTEMD,
                SchedulerBackendState.UNASSESSABLE,
            )
        return _observation(
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendState.INSTALLED,
        )
    if not has_user_systemd:
        return _observation(
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendState.ABSENT,
        )

    result = _run_probe(
        probe,
        (
            "systemctl",
            "--user",
            "show",
            f"{SERVICE_NAME}.timer",
            "--property=LoadState",
            "--property=UnitFileState",
            "--property=ActiveState",
        ),
    )
    if (
        result is None
        or result.returncode != 0
        or (properties := _systemd_properties(result.stdout)) is None
    ):
        state = SchedulerBackendState.UNASSESSABLE
    else:
        state = (
            SchedulerBackendState.ABSENT
            if properties["LoadState"] == "not-found"
            and properties["ActiveState"] == "inactive"
            and properties["UnitFileState"] in {"", "not-found"}
            else SchedulerBackendState.INSTALLED
        )
    return _observation(SchedulerBackendId.SYSTEMD, state)


def _systemd_properties(stdout: str) -> dict[str, str] | None:
    """Parse the complete named systemd property response."""
    expected = {"LoadState", "UnitFileState", "ActiveState"}
    properties: dict[str, str] = {}
    for line in stdout.splitlines():
        name, separator, value = line.partition("=")
        if separator != "=" or name not in expected or name in properties:
            return None
        properties[name] = value
    return properties if properties.keys() == expected else None


def _observe_cron(probe: SchedulerProbe) -> SchedulerBackendObservation:
    """Inspect the complete crontab for Sidekick's owned marker."""
    result = _run_probe(probe, ("crontab", "-l"))
    if result is None:
        state = SchedulerBackendState.UNASSESSABLE
    elif result.returncode == 0:
        state = (
            SchedulerBackendState.INSTALLED
            if CRON_BEGIN in result.stdout or CRON_END in result.stdout
            else SchedulerBackendState.ABSENT
        )
    elif _is_missing_crontab(result):
        state = SchedulerBackendState.ABSENT
    else:
        state = SchedulerBackendState.UNASSESSABLE
    return _observation(SchedulerBackendId.CRON, state)


def _observe_launchd(
    home: Path,
    uid: int,
    probe: SchedulerProbe,
) -> SchedulerBackendObservation:
    """Inspect the owned plist and the live launchd domain."""
    plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    try:
        plist.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        return _observation(
            SchedulerBackendId.LAUNCHD,
            SchedulerBackendState.UNASSESSABLE,
        )
    else:
        return _observation(
            SchedulerBackendId.LAUNCHD,
            SchedulerBackendState.INSTALLED,
        )

    result = _run_probe(probe, ("launchctl", "print", f"gui/{uid}"))
    if result is None or result.returncode != 0 or not result.stdout:
        state = SchedulerBackendState.UNASSESSABLE
    elif LAUNCHD_LABEL in result.stdout:
        state = SchedulerBackendState.INSTALLED
    else:
        state = SchedulerBackendState.ABSENT
    return _observation(SchedulerBackendId.LAUNCHD, state)


def _observe_task_scheduler(
    probe: SchedulerProbe,
) -> SchedulerBackendObservation:
    """Inspect the owned Windows task through fixed output sentinels."""
    script = "\n".join(
        (
            "$ErrorActionPreference = 'Stop'",
            "$tasks = @(Get-ScheduledTask -ErrorAction Stop | "
            "Where-Object { $_.TaskName -eq "
            f"'{SERVICE_NAME}' -and $_.TaskPath -eq '\\' }})",
            "if ($tasks.Count -eq 0) {",
            f"  Write-Output '{_TASK_ABSENT_SENTINEL}'",
            "} else {",
            f"  Write-Output '{_TASK_INSTALLED_SENTINEL}'",
            "}",
        )
    )
    result = _run_probe(probe, powershell_command(script))
    if result is None or result.returncode != 0:
        state = SchedulerBackendState.UNASSESSABLE
    elif result.stdout.strip() == _TASK_ABSENT_SENTINEL:
        state = SchedulerBackendState.ABSENT
    elif result.stdout.strip() == _TASK_INSTALLED_SENTINEL:
        state = SchedulerBackendState.INSTALLED
    else:
        state = SchedulerBackendState.UNASSESSABLE
    return _observation(SchedulerBackendId.TASK_SCHEDULER, state)


def _run_probe(
    probe: SchedulerProbe,
    argv: tuple[str, ...],
) -> SchedulerProbeResult | None:
    """Run one probe or preserve command failure as typed state."""
    try:
        return probe(argv)
    except OSError:
        return None


def _is_missing_crontab(result: SchedulerProbeResult) -> bool:
    """Recognize known Linux and macOS no-crontab diagnostics."""
    if result.stdout:
        return False
    message = result.stderr.strip().lower()
    return message.startswith(("no crontab for ", "crontab: no crontab for "))


def _observation(
    backend: SchedulerBackendId,
    state: SchedulerBackendState,
) -> SchedulerBackendObservation:
    """Build one observation with a fixed, non-native message."""
    match state:
        case SchedulerBackendState.ABSENT:
            message = "Sidekick schedule is not installed."
        case SchedulerBackendState.INSTALLED:
            message = "Sidekick schedule is installed."
        case SchedulerBackendState.UNASSESSABLE:
            message = "Sidekick schedule status could not be assessed."
        case _ as unreachable:
            assert_never(unreachable)
    return SchedulerBackendObservation(backend, state, message)
