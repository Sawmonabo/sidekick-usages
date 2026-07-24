"""Structural types for provider activation coordination."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol

from sidekick_usages.persistence.private_filesystem import PrivateFilesystem
from sidekick_usages.persistence.transaction import PersistenceTransaction

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
