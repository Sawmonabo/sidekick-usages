"""Foundational current account-store persistence tests."""

from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.account_runtime_bridge import (
    active_stored_reference,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_repository import (
    CredentialAuthorityRepository,
)
from tests.test_support import (
    make_account_store_with_private,
)

ACCESS_TOKEN = "test-only-current-access-token"
REFRESH_TOKEN = "test-only-current-refresh-token"


def _account(label: str = "codex-primary") -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=ACCESS_TOKEN,
            refresh_token=REFRESH_TOKEN,
            expiry=UnknownExpiry(),
            account_id="acct-current",
        ),
        plan="pro",
    )


def test_store_separates_index_from_credentials_and_reopens(
    tmp_path: Path,
) -> None:
    store, private = make_account_store_with_private(tmp_path)
    account = _account()

    store.persist(account)

    index_payload = store.path.read_text()
    assert ACCESS_TOKEN not in index_payload
    assert REFRESH_TOKEN not in index_payload
    reopened = AccountStore(store.path, private).load()
    assert reopened.get("codex-primary") == account


def test_state_changes_preserve_stable_identity_and_authority(
    tmp_path: Path,
) -> None:
    store, private = make_account_store_with_private(tmp_path, (_account(),))
    repository = CredentialAuthorityRepository(private)
    before = store.saved_accounts()[0]
    authority_id = active_stored_reference(before)
    payload = repository.read_payload(before.account_id, authority_id)

    store.persist_state(replace(before, plan="business"), expected=before)
    assert store.rename("codex-primary", "codex-work")

    after = store.saved_accounts()[0]
    assert after.account_id == before.account_id
    assert active_stored_reference(after) == authority_id
    assert repository.read_payload(after.account_id, authority_id) == payload


def test_removing_account_removes_its_private_authority(
    tmp_path: Path,
) -> None:
    store, private = make_account_store_with_private(tmp_path, (_account(),))
    saved = store.saved_accounts()[0]
    authority_path = CredentialAuthorityRepository(private).bundle_path(
        saved.account_id,
        active_stored_reference(saved),
    )
    assert authority_path.is_dir()

    assert store.remove("codex-primary")

    assert store.saved_accounts() == ()
    assert not authority_path.exists()
