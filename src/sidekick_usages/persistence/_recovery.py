"""Lock-scoped recovery and full-reset orchestration."""

from pathlib import Path

from sidekick_usages.persistence.artifacts import ArtifactGrammar
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    InterruptedArtifactError,
    PersistenceFilesystemError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.platform.contracts import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
    NativePlatform,
)
from sidekick_usages.persistence.types.artifact import (
    ArtifactPurpose,
    ManagedArtifactKind,
    sha256_digest,
)

__all__ = ["RecoveryOperations"]

_SINGLE_LINK = 1
_INTERRUPTED_PUBLICATION_LINKS = 2


class RecoveryOperations:
    """Private mutation mixin available only through a held transaction."""

    grammar: ArtifactGrammar
    _native: NativePlatform
    _parent: Path

    def qualify(self) -> object:
        raise NotImplementedError

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        raise NotImplementedError

    def read_authority(self) -> FileSnapshot | None:
        raise NotImplementedError

    def read_managed(self, artifact: ManagedArtifact) -> FileSnapshot | None:
        raise NotImplementedError

    def _read(self, basename: str, limit: int) -> FileSnapshot | None:
        raise NotImplementedError

    def _read_error(
        self,
        basename: str,
        error: NativeFilesystemError,
    ) -> PersistenceFilesystemError:
        raise NotImplementedError

    def _find_link_partner(
        self,
        basename: str,
        native: NativeFile,
    ) -> tuple[ManagedArtifact, NativeFile] | None:
        if native.link_count != _INTERRUPTED_PUBLICATION_LINKS:
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
                == _INTERRUPTED_PUBLICATION_LINKS
                and candidate_native.device == native.device
                and candidate_native.inode == native.inode
                and candidate_native.data == native.data
                and sha256_digest(candidate_native.data)
                == sha256_digest(native.data)
            ):
                matches.append((candidate, candidate_native))
        if len(matches) != 1:
            return None
        return matches[0]

    def _validate_recovery_artifact(
        self,
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        raise NotImplementedError

    def _require_expected_authority(
        self,
        expected: ExpectedAuthority,
    ) -> None:
        raise NotImplementedError

    def _native_snapshot(self, native: NativeFile) -> FileSnapshot:
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

    def _complete_interrupted_publication(
        self,
        temporary: ManagedArtifact,
    ) -> tuple[ManagedArtifact, FileSnapshot]:
        """Complete one proven link-publication interruption under the lock."""
        if (
            temporary.kind is not ManagedArtifactKind.TEMPORARY
            or self.grammar.parse(temporary.basename) != temporary
        ):
            raise ValueError("Recovery requires one owned temporary.")
        self.qualify()
        try:
            native = self._native.read(
                self._parent,
                temporary.basename,
                MAX_DOCUMENT_BYTES,
            )
        except NativeFilesystemError as error:
            raise self._read_error(temporary.basename, error) from None
        if (
            native is None
            or native.link_count != _INTERRUPTED_PUBLICATION_LINKS
        ):
            raise UnsafeManagedFileError(temporary.basename)
        partner = self._find_link_partner(temporary.basename, native)
        if partner is None:
            raise UnsafeManagedFileError(temporary.basename)
        final_artifact, final_native = partner
        self._validate_recovery_artifact(final_artifact, final_native.data)
        expected = self._native_snapshot(final_native)
        try:
            removed = self._native.remove_validated(
                self._parent,
                temporary.basename,
                native.device,
                native.inode,
            )
            if not removed:
                raise NativeFilesystemError(NativeFailureKind.REMOVE)
            self._native.harden_cleanup(self._parent)
            final = self._read(final_artifact.basename, MAX_DOCUMENT_BYTES)
        except NativeFilesystemError, PersistenceFilesystemError:
            raise DurabilityUncertainError(final_artifact.basename) from None
        if (
            final is None
            or final.link_count != _SINGLE_LINK
            or final.data != expected.data
            or final.fingerprint != expected.fingerprint
        ):
            raise DurabilityUncertainError(final_artifact.basename)
        return final_artifact, final

    def _recover_or_discard_temporary(
        self,
        temporary: ManagedArtifact,
    ) -> None:
        """Complete a link-2 publication or discard an exact link-1 temp."""
        if (
            temporary.kind is not ManagedArtifactKind.TEMPORARY
            or self.grammar.parse(temporary.basename) != temporary
        ):
            raise ValueError("Recovery requires one owned temporary.")
        snapshot = self.read_managed(temporary)
        if snapshot is None:
            return
        if snapshot.link_count == _INTERRUPTED_PUBLICATION_LINKS:
            self._complete_interrupted_publication(temporary)
            return
        identity = snapshot.fingerprint.identity
        try:
            removed = self._native.remove_validated(
                self._parent,
                temporary.basename,
                identity.device,
                identity.inode,
            )
            if not removed:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            self._native.harden_cleanup(self._parent)
            if self.read_managed(temporary) is not None:
                raise NativeFilesystemError(NativeFailureKind.REMOVE)
        except NativeFilesystemError, PersistenceFilesystemError:
            raise InterruptedArtifactError(temporary.basename) from None
