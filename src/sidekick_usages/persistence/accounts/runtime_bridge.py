"""Runtime projection for persisted credential authorities."""

from dataclasses import replace

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeStoredLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import AuthorityId
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    Credentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.accounts.index import safe_error_code
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    PersistenceError,
)
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.credential import StoredCredentialKind
from sidekick_usages.persistence.types.error import PersistenceCode


class CredentialAuthorityUnavailableError(PersistenceError):
    """A provider-managed authority needs its provider-specific resolver."""

    def __init__(self) -> None:
        self.code = PersistenceCode.AUTHORITY_UNAVAILABLE
        super().__init__(
            "Account credentials require their provider authority resolver."
        )


def copy_runtime_account(
    account: Account,
    *,
    label: AccountLabel | None = None,
) -> Account:
    """Return one independently mutable runtime account."""
    resets = account.heartbeat_window_resets
    return Account(
        label=account.label if label is None else label,
        credentials=account.credentials,
        plan=account.plan,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error=account.last_refresh_error,
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error=account.last_heartbeat_error,
    )


def active_stored_reference(account: SavedAccount) -> AuthorityId:
    """Return the protected authority used by runtime services."""
    authority = account.authority
    if isinstance(authority, ClaudeAccountAuthority):
        if isinstance(authority.subscription, ClaudeStoredLoginAuthority):
            return authority.subscription.authority_id
        if isinstance(authority.subscription, ClaudeManagedLoginAuthority):
            raise CredentialAuthorityUnavailableError
        if authority.setup_token is not None:
            return authority.setup_token.authority_id
        raise InvalidSchemaError
    if isinstance(authority.subscription, CodexManagedAuthority):
        raise CredentialAuthorityUnavailableError
    return authority.subscription.authority_id


def runtime_account_from_saved(
    saved: SavedAccount,
    credentials: Credentials,
) -> Account:
    """Combine secret-free metadata with qualified stored credentials."""
    resets = saved.heartbeat_window_resets
    return Account(
        label=saved.label,
        credentials=credentials,
        plan=saved.plan,
        last_refresh_at=saved.last_refresh_at,
        last_refresh_status=saved.last_refresh_status,
        last_refresh_error=saved.last_refresh_error_code,
        heartbeat_enabled=saved.heartbeat_enabled,
        heartbeat_window_resets=(dict(resets) if resets is not None else None),
        heartbeat_targets=saved.heartbeat_targets,
        last_heartbeat_at=saved.last_heartbeat_at,
        last_heartbeat_status=saved.last_heartbeat_status,
        last_heartbeat_error=saved.last_heartbeat_error_code,
    )


def saved_account_from_runtime_state(
    saved: SavedAccount,
    account: Account,
) -> SavedAccount:
    """Copy only non-secret mutable runtime state into the saved index."""
    if (
        saved.label != account.label
        or saved.provider_id is not account.provider_id
    ):
        raise ValueError("Runtime account identity does not match.")
    return replace(
        saved,
        plan=account.plan,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error_code=safe_error_code(account.last_refresh_error),
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_window_resets=(
            tuple(account.heartbeat_window_resets.items())
            if account.heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error_code=safe_error_code(
            account.last_heartbeat_error
        ),
    )


def credential_authority_reference(
    saved: SavedAccount,
    credentials: Credentials,
) -> AuthorityId | None:
    """Return the protected reference matching one credential variant."""
    authority = saved.authority
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        if not isinstance(authority, ClaudeAccountAuthority):
            raise ValueError("Account provider cannot change.")
        return (
            authority.setup_token.authority_id
            if authority.setup_token is not None
            else None
        )
    if isinstance(credentials, ClaudeLoginCredentials):
        if not isinstance(authority, ClaudeAccountAuthority):
            raise ValueError("Account provider cannot change.")
        subscription = authority.subscription
        if isinstance(subscription, ClaudeManagedLoginAuthority):
            raise CredentialAuthorityUnavailableError
        return (
            subscription.authority_id
            if isinstance(subscription, ClaudeStoredLoginAuthority)
            else None
        )
    if not isinstance(authority, CodexAccountAuthority):
        raise ValueError("Account provider cannot change.")
    if isinstance(authority.subscription, CodexManagedAuthority):
        raise CredentialAuthorityUnavailableError
    return authority.subscription.authority_id


def merge_claude_authority(
    previous: SavedAccount | None,
    candidate: SavedAccount,
) -> SavedAccount:
    """Preserve the other Claude authority on dual-authority updates."""
    if previous is None:
        return candidate
    old = previous.authority
    new = candidate.authority
    if not isinstance(
        old,
        ClaudeAccountAuthority,
    ) or not isinstance(new, ClaudeAccountAuthority):
        return candidate
    return replace(
        candidate,
        authority=ClaudeAccountAuthority(
            setup_token=(
                new.setup_token
                if new.setup_token is not None
                else old.setup_token
            ),
            subscription=(
                new.subscription
                if new.subscription is not None
                else old.subscription
            ),
        ),
    )


def require_active_authority_kind(
    saved: SavedAccount,
    authority: StoredCredentialAuthority,
) -> None:
    """Require the active protected payload to match index metadata."""
    expected: StoredCredentialKind
    if isinstance(saved.authority, CodexAccountAuthority):
        expected = StoredCredentialKind.CODEX_LOGIN
    elif isinstance(
        saved.authority.subscription,
        ClaudeStoredLoginAuthority,
    ):
        expected = StoredCredentialKind.CLAUDE_LOGIN
    else:
        expected = StoredCredentialKind.CLAUDE_SETUP
    if authority.kind is not expected:
        raise InvalidSchemaError


def authority_baseline_matches(
    baseline: ExpectedAuthority,
    observed: FileSnapshot | None,
) -> bool:
    """Return whether fresh authority evidence matches the loaded baseline."""
    if baseline is AuthorityExpectation.ABSENT:
        return observed is None
    return observed is not None and observed.fingerprint == baseline
