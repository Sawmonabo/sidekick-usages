"""Read-only inventory of qualified account persistence evidence."""

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol, assert_never

from sidekick_usages.persistence.artifacts import (
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    DuplicateKeyError,
    FutureSchemaError,
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    InvalidSchemaError,
    MalformedJsonError,
    ManagedFileReadError,
    PersistenceSchemaError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem import (
    FilesystemQualification,
    PersistenceFilesystem,
)
from sidekick_usages.persistence.observations import (
    ArtifactKind,
    ArtifactObservation,
    ArtifactState,
    AuthorityKind,
    AuthorityObservation,
    PersistenceObservation,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    VersionOneDocument,
    decode_authority,
    decode_generation_zero,
    decode_prototype,
    decode_prototype_receipt,
    decode_version_one,
    encode_version_one,
)


class OrphanedPrivateCredentials(StrEnum):
    """Typed external observation of Sidekick-owned private credentials."""

    ABSENT = "absent"
    PRESENT = "present"


class PrototypeMigrationIntent(StrEnum):
    """Explicit operator intent that may inspect a prototype beside v1."""

    IMPORT = "import"
    REIMPORT = "reimport"


class ReadOnlyPersistenceFilesystem(Protocol):
    """Qualified operations required by passive inventory."""

    def qualify(self) -> FilesystemQualification:
        """Require an approved local filesystem."""

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        """Return only siblings in the closed managed grammar."""

    def read_authority(self) -> FileSnapshot | None:
        """Read the bound path without following its final object."""

    def read_managed(
        self,
        artifact: ManagedArtifact,
    ) -> FileSnapshot | None:
        """Bounded-read one exact previously discovered artifact."""


type FilesystemFactory = Callable[[Path], ReadOnlyPersistenceFilesystem]


class PersistenceInventory:
    """Build passive observations from two qualified account locations."""

    def __init__(
        self,
        authority_path: Path,
        prototype_path: Path,
        *,
        filesystem_factory: FilesystemFactory = PersistenceFilesystem,
    ) -> None:
        self._validate_path(authority_path)
        self._validate_path(prototype_path)
        self.authority_path = authority_path
        self.prototype_path = prototype_path
        self._filesystem_factory = filesystem_factory

    def inspect(
        self,
        orphaned_private_credentials: OrphanedPrivateCredentials,
    ) -> PersistenceObservation:
        """Return complete read-only evidence for passive assessment."""
        return self._inspect(orphaned_private_credentials, explicit=False)

    def inspect_for_prototype_migration(
        self,
        orphaned_private_credentials: OrphanedPrivateCredentials,
        intent: PrototypeMigrationIntent,
    ) -> PersistenceObservation:
        """Inspect a prototype only for one explicit migration intent."""
        if not isinstance(intent, PrototypeMigrationIntent):
            raise TypeError("intent must use the closed migration intent.")
        return self._inspect(orphaned_private_credentials, explicit=True)

    def _inspect(
        self,
        orphaned_private_credentials: OrphanedPrivateCredentials,
        *,
        explicit: bool,
    ) -> PersistenceObservation:
        if not isinstance(
            orphaned_private_credentials,
            OrphanedPrivateCredentials,
        ):
            raise TypeError(
                "orphaned_private_credentials must use the closed state."
            )
        try:
            filesystem = self._filesystem_factory(self.authority_path)
            filesystem.qualify()
            managed = filesystem.discover_managed()
        except UnsupportedFilesystemError:
            authority = AuthorityObservation(
                AuthorityKind.UNSUPPORTED_FILESYSTEM
            )
            artifacts = ()
        except UnsafeManagedFileError:
            authority = AuthorityObservation(AuthorityKind.UNSAFE)
            artifacts = ()
        except ManagedFileReadError:
            authority = AuthorityObservation(AuthorityKind.UNREADABLE)
            artifacts = ()
        except InvalidManagedArtifactError:
            authority = AuthorityObservation(AuthorityKind.INVALID_SCHEMA)
            artifacts = ()
        else:
            authority = self._classify_authority(filesystem)
            artifacts = self._read_managed_artifacts(filesystem, managed)

        if _prototype_eligible(
            authority,
            artifacts,
            orphaned_private_credentials,
            explicit=explicit,
        ):
            prototype = self._read_prototype()
        else:
            prototype = None
        if prototype is not None:
            artifacts = (*artifacts, prototype)
        return PersistenceObservation(
            safe_path=self.authority_path,
            authority=authority,
            artifacts=tuple(
                sorted(artifacts, key=lambda artifact: artifact.basename)
            ),
            orphaned_credentials=(
                orphaned_private_credentials
                is OrphanedPrivateCredentials.PRESENT
            ),
        )

    @staticmethod
    def _validate_path(path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Persistence inventory paths must be absolute.")
        require_safe_basename(path.name)

    @staticmethod
    def _read_authority(
        filesystem: ReadOnlyPersistenceFilesystem,
    ) -> AuthorityObservation:
        snapshot = filesystem.read_authority()
        if snapshot is None:
            return AuthorityObservation(AuthorityKind.ABSENT)
        try:
            document = decode_authority(snapshot.data)
        except DuplicateKeyError:
            observation = AuthorityObservation(AuthorityKind.DUPLICATE_KEY)
        except MalformedJsonError:
            observation = AuthorityObservation(AuthorityKind.MALFORMED_JSON)
        except FutureSchemaError as error:
            observation = AuthorityObservation(
                AuthorityKind.FUTURE,
                future_schema_version=error.schema_version,
            )
        except InvalidSchemaError:
            observation = AuthorityObservation(AuthorityKind.INVALID_SCHEMA)
        else:
            if isinstance(document, GenerationZeroDocument):
                observation = AuthorityObservation(
                    AuthorityKind.GENERATION_ZERO,
                    content=snapshot.data,
                    generation_zero=document,
                )
            elif isinstance(document, VersionOneDocument):
                observation = AuthorityObservation(
                    AuthorityKind.VERSION_ONE,
                    content=snapshot.data,
                    version_one=document,
                )
            else:
                assert_never(document)
        return observation

    @classmethod
    def _classify_authority(
        cls,
        filesystem: ReadOnlyPersistenceFilesystem,
    ) -> AuthorityObservation:
        try:
            return cls._read_authority(filesystem)
        except UnsupportedFilesystemError:
            return AuthorityObservation(AuthorityKind.UNSUPPORTED_FILESYSTEM)
        except UnsafeManagedFileError:
            return AuthorityObservation(AuthorityKind.UNSAFE)
        except ManagedFileReadError:
            return AuthorityObservation(AuthorityKind.UNREADABLE)
        except InvalidManagedArtifactError:
            return AuthorityObservation(AuthorityKind.INVALID_SCHEMA)

    def _read_managed_artifacts(
        self,
        filesystem: ReadOnlyPersistenceFilesystem,
        artifacts: tuple[ManagedArtifact, ...],
    ) -> tuple[ArtifactObservation, ...]:
        observations: list[ArtifactObservation] = []
        for artifact in sorted(
            artifacts,
            key=lambda candidate: candidate.basename,
        ):
            if artifact.kind is ManagedArtifactKind.AUTHORITY:
                continue
            observations.append(self._read_managed(filesystem, artifact))
        return tuple(observations)

    def _read_managed(
        self,
        filesystem: ReadOnlyPersistenceFilesystem,
        artifact: ManagedArtifact,
    ) -> ArtifactObservation:
        kind = _artifact_kind(artifact.kind)
        observed = self._read_managed_snapshot(filesystem, artifact, kind)
        if isinstance(observed, ArtifactObservation):
            return observed
        return _validate_managed_artifact(artifact, observed)

    @staticmethod
    def _read_managed_snapshot(
        filesystem: ReadOnlyPersistenceFilesystem,
        artifact: ManagedArtifact,
        kind: ArtifactKind,
    ) -> FileSnapshot | ArtifactObservation:
        try:
            snapshot = filesystem.read_managed(artifact)
        except UnsafeManagedFileError:
            observation = ArtifactObservation(
                kind,
                artifact.basename,
                ArtifactState.UNSAFE,
            )
        except ManagedFileReadError:
            observation = ArtifactObservation(
                kind,
                artifact.basename,
                ArtifactState.UNREADABLE,
            )
        except BackupConflictError:
            observation = _artifact_failure(artifact, kind)
        except InvalidManagedArtifactError:
            observation = _oversized_artifact(artifact, kind)
        except InterruptedArtifactError:
            observation = ArtifactObservation(
                kind,
                artifact.basename,
                ArtifactState.UNREADABLE,
            )
        else:
            return _readable_artifact(artifact, kind, snapshot)
        return observation

    def _read_prototype(self) -> ArtifactObservation | None:
        try:
            filesystem = self._filesystem_factory(self.prototype_path)
            filesystem.qualify()
            snapshot = filesystem.read_authority()
        except UnsupportedFilesystemError, UnsafeManagedFileError:
            return ArtifactObservation(
                ArtifactKind.PROTOTYPE,
                self.prototype_path.name,
                ArtifactState.UNSAFE,
            )
        except ManagedFileReadError:
            return ArtifactObservation(
                ArtifactKind.PROTOTYPE,
                self.prototype_path.name,
                ArtifactState.UNREADABLE,
            )
        except InvalidManagedArtifactError:
            return ArtifactObservation(
                ArtifactKind.PROTOTYPE,
                self.prototype_path.name,
                ArtifactState.BOUND_EXCEEDED,
            )
        if snapshot is None:
            return None
        try:
            prototype = decode_prototype(snapshot.data)
        except DuplicateKeyError:
            state = ArtifactState.DUPLICATE_KEY
        except MalformedJsonError:
            state = ArtifactState.MALFORMED_JSON
        except InvalidSchemaError:
            state = ArtifactState.INVALID_SCHEMA
        else:
            return ArtifactObservation(
                ArtifactKind.PROTOTYPE,
                self.prototype_path.name,
                ArtifactState.VALID,
                content=snapshot.data,
                prototype=prototype,
            )
        return ArtifactObservation(
            ArtifactKind.PROTOTYPE,
            self.prototype_path.name,
            state,
            content=snapshot.data,
        )


def _artifact_kind(kind: ManagedArtifactKind) -> ArtifactKind:
    match kind:
        case ManagedArtifactKind.LOCK:
            return ArtifactKind.LOCK
        case ManagedArtifactKind.GENERATION_ZERO_BACKUP:
            return ArtifactKind.V0_BACKUP
        case ManagedArtifactKind.VERSION_ONE_SNAPSHOT:
            return ArtifactKind.V1_SNAPSHOT
        case ManagedArtifactKind.PROTOTYPE_RECEIPT:
            return ArtifactKind.PROTOTYPE_RECEIPT
        case ManagedArtifactKind.TEMPORARY:
            return ArtifactKind.TEMPORARY
        case ManagedArtifactKind.AUTHORITY:
            raise ValueError(
                "Authority is not a managed artifact observation."
            )
        case _ as unreachable:
            assert_never(unreachable)


def _prototype_eligible(
    authority: AuthorityObservation,
    artifacts: tuple[ArtifactObservation, ...],
    orphaned: OrphanedPrivateCredentials,
    *,
    explicit: bool = False,
) -> bool:
    if authority.kind is AuthorityKind.VERSION_ONE:
        if explicit:
            return all(
                artifact.state is ArtifactState.VALID
                and artifact.kind is not ArtifactKind.TEMPORARY
                for artifact in artifacts
            )
        return any(
            artifact.kind is ArtifactKind.PROTOTYPE_RECEIPT
            and artifact.state is ArtifactState.VALID
            for artifact in artifacts
        )
    if authority.kind is not AuthorityKind.ABSENT:
        return False
    if orphaned is OrphanedPrivateCredentials.PRESENT:
        return False
    blocking = {
        ArtifactKind.V0_BACKUP,
        ArtifactKind.V1_SNAPSHOT,
        ArtifactKind.TEMPORARY,
    }
    return not any(artifact.kind in blocking for artifact in artifacts)


def _validate_managed_artifact(
    artifact: ManagedArtifact,
    snapshot: FileSnapshot,
) -> ArtifactObservation:
    if artifact.kind is ManagedArtifactKind.GENERATION_ZERO_BACKUP:
        return _generation_zero_backup(artifact, snapshot)
    if artifact.kind is ManagedArtifactKind.VERSION_ONE_SNAPSHOT:
        return _version_one_snapshot(artifact, snapshot)
    if artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT:
        return _prototype_receipt(artifact, snapshot)
    raise ValueError("Managed artifact has no content validator.")


def _generation_zero_backup(
    artifact: ManagedArtifact,
    snapshot: FileSnapshot,
) -> ArtifactObservation:
    if artifact.digest != snapshot.fingerprint.digest:
        return _artifact_failure(artifact, ArtifactKind.V0_BACKUP)
    try:
        document = decode_generation_zero(snapshot.data)
    except PersistenceSchemaError:
        return _artifact_failure(artifact, ArtifactKind.V0_BACKUP)
    return ArtifactObservation(
        ArtifactKind.V0_BACKUP,
        artifact.basename,
        ArtifactState.VALID,
        content=snapshot.data,
        generation_zero=document,
    )


def _version_one_snapshot(
    artifact: ManagedArtifact,
    snapshot: FileSnapshot,
) -> ArtifactObservation:
    if artifact.digest != snapshot.fingerprint.digest:
        return _artifact_failure(artifact, ArtifactKind.V1_SNAPSHOT)
    try:
        document = decode_version_one(snapshot.data)
        canonical = encode_version_one(document)
    except PersistenceSchemaError:
        return _artifact_failure(artifact, ArtifactKind.V1_SNAPSHOT)
    if canonical != snapshot.data:
        return _artifact_failure(artifact, ArtifactKind.V1_SNAPSHOT)
    return ArtifactObservation(
        ArtifactKind.V1_SNAPSHOT,
        artifact.basename,
        ArtifactState.VALID,
        content=snapshot.data,
        version_one=document,
    )


def _prototype_receipt(
    artifact: ManagedArtifact,
    snapshot: FileSnapshot,
) -> ArtifactObservation:
    try:
        receipt = decode_prototype_receipt(snapshot.data)
    except PersistenceSchemaError:
        return _artifact_failure(
            artifact,
            ArtifactKind.PROTOTYPE_RECEIPT,
            ArtifactState.INVALID_SCHEMA,
        )
    if artifact.digest != receipt.prototype_sha256:
        return _artifact_failure(
            artifact,
            ArtifactKind.PROTOTYPE_RECEIPT,
            ArtifactState.INVALID_SCHEMA,
        )
    return ArtifactObservation(
        ArtifactKind.PROTOTYPE_RECEIPT,
        artifact.basename,
        ArtifactState.VALID,
        receipt=receipt,
    )


def _artifact_failure(
    artifact: ManagedArtifact,
    kind: ArtifactKind,
    state: ArtifactState = ArtifactState.CONFLICT,
) -> ArtifactObservation:
    return ArtifactObservation(kind, artifact.basename, state)


def _oversized_artifact(
    artifact: ManagedArtifact,
    kind: ArtifactKind,
) -> ArtifactObservation:
    if kind is ArtifactKind.PROTOTYPE_RECEIPT:
        state = ArtifactState.BOUND_EXCEEDED
    elif kind in {ArtifactKind.V0_BACKUP, ArtifactKind.V1_SNAPSHOT}:
        state = ArtifactState.CONFLICT
    else:
        state = ArtifactState.UNREADABLE
    return _artifact_failure(artifact, kind, state)


def _readable_artifact(
    artifact: ManagedArtifact,
    kind: ArtifactKind,
    snapshot: FileSnapshot | None,
) -> FileSnapshot | ArtifactObservation:
    if snapshot is None:
        return _artifact_failure(artifact, kind, ArtifactState.UNREADABLE)
    if kind in {ArtifactKind.LOCK, ArtifactKind.TEMPORARY}:
        return ArtifactObservation(
            kind, artifact.basename, ArtifactState.VALID
        )
    return snapshot


__all__ = [
    "FilesystemFactory",
    "OrphanedPrivateCredentials",
    "PersistenceInventory",
    "PrototypeMigrationIntent",
    "ReadOnlyPersistenceFilesystem",
]
