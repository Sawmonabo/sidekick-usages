"""Qualified bounded reads for owner-private Sidekick state."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import (
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    ManagedFileReadError,
    PersistenceFilesystemError,
    SourceChangedError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.artifact import (
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.models import (
    FilesystemQualification,
    NativeFile,
)
from sidekick_usages.persistence.platform.ports import NativePlatform
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.types.artifact import (
    ArtifactPurpose,
    ManagedArtifactKind,
    sha256_digest,
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

SINGLE_LINK = 1
INTERRUPTED_PUBLICATION_LINKS = 2

type PrivateFileReaderFactory = Callable[[Path], PrivateFileReader]


def _current_platform() -> NativePlatform:
    if sys.platform == "darwin":
        return MacOSPlatform()
    if sys.platform == "win32":
        return WindowsPlatform()
    if sys.platform.startswith("linux"):
        return PosixPlatform()
    raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)


class PrivateFileReader:
    """Own platform qualification and strict bounded private-file reads."""

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

    def read_opaque_private(self) -> FileSnapshot | None:
        """Read and prove one bounded opaque private file when present."""
        self.qualify()
        return self._read(
            self.grammar.authority_basename,
            MAX_DOCUMENT_BYTES,
            require_complete=True,
        )

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        """Enumerate only exact managed names and ignore foreign names."""
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
        """Bounded-read one previously classified managed artifact."""
        if self.grammar.parse(artifact.basename) != artifact:
            raise ValueError("Artifact does not belong to this authority.")
        if limit < 0 or limit > MAX_DOCUMENT_BYTES:
            raise ValueError("Managed read limit is outside the contract.")
        self.qualify()
        return self._read(artifact.basename, limit)

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
        if native.link_count == SINGLE_LINK:
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

    def _find_link_partner(
        self,
        basename: str,
        native: NativeFile,
    ) -> tuple[ManagedArtifact, NativeFile] | None:
        if native.link_count != INTERRUPTED_PUBLICATION_LINKS:
            return None
        current = self.grammar.parse(basename)
        if current is None:
            return None
        try:
            basenames = self._native.list_basenames(self._parent)
        except NativeFilesystemError as error:
            raise self._read_error(basename, error) from None
        matches: list[tuple[ManagedArtifact, NativeFile]] = []
        for candidate_basename in sorted(basenames):
            if candidate_basename == basename:
                continue
            candidate = self.grammar.parse(candidate_basename)
            if candidate is None or not self._is_publication_pair(
                current,
                candidate,
            ):
                continue
            try:
                candidate_native = self._native.read(
                    self._parent,
                    candidate_basename,
                    MAX_DOCUMENT_BYTES,
                )
            except NativeFilesystemError as error:
                raise self._read_error(candidate_basename, error) from None
            if (
                candidate_native is not None
                and candidate_native.link_count
                == INTERRUPTED_PUBLICATION_LINKS
                and candidate_native.device == native.device
                and candidate_native.inode == native.inode
                and candidate_native.data == native.data
                and sha256_digest(candidate_native.data)
                == sha256_digest(native.data)
            ):
                matches.append((candidate, candidate_native))
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _native_snapshot(native: NativeFile) -> FileSnapshot:
        data = native.data
        return FileSnapshot(
            FileFingerprint(
                identity=FileIdentity(native.device, native.inode),
                digest=sha256_digest(data),
                size=len(data),
            ),
            native.link_count,
            data,
        )

    @staticmethod
    def _is_publication_pair(
        left: ManagedArtifact,
        right: ManagedArtifact,
    ) -> bool:
        temporary = (
            left if left.kind is ManagedArtifactKind.TEMPORARY else right
        )
        final = right if temporary is left else left
        if (
            temporary.kind is not ManagedArtifactKind.TEMPORARY
            or temporary.purpose is None
        ):
            return False
        return (
            temporary.purpose is ArtifactPurpose.AUTHORITY
            and final.kind is ManagedArtifactKind.AUTHORITY
        )

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


class PrivateDocumentReader:
    """Own one qualified filesystem reader for a private document."""

    absolute_path_error: ClassVar[str] = (
        "Private document path must be absolute."
    )

    def __init__(
        self,
        path: Path,
        *,
        filesystem_factory: PrivateFileReaderFactory = PrivateFileReader,
    ) -> None:
        if not path.is_absolute():
            raise ValueError(self.absolute_path_error)
        self.path = path
        self._filesystem = filesystem_factory(path)
