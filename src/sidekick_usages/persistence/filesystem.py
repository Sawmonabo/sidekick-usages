"""Qualified filesystem transactions for one account authority."""

from typing import Never

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
)
from sidekick_usages.persistence.account_validation import (
    require_prototype_receipt_digest,
    validate_account_generation,
    validate_account_recovery_artifact,
)
from sidekick_usages.persistence.artifacts import (
    ArtifactPurpose,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
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
    InvalidManagedArtifactError,
    PersistenceFilesystemError,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.private_filesystem import PrivateFilesystem

__all__ = ["PersistenceFilesystem"]


class PersistenceFilesystem(PrivateFilesystem):
    """Persistence-specific filesystem facade bound to one account path."""

    def _validate_recovery_artifact(
        self,
        artifact: ManagedArtifact,
        payload: bytes,
    ) -> None:
        validate_account_recovery_artifact(artifact, payload)

    def _validate_generation(
        self,
        payload: bytes,
        generation: AuthorityGeneration,
    ) -> None:
        validate_account_generation(payload, generation)

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
        require_prototype_receipt_digest(
            payload,
            prototype_digest,
        )
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
