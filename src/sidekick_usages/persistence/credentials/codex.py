"""No-secret persistence rules for managed Codex authorities."""

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.types import ProviderId


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
