"""Lock-scoped recovery and full-reset orchestration."""

from pathlib import Path

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
    NativePlatform,
)
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    ArtifactPurpose,
    AuthorityExpectation,
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    DurabilityUncertainError,
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    PersistenceError,
    PersistenceFilesystemError,
    ResetIncompleteError,
    SourceChangedError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.schemas import (
    MAX_DOCUMENT_BYTES,
    decode_authority,
    decode_generation_zero,
    decode_prototype_receipt,
    decode_version_one,
    encode_version_one,
)

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
        try:
            if artifact.kind is ManagedArtifactKind.AUTHORITY:
                decode_authority(payload)
                return
            if artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT:
                self._validate_recovery_receipt(artifact, payload)
                return
            self._validate_recovery_backup(artifact, payload)
        except PersistenceFilesystemError:
            raise
        except PersistenceError:
            if artifact.kind is ManagedArtifactKind.AUTHORITY:
                raise
            if artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT:
                raise InvalidManagedArtifactError(artifact.basename) from None
            raise BackupConflictError(artifact.basename) from None

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
        expected_kind = {
            ArtifactPurpose.AUTHORITY: ManagedArtifactKind.AUTHORITY,
            ArtifactPurpose.BACKUP: (
                ManagedArtifactKind.GENERATION_ZERO_BACKUP
            ),
            ArtifactPurpose.SNAPSHOT: ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ArtifactPurpose.RECEIPT: ManagedArtifactKind.PROTOTYPE_RECEIPT,
        }[temporary.purpose]
        return final.kind is expected_kind

    @staticmethod
    def _validate_recovery_receipt(
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        receipt = decode_prototype_receipt(payload)
        if receipt.prototype_sha256 != artifact.digest:
            raise InvalidManagedArtifactError(artifact.basename)

    @staticmethod
    def _validate_recovery_backup(
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        if artifact.digest != sha256_digest(payload):
            raise BackupConflictError(artifact.basename)
        if artifact.kind is ManagedArtifactKind.GENERATION_ZERO_BACKUP:
            decode_generation_zero(payload)
            return
        if artifact.kind is ManagedArtifactKind.VERSION_ONE_SNAPSHOT:
            document = decode_version_one(payload)
            if encode_version_one(document) == payload:
                return
        raise BackupConflictError(artifact.basename)

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

    def _delete_credential_artifact(
        self,
        artifact: ManagedArtifact,
    ) -> bool:
        """Delete one validated credential artifact and harden its removal."""
        if artifact.kind not in {
            ManagedArtifactKind.GENERATION_ZERO_BACKUP,
            ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ManagedArtifactKind.TEMPORARY,
        }:
            raise ValueError("Reset can delete only credential artifacts.")
        if self.grammar.parse(artifact.basename) != artifact:
            raise ValueError("Artifact does not belong to this authority.")
        snapshot = self.read_managed(artifact)
        if snapshot is None:
            return False
        if artifact.kind is not ManagedArtifactKind.TEMPORARY:
            self._validate_recovery_artifact(artifact, snapshot.data)
        try:
            identity = snapshot.fingerprint.identity
            removed = self._native.remove_validated(
                self._parent,
                artifact.basename,
                identity.device,
                identity.inode,
            )
            if removed:
                self._native.harden_cleanup(self._parent)
            if self._read(artifact.basename, MAX_DOCUMENT_BYTES) is not None:
                raise ResetIncompleteError(artifact.basename)
        except NativeFilesystemError, PersistenceFilesystemError:
            raise ResetIncompleteError(artifact.basename) from None
        return removed

    def _full_reset(self, expected_source: ExpectedAuthority) -> None:
        """Delete validated credentials before the exact authority."""
        self._require_expected_authority(expected_source)
        credentials = self._credential_artifacts()
        for artifact in credentials:
            snapshot = self.read_managed(artifact)
            if snapshot is None:
                continue
            if artifact.kind is not ManagedArtifactKind.TEMPORARY:
                self._validate_recovery_artifact(artifact, snapshot.data)
        mutated = self._delete_reset_credentials(credentials)
        self._require_no_credentials(reset_started=mutated)
        try:
            self._delete_authority(expected_source)
        except PersistenceError:
            if mutated:
                raise ResetIncompleteError(
                    self.grammar.authority_basename
                ) from None
            raise
        self._require_reset_empty()

    def _credential_artifacts(self) -> tuple[ManagedArtifact, ...]:
        kinds = {
            ManagedArtifactKind.GENERATION_ZERO_BACKUP,
            ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ManagedArtifactKind.TEMPORARY,
        }
        return tuple(
            artifact
            for artifact in self.discover_managed()
            if artifact.kind in kinds
        )

    def _delete_reset_credentials(
        self,
        credentials: tuple[ManagedArtifact, ...],
    ) -> bool:
        mutated = False
        for artifact in credentials:
            try:
                mutated = self._delete_credential_artifact(artifact) or mutated
            except PersistenceError:
                raise ResetIncompleteError(artifact.basename) from None
        return mutated

    def _require_no_credentials(self, *, reset_started: bool) -> None:
        try:
            remaining = self._credential_artifacts()
        except PersistenceError:
            if reset_started:
                raise ResetIncompleteError(
                    self.grammar.authority_basename
                ) from None
            raise
        if remaining:
            raise ResetIncompleteError(remaining[0].basename)

    def _require_reset_empty(self) -> None:
        try:
            self._require_no_credentials(reset_started=True)
            if self.read_authority() is not None:
                raise ResetIncompleteError(self.grammar.authority_basename)
        except PersistenceError:
            raise ResetIncompleteError(
                self.grammar.authority_basename
            ) from None

    def _delete_authority(
        self,
        expected_source: ExpectedAuthority,
    ) -> bool:
        """Delete the exact authority last and harden its removal."""
        self._require_expected_authority(expected_source)
        if expected_source is AuthorityExpectation.ABSENT:
            return False
        identity = expected_source.identity
        try:
            removed = self._native.remove_validated(
                self._parent,
                self.grammar.authority_basename,
                identity.device,
                identity.inode,
            )
        except NativeFilesystemError as error:
            if error.kind is NativeFailureKind.CHANGED:
                raise SourceChangedError from None
            raise ResetIncompleteError(
                self.grammar.authority_basename
            ) from None
        if not removed:
            raise SourceChangedError
        try:
            self._native.harden_cleanup(self._parent)
            if (
                self._read(
                    self.grammar.authority_basename,
                    MAX_DOCUMENT_BYTES,
                )
                is not None
            ):
                raise ResetIncompleteError(self.grammar.authority_basename)
        except NativeFilesystemError, PersistenceFilesystemError:
            raise ResetIncompleteError(
                self.grammar.authority_basename
            ) from None
        return removed


__all__ = ["RecoveryOperations"]
