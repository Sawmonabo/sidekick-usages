"""Load-bearing operation-scoped credential authority contract."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.credentials.authorities import (
    AuthenticatedAccountResolver,
    ClosedCredentialLeaseError,
    CredentialLease,
    MalformedCredentialAuthorityError,
    MismatchedCredentialAuthorityError,
    ProtectedCredentialAuthorityReader,
)
from sidekick_usages.persistence.account_runtime_bridge import (
    active_legacy_reference,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_authorities import (
    CredentialAuthorityRepository,
    LegacyCredentialAuthority,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.managed_migration import (
    ManagedAccountMigrationService,
)
from tests.test_support import (
    make_account_store_with_private,
)


class _MalformedRepository(CredentialAuthorityRepository):
    """Fail one qualified read at strict authority decoding."""

    def __init__(self) -> None:
        pass

    def read(
        self,
        account_id: SidekickAccountId,
        authority_id: AuthorityId,
    ) -> LegacyCredentialAuthority | None:
        del account_id, authority_id
        raise InvalidSchemaError


def test_credential_lease_is_bound_scoped_redacted_and_fail_closed(
    tmp_path: Path,
) -> None:
    secret = "test-only-lease-access-secret"
    legacy, private = make_account_store_with_private(
        tmp_path,
        (
            Account(
                label=AccountLabel("codex-team"),
                credentials=CodexCredentials(
                    access_token=secret,
                    refresh_token="test-only-lease-refresh-secret",
                    expiry=UnknownExpiry(),
                    account_id="acct-synthetic",
                ),
                plan="pro",
            ),
        ),
    )
    ManagedAccountMigrationService(legacy.path, private).migrate()
    store = AccountStore(
        legacy.locations,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    saved = store.saved_accounts()[0]
    repository = CredentialAuthorityRepository(private)
    authority_id = active_legacy_reference(saved)
    authority = repository.read(
        saved.account_id,
        authority_id,
    )
    assert authority is not None
    authority_payload = repository.read_payload(saved.account_id, authority_id)

    store.persist_state(replace(saved, plan="business"), expected=saved)
    saved = store.saved_accounts()[0]
    assert saved.plan == "business"
    assert (
        repository.read_payload(saved.account_id, authority_id)
        == authority_payload
    )
    assert secret not in store.path.read_text()

    resolver = AuthenticatedAccountResolver(
        ProtectedCredentialAuthorityReader(repository)
    )
    with resolver.open(saved) as authenticated:
        lease = authenticated.lease
        assert isinstance(lease, CredentialLease)
        assert lease.account.access_token == secret
        assert lease.account_id == saved.account_id
        assert lease.provider_id is saved.provider_id
        for rendered in (repr(saved), repr(lease), repr(authenticated)):
            assert secret not in rendered

    with pytest.raises(ClosedCredentialLeaseError) as closed:
        _ = lease.account
    assert secret not in repr(closed.value)

    mismatched = replace(
        saved,
        account_id=SidekickAccountId("a4e18d2e-8f88-4dc8-9516-d47e9de27c83"),
    )
    with pytest.raises(MismatchedCredentialAuthorityError) as mismatch:
        CredentialLease(mismatched, authority)
    assert secret not in repr(mismatch.value)

    malformed = AuthenticatedAccountResolver(
        ProtectedCredentialAuthorityReader(_MalformedRepository())
    )
    with (
        pytest.raises(MalformedCredentialAuthorityError) as invalid,
        malformed.open(saved),
    ):
        raise AssertionError("Malformed credentials were exposed.")
    assert secret not in repr(invalid.value)
