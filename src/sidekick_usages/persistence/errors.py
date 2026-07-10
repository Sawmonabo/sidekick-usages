"""Typed failures for the account persistence boundary."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.errors import UsageError


class PersistenceError(UsageError):
    """Base for safe account-persistence failures."""


class PersistenceSchemaError(PersistenceError):
    """A persisted document failed its lexical or schema contract."""


class MalformedJsonError(PersistenceSchemaError):
    """Persisted bytes are not strict UTF-8 JSON."""

    def __init__(self) -> None:
        super().__init__("Account data is not valid UTF-8 JSON.")


class DuplicateKeyError(PersistenceSchemaError):
    """A persisted JSON object repeats a member name."""

    def __init__(self) -> None:
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
        super().__init__("Account data does not match a supported schema.")


class FutureSchemaError(PersistenceSchemaError):
    """A versioned document requires different application software.

    :param schema_version: Unsupported integer schema version.
    """

    def __init__(self, schema_version: int) -> None:
        self.schema_version = schema_version
        super().__init__(
            "Account data uses an unsupported schema version; "
            "install compatible software."
        )


class RollbackCompatibilityError(PersistenceError):
    """The released rollback reader cannot preserve current state."""

    def __init__(self) -> None:
        super().__init__(
            "Rollback cannot preserve an explicit empty heartbeat "
            "collection; normalize it intentionally before retrying."
        )
