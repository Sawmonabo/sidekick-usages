"""Secret-safe protected Claude storage failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)

_STORAGE_FAILURE_MESSAGES = {
    ClaudeProtectedStorageFailure.MISSING: (
        "The Claude credential authority is missing."
    ),
    ClaudeProtectedStorageFailure.UNSAFE: (
        "The Claude credential authority is unsafe."
    ),
    ClaudeProtectedStorageFailure.UNREADABLE: (
        "The Claude credential authority is unreadable."
    ),
    ClaudeProtectedStorageFailure.MALFORMED: (
        "The Claude credential authority is malformed."
    ),
    ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED: (
        "The macOS Keychain is locked or unavailable."
    ),
    ClaudeProtectedStorageFailure.KEYCHAIN_ACCESS_DENIED: (
        "Access to the macOS Keychain credential was denied."
    ),
    ClaudeProtectedStorageFailure.PLAINTEXT_FALLBACK: (
        "Claude used an unsupported plaintext credential fallback."
    ),
    ClaudeProtectedStorageFailure.NAMESPACE_UNPROVEN: (
        "The Claude credential namespace is not proven for this release."
    ),
    ClaudeProtectedStorageFailure.IDENTITY_MISMATCH: (
        "The Claude credential identity does not match."
    ),
}


class ClaudeProtectedStorageError(UsageError):
    """One protected-storage failure containing no credential material."""

    def __init__(self, code: ClaudeProtectedStorageFailure) -> None:
        self.code = code
        super().__init__(_STORAGE_FAILURE_MESSAGES[code])
