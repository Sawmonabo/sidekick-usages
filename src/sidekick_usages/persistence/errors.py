"""Typed failures for the account persistence boundary."""

from sidekick_usages.core.types import ExitCode
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.types.error import (
    ActivitySnapshotFailureKind,
    PersistenceCode,
)


class ActivitySnapshotError(UsageError):
    """A token-activity snapshot could not be trusted or persisted."""

    def __init__(self, kind: ActivitySnapshotFailureKind) -> None:
        self.kind = kind
        message = {
            ActivitySnapshotFailureKind.READ: (
                "Saved token activity cannot be read safely."
            ),
            ActivitySnapshotFailureKind.MALFORMED: (
                "Saved token activity is malformed."
            ),
            ActivitySnapshotFailureKind.WRITE: (
                "Fresh token activity could not be saved durably."
            ),
            ActivitySnapshotFailureKind.CONFLICT: (
                "Saved token activity changed concurrently."
            ),
        }[kind]
        super().__init__(message)


class PersistenceError(UsageError):
    """Base for safe account-persistence failures."""

    code: PersistenceCode


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


class InvalidSchemaError(PersistenceSchemaError):
    """A persisted JSON value violates its generation contract."""

    def __init__(self) -> None:
        self.code = PersistenceCode.INVALID_SCHEMA
        super().__init__("Account data does not match a supported schema.")


class DuplicateCredentialOwnershipError(InvalidSchemaError):
    """One provider credential is assigned to multiple account labels."""

    def __init__(
        self,
        labels: tuple[str, ...],
        *,
        provider_id: str | None = None,
        credential_field: str | None = None,
    ) -> None:
        self.labels = labels
        self.provider_id = provider_id
        self.credential_field = credential_field
        super().__init__()
        self.args = (
            "One provider credential has multiple durable owners: "
            + ", ".join(labels)
            + ".",
        )


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


def exit_code_for_persistence_code(code: PersistenceCode) -> ExitCode:
    """Map one current persistence failure to a process outcome."""
    if code in {
        PersistenceCode.FUTURE_SCHEMA,
        PersistenceCode.INTERRUPTED_ARTIFACTS,
        PersistenceCode.STORE_LOCKED,
        PersistenceCode.SOURCE_CHANGED,
    }:
        return ExitCode.MANUAL_ACTION
    return ExitCode.SYSTEM_ERROR
