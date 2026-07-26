"""Ports for account-index transaction coordination."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)

type AccountLockFactory = Callable[
    [PersistenceFilesystem],
    AccountPersistenceLock,
]
type AccountFilesystemFactory = Callable[[Path], PersistenceFilesystem]


class AccountPersistenceTransaction(Protocol):
    """Held account-index commit capability."""

    def commit_authority(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit exact account-index bytes."""


class AccountPersistenceLock(Protocol):
    """Cooperative account-index lock."""

    def hold(
        self,
    ) -> AbstractContextManager[AccountPersistenceTransaction]:
        """Acquire the account lock."""
