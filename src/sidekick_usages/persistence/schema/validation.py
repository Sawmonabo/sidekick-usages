"""Shared validators for exact persisted boundary text."""

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.types.artifact import Sha256Digest


def bounded_text(value: str, maximum_bytes: int) -> str:
    """Require nonempty bounded UTF-8 while preserving control characters."""
    if not isinstance(value, str):
        raise TypeError("Persisted text must be a string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Persisted text must be valid UTF-8.") from None
    if not encoded or len(encoded) > maximum_bytes:
        raise ValueError("Persisted text must be nonempty and bounded.")
    return value


def canonical_account_id_text(value: str) -> str:
    """Require one canonical Sidekick account identifier."""
    SidekickAccountId(value)
    return value


def sha256_text(value: str) -> str:
    """Require one lowercase SHA-256 hexadecimal value."""
    Sha256Digest(value)
    return value
