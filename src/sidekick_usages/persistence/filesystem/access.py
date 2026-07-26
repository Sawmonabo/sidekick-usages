"""Qualified mutation access for account persistence."""

from typing import IO

from sidekick_usages.persistence.errors import (
    CandidateWriteError,
    DurabilityUncertainError,
    ManagedFileReadError,
    PrivateCredentialRepairError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.recovery import RecoveryOperations


class PersistenceFilesystemAccess(RecoveryOperations):
    """Add lock, permission, and parent mutation to qualified reads."""

    def repair_parent_permissions(self) -> bool:
        """Explicitly harden the released account parent and prove it."""
        self.qualify()
        try:
            repaired = self._native.repair_parent_permissions(self._parent)
            self._native.ensure_parent(self._parent)
        except NativeFilesystemError as error:
            basename = self._parent.name
            if error.kind is NativeFailureKind.UNSUPPORTED:
                raise UnsupportedFilesystemError(basename) from None
            if error.kind in {
                NativeFailureKind.UNSAFE,
                NativeFailureKind.CHANGED,
            }:
                raise UnsafeManagedFileError(basename) from None
            if error.kind is NativeFailureKind.UNREADABLE:
                raise ManagedFileReadError(basename) from None
            if error.kind in {
                NativeFailureKind.SYNCHRONIZE,
                NativeFailureKind.HARDEN,
            }:
                raise DurabilityUncertainError(basename) from None
            raise PrivateCredentialRepairError(basename) from None
        self.qualify()
        return repaired

    def _open_lock_sidecar(self) -> IO[bytes]:
        """Open the secured sidecar consumed only by ``locking.py``."""
        self._prepare_parent()
        try:
            return self._native.open_lock(
                self._parent,
                self.grammar.lock_basename,
            )
        except NativeFilesystemError as error:
            raise self._read_error(
                self.grammar.lock_basename,
                error,
            ) from None

    def _prove_lock_sidecar_identity(self, sidecar: IO[bytes]) -> None:
        """Prove the acquired handle still owns the exact sidecar name."""
        try:
            self._native.prove_lock_identity(
                self._parent,
                self.grammar.lock_basename,
                sidecar,
            )
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.CHANGED,
                NativeFailureKind.UNSAFE,
            }:
                raise UnsafeManagedFileError(
                    self.grammar.lock_basename
                ) from None
            raise self._read_error(
                self.grammar.lock_basename,
                error,
            ) from None

    def _prepare_parent(self) -> None:
        self.qualify()
        try:
            self._native.ensure_parent(self._parent)
        except NativeFilesystemError as error:
            if error.kind is NativeFailureKind.UNSAFE:
                raise UnsafeManagedFileError(
                    self.grammar.authority_basename
                ) from None
            raise CandidateWriteError from None
        self.qualify()
