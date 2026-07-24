"""Structural types for provider activation coordination."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem

type StateLockFactory = Callable[[PrivateFilesystem], StateLock]

__all__ = [
    "StateLock",
    "StateLockFactory",
]


class StateLock(Protocol):
    """Lock object used by activation coordination."""

    def hold(
        self,
    ) -> AbstractContextManager[PersistenceTransaction]:
        """Acquire one qualified state lock."""
