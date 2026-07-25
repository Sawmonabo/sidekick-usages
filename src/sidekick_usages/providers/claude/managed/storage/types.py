"""Closed protected Claude storage types."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.providers.claude.models import ClaudeManagedProfile


class ClaudeProtectedStorageFailure(StrEnum):
    """Safe reasons a protected Claude authority cannot be trusted."""

    MISSING = "missing"
    UNSAFE = "unsafe"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    KEYCHAIN_LOCKED = "keychain_locked"
    KEYCHAIN_ACCESS_DENIED = "keychain_access_denied"
    PLAINTEXT_FALLBACK = "plaintext_fallback"
    NAMESPACE_UNPROVEN = "namespace_unproven"
    IDENTITY_MISMATCH = "identity_mismatch"


class ClaudeCredentialFileSource(Protocol):
    """Read one exact managed-profile credential file."""

    def present(self, profile: ClaudeManagedProfile) -> bool:
        """Report the exact artifact without reading its contents."""

    def read(self, profile: ClaudeManagedProfile) -> bytes | None:
        """Return qualified bounded bytes or proven absence."""
