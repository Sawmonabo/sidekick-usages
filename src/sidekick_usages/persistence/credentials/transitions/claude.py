"""No-secret persistence rules for managed Claude authorities."""

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
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
            or (
                candidate_subscription.refresh_expires_at is not None
                and candidate_subscription.refresh_expires_at
                >= current_subscription.refresh_expires_at
            )
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
