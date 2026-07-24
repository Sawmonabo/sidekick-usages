"""Account persistence generation migration tests."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidekick_usages.core.accounts import (
    AuthorityGeneration as ManagedAuthorityGeneration,
)
from sidekick_usages.core.accounts import (
    AuthorityId,
    ClaudeAccountAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_schema_v3 import (
    VersionThreeDocument,
    decode_version_three,
    encode_version_three,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.credential_authorities import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    PersistenceCode,
    ReplaceFailedError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.managed_migration import (
    ManagedAccountMigrationService,
)
from sidekick_usages.persistence.managed_rollback import (
    require_v060_compatible,
)
from sidekick_usages.persistence.migrations.service import (
    PersistenceMigrationService,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    StoredAccountRecord,
    VersionOneDocument,
    decode_generation_zero,
    decode_version_one,
    encode_generation_zero,
    encode_version_one,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    accounts_to_version_two,
    generation_zero_to_version_one,
    version_one_to_accounts,
    version_one_to_v060,
)
from sidekick_usages.persistence.v060 import ReleasedV060Verifier
from sidekick_usages.providers.codex.auth_migration import (
    CodexPrivateAuthMigrator,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerBackendId,
    SchedulerBackendObservation,
    SchedulerBackendState,
    SchedulerQuiescenceAssessment,
)
from tests.test_support import make_application_paths

EXPIRY = datetime(2026, 7, 11, 12, tzinfo=UTC)
CLAUDE_EXPIRY_MILLISECONDS = 1_783_771_200_000
CODEX_EXPIRY_SECONDS = 1_783_771_200
QUIET = SchedulerQuiescenceAssessment(
    (
        SchedulerBackendObservation(
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendState.ABSENT,
            "Sidekick scheduler is absent.",
        ),
    )
)


class _FailingVersionThreeFilesystem(PersistenceFilesystem):
    """Inject one exact final-index commit failure."""

    fail_version_three = True

    def _commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        if (
            generation is AuthorityGeneration.VERSION_THREE
            and self.fail_version_three
        ):
            self.fail_version_three = False
            raise ReplaceFailedError()
        return super()._commit_authority(
            generation,
            payload,
            expected_source,
        )


class _AccountIds:
    """Deterministic stable account IDs for one migration test."""

    def __init__(self) -> None:
        self._values = iter(
            (
                SidekickAccountId("75cc2b04-05ea-43d2-b897-bc960c85cd63"),
                SidekickAccountId("af5c2e51-9119-4e1c-9a6d-d4855501dc83"),
                SidekickAccountId("252a72de-fdf0-453d-92c3-e079fb02cb76"),
                SidekickAccountId("a41986db-92d3-4c11-8731-f75016d66d66"),
            )
        )

    def __call__(self) -> SidekickAccountId:
        return next(self._values)


class _AuthorityIds:
    """Deterministic protected authority IDs for one migration test."""

    def __init__(self) -> None:
        self._values = iter(
            (
                AuthorityId("a050a4a2-357b-4923-aeed-ed5866475853"),
                AuthorityId("671bd641-87e7-450c-91c9-04863abf3462"),
                AuthorityId("b9b27663-d780-40c6-9cc3-afc8a288571e"),
                AuthorityId("da1065bd-f162-46a8-af45-d82cfa012f89"),
            )
        )

    def __call__(self) -> AuthorityId:
        return next(self._values)


def _stored_record(
    provider_id: ProviderId,
    *,
    targets: tuple[str, ...] | None = None,
    resets: tuple[tuple[str, datetime], ...] | None = None,
) -> StoredAccountRecord:
    is_claude = provider_id is ProviderId.CLAUDE
    return StoredAccountRecord(
        label=AccountLabel(f"{provider_id}-account"),
        provider_id=provider_id,
        provider_account_id=None if is_claude else "acct_test_only",
        access_token=f"test-only-{provider_id}-access",
        refresh_token=f"test-only-{provider_id}-refresh",
        expires_at=EXPIRY,
        plan="max" if is_claude else "plus",
        scopes=("user:profile",) if is_claude else None,
        codex_home=None if is_claude else "/synthetic/codex",
        codex_id_token=None if is_claude else "test-only-id-token",
        codex_last_refresh=None,
        last_refresh_at=None,
        last_refresh_status=None,
        last_refresh_error=None,
        heartbeat_enabled=not is_claude,
        heartbeat_5h_reset_at=None,
        heartbeat_window_resets=resets,
        heartbeat_targets=targets,
        last_heartbeat_at=None,
        last_heartbeat_status=None,
        last_heartbeat_error=None,
    )


def test_forward_and_v060_reverse_preserve_both_provider_units() -> None:
    """Normalized state emits canonical v1 and exact v0.6 epoch units."""
    source = GenerationZeroDocument(
        (
            _stored_record(ProviderId.CLAUDE),
            _stored_record(ProviderId.CODEX),
        )
    )

    version_one = generation_zero_to_version_one(source)
    encoded = encode_version_one(version_one)
    assert encode_version_one(decode_version_one(encoded)) == encoded

    reverse = version_one_to_v060(decode_version_one(encoded))
    reverse_bytes = encode_generation_zero(reverse)
    assert decode_generation_zero(reverse_bytes) == source
    reverse_json = json.loads(reverse_bytes)
    assert (
        reverse_json["claude-account"]["expires_at"]
        == CLAUDE_EXPIRY_MILLISECONDS
    )
    assert reverse_json["codex-account"]["expires_at"] == CODEX_EXPIRY_SECONDS


@pytest.mark.parametrize(
    ("targets", "resets", "compatible"),
    [
        (None, None, True),
        ((), None, False),
        (("standard",), None, True),
        (None, (), False),
        (None, (("standard", EXPIRY),), True),
    ],
)
def test_v060_reverse_rejects_only_unrepresentable_empty_collections(
    targets: tuple[str, ...] | None,
    resets: tuple[tuple[str, datetime], ...] | None,
    *,
    compatible: bool,
) -> None:
    """Rollback never silently collapses explicit empty state to unknown."""
    document = VersionOneDocument(
        (
            _stored_record(
                ProviderId.CODEX,
                targets=targets,
                resets=resets,
            ),
        )
    )

    if compatible:
        assert isinstance(
            version_one_to_v060(document),
            GenerationZeroDocument,
        )
    else:
        with pytest.raises(RollbackCompatibilityError) as exc_info:
            version_one_to_v060(document)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


def test_runtime_account_conversion_is_validated_and_secret_safe() -> None:
    """The legacy persistence bridge preserves complete runtime accounts."""
    accounts = (
        Account(
            label=AccountLabel("claude-max-1"),
            credentials=ClaudeLoginCredentials(
                access_token="claude-access-secret",
                refresh_token="claude-refresh-secret",
                access_expiry=KnownExpiry(EXPIRY),
                refresh_expiry=UnknownExpiry(),
                scopes=("user:profile",),
            ),
            plan="max",
        ),
        Account(
            label=AccountLabel("codex-plus-1"),
            credentials=CodexCredentials(
                access_token="codex-access-secret",
                refresh_token="codex-refresh-secret",
                expiry=KnownExpiry(EXPIRY),
                account_id="acct_test_only",
                id_token="codex-id-secret",
            ),
            plan="plus",
        ),
    )

    document = accounts_to_version_one(accounts)
    assert version_one_to_accounts(document) == accounts
    representation = repr(document)
    assert all(
        secret not in representation
        for secret in (
            "claude-access-secret",
            "claude-refresh-secret",
            "codex-access-secret",
            "codex-refresh-secret",
            "codex-id-secret",
        )
    )


def test_version_one_classifies_unambiguous_legacy_claude_shapes() -> None:
    """Existing complete logins load without inventing unavailable metadata."""
    login = _stored_record(ProviderId.CLAUDE)
    setup = replace(
        login,
        label=AccountLabel("claude-setup"),
        refresh_token=None,
        expires_at=None,
        scopes=("user:inference",),
    )

    accounts = version_one_to_accounts(VersionOneDocument((login, setup)))

    assert accounts[0].credentials == ClaudeLoginCredentials(
        access_token=login.access_token,
        refresh_token="test-only-claude-refresh",
        access_expiry=KnownExpiry(EXPIRY),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
        identity=None,
    )
    assert accounts[1].credentials == ClaudeSetupTokenCredentials(
        access_token=setup.access_token
    )


@pytest.mark.parametrize(
    ("refresh_token", "expires_at", "scopes"),
    [
        ("test-only-refresh", None, ("user:profile",)),
        (None, EXPIRY, ("user:profile",)),
        (None, None, ("user:profile",)),
        ("test-only-refresh", EXPIRY, ("user:inference",)),
    ],
)
def test_version_one_rejects_partial_legacy_claude_logins(
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: tuple[str, ...] | None,
) -> None:
    """Partial login state never degrades into a setup-token credential."""
    record = replace(
        _stored_record(ProviderId.CLAUDE),
        refresh_token=refresh_token,
        expires_at=expires_at,
        scopes=scopes,
    )

    with pytest.raises(InvalidSchemaError):
        version_one_to_accounts(VersionOneDocument((record,)))


@pytest.mark.parametrize(
    "credentials",
    [
        ClaudeLoginCredentials(
            access_token="test-only-access",
            refresh_token="test-only-refresh",
            access_expiry=KnownExpiry(EXPIRY),
            refresh_expiry=KnownExpiry(EXPIRY),
            scopes=("user:profile",),
        ),
        ClaudeLoginCredentials(
            access_token="test-only-access",
            refresh_token="test-only-refresh",
            access_expiry=KnownExpiry(EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
            identity=ClaudeLoginIdentity(
                account_id="test-only-account",
                organization_id="test-only-organization",
            ),
        ),
    ],
)
def test_version_one_rejects_unrepresentable_claude_login_metadata(
    credentials: ClaudeLoginCredentials,
) -> None:
    """The bridge never silently discards Task 3-owned login metadata."""
    account = Account(
        label=AccountLabel("claude-login"),
        credentials=credentials,
    )

    with pytest.raises(InvalidSchemaError):
        accounts_to_version_one((account,))


def _verify_managed_rollback_boundary(
    paths: ApplicationPaths,
    filesystem: PersistenceFilesystem,
    tree: PrivateCredentialTree,
    document: VersionThreeDocument,
    metrics: Path,
) -> None:
    codex = document.accounts[1]
    assert isinstance(codex.authority, CodexAccountAuthority)
    legacy = codex.authority.subscription
    managed_codex = replace(
        codex,
        authority=CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=legacy.authority_id,
                provider_identity=ProviderIdentity("acct_test_only"),
                generation=ManagedAuthorityGeneration("generation-2"),
                verified_at=EXPIRY,
                executable_version="1.2.3",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )
    managed = VersionThreeDocument((document.accounts[0], managed_codex))
    blocked_payload = encode_version_three(managed)
    with pytest.raises(RollbackCompatibilityError):
        require_v060_compatible(managed)

    current = filesystem.read_authority()
    assert current is not None
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_THREE,
            blocked_payload,
            current.fingerprint,
        )
    rollback = PersistenceMigrationService(
        paths,
        scheduler_assessor=lambda: QUIET,
        private_auth_migrator=CodexPrivateAuthMigrator(),
        released_v060_verifier=ReleasedV060Verifier(),
    )
    protected_before = tuple(
        path.name for path in tree.list_owned_directories()
    )
    with pytest.raises(RollbackCompatibilityError):
        rollback.prepare_rollback()

    blocked = filesystem.read_authority()
    assert blocked is not None
    assert blocked.data == blocked_payload
    assert tuple(path.name for path in tree.list_owned_directories()) == (
        protected_before
    )
    with PersistenceLock(filesystem).hold() as transaction:
        restored = transaction.commit_authority(
            AuthorityGeneration.VERSION_THREE,
            encode_version_three(document),
            blocked.fingerprint,
        )
    assert decode_version_three(restored.data) == document

    result = rollback.prepare_rollback()
    released = filesystem.read_authority()
    assert result.code is PersistenceCode.ROLLBACK_PREPARED
    assert released is not None
    assert len(decode_generation_zero(released.data).accounts) == len(
        document.accounts
    )
    assert tree.observe() is OrphanedPrivateCredentials.ABSENT
    assert metrics.read_bytes() == (
        b"synthetic metrics remain independently owned"
    )


def test_managed_migration_is_atomic_and_blocks_unsafe_rollback(
    tmp_path: Path,
) -> None:
    """Secrets move once, interruption recovers, and managed rollback stops."""
    paths = make_application_paths(tmp_path)
    source_accounts = (
        Account(
            label=AccountLabel("claude-max"),
            credentials=ClaudeSetupTokenCredentials(
                access_token="test-only-claude-access"
            ),
            plan="max",
            heartbeat_enabled=True,
            heartbeat_targets=("standard",),
        ),
        Account(
            label=AccountLabel("codex-pro"),
            credentials=CodexCredentials(
                access_token="test-only-codex-access",
                refresh_token="test-only-codex-refresh",
                expiry=KnownExpiry(EXPIRY),
                account_id="acct_test_only",
                id_token="test-only-codex-id",
            ),
            plan="pro",
        ),
    )
    source_payload = encode_version_two(
        accounts_to_version_two(source_accounts)
    )
    filesystem = _FailingVersionThreeFilesystem(paths.accounts.canonical)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_TWO,
            source_payload,
            AuthorityExpectation.ABSENT,
        )
    metrics = paths.activity_snapshots
    metrics.parent.mkdir(parents=True, exist_ok=True)
    metrics.write_bytes(b"synthetic metrics remain independently owned")
    tree = PrivateCredentialTree(
        paths.credential_authorities,
        account_path=paths.accounts.canonical,
    )
    service = ManagedAccountMigrationService(
        paths.accounts.canonical,
        tree,
        account_id_factory=_AccountIds(),
        authority_id_factory=_AuthorityIds(),
        filesystem_factory=lambda _path: filesystem,
    )

    with pytest.raises(ReplaceFailedError):
        service.migrate()

    preserved = filesystem.read_authority()
    assert preserved is not None
    assert preserved.data == source_payload
    assert tree.observe() is OrphanedPrivateCredentials.ABSENT

    document = service.migrate()
    snapshot = filesystem.read_authority()
    assert snapshot is not None
    assert decode_version_three(snapshot.data) == document
    assert metrics.read_bytes() == (
        b"synthetic metrics remain independently owned"
    )
    assert tuple(account.label for account in document.accounts) == (
        "claude-max",
        "codex-pro",
    )
    assert document.accounts[0].heartbeat_enabled is True
    assert document.accounts[0].heartbeat_targets == ("standard",)
    assert all(
        secret not in snapshot.data
        for secret in (
            b"test-only-claude-access",
            b"test-only-codex-access",
            b"test-only-codex-refresh",
            b"test-only-codex-id",
        )
    )
    repository = CredentialAuthorityRepository(tree)
    for saved, expected in zip(
        document.accounts,
        source_accounts,
        strict=True,
    ):
        authority = saved.authority
        reference = (
            authority.setup_token or authority.subscription
            if isinstance(authority, ClaudeAccountAuthority)
            else authority.subscription
        )
        assert reference is not None
        protected = repository.read(
            saved.account_id,
            reference.authority_id,
        )
        assert protected is not None
        assert protected.credentials == expected.credentials

    _verify_managed_rollback_boundary(
        paths,
        filesystem,
        tree,
        document,
        metrics,
    )
