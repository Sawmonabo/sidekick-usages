"""Managed Codex failure classification."""

from sidekick_usages.core.accounts.types import CredentialHealth
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

_APP_SERVER_OUTCOMES = {
    CodexAppServerFailure.EXECUTABLE_MISSING: CodexManagedOutcome.INCOMPATIBLE,
    CodexAppServerFailure.EXECUTABLE_UNSAFE: CodexManagedOutcome.INCOMPATIBLE,
    CodexAppServerFailure.VERSION_UNSUPPORTED: (
        CodexManagedOutcome.INCOMPATIBLE
    ),
    CodexAppServerFailure.CAPABILITY_UNSUPPORTED: (
        CodexManagedOutcome.INCOMPATIBLE
    ),
    CodexAppServerFailure.PROCESS_FAILED: CodexManagedOutcome.TRANSIENT,
    CodexAppServerFailure.PROCESS_TIMEOUT: CodexManagedOutcome.TIMED_OUT,
    CodexAppServerFailure.PROTOCOL_MALFORMED: CodexManagedOutcome.MALFORMED,
    CodexAppServerFailure.REQUEST_REJECTED: CodexManagedOutcome.REJECTED,
    CodexAppServerFailure.PROTOCOL_TIMEOUT: CodexManagedOutcome.TIMED_OUT,
    CodexAppServerFailure.PROTOCOL_CLOSED: CodexManagedOutcome.TRANSIENT,
}
_APP_SERVER_FAILURE_KINDS = {
    CodexManagedOutcome.INCOMPATIBLE: ProviderFailureKind.UNSUPPORTED,
    CodexManagedOutcome.TRANSIENT: ProviderFailureKind.UNREADABLE,
    CodexManagedOutcome.TIMED_OUT: ProviderFailureKind.UNREADABLE,
    CodexManagedOutcome.MALFORMED: ProviderFailureKind.MALFORMED,
    CodexManagedOutcome.REJECTED: ProviderFailureKind.REJECTED,
}
_PROVIDER_OUTCOMES = {
    ProviderFailureKind.MISSING: CodexManagedOutcome.LOGGED_OUT,
    ProviderFailureKind.UNREADABLE: CodexManagedOutcome.TRANSIENT,
    ProviderFailureKind.MALFORMED: CodexManagedOutcome.MALFORMED,
    ProviderFailureKind.INCOMPLETE: CodexManagedOutcome.MALFORMED,
    ProviderFailureKind.EXPIRED: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.REJECTED: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.IDENTITY_MISMATCH: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.UNSUPPORTED: CodexManagedOutcome.INCOMPATIBLE,
}
_OUTCOME_HEALTH = {
    CodexManagedOutcome.HEALTHY: CredentialHealth.HEALTHY,
    CodexManagedOutcome.UNCHANGED: CredentialHealth.REFRESH_DUE,
    CodexManagedOutcome.REJECTED: CredentialHealth.LOGIN_REQUIRED,
    CodexManagedOutcome.LOGGED_OUT: CredentialHealth.LOGIN_REQUIRED,
    CodexManagedOutcome.INCOMPATIBLE: CredentialHealth.UNSUPPORTED,
    CodexManagedOutcome.MALFORMED: CredentialHealth.MALFORMED,
    CodexManagedOutcome.TIMED_OUT: CredentialHealth.REFRESH_DUE,
    CodexManagedOutcome.TRANSIENT: CredentialHealth.REFRESH_DUE,
}


def managed_outcome_for_app_server(
    failure: CodexAppServerFailure,
) -> CodexManagedOutcome:
    """Classify one app-server failure for managed authority policy."""
    return _APP_SERVER_OUTCOMES[failure]


def managed_outcome_for_provider(
    failure: ProviderFailureKind,
) -> CodexManagedOutcome:
    """Classify one provider failure for managed authority policy."""
    return _PROVIDER_OUTCOMES[failure]


def credential_health_for_outcome(
    outcome: CodexManagedOutcome,
) -> CredentialHealth:
    """Return credential health for one managed operation outcome."""
    return _OUTCOME_HEALTH[outcome]


def codex_app_server_failure(
    error: CodexAppServerError,
) -> ProviderFailure:
    """Convert one secret-safe app-server error to provider vocabulary."""
    outcome = managed_outcome_for_app_server(error.code)
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=_APP_SERVER_FAILURE_KINDS[outcome],
        message=str(error),
        action_required=outcome.action_required,
    )
