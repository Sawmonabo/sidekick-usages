"""Pure managed Codex account-state transitions."""

from dataclasses import replace
from datetime import datetime

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import AuthorityId, CredentialHealth
from sidekick_usages.core.types import RefreshStatus
from sidekick_usages.providers.codex.models import CodexAuthSnapshot


def managed_codex_account(
    account: SavedAccount,
    authority_id: AuthorityId,
    snapshot: CodexAuthSnapshot,
    *,
    plan: str,
    executable_version: str,
    verified_at: datetime,
    refreshed: bool,
) -> SavedAccount:
    """Build one healthy managed account from a proven private snapshot."""
    authority = CodexManagedAuthority(
        authority_id=authority_id,
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        verified_at=verified_at,
        executable_version=executable_version,
        health=CredentialHealth.HEALTHY,
    )
    return replace(
        account,
        plan=plan,
        authority=CodexAccountAuthority(subscription=authority),
        credential_health=CredentialHealth.HEALTHY,
        last_refresh_at=(
            verified_at if refreshed else account.last_refresh_at
        ),
        last_refresh_status=(
            RefreshStatus.OK if refreshed else account.last_refresh_status
        ),
        last_refresh_error_code=(
            None if refreshed else account.last_refresh_error_code
        ),
    )
