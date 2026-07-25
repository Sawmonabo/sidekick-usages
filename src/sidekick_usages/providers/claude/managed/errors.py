"""Secret-safe managed-Claude capability failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)

_MANAGED_FAILURE_MESSAGES = {
    ClaudeManagedFailure.FEATURE_DISABLED: (
        "Managed Claude account switching is disabled on native Windows."
    ),
    ClaudeManagedFailure.PLATFORM_UNSUPPORTED: (
        "Managed Claude account switching is unsupported on this platform."
    ),
    ClaudeManagedFailure.PROFILE_UNSAFE: (
        "The managed Claude profile is unsafe."
    ),
    ClaudeManagedFailure.EXECUTABLE_MISSING: (
        "The Claude CLI executable was not found."
    ),
    ClaudeManagedFailure.EXECUTABLE_UNSAFE: (
        "The Claude CLI executable changed or is unsafe."
    ),
    ClaudeManagedFailure.VERSION_UNSUPPORTED: (
        "The installed Claude CLI version is not supported."
    ),
    ClaudeManagedFailure.STATUS_UNSUPPORTED: (
        "The installed Claude CLI lacks the required auth-status capability."
    ),
    ClaudeManagedFailure.LOGIN_UNSUPPORTED: (
        "The installed Claude CLI lacks the required official-login "
        "capability."
    ),
    ClaudeManagedFailure.REFRESH_PROVISIONING_UNPROVEN: (
        "The installed Claude CLI refresh-token login is not proven."
    ),
    ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT: (
        "The official Claude login process timed out."
    ),
    ClaudeManagedFailure.OFFICIAL_LOGIN_UNAVAILABLE: (
        "The official Claude login process is unavailable."
    ),
    ClaudeManagedFailure.OFFICIAL_LOGIN_UNVERIFIED: (
        "The official Claude login did not yield a verified session."
    ),
}


class ClaudeManagedError(UsageError):
    """One managed-Claude failure containing no provider output."""

    def __init__(self, code: ClaudeManagedFailure) -> None:
        self.code = code
        super().__init__(_MANAGED_FAILURE_MESSAGES[code])
