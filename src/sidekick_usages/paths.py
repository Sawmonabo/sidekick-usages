"""Sidekick-owned application path discovery."""

import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from platformdirs import PlatformDirs

from sidekick_usages.errors import UsageError

_XDG_HOME_VARIABLES = ("XDG_DATA_HOME",)
_WINDOWS_OVERRIDE_PREFIX = "WIN_PD_OVERRIDE_"


class PathDiscoveryError(UsageError):
    """The process environment cannot safely resolve application paths.

    :param variable: Environment variable that violates the path contract.
    :param reason: Safe explanation of the rejected value's contract.
    """

    def __init__(self, variable: str, reason: str) -> None:
        self.variable = variable
        super().__init__(f"{variable} {reason}")


@dataclass(frozen=True, slots=True)
class AccountLocations:
    """Locations participating in account-store compatibility.

    :ivar canonical: Native account-store location.
    :ivar existing_sidekick: Existing Sidekick account-store location.
    :ivar prototype_cc_usage: Import-only cc-usage prototype location.
    """

    canonical: Path
    existing_sidekick: Path
    prototype_cc_usage: Path


@dataclass(frozen=True, slots=True)
class PrivateCodexLocations:
    """Sidekick-owned private Codex credential locations.

    :ivar canonical: Native private Codex root.
    :ivar existing_sidekick: Existing Sidekick private Codex root.
    """

    canonical: Path
    existing_sidekick: Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    """Sidekick-owned paths resolved for one application invocation.

    :ivar accounts: Account-store compatibility locations.
    :ivar private_codex: Private Codex credential locations.
    :ivar activity_snapshots: Canonical token-activity snapshot file.
    :ivar credential_refresh: Canonical private refresh-state root.
    :ivar private_claude_profiles: Stable private Claude profile root.
    :ivar credential_authorities: Protected legacy authority root.
    :ivar selected_state: Provider selected-state authority.
    :ivar activation_journals: Provider activation journal root.
    :ivar durable_operations: Durable due and retry operation root.
    :ivar service_state: Supervisor readiness state authority.
    :ivar service_logs: Sanitized supervisor diagnostic root.
    :ivar runtime_directory: Owner-only local control runtime root.
    :ivar supervisor_socket: Local supervisor control socket.
    :ivar supervisor_lock: Per-user supervisor singleton lock.
    """

    accounts: AccountLocations
    private_codex: PrivateCodexLocations
    activity_snapshots: Path
    credential_refresh: Path
    private_claude_profiles: Path
    credential_authorities: Path
    selected_state: Path
    activation_journals: Path
    durable_operations: Path
    service_state: Path
    service_logs: Path
    runtime_directory: Path
    supervisor_socket: Path
    supervisor_lock: Path


def discover_application_paths() -> ApplicationPaths:
    """Discover native and compatibility paths without side effects.

    :return: Sidekick-owned paths for the current user.
    :raises PathDiscoveryError: If an environment override is unsafe.
    """
    _validate_environment()
    home = Path.home()
    compatibility_root = home / ".config" / "sidekick-usages"
    native = PlatformDirs(
        appname="sidekick-usages",
        appauthor=False,
        version=None,
        roaming=False,
        multipath=False,
        opinion=True,
        ensure_exists=False,
        use_site_for_root=False,
    )
    native_data_root = native.user_data_path
    runtime_directory = native.user_runtime_path
    return ApplicationPaths(
        accounts=AccountLocations(
            canonical=native_data_root / "accounts.json",
            existing_sidekick=compatibility_root / "accounts.json",
            prototype_cc_usage=(
                home / ".config" / "cc-usage" / "accounts.json"
            ),
        ),
        private_codex=PrivateCodexLocations(
            canonical=native_data_root / "codex",
            existing_sidekick=compatibility_root / "codex",
        ),
        activity_snapshots=native_data_root / "token-activity.json",
        credential_refresh=native_data_root / "credential-refresh",
        private_claude_profiles=native_data_root / "claude",
        credential_authorities=native_data_root / "codex",
        selected_state=native_data_root / "selected-accounts.json",
        activation_journals=native_data_root / "activation-journals",
        durable_operations=native_data_root / "operations",
        service_state=native_data_root / "service-state.json",
        service_logs=native.user_log_path,
        runtime_directory=runtime_directory,
        supervisor_socket=runtime_directory / "supervisor.sock",
        supervisor_lock=runtime_directory / "supervisor.lock",
    )


def _validate_environment() -> None:
    if sys.platform == "win32":
        for name, value in sorted(os.environ.items()):
            if name.startswith(_WINDOWS_OVERRIDE_PREFIX) and value:
                raise PathDiscoveryError(
                    name,
                    "is reserved for platformdirs tests and cannot be used.",
                )
        return

    for name in _XDG_HOME_VARIABLES:
        value = os.environ.get(name)
        if value and not PurePosixPath(value).is_absolute():
            raise PathDiscoveryError(
                name,
                "must contain an absolute path when set.",
            )
