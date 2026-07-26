"""Official managed-Claude login process boundary."""

import os
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.environment import (
    claude_probe_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.models import ClaudeAuthStatus
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeExecutable,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
    run_interactive_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeInteractiveCommandRunner,
    ClaudeProcessFailure,
)
from sidekick_usages.serialization.json import decode_json_object

_AUTH_STATUS_OUTPUT_BYTES = 4096
_AUTH_STATUS_TIMEOUT_SECONDS = 5.0
_MAXIMUM_LOGIN_OUTPUT_BYTES = 1024 * 1024
_INTERACTIVE_LOGIN_TIMEOUT_SECONDS = 600.0
_OFFICIAL_LOGIN_TIMEOUT_SECONDS = 60.0
_PRIVATE_PROCESS_UMASK = 0o077
_SUBSCRIPTION_AUTH_METHOD = "claude.ai"


def run_official_claude_login(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeOfficialLoginResult:
    """Run one bounded official login without exposing process output."""
    verify_claude_executable(executable)
    try:
        result = runner(
            _official_login_command(executable),
            timeout_seconds=_OFFICIAL_LOGIN_TIMEOUT_SECONDS,
            maximum_output_bytes=_MAXIMUM_LOGIN_OUTPUT_BYTES,
            environment=environment,
            working_directory=working_directory,
            umask=_PRIVATE_PROCESS_UMASK if os.name == "posix" else -1,
        )
    except ClaudeProcessError as error:
        failure = (
            ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT
            if error.code is ClaudeProcessFailure.TIMED_OUT
            else ClaudeManagedFailure.OFFICIAL_LOGIN_UNAVAILABLE
        )
        raise ClaudeManagedError(failure) from None
    finally:
        verify_claude_executable(executable)
    login_result = (
        ClaudeOfficialLoginResult.SUCCEEDED
        if result.return_code == 0
        else ClaudeOfficialLoginResult.FAILED
    )
    del result
    return login_result


def run_interactive_official_claude_login(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeInteractiveCommandRunner = run_interactive_claude_command,
) -> ClaudeOfficialLoginResult:
    """Run official login with direct provider-owned terminal interaction."""
    verify_claude_executable(executable)
    try:
        return_code = runner(
            _official_login_command(executable),
            timeout_seconds=_INTERACTIVE_LOGIN_TIMEOUT_SECONDS,
            environment=environment,
            working_directory=working_directory,
            umask=_PRIVATE_PROCESS_UMASK if os.name == "posix" else -1,
        )
    except ClaudeProcessError as error:
        failure = (
            ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT
            if error.code is ClaudeProcessFailure.TIMED_OUT
            else ClaudeManagedFailure.OFFICIAL_LOGIN_UNAVAILABLE
        )
        raise ClaudeManagedError(failure) from None
    finally:
        verify_claude_executable(executable)
    return (
        ClaudeOfficialLoginResult.SUCCEEDED
        if return_code == 0
        else ClaudeOfficialLoginResult.FAILED
    )


def verify_logged_out_claude_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> None:
    """Require the documented logged-out first-party auth status."""
    status = _read_auth_status(
        executable,
        environment,
        working_directory,
        ClaudeManagedFailure.STATUS_UNSUPPORTED,
        runner,
    )
    if (
        status.return_code != 1
        or status.logged_in
        or status.auth_method != "none"
        or status.api_provider != "firstParty"
    ):
        raise ClaudeManagedError(ClaudeManagedFailure.STATUS_UNSUPPORTED)


def verify_official_claude_login_status(
    executable: ClaudeExecutable,
    source_environment: Mapping[str, str] | None,
    process_home: Path,
    config_directory: Path,
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> None:
    """Require a logged-in first-party status in a credential-free process."""
    environment = claude_probe_environment(
        source_environment,
        isolated_home=process_home,
        config_directory=config_directory,
    )
    status = _read_auth_status(
        executable,
        environment,
        working_directory,
        ClaudeManagedFailure.OFFICIAL_LOGIN_UNVERIFIED,
        runner,
    )
    if (
        status.return_code != 0
        or not status.logged_in
        or status.auth_method != _SUBSCRIPTION_AUTH_METHOD
        or status.api_provider != "firstParty"
    ):
        raise ClaudeManagedError(
            ClaudeManagedFailure.OFFICIAL_LOGIN_UNVERIFIED
        )


def _read_auth_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    failure: ClaudeManagedFailure,
    runner: ClaudeCommandRunner,
) -> ClaudeAuthStatus:
    verify_claude_executable(executable)
    try:
        result = runner(
            (
                str(executable.provenance.path),
                "auth",
                "status",
            ),
            timeout_seconds=_AUTH_STATUS_TIMEOUT_SECONDS,
            maximum_output_bytes=_AUTH_STATUS_OUTPUT_BYTES,
            environment=environment,
            working_directory=working_directory,
        )
    except ClaudeProcessError:
        raise ClaudeManagedError(failure) from None
    finally:
        verify_claude_executable(executable)
    status = _decode_auth_status(result, failure)
    del result
    return status


def _official_login_command(
    executable: ClaudeExecutable,
) -> tuple[str, ...]:
    """Return the sole supported subscription-login command."""
    return (
        str(executable.provenance.path),
        "auth",
        "login",
        "--claudeai",
    )


def _decode_auth_status(
    result: ClaudeCommandResult,
    failure: ClaudeManagedFailure,
) -> ClaudeAuthStatus:
    try:
        payload = decode_json_object(result.output)
    except InvalidPayloadError:
        raise ClaudeManagedError(failure) from None
    logged_in = payload.get("loggedIn")
    auth_method = payload.get("authMethod")
    api_provider = payload.get("apiProvider")
    if (
        not isinstance(logged_in, bool)
        or not isinstance(auth_method, str)
        or not isinstance(api_provider, str)
    ):
        raise ClaudeManagedError(failure)
    return ClaudeAuthStatus(
        return_code=result.return_code,
        logged_in=logged_in,
        auth_method=auth_method,
        api_provider=api_provider,
    )
