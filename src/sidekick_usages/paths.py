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
    """

    accounts: AccountLocations
    private_codex: PrivateCodexLocations
    activity_snapshots: Path


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
