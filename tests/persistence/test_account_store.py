"""Foundational current account-store persistence tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import UnknownExpiry
from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountLifecyclePersistence,
    AccountRemovalPartialFailure,
    AccountRemovalSuccess,
)
from sidekick_usages.credentials.accounts.lifecycle.service import (
    AccountLifecycleCoordinator,
)
from sidekick_usages.paths import managed_codex_home
from sidekick_usages.persistence.accounts.removal.models import (
    AccountRemovalPhase,
)
from sidekick_usages.persistence.accounts.removal.store import (
    AccountRemovalStore,
)
from sidekick_usages.persistence.accounts.runtime_bridge import (
    active_stored_reference,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.errors import PrivateCredentialArtifactError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from tests.support.persistence import (
    make_account_store_with_private,
    make_application_paths,
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
    current = store.read_saved(before.account_id)
    assert current is not None
    store.rename_saved(
        current.account_id,
        AccountLabel("codex-work"),
        expected=current,
    )

    after = store.saved_accounts()[0]
    assert after.account_id == before.account_id
    assert active_stored_reference(after) == authority_id
    assert repository.read_payload(after.account_id, authority_id) == payload


def test_removing_account_removes_its_private_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit profile failure recovers through the durable journal."""
    paths = make_application_paths(tmp_path)
    store, private = make_account_store_with_private(tmp_path, (_account(),))
    saved = store.saved_accounts()[0]
    authority_path = CredentialAuthorityRepository(private).bundle_path(
        saved.account_id,
        active_stored_reference(saved),
    )
    removals = AccountRemovalStore(paths.durable_operations)
    removals.prepare(saved, profile_retired=True)
    changed = replace(saved, plan="business")
    codex_profiles = PrivateCredentialTree(
        paths.private_codex_profiles,
        account_path=paths.accounts,
    )
    claude_profiles = PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )
    profile = managed_codex_home(paths, saved.account_id)
    codex_profiles.ensure_owned_directory(profile)
    lifecycle = AccountLifecycleCoordinator(
        paths,
        AccountLifecyclePersistence(
            accounts=store,
            operations=OperationQueueStore(paths.durable_operations),
            activations=ActivationJournalStore(
                paths.activation_journals,
                paths.durable_operations,
            ),
            selected=SelectedStateStore(paths.selected_state),
            claude_profiles=claude_profiles,
            codex_profiles=codex_profiles,
        ),
    )
    destroy_profile = codex_profiles.destroy_owned_directory
    failed = False

    def fail_once(directory: Path) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise PrivateCredentialArtifactError
        destroy_profile(directory)

    assert authority_path.is_dir()

    store.persist_state(changed, expected=saved)
    monkeypatch.setattr(
        codex_profiles,
        "destroy_owned_directory",
        fail_once,
    )
    interrupted = lifecycle.remove(changed.account_id)
    pending = removals.get(changed.account_id)

    assert isinstance(interrupted, AccountRemovalPartialFailure)
    assert pending is not None
    assert pending.phase is AccountRemovalPhase.METADATA_REMOVED
    assert not authority_path.exists()
    assert profile.is_dir()

    recovered = lifecycle.remove(changed.account_id)

    assert isinstance(recovered, AccountRemovalSuccess)
    assert recovered.label is None
    assert store.saved_accounts() == ()
    assert removals.load() == ()
    assert not profile.exists()
