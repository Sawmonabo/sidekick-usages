"""Descriptor-relative owner-qualified shell file persistence."""

import os
import sys
from pathlib import Path
from typing import Protocol

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import ShellNativeFile

if sys.platform == "darwin":
    from sidekick_usages.persistence.platform.macos.adapter import (
        MacOSPlatform,
    )
elif sys.platform.startswith("linux"):
    from sidekick_usages.persistence.platform.posix.adapter import (
        PosixPlatform,
    )

_MAXIMUM_SHELL_FILE_BYTES = 1024 * 1024
_PRIVATE_FILE_MODE = 0o600

type ShellFileSnapshot = ShellNativeFile


class ShellPersistenceError(OSError):
    """A shell file failed descriptor-relative qualified access."""


class _ShellNativePlatform(Protocol):
    """Native shell operations supplied by the maintained POSIX owner."""

    def read_shell_owned(
        self,
        parent: Path,
        basename: str,
        limit: int,
        *,
        owner_only: bool,
    ) -> ShellNativeFile | None:
        """Stable-read one owner-qualified shell file."""

    def write_shell_atomic(
        self,
        parent: Path,
        basename: str,
        data: bytes,
        expected: ShellNativeFile | None,
        *,
        mode: int,
    ) -> ShellNativeFile:
        """Atomically publish bytes after comparing current state."""

    def remove_shell_validated(
        self,
        parent: Path,
        basename: str,
        device: int,
        inode: int,
    ) -> bool:
        """Remove only the exact previously validated identity."""


def _current_platform() -> _ShellNativePlatform:
    if sys.platform == "darwin":
        return MacOSPlatform()
    if sys.platform.startswith("linux"):
        return PosixPlatform()
    raise ShellPersistenceError


class ShellFileStore:
    """Apply shell policy through the descriptor-relative POSIX owner."""

    def __init__(
        self,
        root: Path,
        effective_user_id: int,
        *,
        _native: _ShellNativePlatform | None = None,
    ) -> None:
        if (
            not root.is_absolute()
            or type(effective_user_id) is not int
            or effective_user_id != os.geteuid()
        ):
            raise ValueError("Shell persistence root is invalid.")
        self._root = root
        self._native = _current_platform() if _native is None else _native

    def read(
        self,
        path: Path,
        *,
        owner_only: bool,
    ) -> ShellFileSnapshot | None:
        """Return one bounded stable descriptor-qualified snapshot."""
        self._require_target(path)
        try:
            return self._native.read_shell_owned(
                path.parent,
                path.name,
                _MAXIMUM_SHELL_FILE_BYTES,
                owner_only=owner_only,
            )
        except NativeFilesystemError as error:
            raise ShellPersistenceError from error

    def write(
        self,
        path: Path,
        expected: ShellFileSnapshot | None,
        payload: bytes,
        *,
        owner_only: bool,
    ) -> ShellFileSnapshot:
        """Atomically publish after a stable expected-state comparison."""
        self._require_target(path)
        if len(payload) > _MAXIMUM_SHELL_FILE_BYTES:
            raise ShellPersistenceError
        mode = (
            _PRIVATE_FILE_MODE
            if owner_only or expected is None
            else expected.mode
        )
        try:
            return self._native.write_shell_atomic(
                path.parent,
                path.name,
                payload,
                expected,
                mode=mode,
            )
        except NativeFilesystemError as error:
            raise ShellPersistenceError from error

    def remove(
        self,
        path: Path,
        expected: ShellFileSnapshot,
    ) -> None:
        """Compare and remove only one exact owner-only generated file."""
        current = self.read(path, owner_only=True)
        if current != expected:
            raise ShellPersistenceError
        try:
            removed = self._native.remove_shell_validated(
                path.parent,
                path.name,
                expected.device,
                expected.inode,
            )
        except NativeFilesystemError as error:
            raise ShellPersistenceError from error
        if not removed:
            raise ShellPersistenceError

    def _require_target(self, path: Path) -> None:
        if not path.is_absolute() or path == self._root:
            raise ShellPersistenceError
        try:
            path.relative_to(self._root)
        except ValueError:
            raise ShellPersistenceError from None
