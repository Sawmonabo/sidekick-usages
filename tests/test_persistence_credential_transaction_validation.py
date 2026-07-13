"""Credential transaction validation and fail-closed tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.models import CodexCredentials
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
)
from sidekick_usages.persistence.credential_transactions import (
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from tests.test_persistence_credential_transactions import (
    _NEW_AUTH,
    _OLD_AUTH,
    _authority_payload,
    _codex_account,
    _crash_commit,
    _protected_filesystem,
    _seed_transaction_state,
    _SimulatedCrash,
)
from tests.test_support import make_application_paths


def test_canonical_credential_change_requires_a_prepared_bundle(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    _protected_filesystem(paths.accounts.canonical)
    private = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    bundle = paths.private_codex.canonical / "primary"
    original = _codex_account("primary", bundle, "old-token")
    store.persist_credentials(
        original,
        private_bundle=PreparedPrivateBundleWrite(
            bundle,
            {"auth.json": _OLD_AUTH},
            False,
            {"auth.json": None},
        ),
    )

    metadata_only = store.get("primary")
    assert metadata_only is not None
    metadata_only.plan = "team"
    store.persist(metadata_only)

    changed = store.get("primary")
    assert changed is not None
    credentials = changed.credentials
    assert isinstance(credentials, CodexCredentials)
    changed.credentials = replace(credentials, access_token="new-token")
    with pytest.raises(PrivateCredentialCollisionError):
        store.persist(changed)

    persisted = store.get("primary")
    assert persisted is not None
    assert persisted.access_token == "old-token"
    assert persisted.plan == "team"
    assert private.read_bundle_file(bundle, "auth.json") == _OLD_AUTH


def test_malformed_journal_and_third_authority_fail_closed(
    tmp_path: Path,
) -> None:
    filesystem, tree, bundle, base, target_payload = _seed_transaction_state(
        tmp_path
    )
    with PersistenceLock(filesystem).hold():
        tree.ensure_transaction_directory()
        tree.write_owned_file(
            tree.transaction_directory,
            PRIVATE_TRANSACTION_JOURNAL,
            b'{"journal_version":1,"files":[]}',
            expected_source=AuthorityExpectation.ABSENT,
        )
        with pytest.raises(InterruptedArtifactError):
            PrivateCredentialTransaction(
                tree,
                filesystem.read_authority,
            ).recover()
    assert tree.transaction_directory_present()

    tree.destroy_owned_directory(tree.transaction_directory)
    with pytest.raises(_SimulatedCrash):
        _crash_commit(
            filesystem,
            PrivateCredentialTransaction(
                tree,
                filesystem.read_authority,
            ),
            bundle,
            base,
            target_payload,
            after_authority=False,
        )
    third_payload = _authority_payload(
        _codex_account("primary", bundle, "test-only-third-token")
    )
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_TWO,
            third_payload,
            base.fingerprint,
        )
    with (
        PersistenceLock(filesystem).hold(),
        pytest.raises(SourceChangedError),
    ):
        PrivateCredentialTransaction(
            tree,
            filesystem.read_authority,
        ).recover()
    assert tree.transaction_directory_present()
    assert tree.read_bundle_file(bundle, "auth.json") == _NEW_AUTH
