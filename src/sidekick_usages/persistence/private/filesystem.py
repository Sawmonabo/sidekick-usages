"""Qualified transactions for one owner-private state file."""

from collections.abc import Callable

from sidekick_usages.persistence.errors import (
    CandidateWriteError,
    DurabilityUncertainError,
    InterruptedArtifactError,
    PersistenceError,
    PersistenceFilesystemError,
    ReplaceFailedError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem.access import (
    PersistenceFilesystemAccess,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
)
from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.types import NativeFailureKind
from sidekick_usages.persistence.types.artifact import (
    ArtifactPurpose,
    AuthorityExpectation,
)

_TEMPORARY_CREATE_ATTEMPTS = 32


class PrivateFilesystem(PersistenceFilesystemAccess):
    """Provide one shared atomic private-file implementation."""

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
        validate: Callable[[bytes], object] | None,
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
    ) -> tuple[str, FileSnapshot]:
        for _attempt in range(_TEMPORARY_CREATE_ATTEMPTS):
            basename = self.grammar.temporary_basename(purpose)
            try:
                native = self._native.create_private(
                    self._parent,
                    basename,
                    payload,
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
