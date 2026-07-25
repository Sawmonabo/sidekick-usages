"""Secret-safe usage failure classification and recovery guidance."""

from typing import assert_never

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeSetupTokenAuthority,
    SavedAccount,
)
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.persistence.errors import (
    PersistenceError,
)
from sidekick_usages.providers.claude.credentials import (
    CLAUDE_SETUP_REJECTION_MESSAGE,
    CLAUDE_SUBSCRIPTION_LOGIN_REJECTED,
)
from sidekick_usages.usage.models import (
    AuthenticationFailure,
    CredentialRecoveryKind,
    FetchFailure,
    ForbiddenFailure,
    PersistenceFailure,
    RateLimitFailure,
    TransientFailure,
)


def credential_recovery_kind(
    account: SavedAccount,
) -> CredentialRecoveryKind:
    """Classify credentials without exposing credential material."""
    authority = account.authority
    if isinstance(authority, ClaudeAccountAuthority):
        if authority.subscription is not None:
            return CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN
        if isinstance(authority.setup_token, ClaudeSetupTokenAuthority):
            return CredentialRecoveryKind.CLAUDE_SETUP_TOKEN
        raise ValueError("Claude account has no credential authority.")
    return CredentialRecoveryKind.CODEX_LOGIN


def persistence_failure(
    account: SavedAccount,
    error: PersistenceError,
) -> PersistenceFailure:
    """Return one safe usage failure for persistence state."""
    return PersistenceFailure(
        label=account.label,
        provider_id=account.provider_id,
        plan=account.plan,
        message=str(error),
        persistence_code=error.code,
    )


def failure_from_error(
    account: SavedAccount,
    error: UsageError,
) -> FetchFailure:
    """Classify one operational error without retaining provider secrets."""
    if isinstance(error, PersistenceError):
        return persistence_failure(account, error)
    if isinstance(error, AuthError):
        return AuthenticationFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=_authentication_cause(account),
            credential_kind=credential_recovery_kind(account),
        )
    if isinstance(error, ForbiddenError):
        return ForbiddenFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=error.api_message or str(error),
            required_scope=error.required_scope,
        )
    if isinstance(error, RateLimitError):
        return RateLimitFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=str(error),
            retry_after_seconds=error.retry_after,
        )
    if isinstance(error, TransientError):
        return TransientFailure(
            label=account.label,
            provider_id=account.provider_id,
            plan=account.plan,
            message=str(error),
        )
    return FetchFailure(
        label=account.label,
        provider_id=account.provider_id,
        plan=account.plan,
        message=str(error),
    )


def _authentication_cause(account: SavedAccount) -> str:
    """Return one secret-safe cause owned by the credential boundary."""
    kind = credential_recovery_kind(account)
    if kind is CredentialRecoveryKind.CLAUDE_SETUP_TOKEN:
        return CLAUDE_SETUP_REJECTION_MESSAGE
    if kind is CredentialRecoveryKind.CLAUDE_SUBSCRIPTION_LOGIN:
        return CLAUDE_SUBSCRIPTION_LOGIN_REJECTED
    if kind is CredentialRecoveryKind.CODEX_LOGIN:
        return "Codex rejected the saved login."
    return assert_never(kind)
