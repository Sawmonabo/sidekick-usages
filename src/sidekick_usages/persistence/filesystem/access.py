"""Qualified reads and native access for account persistence."""

import sys
from pathlib import Path
from typing import IO

from sidekick_usages.persistence._recovery import RecoveryOperations
from sidekick_usages.persistence.platform.contracts import (
    FilesystemQualification,
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
    NativePlatform,
)

if sys.platform == "darwin":
    from sidekick_usages.persistence.platform.macos.adapter import (
        MacOSPlatform,
    )
elif sys.platform == "win32":
    from sidekick_usages.persistence.platform.windows.adapter import (
        WindowsPlatform,
    )
elif sys.platform.startswith("linux"):
    from sidekick_usages.persistence.platform.posix.adapter import (
        PosixPlatform,
    )
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import (
    CandidateWriteError,
    DurabilityUncertainError,
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    ManagedFileReadError,
    PersistenceFilesystemError,
    PrivateCredentialRepairError,
    SourceChangedError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.artifact import (
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.types.artifact import ManagedArtifactKind

__all__ = ["PersistenceFilesystemAccess"]

_SINGLE_LINK = 1


def _current_platform() -> NativePlatform:
    if sys.platform == "darwin":
        return MacOSPlatform()
    if sys.platform == "win32":
        return WindowsPlatform()
    if sys.platform.startswith("linux"):
        return PosixPlatform()
    raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)


class PersistenceFilesystemAccess(RecoveryOperations):
    """Own platform qualification, bounded reads, and lock access."""

    def __init__(
        self,
        authority_path: Path,
        *,
        _native: NativePlatform | None = None,
    ) -> None:
        if not authority_path.is_absolute():
            raise ValueError("Account authority path must be absolute.")
        require_safe_basename(authority_path.name)
        self.authority_path = authority_path
        self.grammar = ArtifactGrammar(authority_path.name)
        self._parent = authority_path.parent
        try:
            self._native = _native or _current_platform()
        except NativeFilesystemError:
            raise UnsupportedFilesystemError from None

    def qualify(self) -> FilesystemQualification:
        """Require the authority parent to use an approved local filesystem."""
        try:
            family = self._native.qualify(self._parent)
        except NativeFilesystemError as error:
            if error.kind is NativeFailureKind.UNSAFE:
                raise UnsafeManagedFileError(
                    self.grammar.authority_basename
                ) from None
            raise UnsupportedFilesystemError from None
        return FilesystemQualification(family, self.authority_path)

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

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        """Enumerate only exact managed names and ignore every foreign name."""
        self.qualify()
        try:
            basenames = self._native.list_basenames(self._parent)
        except NativeFilesystemError as error:
            raise self._read_error(
                self.grammar.authority_basename,
                error,
            ) from None
        artifacts = (
            artifact
            for basename in basenames
            if (artifact := self.grammar.parse(basename)) is not None
        )
        return tuple(sorted(artifacts, key=lambda artifact: artifact.basename))

    def read_authority(self) -> FileSnapshot | None:
        """Read the current authority without following its final object."""
        self.qualify()
        return self._read(self.grammar.authority_basename, MAX_DOCUMENT_BYTES)

    def read_managed(
        self,
        artifact: ManagedArtifact,
        *,
        limit: int = MAX_DOCUMENT_BYTES,
    ) -> FileSnapshot | None:
        """Bounded-read one previously classified exact managed artifact."""
        if self.grammar.parse(artifact.basename) != artifact:
            raise ValueError("Artifact does not belong to this authority.")
        if limit < 0 or limit > MAX_DOCUMENT_BYTES:
            raise ValueError("Managed read limit is outside the contract.")
        self.qualify()
        return self._read(artifact.basename, limit)

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

    def _read(
        self,
        basename: str,
        limit: int,
        *,
        source_revalidation: bool = False,
        require_complete: bool = False,
    ) -> FileSnapshot | None:
        try:
            native = self._native.read(self._parent, basename, limit)
        except NativeFilesystemError as error:
            if source_revalidation and error.kind in {
                NativeFailureKind.CHANGED,
                NativeFailureKind.TOO_LARGE,
            }:
                raise SourceChangedError from None
            raise self._read_error(basename, error) from None
        if native is None:
            return None
        return self._snapshot_with_link_proof(
            basename,
            native,
            require_complete=require_complete or source_revalidation,
        )

    def _snapshot_with_link_proof(
        self,
        basename: str,
        native: NativeFile,
        *,
        require_complete: bool = False,
    ) -> FileSnapshot:
        if native.link_count == _SINGLE_LINK:
            return self._native_snapshot(native)
        partner = self._find_link_partner(basename, native)
        if partner is None:
            raise UnsafeManagedFileError(basename)
        if require_complete:
            current = self.grammar.parse(basename)
            if current is None:
                raise UnsafeManagedFileError(basename)
            interrupted = (
                current
                if current.kind is ManagedArtifactKind.TEMPORARY
                else partner[0]
            )
            raise InterruptedArtifactError(interrupted.basename)
        return self._native_snapshot(native)

    def _read_error(
        self,
        basename: str,
        error: NativeFilesystemError,
    ) -> PersistenceFilesystemError:
        if error.kind is NativeFailureKind.UNSUPPORTED:
            return UnsupportedFilesystemError()
        if error.kind is NativeFailureKind.UNSAFE:
            return UnsafeManagedFileError(basename)
        if error.kind is NativeFailureKind.TOO_LARGE:
            artifact = self.grammar.parse(basename)
            if artifact is None:
                return ManagedFileReadError(basename)
            if artifact.kind is ManagedArtifactKind.AUTHORITY:
                return InvalidManagedArtifactError(basename)
        return ManagedFileReadError(basename)
