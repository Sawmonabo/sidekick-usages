"""Qualified storage for protected credential authorities."""

from pathlib import Path

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeStoredLoginAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import Account
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
    stored_credential_kind,
)
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.authority import (
    AUTHORITY_BASENAME,
    decode_credential_authority,
    encode_credential_authority,
)


def authority_for_account(
    account: Account,
    *,
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
) -> StoredCredentialAuthority:
    """Bind one account's protected credentials to stable IDs."""
    return StoredCredentialAuthority(
        authority_id=authority_id,
        account_id=account_id,
        provider_id=account.provider_id,
        kind=stored_credential_kind(account.credentials),
        credentials=account.credentials,
    )


def authority_bundle_name(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
) -> str:
    """Return the qualified direct bundle name for one authority."""
    return f"{account_id}--{authority_id}"


def referenced_stored_authorities(
    account: SavedAccount,
) -> tuple[AuthorityId, ...]:
    """Return every protected authority owned by one account."""
    authority = account.authority
    references: list[AuthorityId] = []
    if isinstance(authority, ClaudeAccountAuthority):
        if authority.setup_token is not None:
            references.append(authority.setup_token.authority_id)
        if isinstance(authority.subscription, ClaudeStoredLoginAuthority):
            references.append(authority.subscription.authority_id)
    elif isinstance(authority.subscription, CodexStoredAuthority):
        references.append(authority.subscription.authority_id)
    return tuple(references)


class CredentialAuthorityRepository:
    """Qualified protected authority storage."""

    def __init__(self, tree: PrivateCredentialTree) -> None:
        self.tree = tree

    def bundle_path(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> Path:
        """Return one direct protected bundle derived from stable IDs."""
        return self.tree.root / authority_bundle_name(
            account_id,
            authority_id,
        )

    def prepare_write(
        self,
        authority: StoredCredentialAuthority,
        *,
        expected_payload: bytes | None = None,
    ) -> PreparedPrivateBundleWrite:
        """Prepare one coordinated protected authority write."""
        return PreparedPrivateBundleWrite(
            path=self.bundle_path(
                authority.account_id,
                authority.authority_id,
            ),
            files={AUTHORITY_BASENAME: encode_credential_authority(authority)},
            expected_bundle_present=expected_payload is not None,
            expected_files=(
                {AUTHORITY_BASENAME: expected_payload}
                if expected_payload is not None
                else {}
            ),
        )

    def read_payload(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> bytes | None:
        """Read exact protected bytes for one qualified authority."""
        bundle = self.bundle_path(account_id, authority_id)
        return self.tree.read_bundle_file(bundle, AUTHORITY_BASENAME)

    def read(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> StoredCredentialAuthority | None:
        """Read and rebind one exact protected authority."""
        payload = self.read_payload(account_id, authority_id)
        if payload is None:
            return None
        authority = decode_credential_authority(payload)
        if (
            authority.account_id != account_id
            or authority.authority_id != authority_id
        ):
            raise InvalidSchemaError
        return authority
