"""Transactional runtime account-store behavior tests."""

from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from types import TracebackType

import pytest

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeLegacyLoginAuthority,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
)
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence._platform import (
    FilesystemFamily,
    FilesystemQualification,
)
from sidekick_usages.persistence.account_store import (
    AccountStore,
    AccountStoreStateError,
)
from sidekick_usages.persistence.account_store_v3 import (
    AccountLabelAmbiguityError,
)
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    ArtifactPurpose,
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    PersistenceCode,
    PersistenceError,
    ReplaceFailedError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.managed_migration import (
    ManagedAccountMigrationService,
)
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.schema.account import (
    decode_version_three,
    encode_version_three,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    VersionTwoDocument,
    decode_version_two,
    encode_generation_zero,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
    version_two_to_accounts,
)
from tests.test_support import (
    REFERENCE_TIME,
    make_account_store_with_private,
    make_application_paths,
)


def _snapshot(payload: bytes, *, inode: int) -> FileSnapshot:
    return FileSnapshot(
        FileFingerprint(
            FileIdentity(1, inode),
            sha256_digest(payload),
            len(payload),
        ),
        1,
        payload,
    )


class InMemoryFilesystem(PersistenceFilesystem):
    """Qualified observable authority used by store boundary tests."""

    def __init__(self, path: Path, payload: bytes | None = None) -> None:
        self.authority_path = path
        self.grammar = ArtifactGrammar(path.name)
        self.snapshot = (
            _snapshot(payload, inode=1) if payload is not None else None
        )
        self.managed: dict[ManagedArtifact, FileSnapshot | None] = {}
        self.commit_failure: PersistenceError | None = None
        self.release_failure: Exception | None = None
        self._next_inode = 2

    def qualify(self) -> FilesystemQualification:
        return FilesystemQualification(
            FilesystemFamily.EXT4,
            self.authority_path,
        )

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        return tuple(self.managed)

    def read_authority(self) -> FileSnapshot | None:
        return self.snapshot

    def read_external_private_source(self) -> FileSnapshot | None:
        return self.snapshot

    def read_managed(
        self,
        artifact: ManagedArtifact,
        *,
        limit: int = 16 * 1024 * 1024,
    ) -> FileSnapshot | None:
        del limit
        return self.managed[artifact]

    def commit(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        assert generation is AuthorityGeneration.VERSION_TWO
        decode_version_two(payload)
        observed: ExpectedAuthority = (
            AuthorityExpectation.ABSENT
            if self.snapshot is None
            else self.snapshot.fingerprint
        )
        if observed != expected_source:
            raise SourceChangedError
        if self.commit_failure is not None:
            raise self.commit_failure
        self.snapshot = _snapshot(payload, inode=self._next_inode)
        self._next_inode += 1
        return self.snapshot


class InMemoryTransaction:
    """Held mutation capability backed by observable authority state."""

    def __init__(self, filesystem: InMemoryFilesystem) -> None:
        self._filesystem = filesystem

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        return self._filesystem.commit(generation, payload, expected_source)


class HeldInMemoryTransaction(AbstractContextManager[InMemoryTransaction]):
    """Context ownership for one in-memory mutation capability."""

    def __init__(
        self,
        transaction: InMemoryTransaction,
        release_failure: Exception | None,
    ) -> None:
        self._transaction = transaction
        self._release_failure = release_failure

    def __enter__(self) -> InMemoryTransaction:
        return self._transaction

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_value, traceback
        if exc_type is None and self._release_failure is not None:
            raise self._release_failure
        return False


class InMemoryLock:
    """Yield the test mutation capability without testing Portalocker."""

    def __init__(self, filesystem: PersistenceFilesystem) -> None:
        if not isinstance(filesystem, InMemoryFilesystem):
            raise TypeError("In-memory lock requires its filesystem.")
        self._filesystem = filesystem

    def hold(self) -> HeldInMemoryTransaction:
        return HeldInMemoryTransaction(
            InMemoryTransaction(self._filesystem),
            self._filesystem.release_failure,
        )


class InMemoryFilesystemFactory:
    """Return the one boundary bound to each injected absolute path."""

    def __init__(self, *filesystems: InMemoryFilesystem) -> None:
        self._filesystems = {
            filesystem.authority_path: filesystem for filesystem in filesystems
        }

    def __call__(self, path: Path) -> PersistenceFilesystem:
        return self._filesystems[path]


class OrphanedCredentialsObserver:
    """Mutable external evidence observed at each assessment point."""

    def __init__(self) -> None:
        self.state = OrphanedPrivateCredentials.ABSENT

    def __call__(self) -> OrphanedPrivateCredentials:
        return self.state


def _account(label: str, provider_id: ProviderId) -> Account:
    if provider_id is ProviderId.CLAUDE:
        credentials = ClaudeSetupTokenCredentials(
            access_token=f"test-only-{label}-access"
        )
    else:
        credentials = CodexCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            account_id=f"acct_{label}",
        )
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
        plan="max" if provider_id is ProviderId.CLAUDE else "plus",
        heartbeat_window_resets={"standard": REFERENCE_TIME},
    )


def _claude_login_account(label: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeLoginCredentials(
            access_token=f"test-only-{label}-access",
            refresh_token=f"test-only-{label}-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile", "user:inference"),
        ),
        plan="max",
        heartbeat_window_resets={"standard": REFERENCE_TIME},
    )


def _version_two(*accounts: Account) -> bytes:
    return encode_version_two(accounts_to_version_two(accounts))


def _store(
    root: Path,
    payload: bytes | None,
) -> tuple[AccountStore, InMemoryFilesystem, OrphanedCredentialsObserver]:
    paths = make_application_paths(root)
    authority = InMemoryFilesystem(paths.accounts.canonical, payload)
    prototype = InMemoryFilesystem(paths.accounts.prototype_cc_usage)
    observer = OrphanedCredentialsObserver()
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=observer,
        filesystem_factory=InMemoryFilesystemFactory(authority, prototype),
        lock_factory=InMemoryLock,
    )
    return store, authority, observer


@pytest.mark.parametrize(
    ("payload", "error_type", "code"),
    [
        (
            encode_generation_zero(GenerationZeroDocument(())),
            AccountStoreStateError,
            PersistenceCode.MIGRATION_REQUIRED,
        ),
        (
            b'{"schema_version":4,"accounts":{}}\n',
            FutureSchemaError,
            PersistenceCode.FUTURE_SCHEMA,
        ),
        (b"{", MalformedJsonError, PersistenceCode.MALFORMED_JSON),
        (
            b'{"schema_version":1,"accounts":[]}\n',
            InvalidSchemaError,
            PersistenceCode.INVALID_SCHEMA,
        ),
    ],
)
def test_load_rejects_every_noncurrent_authority_without_mutation(
    tmp_path: Path,
    payload: bytes,
    error_type: type[PersistenceError],
    code: PersistenceCode,
) -> None:
    """Load never migrates or converts unsupported authority to empty."""
    store, filesystem, _observer = _store(tmp_path, payload)

    with pytest.raises(error_type) as exc_info:
        store.load()

    assert exc_info.value.code is code
    assert filesystem.snapshot is not None
    assert filesystem.snapshot.data == payload
    if isinstance(exc_info.value, AccountStoreStateError):
        assert exc_info.value.next_command == (
            "sidekick-usages",
            "migrate",
            "accounts",
        )


def test_loaded_queries_never_expose_mutable_store_state(
    tmp_path: Path,
) -> None:
    """Every account-returning query owns its mutable result."""
    account = _account("claude-max-1", ProviderId.CLAUDE)
    store, _filesystem, _observer = _store(
        tmp_path,
        _version_two(account),
    )
    with pytest.raises(RuntimeError, match="must be loaded"):
        len(store)
    store.load()

    returned = (
        store.get("claude-max-1"),
        next(iter(store)),
        store.find_by_token(ProviderId.CLAUDE, account.access_token),
        store.filter_by_provider(ProviderId.CLAUDE)[0],
    )
    for candidate in returned:
        assert candidate is not None
        candidate.plan = "tampered"
        candidate.heartbeat_window_resets = {"other": REFERENCE_TIME}
        preserved = store.get("claude-max-1")
        assert preserved is not None
        assert preserved.plan == "max"
        assert tuple(preserved.heartbeat_window_resets or ()) == ("standard",)


def test_store_round_trips_complete_legacy_claude_login(
    tmp_path: Path,
) -> None:
    """The current store retains all v1 login and operational fields."""
    account = _claude_login_account("claude-login")
    store, filesystem, _observer = _store(
        tmp_path,
        _version_two(account),
    )

    store.load()

    assert store.get("claude-login") == account
    loaded = store.get("claude-login")
    assert loaded is not None
    loaded.plan = "team"
    store.persist(loaded)
    assert store.get("claude-login") == loaded
    assert filesystem.snapshot is not None
    assert version_two_to_accounts(
        decode_version_two(filesystem.snapshot.data)
    ) == (loaded,)


def test_failed_persist_preserves_memory_disk_and_baseline_for_retry(
    tmp_path: Path,
) -> None:
    """A failed candidate never leaks into memory or the valid authority."""
    store, filesystem, _observer = _store(tmp_path, None)
    store.load()
    original = _account("claude-max-1", ProviderId.CLAUDE)
    store.persist(original)
    original.plan = "caller-mutated"
    before = filesystem.snapshot
    assert before is not None

    updated = store.get("claude-max-1")
    assert updated is not None
    updated.plan = "team"
    filesystem.commit_failure = ReplaceFailedError()
    with pytest.raises(ReplaceFailedError):
        store.persist(updated)

    preserved = store.get("claude-max-1")
    assert preserved is not None
    assert preserved.plan == "max"
    assert filesystem.snapshot == before

    filesystem.commit_failure = None
    store.persist(updated)
    assert store.get("claude-max-1") == updated
    assert filesystem.snapshot is not None
    document = decode_version_two(filesystem.snapshot.data)
    assert encode_version_two(document) == filesystem.snapshot.data


def test_release_failure_after_commit_keeps_memory_at_durable_state(
    tmp_path: Path,
) -> None:
    store, filesystem, _observer = _store(tmp_path, None)
    store.load()
    account = _account("claude-max-1", ProviderId.CLAUDE)
    filesystem.release_failure = RuntimeError("release malfunction")

    with pytest.raises(RuntimeError, match="release malfunction"):
        store.persist(account)

    assert store.get("claude-max-1") == account
    assert filesystem.snapshot is not None
    assert decode_version_two(filesystem.snapshot.data).accounts[0].label == (
        account.label
    )

    filesystem.release_failure = None
    updated = store.get("claude-max-1")
    assert updated is not None
    updated.plan = "team"
    store.persist(updated)
    assert store.get("claude-max-1") == updated


def test_persist_reassesses_credentials_and_artifacts_under_lock(
    tmp_path: Path,
) -> None:
    """State that appears after load blocks writing until it is resolved."""
    store, filesystem, observer = _store(tmp_path, None)
    store.load()
    account = _account("codex-plus-1", ProviderId.CODEX)

    observer.state = OrphanedPrivateCredentials.PRESENT
    with pytest.raises(AccountStoreStateError) as orphaned_error:
        store.persist(account)
    assert orphaned_error.value.code is PersistenceCode.INTERRUPTED_ARTIFACTS
    assert filesystem.snapshot is None

    observer.state = OrphanedPrivateCredentials.ABSENT
    temporary_name = (
        f".{filesystem.authority_path.name}."
        f"{ArtifactPurpose.AUTHORITY}.{'0' * 32}.tmp"
    )
    temporary = filesystem.grammar.parse(temporary_name)
    assert temporary is not None
    filesystem.managed[temporary] = _snapshot(b"candidate", inode=99)
    with pytest.raises(AccountStoreStateError) as artifact_error:
        store.persist(account)
    assert artifact_error.value.code is PersistenceCode.INTERRUPTED_ARTIFACTS
    assert filesystem.snapshot is None

    filesystem.managed.clear()
    store.persist(account)
    assert store.get("codex-plus-1") == account

    external = _account("claude-external", ProviderId.CLAUDE)
    external_payload = _version_two(external)
    filesystem.snapshot = _snapshot(external_payload, inode=101)
    with pytest.raises(SourceChangedError):
        store.persist(_account("claude-new", ProviderId.CLAUDE))
    assert tuple(item.label for item in store) == ("codex-plus-1",)
    assert filesystem.snapshot.data == external_payload


def test_crud_mutations_commit_one_complete_ordered_candidate(
    tmp_path: Path,
) -> None:
    """Rename, remove, and provider reset leave memory and v1 identical."""
    store, filesystem, _observer = _store(tmp_path, None)
    store.load()
    for account in (
        _account("claude-one", ProviderId.CLAUDE),
        _account("codex-one", ProviderId.CODEX),
        _account("claude-two", ProviderId.CLAUDE),
    ):
        store.persist(account)

    before_collision = filesystem.snapshot
    assert store.rename("claude-one", "codex-one") is False
    assert filesystem.snapshot == before_collision
    assert store.rename("claude-one", "claude-team") is True
    assert store.remove("claude-two") is True
    assert store.remove("missing") is False
    assert store.reset_provider(ProviderId.CLAUDE) == 1
    assert tuple(account.label for account in store) == ("codex-one",)
    assert store.reset_provider(ProviderId.CODEX) == 1
    assert len(store) == 0

    assert filesystem.snapshot is not None
    document = decode_version_two(filesystem.snapshot.data)
    assert document == VersionTwoDocument(())


def test_managed_store_preserves_ids_and_qualifies_duplicate_labels(
    tmp_path: Path,
) -> None:
    """The public store keeps stable IDs and rejects ambiguous old syntax."""
    source = _account("shared-label", ProviderId.CLAUDE)
    _legacy, private = make_account_store_with_private(tmp_path, (source,))
    paths = make_application_paths(tmp_path)
    ManagedAccountMigrationService(
        paths.accounts.canonical,
        private,
    ).migrate()
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    saved = store.saved_accounts()[0]
    assert isinstance(saved.authority, ClaudeAccountAuthority)
    dual = replace(
        saved,
        authority=ClaudeAccountAuthority(
            setup_token=saved.authority.setup_token,
            subscription=ClaudeLegacyLoginAuthority(
                authority_id=AuthorityId(
                    "671bd641-87e7-450c-91c9-04863abf3462"
                ),
                provider_identity=ProviderIdentity(
                    "synthetic-claude-identity"
                ),
                access_expires_at=REFERENCE_TIME,
                refresh_expires_at=None,
                health=CredentialHealth.MIGRATION_REQUIRED,
            ),
        ),
    )
    document = VersionThreeDocument((dual,))
    encoded = encode_version_three(document)
    assert decode_version_three(encoded) == document
    assert "synthetic-claude-identity" not in repr(document)
    assert b"access_token" not in encoded
    assert b"refresh_token" not in encoded
    assert b"id_token" not in encoded
    with pytest.raises(ValueError, match="require"):
        ClaudeAccountAuthority()

    original_id = store.resolve_account_id(
        ProviderId.CLAUDE,
        source.label,
    )
    assert original_id is not None

    assert store.rename("shared-label", "renamed") is True
    assert (
        store.resolve_account_id(
            ProviderId.CLAUDE,
            AccountLabel("renamed"),
        )
        == original_id
    )
    codex = _account("renamed", ProviderId.CODEX)
    store.persist(codex)

    with pytest.raises(AccountLabelAmbiguityError):
        store.get("renamed")

    assert (
        store.get(
            "renamed",
            provider_id=ProviderId.CLAUDE,
        )
        is not None
    )
    assert (
        store.get(
            "renamed",
            provider_id=ProviderId.CODEX,
        )
        == codex
    )
    payload = paths.accounts.canonical.read_bytes()
    assert b"test-only-shared-label-access" not in payload
    assert b"test-only-renamed-access" not in payload
