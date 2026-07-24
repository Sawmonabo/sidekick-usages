"""Public daemon lifecycle facade and lightweight control boundaries."""

from sidekick_usages.daemon import legacy as _legacy
from sidekick_usages.daemon.legacy import (
    DAEMON_DIR_NAME as DAEMON_DIR_NAME,
)
from sidekick_usages.daemon.legacy import (
    WINDOWS_DAEMON_SUBDIR as WINDOWS_DAEMON_SUBDIR,
)
from sidekick_usages.daemon.legacy import (
    CommandResult as CommandResult,
)
from sidekick_usages.daemon.legacy import (
    CronBackend as CronBackend,
)
from sidekick_usages.daemon.legacy import (
    DaemonManager as DaemonManager,
)
from sidekick_usages.daemon.legacy import (
    DaemonOperation as DaemonOperation,
)
from sidekick_usages.daemon.legacy import (
    DaemonOperationResult as DaemonOperationResult,
)
from sidekick_usages.daemon.legacy import (
    HiddenWindowsLauncher as HiddenWindowsLauncher,
)
from sidekick_usages.daemon.legacy import (
    LaunchdBackend as LaunchdBackend,
)
from sidekick_usages.daemon.legacy import (
    PlatformInfo as PlatformInfo,
)
from sidekick_usages.daemon.legacy import (
    SchedulerBackend as SchedulerBackend,
)
from sidekick_usages.daemon.legacy import (
    SystemCommandRunner as SystemCommandRunner,
)
from sidekick_usages.daemon.legacy import (
    SystemdBackend as SystemdBackend,
)
from sidekick_usages.daemon.legacy import (
    TaskSchedulerBackend as TaskSchedulerBackend,
)
from sidekick_usages.daemon.legacy import (
    ps_here_string as ps_here_string,
)
from sidekick_usages.daemon.legacy import (
    ps_quote as ps_quote,
)
from sidekick_usages.daemon.legacy import (
    resolve_maintenance_command as resolve_maintenance_command,
)
from sidekick_usages.daemon.legacy import (
    xml_escape as xml_escape,
)

# These module aliases preserve the existing monkeypatch surface while the
# legacy scheduler implementation is split into its final backend owners.
shutil = _legacy.shutil
subprocess = _legacy.subprocess

__all__ = [
    "DAEMON_DIR_NAME",
    "WINDOWS_DAEMON_SUBDIR",
    "CommandResult",
    "CronBackend",
    "DaemonManager",
    "DaemonOperation",
    "DaemonOperationResult",
    "HiddenWindowsLauncher",
    "LaunchdBackend",
    "PlatformInfo",
    "SchedulerBackend",
    "SystemCommandRunner",
    "SystemdBackend",
    "TaskSchedulerBackend",
    "ps_here_string",
    "ps_quote",
    "resolve_maintenance_command",
    "xml_escape",
]
