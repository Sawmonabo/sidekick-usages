"""Closed current persistence status values."""

from enum import StrEnum


class PersistenceState(StrEnum):
    """Supported account-index presence states."""

    EMPTY = "empty"
    CURRENT = "current"
