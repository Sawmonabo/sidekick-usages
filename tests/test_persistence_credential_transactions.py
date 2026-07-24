"""Foundational private-credential transaction tests."""

from pathlib import Path
from typing import Never

import pytest

from sidekick_usages.persistence.credentials.transactions.transaction import (
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
)
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from tests.test_support import make_application_paths

AUTHORITY_PAYLOAD = encode_version_three(VersionThreeDocument(()))
PRIVATE_PAYLOAD = b"test-only-private-auth"


class _SimulatedCrash(BaseException):
    """Represent process loss beyond ordinary exception recovery."""


class _CrashBeforeAuthority:
    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> Never:
        del payload, expected_source
        raise _SimulatedCrash


class _CrashAfterAuthority:
    def __init__(self, transaction: PersistenceTransaction) -> None:
        self._transaction = transaction

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> Never:
        self._transaction.commit_authority(payload, expected_source)
        raise _SimulatedCrash


def _boundaries(
    root: Path,
) -> tuple[PersistenceFilesystem, PrivateCredentialTree]:
    paths = make_application_paths(root)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return filesystem, private


def _bundle(private: PrivateCredentialTree) -> PreparedPrivateBundleWrite:
    return PreparedPrivateBundleWrite(
        private.root / "account-authority",
        {"auth.json": PRIVATE_PAYLOAD},
        False,
        {"auth.json": None},
    )


def test_transaction_publishes_private_bytes_before_current_authority(
    tmp_path: Path,
) -> None:
    filesystem, private = _boundaries(tmp_path)
    coordinator = PrivateCredentialTransaction(
        private,
        filesystem.read_authority,
    )
    with PersistenceLock(filesystem).hold() as transaction:
        final = coordinator.commit(
            transaction,
            AUTHORITY_PAYLOAD,
            AuthorityExpectation.ABSENT,
            private_bundles=(_bundle(private),),
            displaced_bundles=(),
        )

    stored = private.read_owned_file(
        private.root / "account-authority",
        "auth.json",
    )
    assert final.data == AUTHORITY_PAYLOAD
    assert stored is not None
    assert stored.data == PRIVATE_PAYLOAD
    assert not private.transaction_directory.exists()


def test_recovery_resolves_both_sides_of_authority_commit(
    tmp_path: Path,
) -> None:
    before_filesystem, before_private = _boundaries(tmp_path / "before")
    before = PrivateCredentialTransaction(
        before_private,
        before_filesystem.read_authority,
    )
    with (
        pytest.raises(_SimulatedCrash),
        PersistenceLock(before_filesystem).hold(),
    ):
        before.commit(
            _CrashBeforeAuthority(),
            AUTHORITY_PAYLOAD,
            AuthorityExpectation.ABSENT,
            private_bundles=(_bundle(before_private),),
            displaced_bundles=(),
        )
    with PersistenceLock(before_filesystem).hold():
        assert before.recover()
    assert before_filesystem.read_authority() is None
    assert not (before_private.root / "account-authority").exists()

    after_filesystem, after_private = _boundaries(tmp_path / "after")
    after = PrivateCredentialTransaction(
        after_private,
        after_filesystem.read_authority,
    )
    with (
        pytest.raises(_SimulatedCrash),
        PersistenceLock(after_filesystem).hold() as transaction,
    ):
        after.commit(
            _CrashAfterAuthority(transaction),
            AUTHORITY_PAYLOAD,
            AuthorityExpectation.ABSENT,
            private_bundles=(_bundle(after_private),),
            displaced_bundles=(),
        )
    with PersistenceLock(after_filesystem).hold():
        assert after.recover()
    authority = after_filesystem.read_authority()
    stored = after_private.read_owned_file(
        after_private.root / "account-authority",
        "auth.json",
    )
    assert authority is not None
    assert authority.data == AUTHORITY_PAYLOAD
    assert stored is not None
    assert stored.data == PRIVATE_PAYLOAD
    assert not after_private.transaction_directory.exists()
