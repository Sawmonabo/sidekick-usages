"""Provider-safe failures for managed Claude migration."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.managed.exchange.types import (
    ClaudeExchangeFailureKind,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)

EXCHANGE_FAILURES: dict[
    ClaudeExchangeFailureKind,
    tuple[ProviderFailureKind, str, bool],
] = {
    ClaudeExchangeFailureKind.IDENTITY_MISMATCH: (
        ProviderFailureKind.IDENTITY_MISMATCH,
        "Official Claude login belongs to a different account.",
        True,
    ),
    ClaudeExchangeFailureKind.MALFORMED: (
        ProviderFailureKind.MALFORMED,
        "Managed Claude credential state is malformed.",
        True,
    ),
    ClaudeExchangeFailureKind.INCOMPATIBLE: (
        ProviderFailureKind.UNSUPPORTED,
        "Managed Claude credential storage is unsupported.",
        True,
    ),
    ClaudeExchangeFailureKind.UNREADABLE: (
        ProviderFailureKind.UNREADABLE,
        "Managed Claude credential state is unreadable.",
        True,
    ),
    ClaudeExchangeFailureKind.MISSING: (
        ProviderFailureKind.MISSING,
        "Managed Claude login is missing.",
        True,
    ),
    ClaudeExchangeFailureKind.TIMED_OUT: (
        ProviderFailureKind.UNREADABLE,
        "Official Claude login timed out.",
        False,
    ),
    ClaudeExchangeFailureKind.TRANSIENT: (
        ProviderFailureKind.UNREADABLE,
        "Official Claude login is temporarily unavailable.",
        False,
    ),
    ClaudeExchangeFailureKind.LOGIN_FAILED: (
        ProviderFailureKind.REJECTED,
        "Official Claude rejected the saved login.",
        True,
    ),
    ClaudeExchangeFailureKind.UNCHANGED: (
        ProviderFailureKind.REJECTED,
        "Official Claude did not advance the credential generation.",
        False,
    ),
    ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED: (
        ProviderFailureKind.IDENTITY_MISMATCH,
        "Managed Claude state requires reconciliation.",
        True,
    ),
}


def migration_failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    action_required: bool = True,
) -> ProviderFailure:
    """Build one redacted Claude migration failure."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        message=message,
        action_required=action_required,
    )


def exchange_failure(
    kind: ClaudeExchangeFailureKind,
) -> ProviderFailure:
    """Translate one managed-login exchange failure."""
    failure_kind, message, action_required = EXCHANGE_FAILURES[kind]
    return migration_failure(
        failure_kind,
        message,
        action_required=action_required,
    )
