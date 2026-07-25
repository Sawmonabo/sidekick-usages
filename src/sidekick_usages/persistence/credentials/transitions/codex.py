"""No-secret persistence rules for managed Codex authorities."""

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import CredentialHealth
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.persistence.credentials.transitions.account import (
    account_transition_state_matches,
)


def managed_codex_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether metadata changes preserve the managed authority."""
    if (
        not account_transition_state_matches(
            current,
            candidate,
            ProviderId.CODEX,
        )
        or not isinstance(current.authority, CodexAccountAuthority)
        or not isinstance(candidate.authority, CodexAccountAuthority)
    ):
        return False
    current_subscription = current.authority.subscription
    candidate_subscription = candidate.authority.subscription
    return (
        isinstance(current_subscription, CodexManagedAuthority)
        and isinstance(candidate_subscription, CodexManagedAuthority)
        and current_subscription.authority_id
        == candidate_subscription.authority_id
        and current_subscription.provider_identity
        == candidate_subscription.provider_identity
    )


def stored_codex_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether one stored authority became its managed replacement."""
    if (
        not account_transition_state_matches(
            current,
            candidate,
            ProviderId.CODEX,
        )
        or current.plan != candidate.plan
        or not isinstance(current.authority, CodexAccountAuthority)
        or not isinstance(candidate.authority, CodexAccountAuthority)
    ):
        return False
    current_subscription = current.authority.subscription
    candidate_subscription = candidate.authority.subscription
    return (
        isinstance(current_subscription, CodexStoredAuthority)
        and current_subscription.provider_identity is not None
        and isinstance(candidate_subscription, CodexManagedAuthority)
        and current_subscription.authority_id
        == candidate_subscription.authority_id
        and current_subscription.provider_identity
        == candidate_subscription.provider_identity
        and (
            current_subscription.generation is None
            or current_subscription.generation
            != candidate_subscription.generation
        )
        and candidate_subscription.health is CredentialHealth.HEALTHY
        and candidate.credential_health is CredentialHealth.HEALTHY
        and candidate.last_refresh_at == candidate_subscription.verified_at
        and candidate.last_refresh_status is RefreshStatus.OK
        and candidate.last_refresh_error_code is None
    )
