"""No-secret persistence rules for Claude authority transitions."""

from dataclasses import replace

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeStoredLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
)
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.persistence.credentials.transitions.account import (
    account_transition_state_matches,
)
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.types.credential import StoredCredentialKind


def managed_claude_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether one verified refresh preserves its Claude authority."""
    if (
        not account_transition_state_matches(
            current,
            candidate,
            ProviderId.CLAUDE,
        )
        or not isinstance(current.authority, ClaudeAccountAuthority)
        or not isinstance(candidate.authority, ClaudeAccountAuthority)
        or current.authority.setup_token != candidate.authority.setup_token
    ):
        return False
    current_subscription = current.authority.subscription
    candidate_subscription = candidate.authority.subscription
    return (
        isinstance(current_subscription, ClaudeManagedLoginAuthority)
        and isinstance(candidate_subscription, ClaudeManagedLoginAuthority)
        and current_subscription.authority_id
        == candidate_subscription.authority_id
        and current_subscription.provider_identity
        == candidate_subscription.provider_identity
        and current_subscription.generation
        != candidate_subscription.generation
        and candidate_subscription.access_expires_at
        >= current_subscription.access_expires_at
        and candidate_subscription.access_expires_at
        > candidate_subscription.verified_at
        and (
            current_subscription.refresh_expires_at is None
            or candidate_subscription.refresh_expires_at is not None
        )
        and (
            candidate_subscription.refresh_expires_at is None
            or candidate_subscription.refresh_expires_at
            > candidate_subscription.verified_at
        )
        and candidate_subscription.verified_at
        >= current_subscription.verified_at
        and candidate_subscription.health is CredentialHealth.HEALTHY
        and candidate_subscription.action is CredentialAction.NONE
        and candidate.credential_health is CredentialHealth.HEALTHY
        and candidate.last_refresh_at == candidate_subscription.verified_at
        and candidate.last_refresh_status is RefreshStatus.OK
        and candidate.last_refresh_error_code is None
    )


def stored_claude_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether stored Claude state became a managed subscription."""
    if (
        not account_transition_state_matches(
            current,
            candidate,
            ProviderId.CLAUDE,
        )
        or not isinstance(current.authority, ClaudeAccountAuthority)
        or not isinstance(candidate.authority, ClaudeAccountAuthority)
        or current.authority.setup_token != candidate.authority.setup_token
    ):
        return False
    source = current.authority.subscription
    managed = candidate.authority.subscription
    if not isinstance(managed, ClaudeManagedLoginAuthority):
        return False
    if isinstance(source, ClaudeStoredLoginAuthority):
        authority_matches = source.authority_id == managed.authority_id and (
            source.provider_identity is None
            or source.provider_identity == managed.provider_identity
        )
    elif source is None and current.authority.setup_token is not None:
        authority_matches = (
            managed.authority_id != current.authority.setup_token.authority_id
        )
    else:
        return False
    return (
        authority_matches
        and managed.access_expires_at > managed.verified_at
        and (
            managed.refresh_expires_at is None
            or managed.refresh_expires_at > managed.verified_at
        )
        and managed.health is CredentialHealth.HEALTHY
        and managed.action is CredentialAction.NONE
        and candidate.credential_health is CredentialHealth.HEALTHY
        and candidate.last_refresh_at == managed.verified_at
        and candidate.last_refresh_status is RefreshStatus.OK
        and candidate.last_refresh_error_code is None
    )


def reconciled_claude_setup_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether a managed association returned to its setup token."""
    current_authority = current.authority
    candidate_authority = candidate.authority
    if (
        not isinstance(current_authority, ClaudeAccountAuthority)
        or not isinstance(candidate_authority, ClaudeAccountAuthority)
        or current_authority.setup_token is None
        or not isinstance(
            current_authority.subscription,
            ClaudeManagedLoginAuthority,
        )
        or candidate_authority
        != ClaudeAccountAuthority(
            setup_token=current_authority.setup_token,
            subscription=None,
        )
    ):
        return False
    return candidate == replace(
        current,
        authority=candidate_authority,
        credential_health=current_authority.setup_token.health,
        last_refresh_at=None,
        last_refresh_status=None,
        last_refresh_error_code=None,
    )


def stored_claude_setup_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
    authority: StoredCredentialAuthority,
) -> bool:
    """Return whether only one protected setup-token authority changed."""
    old_authority = current.authority
    new_authority = candidate.authority
    if (
        not isinstance(old_authority, ClaudeAccountAuthority)
        or not isinstance(new_authority, ClaudeAccountAuthority)
        or new_authority.setup_token is None
        or old_authority.subscription != new_authority.subscription
        or candidate != replace(current, authority=new_authority)
        or authority.account_id != current.account_id
        or authority.authority_id != new_authority.setup_token.authority_id
        or authority.provider_id is not ProviderId.CLAUDE
        or authority.kind is not StoredCredentialKind.CLAUDE_SETUP
    ):
        return False
    old_setup = old_authority.setup_token
    subscription = new_authority.subscription
    return (
        old_setup is None
        or old_setup.authority_id == new_authority.setup_token.authority_id
    ) and (
        subscription is None
        or subscription.authority_id != new_authority.setup_token.authority_id
    )
