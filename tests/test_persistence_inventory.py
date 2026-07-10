"""Read-only persistence inventory boundary tests."""

from pathlib import Path

import pytest

from sidekick_usages.persistence._platform import FilesystemFamily
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    sha256_digest,
)
from sidekick_usages.persistence.assessment import (
    ArtifactKind,
    ArtifactState,
    AuthorityKind,
    PersistenceCode,
    assess_persistence,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    ManagedFileReadError,
    PersistenceFilesystemError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem import FilesystemQualification
from sidekick_usages.persistence.inventory import (
    OrphanedPrivateCredentials,
    PersistenceInventory,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeReceipt,
    VersionOneDocument,
    decode_prototype,
    encode_generation_zero,
    encode_prototype_receipt,
    encode_version_one,
)
from sidekick_usages.persistence.transforms import prototype_to_version_one

_QUALIFIED_ROOT = Path.cwd() / "qualified-test-root"
AUTHORITY_PATH = _QUALIFIED_ROOT / "sidekick" / "accounts.json"
PROTOTYPE_PATH = _QUALIFIED_ROOT / "prototype" / "accounts.json"
GENERATION_ZERO = encode_generation_zero(GenerationZeroDocument(()))
VERSION_ONE = encode_version_one(VersionOneDocument(()))
PROTOTYPE = b'{"primary":{"token":"test-only-secret","plan":"max"}}'
PROTOTYPE_VERSION_ONE = encode_version_one(
    prototype_to_version_one(decode_prototype(PROTOTYPE))
)
FUTURE_SCHEMA_VERSION = 2


def _snapshot(payload: bytes, *, inode: int = 1) -> FileSnapshot:
    return FileSnapshot(
        FileFingerprint(
            FileIdentity(1, inode),
            sha256_digest(payload),
            len(payload),
        ),
        1,
        payload,
    )


class FakeFilesystem:
    """Injectable qualified, no-follow inventory boundary."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.grammar = ArtifactGrammar(path.name)
        self.siblings: tuple[str, ...] = ()
        self.snapshots: dict[str, FileSnapshot] = {}
        self.read_errors: dict[str, PersistenceFilesystemError] = {}
        self.qualify_error: PersistenceFilesystemError | None = None
        self.discover_error: PersistenceFilesystemError | None = None
        self.calls: list[str] = []

    def qualify(self) -> FilesystemQualification:
        self.calls.append("qualify")
        if self.qualify_error is not None:
            raise self.qualify_error
        return FilesystemQualification(FilesystemFamily.EXT4, self.path)

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        self.calls.append("discover")
        if self.discover_error is not None:
            raise self.discover_error
        return tuple(
            artifact
            for basename in self.siblings
            if (artifact := self.grammar.parse(basename)) is not None
        )

    def read_authority(self) -> FileSnapshot | None:
        self.calls.append("read-authority")
        return self._read(self.path.name)

    def read_managed(
        self,
        artifact: ManagedArtifact,
    ) -> FileSnapshot | None:
        self.calls.append(f"read:{artifact.basename}")
        return self._read(artifact.basename)

    def _read(self, basename: str) -> FileSnapshot | None:
        if (error := self.read_errors.get(basename)) is not None:
            raise error
        return self.snapshots.get(basename)


class FakeFilesystemFactory:
    def __init__(self, *filesystems: FakeFilesystem) -> None:
        self.filesystems = {
            filesystem.path: filesystem for filesystem in filesystems
        }

    def __call__(self, path: Path) -> FakeFilesystem:
        return self.filesystems[path]


def _inventory(
    authority: FakeFilesystem,
    prototype: FakeFilesystem,
) -> PersistenceInventory:
    return PersistenceInventory(
        AUTHORITY_PATH,
        PROTOTYPE_PATH,
        filesystem_factory=FakeFilesystemFactory(authority, prototype),
    )


@pytest.mark.parametrize(
    ("case", "expected", "retains_content"),
    [
        ("absent", AuthorityKind.ABSENT, False),
        ("generation-zero", AuthorityKind.GENERATION_ZERO, True),
        ("version-one", AuthorityKind.VERSION_ONE, True),
        ("future", AuthorityKind.FUTURE, False),
        ("duplicate", AuthorityKind.DUPLICATE_KEY, False),
        ("malformed", AuthorityKind.MALFORMED_JSON, False),
        ("invalid", AuthorityKind.INVALID_SCHEMA, False),
        ("oversized", AuthorityKind.INVALID_SCHEMA, False),
        ("unreadable", AuthorityKind.UNREADABLE, False),
        ("unsafe", AuthorityKind.UNSAFE, False),
        ("unsupported", AuthorityKind.UNSUPPORTED_FILESYSTEM, False),
    ],
)
def test_authority_classification_is_exact_and_qualifies_first(
    case: str,
    expected: AuthorityKind,
    *,
    retains_content: bool,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    payload = {
        "generation-zero": GENERATION_ZERO,
        "version-one": VERSION_ONE,
        "future": b'{"schema_version":2,"accounts":{}}',
        "duplicate": b'{"same":1,"same":2}',
        "malformed": b'{"test-only-secret":',
        "invalid": b'{"account":true}',
    }.get(case)
    if payload is not None:
        authority.snapshots[AUTHORITY_PATH.name] = _snapshot(payload)
    elif case == "unreadable":
        authority.read_errors[AUTHORITY_PATH.name] = ManagedFileReadError(
            AUTHORITY_PATH.name
        )
    elif case == "unsafe":
        authority.read_errors[AUTHORITY_PATH.name] = UnsafeManagedFileError(
            AUTHORITY_PATH.name
        )
    elif case == "unsupported":
        authority.qualify_error = UnsupportedFilesystemError()
    elif case == "oversized":
        authority.read_errors[AUTHORITY_PATH.name] = (
            InvalidManagedArtifactError(AUTHORITY_PATH.name)
        )

    observation = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.ABSENT
    )

    assert observation.authority.kind is expected
    assert (observation.authority.content is not None) is retains_content
    assert authority.calls[0] == "qualify"
    if expected is AuthorityKind.FUTURE:
        assert (
            observation.authority.future_schema_version
            == FUTURE_SCHEMA_VERSION
        )
    assert bool(prototype.calls) is (case == "absent")
    if expected is AuthorityKind.UNSUPPORTED_FILESYSTEM:
        assert authority.calls == ["qualify"]
        assert prototype.calls == []


@pytest.mark.parametrize(
    ("read_error", "authority_code"),
    [
        (
            UnsafeManagedFileError(AUTHORITY_PATH.name),
            PersistenceCode.UNSAFE_PERMISSIONS,
        ),
        (
            ManagedFileReadError(AUTHORITY_PATH.name),
            PersistenceCode.UNREADABLE,
        ),
    ],
)
def test_authority_io_failure_retains_managed_backup_findings(
    read_error: PersistenceFilesystemError,
    authority_code: PersistenceCode,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    backup, _, _, _ = _managed_case("v0-digest-mismatch", authority)
    authority.read_errors[AUTHORITY_PATH.name] = read_error
    prototype = FakeFilesystem(PROTOTYPE_PATH)

    assessment = assess_persistence(
        _inventory(authority, prototype).inspect(
            OrphanedPrivateCredentials.ABSENT
        )
    )

    assert tuple(
        (issue.code, issue.artifact_basename) for issue in assessment.issues
    ) == (
        (authority_code, None),
        (PersistenceCode.BACKUP_CONFLICT, backup),
    )
    assert authority.calls == [
        "qualify",
        "discover",
        "read-authority",
        f"read:{backup}",
    ]


def _managed_case(
    case: str,
    filesystem: FakeFilesystem,
) -> tuple[str, ArtifactKind, ArtifactState, bool]:
    grammar = filesystem.grammar
    if case.startswith("v0"):
        payload = (
            VERSION_ONE if case == "v0-wrong-generation" else GENERATION_ZERO
        )
        digest = (
            sha256_digest(b"different")
            if case == "v0-digest-mismatch"
            else sha256_digest(payload)
        )
        basename = f"{grammar.authority_basename}.v0.{digest}.bak"
        expected = (
            ArtifactState.VALID
            if case == "v0-valid"
            else ArtifactState.CONFLICT
        )
        kind = ArtifactKind.V0_BACKUP
    elif case.startswith("v1"):
        payload = (
            b'{"schema_version":1,"accounts":{}}'
            if case == "v1-noncanonical"
            else VERSION_ONE
        )
        basename = (
            f"{grammar.authority_basename}.v1.{sha256_digest(payload)}.bak"
        )
        expected = (
            ArtifactState.VALID
            if case == "v1-valid"
            else ArtifactState.CONFLICT
        )
        kind = ArtifactKind.V1_SNAPSHOT
    elif case.startswith("receipt"):
        prototype_digest = sha256_digest(PROTOTYPE)
        receipt = PrototypeReceipt(str(prototype_digest))
        payload = encode_prototype_receipt(receipt)
        name_digest = (
            sha256_digest(b"different")
            if case == "receipt-name-mismatch"
            else prototype_digest
        )
        basename = grammar.receipt_basename(name_digest)
        expected = (
            ArtifactState.VALID
            if case == "receipt-valid"
            else (
                ArtifactState.BOUND_EXCEEDED
                if case == "receipt-oversized"
                else ArtifactState.INVALID_SCHEMA
            )
        )
        kind = ArtifactKind.PROTOTYPE_RECEIPT
    elif case.startswith("lock"):
        payload = b""
        basename = grammar.lock_basename
        kind = ArtifactKind.LOCK
        expected = (
            ArtifactState.UNSAFE
            if case == "lock-unsafe"
            else (
                ArtifactState.UNREADABLE
                if case == "lock-oversized"
                else ArtifactState.VALID
            )
        )
    else:
        payload = b"test-only-temporary-secret"
        basename = f".{grammar.authority_basename}.authority.{'0' * 32}.tmp"
        kind = ArtifactKind.TEMPORARY
        expected = (
            ArtifactState.UNREADABLE
            if case in {"temporary-unreadable", "temporary-oversized"}
            else ArtifactState.VALID
        )
    filesystem.siblings = (basename, AUTHORITY_PATH.name)
    filesystem.snapshots[basename] = _snapshot(payload, inode=2)
    if case == "lock-unsafe":
        filesystem.read_errors[basename] = UnsafeManagedFileError(basename)
    elif case in {"temporary-unreadable", "lock-oversized"}:
        filesystem.read_errors[basename] = ManagedFileReadError(basename)
    elif case == "v0-oversized":
        filesystem.read_errors[basename] = BackupConflictError(basename)
    elif case == "receipt-oversized":
        filesystem.read_errors[basename] = InvalidManagedArtifactError(
            basename
        )
    elif case == "temporary-oversized":
        filesystem.read_errors[basename] = InterruptedArtifactError(basename)
    retains_content = expected is ArtifactState.VALID and kind in {
        ArtifactKind.V0_BACKUP,
        ArtifactKind.V1_SNAPSHOT,
    }
    return basename, kind, expected, retains_content


@pytest.mark.parametrize(
    "case",
    [
        "v0-valid",
        "v0-digest-mismatch",
        "v0-wrong-generation",
        "v0-oversized",
        "v1-valid",
        "v1-noncanonical",
        "receipt-valid",
        "receipt-name-mismatch",
        "receipt-oversized",
        "lock-valid",
        "lock-unsafe",
        "lock-oversized",
        "temporary-valid",
        "temporary-unreadable",
        "temporary-oversized",
    ],
)
def test_managed_artifacts_validate_content_identity_and_security(
    case: str,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    authority.snapshots[AUTHORITY_PATH.name] = _snapshot(VERSION_ONE)
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    basename, kind, state, retains_content = _managed_case(case, authority)

    inventory = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.ABSENT
    )
    artifact = next(
        item for item in inventory.artifacts if item.basename == basename
    )

    assert (artifact.kind, artifact.state) == (kind, state)
    assert (artifact.content is not None) is retains_content
    assert f"read:{basename}" in authority.calls


@pytest.mark.parametrize(
    ("case", "state", "code", "retains_content"),
    [
        ("absent", None, PersistenceCode.EMPTY, False),
        (
            "valid",
            ArtifactState.VALID,
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            True,
        ),
        (
            "duplicate",
            ArtifactState.DUPLICATE_KEY,
            PersistenceCode.DUPLICATE_KEY,
            True,
        ),
        (
            "malformed",
            ArtifactState.MALFORMED_JSON,
            PersistenceCode.MALFORMED_JSON,
            True,
        ),
        (
            "invalid",
            ArtifactState.INVALID_SCHEMA,
            PersistenceCode.INVALID_SCHEMA,
            True,
        ),
        (
            "oversized",
            ArtifactState.BOUND_EXCEEDED,
            PersistenceCode.INVALID_SCHEMA,
            False,
        ),
        (
            "unreadable",
            ArtifactState.UNREADABLE,
            PersistenceCode.UNREADABLE,
            False,
        ),
        (
            "unsafe",
            ArtifactState.UNSAFE,
            PersistenceCode.UNSAFE_PERMISSIONS,
            False,
        ),
        (
            "unsupported",
            ArtifactState.UNSAFE,
            PersistenceCode.UNSAFE_PERMISSIONS,
            False,
        ),
    ],
)
def test_external_prototype_maps_eligibility_without_mutation(
    case: str,
    state: ArtifactState | None,
    code: PersistenceCode,
    *,
    retains_content: bool,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    payload = {
        "valid": PROTOTYPE,
        "duplicate": b'{"a":{"token":"x","token":"y","plan":"max"}}',
        "malformed": b'{"test-only-secret":',
        "invalid": b'{"a":{"token":"test-only-secret"}}',
    }.get(case)
    if payload is not None:
        prototype.snapshots[PROTOTYPE_PATH.name] = _snapshot(payload)
    elif case == "unreadable":
        prototype.read_errors[PROTOTYPE_PATH.name] = ManagedFileReadError(
            PROTOTYPE_PATH.name
        )
    elif case == "unsafe":
        prototype.read_errors[PROTOTYPE_PATH.name] = UnsafeManagedFileError(
            PROTOTYPE_PATH.name
        )
    elif case == "unsupported":
        prototype.qualify_error = UnsupportedFilesystemError()
    elif case == "oversized":
        prototype.read_errors[PROTOTYPE_PATH.name] = (
            InvalidManagedArtifactError(PROTOTYPE_PATH.name)
        )

    observation = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.ABSENT
    )
    prototypes = tuple(
        artifact
        for artifact in observation.artifacts
        if artifact.kind is ArtifactKind.PROTOTYPE
    )

    assert assess_persistence(observation).code is code
    if state is None:
        assert prototypes == ()
    else:
        assert len(prototypes) == 1
        assert prototypes[0].state is state
        assert (prototypes[0].content is not None) is retains_content
        assert prototype.calls[0] == "qualify"


@pytest.mark.parametrize(
    ("authority_payload", "expected"),
    [
        (None, PersistenceCode.EMPTY),
        (PROTOTYPE_VERSION_ONE, PersistenceCode.PROTOTYPE_IMPORTED),
    ],
    ids=("absent-authority", "version-one-authority"),
)
def test_exact_receipt_enables_only_the_required_prototype_relation(
    authority_payload: bytes | None,
    expected: PersistenceCode,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    if authority_payload is not None:
        authority.snapshots[AUTHORITY_PATH.name] = _snapshot(authority_payload)
    digest = sha256_digest(PROTOTYPE)
    receipt_basename = authority.grammar.receipt_basename(digest)
    authority.siblings = (receipt_basename,)
    authority.snapshots[receipt_basename] = _snapshot(
        encode_prototype_receipt(PrototypeReceipt(str(digest))),
        inode=2,
    )
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    prototype.snapshots[PROTOTYPE_PATH.name] = _snapshot(PROTOTYPE)

    observation = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.ABSENT
    )

    assert prototype.calls[:2] == ["qualify", "read-authority"]
    assert assess_persistence(observation).code is expected


@pytest.mark.parametrize("blocker", ["backup", "temporary", "credentials"])
def test_absent_authority_does_not_open_an_ineligible_prototype(
    blocker: str,
) -> None:
    authority = FakeFilesystem(AUTHORITY_PATH)
    orphaned = OrphanedPrivateCredentials.ABSENT
    if blocker == "backup":
        basename = (
            f"{AUTHORITY_PATH.name}.v0.{sha256_digest(GENERATION_ZERO)}.bak"
        )
        authority.siblings = (basename,)
        authority.snapshots[basename] = _snapshot(GENERATION_ZERO, inode=2)
    elif blocker == "temporary":
        basename = f".{AUTHORITY_PATH.name}.backup.{'f' * 32}.tmp"
        authority.siblings = (basename,)
        authority.snapshots[basename] = _snapshot(b"secret", inode=2)
    else:
        orphaned = OrphanedPrivateCredentials.PRESENT
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    prototype.snapshots[PROTOTYPE_PATH.name] = _snapshot(PROTOTYPE)

    observation = _inventory(authority, prototype).inspect(orphaned)

    assert prototype.calls == []
    assert assess_persistence(observation).code is (
        PersistenceCode.INTERRUPTED_ARTIFACTS
    )


def test_inventory_is_sorted_secret_safe_and_ignores_foreign_siblings() -> (
    None
):
    authority = FakeFilesystem(AUTHORITY_PATH)
    authority.snapshots[AUTHORITY_PATH.name] = _snapshot(VERSION_ONE)
    grammar = authority.grammar
    lock = grammar.lock_basename
    temporary = f".{AUTHORITY_PATH.name}.backup.{'a' * 32}.tmp"
    v0 = f"{AUTHORITY_PATH.name}.v0.{sha256_digest(GENERATION_ZERO)}.bak"
    authority.siblings = (
        "foreign-test-only-secret",
        temporary,
        v0,
        "accounts.json.v0.NOT-MANAGED.bak",
        lock,
        AUTHORITY_PATH.name,
    )
    authority.snapshots.update(
        {
            lock: _snapshot(b"", inode=2),
            temporary: _snapshot(b"test-only-temporary-secret", inode=3),
            v0: _snapshot(GENERATION_ZERO, inode=4),
            "foreign-test-only-secret": _snapshot(
                b"foreign-secret-content",
                inode=5,
            ),
        }
    )
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    prototype.snapshots[PROTOTYPE_PATH.name] = _snapshot(PROTOTYPE)

    observation = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.PRESENT
    )
    basenames = tuple(artifact.basename for artifact in observation.artifacts)

    assert basenames == tuple(sorted(basenames))
    assert observation.orphaned_credentials is True
    assert not any("foreign" in call for call in authority.calls)
    assert "test-only-secret" not in repr(observation)
    assert "temporary-secret" not in repr(observation)
    assert "foreign-secret-content" not in repr(observation)
