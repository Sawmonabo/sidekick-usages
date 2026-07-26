"""Shared validators for exact persisted boundary text."""

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.persistence.types.artifact import Sha256Digest


def canonical_account_id_text(value: str) -> str:
    """Require one canonical Sidekick account identifier."""
    SidekickAccountId(value)
    return value


def sha256_text(value: str) -> str:
    """Require one lowercase SHA-256 hexadecimal value."""
    Sha256Digest(value)
    return value
