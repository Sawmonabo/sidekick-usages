"""Release-pinned managed Claude capability gate."""

from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    SUPPORTED_CLAUDE_VERSION,
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.login.service import (
    verify_logged_out_claude_status,
)
from sidekick_usages.providers.claude.managed.models import (
    ClaudeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeExecutable,
    ClaudeManagedProfile,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_LOGIN_HELP_OUTPUT_BYTES = 64 * 1024
_LOGIN_HELP_TIMEOUT_SECONDS = 5.0
_MANAGED_PLATFORMS = {
    HostPlatform.LINUX: ClaudeManagedPlatform.LINUX_FILE,
    HostPlatform.WSL: ClaudeManagedPlatform.WSL_FILE,
    HostPlatform.MACOS_ARM64: ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
    HostPlatform.MACOS_X64: ClaudeManagedPlatform.MACOS_X64_KEYCHAIN,
}
_REQUIRED_LOGIN_OPTIONS = (
    "--claudeai",
    "--console",
    "--email",
    "--sso",
)
_REFRESH_TOKEN_PROVISIONING_VERSIONS = frozenset({SUPPORTED_CLAUDE_VERSION})


def managed_claude_platform(
    host: HostPlatform,
) -> ClaudeManagedPlatform:
    """Map one supported host to its exact Claude storage boundary."""
    if host is HostPlatform.WINDOWS:
        raise ClaudeManagedError(ClaudeManagedFailure.FEATURE_DISABLED)
    try:
        return _MANAGED_PLATFORMS[host]
    except KeyError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.PLATFORM_UNSUPPORTED
        ) from None


def probe_claude_capabilities(
    executable: ClaudeExecutable,
    profile: ClaudeManagedProfile,
    platform: ClaudeManagedPlatform,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeCapabilities:
    """Prove required auth surfaces without starting official login."""
    verify_claude_executable(executable)
    _probe_status(executable, environment, working_directory, runner)
    _probe_login(executable, environment, working_directory, runner)
    if executable.version not in _REFRESH_TOKEN_PROVISIONING_VERSIONS:
        raise ClaudeManagedError(
            ClaudeManagedFailure.REFRESH_PROVISIONING_UNPROVEN
        )
    verify_claude_executable(executable)
    return ClaudeCapabilities(executable, profile, platform)


def _probe_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    runner: ClaudeCommandRunner,
) -> None:
    verify_logged_out_claude_status(
        executable,
        environment,
        working_directory,
        runner=runner,
    )


def _probe_login(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    runner: ClaudeCommandRunner,
) -> None:
    try:
        result = runner(
            (
                str(executable.provenance.path),
                "auth",
                "login",
                "--help",
            ),
            timeout_seconds=_LOGIN_HELP_TIMEOUT_SECONDS,
            maximum_output_bytes=_LOGIN_HELP_OUTPUT_BYTES,
            environment=environment,
            working_directory=working_directory,
        )
    except ClaudeProcessError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.LOGIN_UNSUPPORTED
        ) from None
    try:
        help_text = result.output.decode("utf-8")
    except UnicodeDecodeError:
        raise ClaudeManagedError(
            ClaudeManagedFailure.LOGIN_UNSUPPORTED
        ) from None
    if result.return_code != 0 or any(
        option not in help_text for option in _REQUIRED_LOGIN_OPTIONS
    ):
        raise ClaudeManagedError(ClaudeManagedFailure.LOGIN_UNSUPPORTED)
