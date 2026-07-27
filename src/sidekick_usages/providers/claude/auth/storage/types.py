"""Closed protected Claude storage types."""

from enum import StrEnum
from typing import Protocol

from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeCredentialPayload,
)
from sidekick_usages.providers.claude.types import ClaudeProfile


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
    PROOF_CHANGED = "proof_changed"


class ClaudeCredentialFileSource(Protocol):
    """Read one exact native or private profile credential file."""

    def present(self, profile: ClaudeProfile) -> bool:
        """Report the exact artifact without reading its contents."""

    def read(
        self,
        profile: ClaudeProfile,
    ) -> ClaudeCredentialPayload | None:
        """Return qualified bounded payload or proven absence."""
