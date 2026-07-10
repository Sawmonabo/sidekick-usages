"""Load-bearing private-bundle and authority transaction tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.assessment import assess_persistence
from sidekick_usages.persistence.credential_transaction_schema import (
    CredentialTransactionJournal,
    decode_credential_journal,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    CandidateWriteError,
    InterruptedArtifactError,
    PersistenceCode,
    PrivateCredentialCollisionError,
    ReplaceFailedError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import (
    OrphanedPrivateCredentials,
    PersistenceInventory,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    encode_version_one,
)
from sidekick_usages.persistence.transaction import PersistenceTransaction
from sidekick_usages.persistence.transforms import accounts_to_version_one
from tests.test_support import make_application_paths

_OLD_AUTH = b"test-only-old-private-auth"
_NEW_AUTH = b"test-only-new-private-auth"
_CONFIG = b'test-only-mode = "file"\n'
_CODEX_REMOVED = 2
_SECOND_SOURCE_READ = 2
_TARGET_SWAP_CHECKPOINT = 4


class _SimulatedCrash(BaseException):
    """Process-loss signal intentionally not caught by transaction code."""


class _CrashBeforeAuthority:
    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        del generation, payload, expected_source
        raise _SimulatedCrash


class _CrashAfterAuthority:
    def __init__(self, transaction: PersistenceTransaction) -> None:
        self._transaction = transaction

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        self._transaction.commit_authority(
            generation,
            payload,
            expected_source,
        )
        raise _SimulatedCrash


class _AuthorityFailure:
    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        del generation, payload, expected_source
        raise ReplaceFailedError


class _ChangeGuardAfterAuthority:
    """Change the retained source after the target commit point."""

    def __init__(
        self,
        transaction: PersistenceTransaction,
        source: PersistenceFilesystem,
        source_snapshot: FileSnapshot,
        changed_source: bytes,
    ) -> None:
        self._transaction = transaction
        self._source = source
        self._source_snapshot = source_snapshot
        self._changed_source = changed_source

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        final = self._transaction.commit_authority(
            generation,
            payload,
            expected_source,
        )
        with PersistenceLock(self._source).hold() as transaction:
            transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                self._changed_source,
                self._source_snapshot.fingerprint,
            )
        return final


class _OneShotPrivateFailureTree(PrivateCredentialTree):
    """Fail after the first target file changed, then permit recovery."""

    def __init__(
        self,
        root: Path,
        *,
        account_path: Path,
        failed_bundle: Path,
    ) -> None:
        super().__init__(root, account_path=account_path)
        self._failed_bundle = failed_bundle
        self._failed = False

    def write_owned_file(
        self,
        directory: Path,
        basename: str,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        if (
            directory == self._failed_bundle
            and basename == "config.toml"
            and not self._failed
        ):
            self._failed = True
            raise CandidateWriteError(basename)
        return super().write_owned_file(
            directory,
            basename,
            payload,
            expected_source=expected_source,
        )


def _codex_account(label: str, auth_home: Path, token: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=token,
            refresh_token=f"{token}-refresh",
            account_id=f"{label}-id",
            auth_home=str(auth_home),
            id_token=f"{token}-id",
        ),
        plan="pro",
    )


def _claude_account(label: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeCredentials(access_token=f"{label}-token"),
    )


def _authority_payload(*accounts: Account) -> bytes:
    return encode_version_one(accounts_to_version_one(accounts))


def _protected_filesystem(path: Path) -> PersistenceFilesystem:
    """Build a native persistence boundary below a protected test parent."""
    filesystem = PersistenceFilesystem(path)
    filesystem.repair_parent_permissions()
    return filesystem


def _seed_transaction_state(
    tmp_path: Path,
    *,
    tree_type: type[PrivateCredentialTree] = PrivateCredentialTree,
) -> tuple[
    PersistenceFilesystem,
    PrivateCredentialTree,
    Path,
    FileSnapshot,
    bytes,
]:
    paths = make_application_paths(tmp_path)
    filesystem = _protected_filesystem(paths.accounts.canonical)
    bundle = paths.private_codex.canonical / "codex-primary"
    if tree_type is _OneShotPrivateFailureTree:
        tree = _OneShotPrivateFailureTree(
            paths.private_codex.canonical,
            account_path=paths.accounts.canonical,
            failed_bundle=bundle,
        )
    else:
        tree = PrivateCredentialTree(
            paths.private_codex.canonical,
            account_path=paths.accounts.canonical,
        )
    tree.write_bundle(
        bundle,
        {"auth.json": _OLD_AUTH, "config.toml": _CONFIG},
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )
    base_payload = _authority_payload(
        _codex_account("primary", bundle, "test-only-old-token")
    )
    with PersistenceLock(filesystem).hold() as transaction:
        base = transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            base_payload,
            AuthorityExpectation.ABSENT,
        )
    target_payload = _authority_payload(
        _codex_account("primary", bundle, "test-only-new-token")
    )
    return filesystem, tree, bundle, base, target_payload


def _mutation(bundle: Path) -> PreparedPrivateBundleWrite:
    return PreparedPrivateBundleWrite(
        path=bundle,
        files={"auth.json": _NEW_AUTH, "config.toml": _CONFIG},
        expected_bundle_present=True,
        expected_files={"auth.json": _OLD_AUTH},
    )


def _crash_commit(
    filesystem: PersistenceFilesystem,
    coordinator: PrivateCredentialTransaction,
    bundle: Path,
    base: FileSnapshot,
    target_payload: bytes,
    *,
    after_authority: bool,
) -> None:
    with PersistenceLock(filesystem).hold() as transaction:
        authority = (
            _CrashAfterAuthority(transaction)
            if after_authority
            else _CrashBeforeAuthority()
        )
        coordinator.commit(
            authority,
            target_payload,
            base.fingerprint,
            private_bundles=(_mutation(bundle),),
            displaced_bundles=(),
        )


def _failed_commit(
    filesystem: PersistenceFilesystem,
    coordinator: PrivateCredentialTransaction,
    bundle: Path,
    base: FileSnapshot,
    target_payload: bytes,
    *,
    private_failure: bool,
) -> None:
    with PersistenceLock(filesystem).hold() as transaction:
        authority = transaction if private_failure else _AuthorityFailure()
        coordinator.commit(
            authority,
            target_payload,
            base.fingerprint,
            private_bundles=(_mutation(bundle),),
            displaced_bundles=(),
        )


def _crash_multi_bundle_first_write(
    filesystem: PersistenceFilesystem,
    coordinator: PrivateCredentialTransaction,
    target_payload: bytes,
    bundles: tuple[PreparedPrivateBundleWrite, ...],
) -> None:
    with PersistenceLock(filesystem).hold():
        coordinator.commit(
            _CrashBeforeAuthority(),
            target_payload,
            AuthorityExpectation.ABSENT,
            private_bundles=bundles,
            displaced_bundles=(),
        )


def _commit_with_guard_change(
    target: PersistenceFilesystem,
    source: PersistenceFilesystem,
    coordinator: PrivateCredentialTransaction,
    target_payload: bytes,
    bundle: PreparedPrivateBundleWrite,
    guard: CredentialSourceGuard,
    source_snapshot: FileSnapshot,
    changed_source: bytes,
) -> None:
    with PersistenceLock(target).hold() as transaction:
        coordinator.commit(
            _ChangeGuardAfterAuthority(
                transaction,
                source,
                source_snapshot,
                changed_source,
            ),
            target_payload,
            AuthorityExpectation.ABSENT,
            private_bundles=(bundle,),
            displaced_bundles=(),
            source_guard=guard,
        )


@pytest.mark.parametrize("checkpoint", ["before_authority", "after_authority"])
def test_fresh_recovery_converges_to_one_old_or_new_state(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    filesystem, tree, bundle, base, target_payload = _seed_transaction_state(
        tmp_path
    )
    coordinator = PrivateCredentialTransaction(
        tree,
        filesystem.read_authority,
    )
    with pytest.raises(_SimulatedCrash):
        _crash_commit(
            filesystem,
            coordinator,
            bundle,
            base,
            target_payload,
            after_authority=checkpoint == "after_authority",
        )

    fresh_tree = PrivateCredentialTree(
        tree.root,
        account_path=filesystem.authority_path,
    )
    assert fresh_tree.observe() is OrphanedPrivateCredentials.INTERRUPTED
    inventory = PersistenceInventory(
        filesystem.authority_path,
        tmp_path / "prototype" / "accounts.json",
    )
    assert (
        assess_persistence(inventory.inspect(fresh_tree.observe())).code
        is PersistenceCode.INTERRUPTED_ARTIFACTS
    )
    journal = fresh_tree.read_owned_file(
        fresh_tree.transaction_directory,
        PRIVATE_TRANSACTION_JOURNAL,
    )
    assert journal is not None
    assert _OLD_AUTH not in journal.data
    assert _NEW_AUTH not in journal.data

    with PersistenceLock(filesystem).hold():
        assert PrivateCredentialTransaction(
            fresh_tree,
            filesystem.read_authority,
        ).recover()

    expected_payload = (
        base.data if checkpoint == "before_authority" else target_payload
    )
    expected_auth = (
        _OLD_AUTH if checkpoint == "before_authority" else _NEW_AUTH
    )
    authority = filesystem.read_authority()
    assert authority is not None
    assert authority.data == expected_payload
    assert fresh_tree.read_bundle_file(bundle, "auth.json") == expected_auth
    assert not fresh_tree.transaction_directory_present()


@pytest.mark.parametrize("failure_point", ["private", "authority"])
def test_failed_private_or_authority_commit_never_claims_success(
    tmp_path: Path,
    failure_point: str,
) -> None:
    tree_type = (
        _OneShotPrivateFailureTree
        if failure_point == "private"
        else PrivateCredentialTree
    )
    filesystem, tree, bundle, base, target_payload = _seed_transaction_state(
        tmp_path,
        tree_type=tree_type,
    )
    coordinator = PrivateCredentialTransaction(
        tree,
        filesystem.read_authority,
    )
    expected_error = (
        CandidateWriteError
        if failure_point == "private"
        else ReplaceFailedError
    )
    with pytest.raises(expected_error):
        _failed_commit(
            filesystem,
            coordinator,
            bundle,
            base,
            target_payload,
            private_failure=failure_point == "private",
        )

    authority = filesystem.read_authority()
    assert authority is not None
    assert authority.data == base.data
    assert tree.read_bundle_file(bundle, "auth.json") == _OLD_AUTH
    assert not tree.transaction_directory_present()


def test_multi_bundle_journal_is_deterministic_and_recovers_as_one_unit(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    filesystem = _protected_filesystem(paths.accounts.canonical)
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    first = paths.private_codex.canonical / "a-first"
    second = paths.private_codex.canonical / "b-second"
    target_payload = _authority_payload(
        _codex_account("first", first, "first-token"),
        _codex_account("second", second, "second-token"),
    )
    bundles = (
        PreparedPrivateBundleWrite(
            second,
            {"auth.json": b"second-private-auth"},
            False,
            {"auth.json": None},
        ),
        PreparedPrivateBundleWrite(
            first,
            {"auth.json": b"first-private-auth"},
            False,
            {"auth.json": None},
        ),
    )
    coordinator = PrivateCredentialTransaction(
        tree,
        filesystem.read_authority,
    )
    with pytest.raises(_SimulatedCrash):
        _crash_multi_bundle_first_write(
            filesystem,
            coordinator,
            target_payload,
            bundles,
        )
    snapshot = tree.read_owned_file(
        tree.transaction_directory,
        PRIVATE_TRANSACTION_JOURNAL,
    )
    assert snapshot is not None
    journal = decode_credential_journal(snapshot.data)
    assert isinstance(journal, CredentialTransactionJournal)
    assert journal.target_bundles == ("a-first", "b-second")
    assert tuple(item.bundle_basename for item in journal.files) == (
        "a-first",
        "b-second",
    )

    with PersistenceLock(filesystem).hold():
        assert coordinator.recover()
    assert filesystem.read_authority() is None
    assert not first.exists()
    assert not second.exists()
    assert not tree.transaction_directory_present()


def test_windows_namespace_aliases_are_rejected_before_journaling(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    filesystem = _protected_filesystem(paths.accounts.canonical)
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    with pytest.raises(ValueError, match="portable namespace"):
        PreparedPrivateBundleWrite(
            paths.private_codex.canonical / "files",
            {"auth.json": b"first", "AUTH.JSON": b"second"},
            False,
        )
    with pytest.raises(ValueError, match="safe basename"):
        PreparedPrivateBundleWrite(
            paths.private_codex.canonical / "trailing.",
            {"auth.json": b"private-auth"},
            False,
        )

    bundles = tuple(
        PreparedPrivateBundleWrite(
            paths.private_codex.canonical / name,
            {"auth.json": name.encode()},
            False,
            {"auth.json": None},
        )
        for name in ("Bundle", "bundle")
    )
    target_payload = _authority_payload(
        _codex_account("first", bundles[0].path, "first-token"),
        _codex_account("second", bundles[1].path, "second-token"),
    )
    with (
        PersistenceLock(filesystem).hold(),
        pytest.raises(ValueError, match="portable namespace"),
    ):
        PrivateCredentialTransaction(
            tree,
            filesystem.read_authority,
        ).commit(
            _CrashBeforeAuthority(),
            target_payload,
            AuthorityExpectation.ABSENT,
            private_bundles=bundles,
            displaced_bundles=(),
        )
    assert not tree.transaction_directory_present()


def test_store_load_recovers_crashed_first_persist_to_empty(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    filesystem = _protected_filesystem(paths.accounts.canonical)
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    bundle = paths.private_codex.canonical / "first"
    target_payload = _authority_payload(
        _codex_account("first", bundle, "first-token")
    )
    mutation = PreparedPrivateBundleWrite(
        bundle,
        {"auth.json": b"first-private-auth"},
        False,
        {"auth.json": None},
    )
    with pytest.raises(_SimulatedCrash):
        _crash_multi_bundle_first_write(
            filesystem,
            PrivateCredentialTransaction(
                tree,
                filesystem.read_authority,
            ),
            target_payload,
            (mutation,),
        )

    fresh_tree = PrivateCredentialTree(
        tree.root,
        account_path=filesystem.authority_path,
    )
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=fresh_tree.observe,
        private_credentials=fresh_tree,
    ).load()

    assert len(store) == 0
    assert filesystem.read_authority() is None
    assert not bundle.exists()
    assert not fresh_tree.transaction_directory_present()


def test_distinct_source_guard_change_after_target_commit_fails_closed(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path / "target")
    target = _protected_filesystem(paths.accounts.canonical)
    source_path = tmp_path / "compatibility" / "accounts.json"
    source = _protected_filesystem(source_path)
    source_payload = _authority_payload(_claude_account("source"))
    with PersistenceLock(source).hold() as transaction:
        source_snapshot = transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            source_payload,
            AuthorityExpectation.ABSENT,
        )
    tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
    )
    bundle_path = paths.private_codex.canonical / "relocated"
    bundle = PreparedPrivateBundleWrite(
        bundle_path,
        {"auth.json": b"relocated-private-auth"},
        False,
        {"auth.json": None},
    )
    target_payload = _authority_payload(
        _codex_account("relocated", bundle_path, "relocated-token")
    )
    guard = CredentialSourceGuard(
        source_path,
        source_snapshot.fingerprint,
        source.read_authority,
    )
    coordinator = PrivateCredentialTransaction(tree, target.read_authority)
    changed_source = _authority_payload(_claude_account("changed"))
    with pytest.raises(SourceChangedError):
        _commit_with_guard_change(
            target,
            source,
            coordinator,
            target_payload,
            bundle,
            guard,
            source_snapshot,
            changed_source,
        )
    target_snapshot = target.read_authority()
    assert target_snapshot is not None
    assert target_snapshot.data == target_payload
    assert tree.transaction_directory_present()


def test_account_removal_cleans_only_unreferenced_canonical_bundles(
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
    first = paths.private_codex.canonical / "first"
    second = paths.private_codex.canonical / "second"
    external = tmp_path / "external-codex"
    external.mkdir()
    external_auth = external / "auth.json"
    external_auth.write_bytes(b"external-private-auth")
    store.persist_credentials(
        _codex_account("first", first, "first-token"),
        private_bundle=PreparedPrivateBundleWrite(
            first,
            {"auth.json": b"first-private-auth"},
            False,
            {"auth.json": None},
        ),
    )
    store.persist_credentials(
        _codex_account("second", second, "second-token"),
        private_bundle=PreparedPrivateBundleWrite(
            second,
            {"auth.json": b"second-private-auth"},
            False,
            {"auth.json": None},
        ),
    )
    store.persist(_codex_account("external", external, "external-token"))
    store.persist(_claude_account("claude"))
    unrelated = paths.private_codex.canonical / "unrelated"
    private.write_bundle(
        unrelated,
        {"auth.json": b"unrelated-private-auth"},
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )
    with pytest.raises(PrivateCredentialCollisionError):
        store.persist(_codex_account("unproven", unrelated, "unproven-token"))
    assert store.get("unproven") is None

    assert store.remove_credentials("first")
    assert not first.exists()
    assert second.exists()
    assert unrelated.exists()
    assert store.reset_provider_credentials(ProviderId.CODEX) == _CODEX_REMOVED
    assert not second.exists()
    assert unrelated.exists()
    assert external_auth.read_bytes() == b"external-private-auth"
    assert store.get("claude") is not None


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
            AuthorityGeneration.VERSION_ONE,
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
