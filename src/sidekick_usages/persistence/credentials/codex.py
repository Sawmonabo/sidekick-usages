"""No-secret persistence rules for managed Codex authorities."""

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import CredentialHealth
from sidekick_usages.core.types import ProviderId, RefreshStatus


def managed_codex_transition_matches(
    current: SavedAccount,
    candidate: SavedAccount,
) -> bool:
    """Return whether metadata changes preserve the managed authority."""
    if (
        current.account_id != candidate.account_id
        or current.label != candidate.label
        or current.provider_id is not ProviderId.CODEX
        or candidate.provider_id is not ProviderId.CODEX
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
        current.account_id != candidate.account_id
        or current.label != candidate.label
        or current.provider_id is not ProviderId.CODEX
        or candidate.provider_id is not ProviderId.CODEX
        or current.plan != candidate.plan
        or current.heartbeat_enabled != candidate.heartbeat_enabled
        or current.heartbeat_window_resets != candidate.heartbeat_window_resets
        or current.heartbeat_targets != candidate.heartbeat_targets
        or current.last_heartbeat_at != candidate.last_heartbeat_at
        or current.last_heartbeat_status != candidate.last_heartbeat_status
        or current.last_heartbeat_error_code
        != candidate.last_heartbeat_error_code
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
