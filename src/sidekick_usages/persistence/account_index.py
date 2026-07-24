"""In-memory stable-ID account index with provider-qualified labels."""

from collections.abc import Iterator

from sidekick_usages.core.accounts import (
    AuthorityGeneration,
    AuthorityId,
    ClaudeAccountAuthority,
    ClaudeLegacyLoginAuthority,
    ClaudeSetupTokenAuthority,
    CodexAccountAuthority,
    CodexLegacyAuthority,
    CredentialHealth,
    ProviderIdentity,
    SavedAccount,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.account_schema_v3 import (
    VersionThreeDocument,
)


class AccountLabelAmbiguityError(ValueError):
    """A label-only lookup matches more than one provider."""


def legacy_error_code(value: str | None) -> str | None:
    """Collapse legacy display text to one non-secret durable code."""
    return None if value is None else "legacy_failure"


def _provider_identity(account: Account) -> ProviderIdentity | None:
    """Return available stable provider identity without token material."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeLoginCredentials):
        identity = credentials.identity
        if identity is None:
            return None
        encoded = (
            f"{len(identity.account_id.encode('utf-8'))}:"
            f"{identity.account_id}{identity.organization_id}"
        )
        return ProviderIdentity(encoded)
    if isinstance(credentials, CodexCredentials):
        return (
            ProviderIdentity(credentials.account_id)
            if credentials.account_id is not None
            else None
        )
    return None


def _legacy_authority(
    account: Account,
    authority_id: AuthorityId,
) -> ClaudeAccountAuthority | CodexAccountAuthority:
    """Create secret-free authority metadata for one legacy account."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return ClaudeAccountAuthority(
            setup_token=ClaudeSetupTokenAuthority(
                authority_id=authority_id,
                expires_at=None,
                health=CredentialHealth.UNKNOWN,
                observed_at=account.last_refresh_at,
            )
        )
    if isinstance(credentials, ClaudeLoginCredentials):
        return ClaudeAccountAuthority(
            subscription=ClaudeLegacyLoginAuthority(
                authority_id=authority_id,
                provider_identity=_provider_identity(account),
                access_expires_at=credentials.access_expiry.at,
                refresh_expires_at=(
                    credentials.refresh_expiry.at
                    if isinstance(credentials.refresh_expiry, KnownExpiry)
                    else None
                ),
                health=CredentialHealth.MIGRATION_REQUIRED,
                observed_at=account.last_refresh_at,
            )
        )
    generation = (
        AuthorityGeneration(credentials.auth_last_refresh)
        if credentials.auth_last_refresh is not None
        else None
    )
    return CodexAccountAuthority(
        subscription=CodexLegacyAuthority(
            authority_id=authority_id,
            provider_identity=_provider_identity(account),
            expires_at=(
                credentials.expiry.at
                if isinstance(credentials.expiry, KnownExpiry)
                else None
            ),
            generation=generation,
            health=CredentialHealth.MIGRATION_REQUIRED,
            observed_at=account.last_refresh_at,
        )
    )


def legacy_saved_account(
    account: Account,
    *,
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
) -> SavedAccount:
    """Convert one legacy runtime account to secret-free index metadata."""
    return SavedAccount(
        account_id=account_id,
        label=account.label,
        provider_id=account.provider_id,
        plan=account.plan,
        authority=_legacy_authority(account, authority_id),
        credential_health=CredentialHealth.MIGRATION_REQUIRED,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error_code=legacy_error_code(account.last_refresh_error),
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
        heartbeat_window_resets=(
            tuple(account.heartbeat_window_resets.items())
            if account.heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error_code=legacy_error_code(
            account.last_heartbeat_error
        ),
    )


class AccountIndex:
    """Mutable stable-ID index used inside one qualified transaction."""

    def __init__(self, accounts: tuple[SavedAccount, ...] = ()) -> None:
        document = VersionThreeDocument(accounts)
        self._accounts = {
            account.account_id: account for account in document.accounts
        }
        self._labels = {
            (account.provider_id, account.label): account.account_id
            for account in document.accounts
        }

    def __iter__(self) -> Iterator[SavedAccount]:
        """Iterate immutable accounts in insertion order."""
        return iter(tuple(self._accounts.values()))

    def __len__(self) -> int:
        """Return the indexed account count."""
        return len(self._accounts)

    def get(self, account_id: SidekickAccountId) -> SavedAccount | None:
        """Return one immutable account by stable ID."""
        return self._accounts.get(account_id)

    def resolve(
        self,
        provider_id: ProviderId,
        label: AccountLabel,
    ) -> SavedAccount | None:
        """Resolve one exact provider-qualified account label."""
        account_id = self._labels.get((provider_id, label))
        return (
            self._accounts.get(account_id) if account_id is not None else None
        )

    def resolve_label(self, label: AccountLabel) -> SavedAccount | None:
        """Resolve an unqualified label or reject cross-provider ambiguity."""
        matches = tuple(
            account
            for account in self._accounts.values()
            if account.label == label
        )
        if len(matches) > 1:
            raise AccountLabelAmbiguityError(
                "Account label matches more than one provider."
            )
        return matches[0] if matches else None

    def add(self, account: SavedAccount) -> None:
        """Add one account while enforcing stable-ID and label uniqueness."""
        if account.account_id in self._accounts:
            raise ValueError("Sidekick account ID already exists.")
        label_key = (account.provider_id, account.label)
        if label_key in self._labels:
            raise ValueError("Provider account label already exists.")
        self._accounts[account.account_id] = account
        self._labels[label_key] = account.account_id

    def replace(self, account: SavedAccount) -> None:
        """Replace one stable account while preserving insertion order."""
        current = self._accounts.get(account.account_id)
        if current is None:
            raise ValueError("Sidekick account ID does not exist.")
        current_key = (current.provider_id, current.label)
        target_key = (account.provider_id, account.label)
        owner = self._labels.get(target_key)
        if owner is not None and owner != account.account_id:
            raise ValueError("Provider account label already exists.")
        del self._labels[current_key]
        self._labels[target_key] = account.account_id
        self._accounts[account.account_id] = account

    def add_legacy(
        self,
        account: Account,
        *,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> SavedAccount:
        """Convert and add one legacy runtime account without its secret."""
        saved = legacy_saved_account(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        self.add(saved)
        return saved

    def rename(
        self,
        provider_id: ProviderId,
        old_label: AccountLabel,
        new_label: AccountLabel,
    ) -> bool:
        """Rename one account without changing its stable identifier."""
        source_key = (provider_id, old_label)
        account_id = self._labels.get(source_key)
        if account_id is None:
            return False
        target_key = (provider_id, new_label)
        if target_key in self._labels:
            return False
        account = self._accounts[account_id].renamed(new_label)
        del self._labels[source_key]
        self._labels[target_key] = account_id
        self._accounts[account_id] = account
        return True

    def remove(self, account_id: SidekickAccountId) -> SavedAccount | None:
        """Remove one stable account from this transaction candidate."""
        account = self._accounts.pop(account_id, None)
        if account is not None:
            del self._labels[(account.provider_id, account.label)]
        return account

    def reset_provider(
        self,
        provider_id: ProviderId,
    ) -> tuple[SavedAccount, ...]:
        """Remove and return every account owned by one provider."""
        removed = tuple(
            account
            for account in self._accounts.values()
            if account.provider_id is provider_id
        )
        for account in removed:
            self.remove(account.account_id)
        return removed

    def document(self) -> VersionThreeDocument:
        """Return the current immutable document candidate."""
        return VersionThreeDocument(tuple(self._accounts.values()))


__all__ = [
    "AccountIndex",
    "AccountLabelAmbiguityError",
    "legacy_error_code",
    "legacy_saved_account",
]
