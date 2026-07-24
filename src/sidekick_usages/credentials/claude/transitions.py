"""Claude credential-variant transition policy."""

from dataclasses import replace

from sidekick_usages.core.models import (
    ClaudeCredentials,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


def _identity_failure(message: str) -> ProviderFailure:
    """Return one secret-safe Claude authorization failure."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=ProviderFailureKind.IDENTITY_MISMATCH,
        message=message,
    )


def _replace_login(
    current: ClaudeLoginCredentials,
    incoming: ClaudeLoginCredentials,
    *,
    replace_identity: bool,
) -> ClaudeCredentials | ProviderFailure:
    """Apply identity policy within the subscription-login variant."""
    if current.identity is not None and incoming.identity is not None:
        identity_proven = current.identity == incoming.identity
    else:
        identity_proven = current.access_token == incoming.access_token
    if not replace_identity and not identity_proven:
        return _identity_failure(
            "Refusing a different Claude login without matching stable "
            "identity; use --replace-identity to replace this label."
        )
    if replace_identity or incoming.identity is not None:
        return incoming
    return replace(incoming, identity=current.identity)


def authorize_claude_setup_token_transition(
    current: ClaudeCredentials,
    *,
    replace_identity: bool,
    replace_auth_method: bool,
) -> ProviderFailure | None:
    """Authorize replacing current Claude credentials with a setup token."""
    if isinstance(current, ClaudeSetupTokenCredentials):
        return None
    if not replace_auth_method:
        return _identity_failure(
            "Claude authentication-method replacement requires --force."
        )
    if not replace_identity:
        return _identity_failure(
            "Claude stable-identity replacement also requires "
            "--replace-identity."
        )
    return None


def apply_claude_transition(
    current: ClaudeCredentials,
    incoming: ClaudeCredentials,
    *,
    replace_identity: bool,
    replace_auth_method: bool,
) -> ClaudeCredentials | ProviderFailure:
    """Return an authorized complete Claude credential replacement."""
    if isinstance(incoming, ClaudeSetupTokenCredentials):
        failure = authorize_claude_setup_token_transition(
            current,
            replace_identity=replace_identity,
            replace_auth_method=replace_auth_method,
        )
        return failure if failure is not None else incoming
    if isinstance(current, ClaudeLoginCredentials) and isinstance(
        incoming,
        ClaudeLoginCredentials,
    ):
        return _replace_login(
            current,
            incoming,
            replace_identity=replace_identity,
        )
    if not replace_auth_method:
        return _identity_failure(
            "Claude authentication-method replacement requires "
            "--replace-auth-method."
        )
    identity_changes = (
        isinstance(current, ClaudeSetupTokenCredentials)
        and isinstance(incoming, ClaudeLoginCredentials)
        and incoming.identity is not None
    )
    if identity_changes and not replace_identity:
        return _identity_failure(
            "Claude stable-identity replacement also requires "
            "--replace-identity."
        )
    return incoming
