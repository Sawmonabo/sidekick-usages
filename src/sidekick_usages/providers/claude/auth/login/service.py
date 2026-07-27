"""Official Claude login process boundary."""

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path

from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.claude.auth.login.models import (
    ClaudeAuthStatus,
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import (
    ClaudeManagedError,
    raise_managed_capability_error,
)
from sidekick_usages.providers.claude.managed.executable import (
    verify_claude_executable,
)
from sidekick_usages.providers.claude.managed.types import ClaudeManagedFailure
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
_FIRST_PARTY_API_PROVIDER = "firstParty"
_STATUS_IDENTITY_PREFIX = "claude-auth-status-sha256:"


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
    cancelled: Callable[[], bool] | None = None,
) -> None:
    """Require the documented logged-out first-party auth status."""
    status = _read_auth_status(
        executable,
        environment,
        working_directory,
        ClaudeManagedFailure.STATUS_UNSUPPORTED,
        runner,
        cancelled,
    )
    if (
        status.return_code != 1
        or status.logged_in
        or status.auth_method != "none"
        or status.api_provider != _FIRST_PARTY_API_PROVIDER
    ):
        raise ClaudeManagedError(ClaudeManagedFailure.STATUS_UNSUPPORTED)


def verify_official_claude_login_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> None:
    """Require a logged-in first-party status in a credential-free process."""
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
        or status.api_provider != _FIRST_PARTY_API_PROVIDER
    ):
        raise ClaudeManagedError(
            ClaudeManagedFailure.OFFICIAL_LOGIN_UNVERIFIED
        )


def read_official_claude_auth_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    *,
    runner: ClaudeCommandRunner = run_bounded_claude_command,
) -> ClaudeAuthStatus:
    """Return bounded non-secret native status from the official Claude CLI."""
    return _read_auth_status(
        executable,
        environment,
        working_directory,
        ClaudeManagedFailure.OFFICIAL_LOGIN_UNVERIFIED,
        runner,
    )


def claude_status_provider_identity(
    status: ClaudeAuthStatus,
) -> ProviderIdentity | None:
    """Hash provider-returned status identity for external-only tracking."""
    if (
        status.return_code != 0
        or not status.logged_in
        or status.auth_method != _SUBSCRIPTION_AUTH_METHOD
        or status.api_provider != _FIRST_PARTY_API_PROVIDER
        or status.email is None
    ):
        return None
    email = status.email.encode("utf-8")
    organization_id = (
        b""
        if status.organization_id is None
        else status.organization_id.encode("utf-8")
    )
    material = (
        len(email).to_bytes(4, byteorder="big")
        + email
        + len(organization_id).to_bytes(4, byteorder="big")
        + organization_id
    )
    return ProviderIdentity(
        _STATUS_IDENTITY_PREFIX + hashlib.sha256(material).hexdigest()
    )


def _read_auth_status(
    executable: ClaudeExecutable,
    environment: Mapping[str, str],
    working_directory: Path,
    failure: ClaudeManagedFailure,
    runner: ClaudeCommandRunner,
    cancelled: Callable[[], bool] | None = None,
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
            cancelled=cancelled,
        )
    except ClaudeProcessError as error:
        raise_managed_capability_error(error, failure)
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
    email = payload.get("email")
    organization_id = payload.get("orgId")
    organization_name = payload.get("orgName")
    subscription_type = payload.get("subscriptionType")
    if (
        not isinstance(logged_in, bool)
        or not isinstance(auth_method, str)
        or not isinstance(api_provider, str)
        or (email is not None and not isinstance(email, str))
        or (
            organization_id is not None
            and not isinstance(organization_id, str)
        )
        or (
            organization_name is not None
            and not isinstance(organization_name, str)
        )
        or (
            subscription_type is not None
            and not isinstance(subscription_type, str)
        )
    ):
        raise ClaudeManagedError(failure)
    try:
        return ClaudeAuthStatus(
            return_code=result.return_code,
            logged_in=logged_in,
            auth_method=auth_method,
            api_provider=api_provider,
            email=email,
            organization_id=organization_id,
            organization_name=organization_name,
            subscription_type=subscription_type,
        )
    except TypeError, ValueError:
        raise ClaudeManagedError(failure) from None
