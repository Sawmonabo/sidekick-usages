"""Typed failures for the account persistence boundary."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.errors import UsageError


class PersistenceError(UsageError):
    """Base for safe account-persistence failures."""

    code: PersistenceCode


class PersistenceCode(StrEnum):
    """Closed passive and operation-time persistence outcomes."""

    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"
    UNSAFE_PERMISSIONS = "unsafe_permissions"
    UNREADABLE = "unreadable"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    FUTURE_SCHEMA = "future_schema"
    INVALID_SCHEMA = "invalid_schema"
    BACKUP_CONFLICT = "backup_conflict"
    INTERRUPTED_ARTIFACTS = "interrupted_artifacts"
    LEGACY_WRITER_DETECTED = "legacy_writer_detected"
    ROLLBACK_PREPARED = "rollback_prepared"
    MIGRATION_REQUIRED = "migration_required"
    PROTOTYPE_IMPORT_REQUIRED = "prototype_import_required"
    PROTOTYPE_IMPORTED = "prototype_imported"
    CURRENT = "current"
    EMPTY = "empty"
    ROLLBACK_REQUIRED = "rollback_required"
    STORE_LOCKED = "store_locked"
    SOURCE_CHANGED = "source_changed"
    REPLACE_FAILED = "replace_failed"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    RESET_INCOMPLETE = "reset_incomplete"


class PersistenceSchemaError(PersistenceError):
    """A persisted document failed its lexical or schema contract."""


class MalformedJsonError(PersistenceSchemaError):
    """Persisted bytes are not strict UTF-8 JSON."""

    def __init__(self) -> None:
        self.code = PersistenceCode.MALFORMED_JSON
        super().__init__("Account data is not valid UTF-8 JSON.")


class DuplicateKeyError(PersistenceSchemaError):
    """A persisted JSON object repeats a member name."""

    def __init__(self) -> None:
        self.code = PersistenceCode.DUPLICATE_KEY
        super().__init__("Account data contains a duplicate JSON member.")


class SchemaIssueCode(StrEnum):
    """Safe validation failure categories owned by persistence."""

    MISSING_FIELD = "missing_field"
    UNEXPECTED_FIELD = "unexpected_field"
    INVALID_TYPE = "invalid_type"
    INVALID_VALUE = "invalid_value"


@dataclass(frozen=True, slots=True)
class SchemaIssue:
    """One input-free validation location and failure category."""

    path: tuple[str | int, ...]
    code: SchemaIssueCode
    message: str


class InvalidSchemaError(PersistenceSchemaError):
    """A persisted JSON value violates its generation contract."""

    def __init__(
        self,
        issues: tuple[SchemaIssue, ...] = (),
    ) -> None:
        self.issues = issues
        self.code = PersistenceCode.INVALID_SCHEMA
        super().__init__("Account data does not match a supported schema.")


class FutureSchemaError(PersistenceSchemaError):
    """A versioned document requires different application software.

    :param schema_version: Unsupported integer schema version.
    """

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        self.code = PersistenceCode.FUTURE_SCHEMA
        super().__init__(
            "Account data uses an unsupported schema version; "
            "install compatible software."
        )


class RollbackCompatibilityError(PersistenceError):
    """The released rollback reader cannot preserve current state."""

    def __init__(self) -> None:
        self.code = PersistenceCode.ROLLBACK_REQUIRED
        super().__init__(
            "Rollback cannot preserve an explicit empty heartbeat "
            "collection; normalize it intentionally before retrying."
        )


class PersistenceFilesystemError(PersistenceError):
    """Base for input-free qualified filesystem failures."""

    def __init__(
        self,
        code: PersistenceCode,
        message: str,
        artifact_basename: str | None = None,
    ) -> None:
        self.code = code
        self.artifact_basename = artifact_basename
        super().__init__(message)


class UnsupportedFilesystemError(PersistenceFilesystemError):
    """The authority parent cannot satisfy the native contract."""

    def __init__(self, basename: str | None = None) -> None:
        """Identify the bound artifact when its filesystem is unsupported.

        :param basename: Safe bound artifact basename, when known.
        """
        super().__init__(
            PersistenceCode.UNSUPPORTED_FILESYSTEM,
            "Account persistence requires a supported local filesystem.",
            basename,
        )


class UnsafeManagedFileError(PersistenceFilesystemError):
    """A managed object has unsafe or unassessable security state."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.UNSAFE_PERMISSIONS,
            "A managed account artifact has unsafe permissions or identity.",
            basename,
        )


class ManagedFileReadError(PersistenceFilesystemError):
    """A protected managed object cannot be bounded-read reliably."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.UNREADABLE,
            "A managed account artifact cannot be read safely.",
            basename,
        )


class InvalidManagedArtifactError(PersistenceFilesystemError):
    """A managed authority-like object violates its closed contract."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.INVALID_SCHEMA,
            "A managed account artifact violates its schema or identity.",
            basename,
        )


class CandidateWriteError(PersistenceFilesystemError):
    """A private candidate could not be created and synchronized."""

    def __init__(self, basename: str | None = None) -> None:
        super().__init__(
            PersistenceCode.REPLACE_FAILED,
            "The durable account candidate could not be prepared.",
            basename,
        )


class InterruptedArtifactError(PersistenceFilesystemError):
    """A private candidate remains and needs locked reassessment."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            "A managed account temporary requires locked recovery.",
            basename,
        )


class SourceChangedError(PersistenceFilesystemError):
    """The authority differs from the caller's exact baseline."""

    def __init__(self) -> None:
        super().__init__(
            PersistenceCode.SOURCE_CHANGED,
            "Account data changed before the durable commit point.",
        )


class BackupConflictError(PersistenceFilesystemError):
    """An immutable content-addressed target is not exact."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.BACKUP_CONFLICT,
            "An immutable account artifact conflicts with its identity.",
            basename,
        )


class ReplaceFailedError(PersistenceFilesystemError):
    """Native replacement failed before success was observed."""

    def __init__(self) -> None:
        super().__init__(
            PersistenceCode.REPLACE_FAILED,
            "The authoritative account file could not be replaced.",
        )


class DurabilityUncertainError(PersistenceFilesystemError):
    """Replacement occurred but final durability cannot be proven."""

    def __init__(self, basename: str | None = None) -> None:
        super().__init__(
            PersistenceCode.DURABILITY_UNCERTAIN,
            "Account bytes may be current, but durability is unconfirmed.",
            basename,
        )


class ResetIncompleteError(PersistenceFilesystemError):
    """A credential-bearing managed artifact could not be removed."""

    def __init__(self, basename: str) -> None:
        super().__init__(
            PersistenceCode.RESET_INCOMPLETE,
            "Account reset could not remove every credential artifact.",
            basename,
        )


class PrivateCredentialArtifactError(PersistenceFilesystemError):
    """A private provider credential artifact cannot be handled safely."""

    def __init__(self) -> None:
        super().__init__(
            PersistenceCode.RESET_INCOMPLETE,
            "Private credential artifacts cannot be handled safely.",
        )


class PrivateCredentialRepairError(PersistenceFilesystemError):
    """Private credential security metadata could not be repaired."""

    def __init__(self, basename: str) -> None:
        """Bind the failure to the private credential root.

        :param basename: Safe private credential root basename.
        """
        super().__init__(
            PersistenceCode.REPLACE_FAILED,
            "Private credential permissions could not be repaired safely.",
            basename,
        )


class PrivateCredentialCollisionError(PersistenceFilesystemError):
    """A private bundle cannot be proven to belong to this account."""

    def __init__(self, basename: str) -> None:
        """Bind the collision to a safe private bundle basename.

        :param basename: Private bundle basename.
        """
        super().__init__(
            PersistenceCode.SOURCE_CHANGED,
            "A private credential bundle belongs to another account.",
            basename,
        )
