"""Closed failure codes for current persistence operations."""

from enum import StrEnum


class ActivitySnapshotFailureKind(StrEnum):
    """Closed failures from the token-activity snapshot boundary."""

    READ = "read"
    MALFORMED = "malformed"
    WRITE = "write"
    CONFLICT = "conflict"


class UsageSnapshotFailureKind(StrEnum):
    """Closed failures from the account-usage snapshot boundary."""

    READ = "read"
    MALFORMED = "malformed"
    WRITE = "write"
    CONFLICT = "conflict"


class PersistenceCode(StrEnum):
    """Current passive and operation-time persistence failures."""

    UNSUPPORTED_FILESYSTEM = "unsupported_filesystem"
    UNSAFE_PERMISSIONS = "unsafe_permissions"
    UNREADABLE = "unreadable"
    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"
    FUTURE_SCHEMA = "future_schema"
    INVALID_SCHEMA = "invalid_schema"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    INTERRUPTED_ARTIFACTS = "interrupted_artifacts"
    STORE_LOCKED = "store_locked"
    SOURCE_CHANGED = "source_changed"
    REPLACE_FAILED = "replace_failed"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    RESET_INCOMPLETE = "reset_incomplete"
