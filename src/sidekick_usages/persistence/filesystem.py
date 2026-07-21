"""Qualified filesystem transactions for one account authority."""

from collections.abc import Callable
from typing import Never

from sidekick_usages.persistence._platform import (
    FilesystemQualification,
    NativeFailureKind,
    NativeFilesystemError,
)
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    ArtifactPurpose,
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    CandidateWriteError,
    DurabilityUncertainError,
    InterruptedArtifactError,
    InvalidManagedArtifactError,
    ManagedFileReadError,
    PersistenceCode,
    PersistenceError,
    PersistenceFilesystemError,
    ReplaceFailedError,
    SourceChangedError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem_access import (
    PersistenceFilesystemAccess,
)
from sidekick_usages.persistence.schemas import (
    MAX_DOCUMENT_BYTES,
    decode_prototype_receipt,
)

_TEMPORARY_CREATE_ATTEMPTS = 32
_INTERRUPTED_PUBLICATION_LINKS = 2

type _PayloadValidator = Callable[[bytes], None]


class PersistenceFilesystem(PersistenceFilesystemAccess):
    """Persistence-specific filesystem facade bound to one account path."""

    def _publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish or exactly reuse a content-addressed source snapshot."""
        self._validate_generation(source.data, generation)
        final_basename = self.grammar.backup_basename(
            generation,
            source.fingerprint.digest,
        )
        purpose = (
            ArtifactPurpose.BACKUP
            if generation is AuthorityGeneration.GENERATION_ZERO
            else ArtifactPurpose.SNAPSHOT
        )
        self._prepare_parent()
        return self._publish_content_addressed(
            final_basename,
            source.data,
            purpose,
            expected_source=source.fingerprint,
            copy_source=True,
        )

    def _publish_migration_snapshot(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish validated bytes imported from a locked peer authority."""
        self._validate_generation(payload, generation)
        final_basename = self.grammar.backup_basename(
            generation,
            sha256_digest(payload),
        )
        self._prepare_parent()
        purpose = (
            ArtifactPurpose.BACKUP
            if generation is AuthorityGeneration.GENERATION_ZERO
            else ArtifactPurpose.SNAPSHOT
        )
        return self._publish_content_addressed(
            final_basename,
            payload,
            purpose,
            expected_source=None,
            copy_source=False,
        )

    def _publish_receipt(
        self,
        prototype_digest: Sha256Digest,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish or exactly reuse one canonical non-secret receipt."""
        receipt = decode_prototype_receipt(payload)
        if receipt.prototype_sha256 != prototype_digest:
            raise ValueError("Receipt digest does not match its basename.")
        self._prepare_parent()
        return self._publish_content_addressed(
            self.grammar.receipt_basename(prototype_digest),
            payload,
            ArtifactPurpose.RECEIPT,
            expected_source=None,
            copy_source=False,
        )

    def _commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Atomically commit, harden, reopen, and prove exact authority."""

        def validate(candidate: bytes) -> None:
            self._validate_generation(candidate, generation)

        validate(payload)
        return self._commit_payload(payload, expected_source, validate)

    def commit_opaque_private(
        self,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        """Atomically write bounded opaque bytes to this private path.

        :param payload: Exact private bytes to commit.
        :param expected_source: Optional caller-proven source expectation.
        :returns: Reopened and verified private file state.
        """
        if len(payload) > MAX_DOCUMENT_BYTES:
            raise CandidateWriteError(self.grammar.authority_basename)
        self._prepare_parent()
        expected = expected_source
        if expected is None:
            current = self._read(
                self.grammar.authority_basename,
                MAX_DOCUMENT_BYTES,
                require_complete=True,
            )
            expected = (
                AuthorityExpectation.ABSENT
                if current is None
                else current.fingerprint
            )
        return self._commit_payload(payload, expected, None)

    def read_opaque_private(self) -> FileSnapshot | None:
        """Read and prove one bounded opaque private file when present."""
        self.qualify()
        return self._read(
            self.grammar.authority_basename,
            MAX_DOCUMENT_BYTES,
            require_complete=True,
        )

    def delete_opaque_private(self, expected: FileFingerprint) -> None:
        """Delete one exact private file and prove namespace absence.

        :param expected: Fingerprint of the exact file to remove.
        """
        self.qualify()
        current = self._read(
            self.grammar.authority_basename,
            MAX_DOCUMENT_BYTES,
            require_complete=True,
        )
        if current is None or current.fingerprint != expected:
            raise SourceChangedError
        try:
            removed = self._native.remove_validated(
                self._parent,
                self.grammar.authority_basename,
                expected.identity.device,
                expected.identity.inode,
            )
            if not removed:
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
            self._native.harden_cleanup(self._parent)
            if (
                self._read(
                    self.grammar.authority_basename,
                    MAX_DOCUMENT_BYTES,
                    require_complete=True,
                )
                is not None
            ):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.CHANGED,
                NativeFailureKind.UNSAFE,
            }:
                raise SourceChangedError from None
            raise DurabilityUncertainError(
                self.grammar.authority_basename
            ) from None

    def _commit_payload(
        self,
        payload: bytes,
        expected_source: ExpectedAuthority,
        validate: _PayloadValidator | None,
    ) -> FileSnapshot:
        """Commit exact bytes with optional boundary-specific validation."""
        self._prepare_parent()
        temporary_basename, candidate = self._create_candidate(
            ArtifactPurpose.AUTHORITY,
            payload,
        )
        if validate is not None:
            validate(candidate.data)
        try:
            self._require_expected_authority(expected_source)
        except PersistenceFilesystemError:
            self._remove_candidate(temporary_basename)
            raise

        destination_exists = expected_source is not AuthorityExpectation.ABSENT
        try:
            self._native.replace(
                self._parent,
                temporary_basename,
                self.grammar.authority_basename,
                destination_exists=destination_exists,
                device=candidate.fingerprint.identity.device,
                inode=candidate.fingerprint.identity.inode,
            )
        except NativeFilesystemError as error:
            if error.kind is NativeFailureKind.EXISTS:
                self._remove_candidate(temporary_basename)
                raise SourceChangedError from None
            if self._replacement_may_have_committed(
                payload,
                expected_source,
            ):
                raise DurabilityUncertainError(
                    self.grammar.authority_basename
                ) from None
            self._remove_candidate(temporary_basename)
            raise ReplaceFailedError from None

        try:
            self._native.harden(
                self._parent,
                self.grammar.authority_basename,
                MAX_DOCUMENT_BYTES,
            )
            final = self._read(
                self.grammar.authority_basename,
                MAX_DOCUMENT_BYTES,
                require_complete=True,
            )
            if (
                final is None
                or final.data != payload
                or final.fingerprint.identity != candidate.fingerprint.identity
            ):
                raise DurabilityUncertainError(self.grammar.authority_basename)
            if validate is not None:
                validate(final.data)
            return final
        except NativeFilesystemError, PersistenceFilesystemError:
            raise DurabilityUncertainError(
                self.grammar.authority_basename
            ) from None
        except PersistenceError:
            raise DurabilityUncertainError(
                self.grammar.authority_basename
            ) from None

    def _create_candidate(
        self,
        purpose: ArtifactPurpose,
        payload: bytes,
        *,
        copy_source: bool = False,
    ) -> tuple[str, FileSnapshot]:
        for _attempt in range(_TEMPORARY_CREATE_ATTEMPTS):
            basename = self.grammar.temporary_basename(purpose)
            try:
                native = (
                    self._native.copy_private(
                        self._parent,
                        self.grammar.authority_basename,
                        basename,
                        payload,
                    )
                    if copy_source
                    else self._native.create_private(
                        self._parent,
                        basename,
                        payload,
                    )
                )
            except NativeFilesystemError as error:
                if error.kind is NativeFailureKind.EXISTS:
                    continue
                self._remove_candidate(basename)
                raise CandidateWriteError(basename) from None
            candidate = self._native_snapshot(native)
            if candidate.data != payload:
                self._remove_candidate(basename)
                raise CandidateWriteError(basename)
            return basename, candidate
        raise CandidateWriteError()

    def _publish_content_addressed(
        self,
        final_basename: str,
        payload: bytes,
        purpose: ArtifactPurpose,
        *,
        expected_source: FileFingerprint | None,
        copy_source: bool,
    ) -> ManagedArtifact:
        if expected_source is not None:
            self._require_expected_authority(expected_source)
        if (
            existing_artifact := self._reuse_immutable(
                final_basename,
                payload,
            )
        ) is not None:
            return existing_artifact

        temporary_basename, candidate = self._create_candidate(
            purpose,
            payload,
            copy_source=copy_source,
        )
        if expected_source is not None:
            try:
                self._require_expected_authority(expected_source)
            except PersistenceFilesystemError:
                self._remove_candidate(temporary_basename)
                raise
        publication_error: NativeFilesystemError | None = None
        try:
            self._native.publish_no_replace(
                self._parent,
                temporary_basename,
                final_basename,
                candidate.fingerprint.identity.device,
                candidate.fingerprint.identity.inode,
            )
        except NativeFilesystemError as error:
            publication_error = error
        if publication_error is not None:
            if publication_error.kind is NativeFailureKind.EXISTS:
                return self._resolve_publication_collision(
                    temporary_basename,
                    final_basename,
                    payload,
                )
            self._handle_failed_publication(
                temporary_basename,
                final_basename,
                payload,
            )
        self._verify_published(
            temporary_basename,
            final_basename,
            payload,
            candidate,
        )
        return self._artifact_for(final_basename)

    def _artifact_for(self, final_basename: str) -> ManagedArtifact:
        artifact = self.grammar.parse(final_basename)
        if artifact is None:
            raise ValueError("Immutable target is outside the grammar.")
        return artifact

    def _reuse_immutable(
        self,
        final_basename: str,
        payload: bytes,
    ) -> ManagedArtifact | None:
        existing = self._read_immutable(final_basename)
        if existing is None:
            return None
        if existing.data != payload:
            raise self._immutable_conflict(final_basename)
        return self._artifact_for(final_basename)

    def _resolve_publication_collision(
        self,
        temporary_basename: str,
        final_basename: str,
        payload: bytes,
    ) -> ManagedArtifact:
        self._remove_candidate(temporary_basename)
        raced = self._read_immutable(final_basename)
        if raced is None or raced.data != payload:
            raise self._immutable_conflict(final_basename)
        return self._artifact_for(final_basename)

    def _handle_failed_publication(
        self,
        temporary_basename: str,
        final_basename: str,
        payload: bytes,
    ) -> Never:
        try:
            observed = self._read_immutable(final_basename)
        except PersistenceFilesystemError:
            raise DurabilityUncertainError(final_basename) from None
        if observed is not None:
            if observed.data == payload:
                raise DurabilityUncertainError(final_basename)
            self._remove_candidate(temporary_basename)
            raise self._immutable_conflict(final_basename)
        self._remove_candidate(temporary_basename)
        raise CandidateWriteError(temporary_basename)

    def _verify_published(
        self,
        temporary_basename: str,
        final_basename: str,
        payload: bytes,
        candidate: FileSnapshot,
    ) -> None:
        try:
            self._native.harden(
                self._parent,
                final_basename,
                MAX_DOCUMENT_BYTES,
            )
            self._remove_candidate(
                temporary_basename,
                post_publication=True,
                identity=candidate.fingerprint.identity,
            )
            final = self._read_immutable(final_basename)
        except NativeFilesystemError, PersistenceFilesystemError:
            raise DurabilityUncertainError(final_basename) from None
        if (
            final is None
            or final.data != payload
            or final.fingerprint.identity != candidate.fingerprint.identity
        ):
            raise DurabilityUncertainError(final_basename)

    def _read_immutable(self, basename: str) -> FileSnapshot | None:
        try:
            native = self._native.read(
                self._parent,
                basename,
                MAX_DOCUMENT_BYTES,
            )
        except NativeFilesystemError as error:
            raise self._read_error(basename, error) from None
        if native is None:
            return None
        return self._snapshot_with_link_proof(
            basename,
            native,
            require_complete=True,
        )

    def _immutable_conflict(
        self,
        basename: str,
    ) -> PersistenceFilesystemError:
        artifact = self.grammar.parse(basename)
        if (
            artifact is not None
            and artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT
        ):
            return InvalidManagedArtifactError(basename)
        return BackupConflictError(basename)

    def _require_expected_authority(
        self,
        expected: ExpectedAuthority,
    ) -> None:
        observed = self._read(
            self.grammar.authority_basename,
            MAX_DOCUMENT_BYTES,
            source_revalidation=True,
        )
        if expected is AuthorityExpectation.ABSENT:
            if observed is not None:
                raise SourceChangedError()
            return
        if observed is None or observed.fingerprint != expected:
            raise SourceChangedError()

    def _remove_candidate(
        self,
        basename: str,
        *,
        post_publication: bool = False,
        identity: FileIdentity | None = None,
    ) -> None:
        try:
            if post_publication:
                if identity is None:
                    raise ValueError(
                        "Published cleanup requires candidate identity."
                    )
                removed = self._native.remove_validated(
                    self._parent,
                    basename,
                    identity.device,
                    identity.inode,
                )
            else:
                removed = self._native.remove_candidate(
                    self._parent,
                    basename,
                )
            if removed or post_publication:
                self._native.harden_cleanup(self._parent)
        except NativeFilesystemError:
            if post_publication:
                raise DurabilityUncertainError(basename) from None
            raise InterruptedArtifactError(basename) from None

    def _replacement_may_have_committed(
        self,
        payload: bytes,
        expected: ExpectedAuthority,
    ) -> bool:
        try:
            observed = self._read(
                self.grammar.authority_basename,
                MAX_DOCUMENT_BYTES,
            )
        except PersistenceFilesystemError:
            return True
        if (
            expected is not AuthorityExpectation.ABSENT
            and observed is not None
            and observed.fingerprint == expected
        ):
            return False
        if observed is not None and observed.data == payload:
            return True
        if expected is AuthorityExpectation.ABSENT:
            return observed is not None
        return observed is None or observed.fingerprint != expected


__all__ = [
    "ArtifactGrammar",
    "ArtifactPurpose",
    "AuthorityExpectation",
    "AuthorityGeneration",
    "BackupConflictError",
    "CandidateWriteError",
    "DurabilityUncertainError",
    "ExpectedAuthority",
    "FileFingerprint",
    "FileIdentity",
    "FileSnapshot",
    "FilesystemQualification",
    "InvalidManagedArtifactError",
    "ManagedArtifact",
    "ManagedArtifactKind",
    "ManagedFileReadError",
    "PersistenceCode",
    "PersistenceFilesystem",
    "PersistenceFilesystemError",
    "ReplaceFailedError",
    "Sha256Digest",
    "SourceChangedError",
    "UnsafeManagedFileError",
    "UnsupportedFilesystemError",
    "sha256_digest",
]
