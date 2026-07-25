"""Provider-neutral saved-account transition invariants."""

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.types import ProviderId


def account_transition_state_matches(
    current: SavedAccount,
    candidate: SavedAccount,
    provider_id: ProviderId,
) -> bool:
    """Return whether identity and heartbeat state remain unchanged."""
    return (
        current.account_id == candidate.account_id
        and current.label == candidate.label
        and current.provider_id is provider_id
        and candidate.provider_id is provider_id
        and current.heartbeat_enabled == candidate.heartbeat_enabled
        and current.heartbeat_window_resets
        == candidate.heartbeat_window_resets
        and current.heartbeat_targets == candidate.heartbeat_targets
        and current.last_heartbeat_at == candidate.last_heartbeat_at
        and current.last_heartbeat_status == candidate.last_heartbeat_status
        and current.last_heartbeat_error_code
        == candidate.last_heartbeat_error_code
    )
