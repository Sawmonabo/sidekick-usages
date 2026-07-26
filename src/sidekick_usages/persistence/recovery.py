"""Lock-scoped recovery and full-reset orchestration."""

from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    InterruptedArtifactError,
    PersistenceFilesystemError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem.reader import (
    INTERRUPTED_PUBLICATION_LINKS,
    SINGLE_LINK,
    PrivateFileReader,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.types.artifact import ManagedArtifactKind


class RecoveryOperations(PrivateFileReader):
    """Private mutation mixin available only through a held transaction."""

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
            or native.link_count != INTERRUPTED_PUBLICATION_LINKS
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
            or final.link_count != SINGLE_LINK
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
        if snapshot.link_count == INTERRUPTED_PUBLICATION_LINKS:
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
