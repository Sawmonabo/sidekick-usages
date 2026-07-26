"""Closed official Claude exchange outcomes."""

from enum import StrEnum

from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)


class ClaudeExchangeFailureKind(StrEnum):
    """Secret-safe reasons an official credential exchange failed."""

    INCOMPATIBLE = "incompatible"
    LOGIN_FAILED = "login_failed"
    TIMED_OUT = "timed_out"
    TRANSIENT = "transient"
    UNCHANGED = "unchanged"
    IDENTITY_MISMATCH = "identity_mismatch"
    MISSING = "missing"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"
    RECONCILIATION_REQUIRED = "reconciliation_required"


def claude_exchange_storage_failure(
    failure: ClaudeProtectedStorageFailure,
) -> ClaudeExchangeFailureKind:
    """Translate one protected-storage failure into exchange vocabulary."""
    if failure is ClaudeProtectedStorageFailure.MISSING:
        return ClaudeExchangeFailureKind.MISSING
    if failure is ClaudeProtectedStorageFailure.IDENTITY_MISMATCH:
        return ClaudeExchangeFailureKind.IDENTITY_MISMATCH
    if failure is ClaudeProtectedStorageFailure.MALFORMED:
        return ClaudeExchangeFailureKind.MALFORMED
    if failure in {
        ClaudeProtectedStorageFailure.KEYCHAIN_ACCESS_DENIED,
        ClaudeProtectedStorageFailure.KEYCHAIN_LOCKED,
        ClaudeProtectedStorageFailure.UNREADABLE,
    }:
        return ClaudeExchangeFailureKind.UNREADABLE
    return ClaudeExchangeFailureKind.INCOMPATIBLE
