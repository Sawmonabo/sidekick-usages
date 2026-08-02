"""Sidekick-owned application path discovery."""

import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from platformdirs import PlatformDirs

from sidekick_usages.core.accounts.types import SidekickAccountId
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
class ApplicationPaths:
    """Sidekick-owned paths resolved for one application invocation.

    :ivar accounts: Current account-index authority.
    :ivar private_credentials: Protected Sidekick credential root.
    :ivar private_codex_profiles: Stable private Codex profile root.
    :ivar activity_snapshots: Canonical token-activity snapshot file.
    :ivar usage_snapshots: Canonical account-usage snapshot file.
    :ivar metrics_refresh_status: Latest sanitized metrics-refresh result.
    :ivar credential_refresh: Canonical private refresh-state root.
    :ivar private_claude_profiles: Stable private Claude profile root.
    :ivar selected_state: Provider selected-state authority.
    :ivar activation_journals: Provider activation journal root.
    :ivar selection_journals: Global selection operation journal root.
    :ivar durable_operations: Durable due and retry operation root.
    :ivar codex_session_home: Non-secret coordinated Codex session home.
    :ivar shell_integration: Generated POSIX shell integration source.
    :ivar service_state: Supervisor readiness state authority.
    :ivar service_setup_acknowledgement: Approved control protocol generation.
    :ivar service_logs: Sanitized supervisor diagnostic root.
    :ivar runtime_directory: Owner-only local control runtime root.
    :ivar supervisor_socket: Local supervisor control socket.
    :ivar participant_sockets: Owner-only participant socket root.
    :ivar systemd_user_service: Linux user-service definition.
    :ivar launch_agent: macOS per-user LaunchAgent definition.
    """

    accounts: Path
    private_credentials: Path
    private_codex_profiles: Path
    activity_snapshots: Path
    usage_snapshots: Path
    metrics_refresh_status: Path
    credential_refresh: Path
    private_claude_profiles: Path
    selected_state: Path
    activation_journals: Path
    selection_journals: Path
    durable_operations: Path
    codex_session_home: Path
    shell_integration: Path
    service_state: Path
    service_setup_acknowledgement: Path
    service_logs: Path
    runtime_directory: Path
    supervisor_socket: Path
    participant_sockets: Path
    systemd_user_service: Path
    launch_agent: Path


def managed_codex_home(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> Path:
    """Derive one private Codex home from its stable account ID."""
    return _managed_profile_path(
        paths.private_codex_profiles,
        account_id,
        provider_name="Codex",
    )


def managed_claude_config_dir(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> Path:
    """Derive a private Claude config directory from its stable account ID."""
    return _managed_profile_path(
        paths.private_claude_profiles,
        account_id,
        provider_name="Claude",
    )


def _managed_profile_path(
    root: Path,
    account_id: SidekickAccountId,
    *,
    provider_name: str,
) -> Path:
    if not root.is_absolute():
        raise ValueError(
            f"Private {provider_name} profile root must be absolute."
        )
    try:
        normalized = root.resolve(strict=False)
    except OSError, RuntimeError:
        raise ValueError(
            f"Private {provider_name} profile root is unsafe."
        ) from None
    if normalized != root:
        raise ValueError(f"Private {provider_name} profile root is unsafe.")
    if unicodedata.normalize("NFC", str(root)) != str(root):
        raise ValueError(
            f"Private {provider_name} profile root is not normalized."
        )
    profile = root / str(account_id)
    if profile.parent != root:
        raise ValueError(
            f"Private {provider_name} profile path escaped its root."
        )
    return profile


def discover_application_paths() -> ApplicationPaths:
    """Discover current Sidekick paths without side effects.

    :return: Sidekick-owned paths for the current user.
    :raises PathDiscoveryError: If an environment override is unsafe.
    """
    _validate_environment()
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
    private_root = native_data_root / "credentials"
    runtime_directory = native.user_runtime_path
    return ApplicationPaths(
        accounts=native_data_root / "accounts.json",
        private_credentials=private_root,
        private_codex_profiles=private_root / "codex",
        activity_snapshots=native_data_root / "token-activity.json",
        usage_snapshots=native_data_root / "usage-metrics.json",
        metrics_refresh_status=native_data_root / "metrics-refresh.json",
        credential_refresh=native_data_root / "credential-refresh",
        private_claude_profiles=private_root / "claude",
        selected_state=native_data_root / "selected-accounts.json",
        activation_journals=native_data_root / "activation-journals",
        selection_journals=native_data_root / "selection-journals",
        durable_operations=native_data_root / "operations",
        codex_session_home=native_data_root / "sessions" / "codex",
        shell_integration=native_data_root / "shell-integration.sh",
        service_state=native_data_root / "service-state.json",
        service_setup_acknowledgement=(
            native_data_root / "service-setup-acknowledgement.json"
        ),
        service_logs=native.user_log_path,
        runtime_directory=runtime_directory,
        supervisor_socket=runtime_directory / "supervisor.sock",
        participant_sockets=runtime_directory / "participants",
        systemd_user_service=(
            Path.home()
            / ".config"
            / "systemd"
            / "user"
            / "sidekick-usages.service"
        ),
        launch_agent=(
            Path.home()
            / "Library"
            / "LaunchAgents"
            / "com.sidekick-usages.supervisor.plist"
        ),
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
