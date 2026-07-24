"""Claude setup-token persistence preflight."""

from sidekick_usages.core.models import (
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.claude.transitions import (
    authorize_claude_setup_token_transition,
)
from sidekick_usages.credentials.models import ClaudeSetupTokenSavePreview
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


def preview_claude_setup_token_save(
    store: AccountStore,
    label: AccountLabel | None,
    *,
    force: bool,
    replace_identity: bool,
) -> ClaudeSetupTokenSavePreview | ProviderFailure | None:
    """Authorize a known login-to-setup crossing before token capture."""
    if label is None:
        return None
    target = store.get(str(label))
    if target is None:
        return None
    credentials = target.credentials
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return None
    if not isinstance(credentials, ClaudeLoginCredentials):
        return ProviderFailure(
            provider_id=ProviderId.CLAUDE,
            kind=ProviderFailureKind.IDENTITY_MISMATCH,
            message="The requested label belongs to another provider.",
        )
    failure = authorize_claude_setup_token_transition(
        credentials,
        replace_identity=replace_identity,
        replace_auth_method=force,
    )
    if failure is not None:
        return failure
    return ClaudeSetupTokenSavePreview(label)
