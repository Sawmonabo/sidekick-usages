"""Explicit durable account-schema migration coordination."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    Sha256Digest,
)
from sidekick_usages.persistence.assessment import (
    ArtifactKind,
    ArtifactObservation,
    ArtifactState,
    AuthorityKind,
    PersistenceAssessment,
    PersistenceCode,
    PersistenceCompositionFailure,
    PersistenceObservation,
    PersistenceOperationResult,
    assess_persistence,
    make_operation_result,
    recovery_guidance,
    recovery_next_command,
)
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    ManagedFileReadError,
    PersistenceError,
    ResetIncompleteError,
    RollbackCompatibilityError,
    SourceChangedError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import (
    OrphanedPrivateCredentials,
    PersistenceInventory,
    PrototypeMigrationIntent,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.migrations.account_codecs import (
    CURRENT_VERSION_TWO,
    MIGRATABLE_GENERATION_ZERO,
    accounts_from_current,
    credential_migration_preflight,
    generation_zero_payload,
    prototype_payload,
    rollback_payload,
    version_one_payload,
)
from sidekick_usages.persistence.migrations.account_preview import (
    AccountMigrationPreview,
)
from sidekick_usages.persistence.migrations.errors import (
    PersistenceMigrationStateError,
    PrototypeReimportRequiredError,
    ReleasedVerifierBoundaryError,
    SchedulerMutationBlockedError,
    VerificationPhase,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialArtifacts,
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.schemas import (
    PrototypeReceipt,
    decode_prototype,
    encode_generation_zero,
    encode_prototype_receipt,
)
from sidekick_usages.persistence.transforms import version_two_to_v060
from sidekick_usages.scheduler_quiescence import (
    SchedulerQuiescenceAssessment,
)


class ReleasedV060Verifier(Protocol):
    """Non-mutating exact released-reader compatibility oracle."""

    def preflight(self) -> None:
        """Prove the pinned oracle is available before mutation."""

    def verify(self, account_path: Path, expected: FileSnapshot) -> None:
        """Verify exact reopened identity and bytes through the old reader."""


@dataclass(frozen=True, slots=True)
class PermissionRepairOperationResult:
    """Verified permission repair and its fresh persistence assessment."""

    repair: PrivateCredentialRepairResult
    assessment: PersistenceAssessment


class MigrationTransaction(Protocol):
    """Lock-scoped durable operations required by migrations."""

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        """Publish or reuse one exact immutable source snapshot."""

    def publish_migration_snapshot(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish validated bytes copied from another locked authority."""

    def publish_receipt(
        self,
        prototype_digest: Sha256Digest,
        payload: bytes,
    ) -> ManagedArtifact:
        """Publish or reuse one exact prototype receipt."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove one exact authoritative generation."""

    def recover_or_discard_temporary(
        self,
        temporary: ManagedArtifact,
    ) -> None:
        """Resolve one exact safe managed temporary."""

    def full_reset(self, expected_source: ExpectedAuthority) -> None:
        """Delete credentials before deleting the exact authority."""


class AccountMigrationLock(Protocol):
    """Cooperative lock yielding one active mutation capability."""

    def hold(self) -> AbstractContextManager[MigrationTransaction]:
        """Acquire the lock and yield its mutation capability."""


type AccountFilesystemFactory = Callable[[Path], PersistenceFilesystem]
type AccountLockFactory = Callable[
    [PersistenceFilesystem],
    AccountMigrationLock,
]


class AccountMigrationCoordinator:
    """Assess and execute explicit lock-scoped persistence transitions."""

    def __init__(
        self,
        account_path: Path,
        prototype_path: Path,
        *,
        scheduler_assessor: Callable[[], SchedulerQuiescenceAssessment],
        private_credential_artifacts: PrivateCredentialArtifacts,
        released_v060_verifier: ReleasedV060Verifier,
        filesystem_factory: AccountFilesystemFactory = PersistenceFilesystem,
        lock_factory: AccountLockFactory = PersistenceLock,
    ) -> None:
        self.path = account_path
        self._filesystem = filesystem_factory(self.path)
        self._prototype_path = prototype_path
        self._prototype_filesystem = filesystem_factory(prototype_path)
        self._filesystem_factory = filesystem_factory
        self._scheduler_assessor = scheduler_assessor
        self._private_credential_artifacts = private_credential_artifacts
        self._released_v060_verifier = released_v060_verifier
        self._lock_factory = lock_factory
        self._inventory = PersistenceInventory(
            self.path,
            prototype_path,
            filesystem_factory=self._inventory_filesystem,
        )

    def assess(self) -> PersistenceAssessment:
        """Return one complete passive persistence assessment."""
        return assess_persistence(self._inspect())

    def mutation_preview(self) -> PersistenceAssessment:
        """Require quiescence and return a pre-confirmation assessment."""
        self._require_scheduler_quiescence()
        return self.assess()

    def account_migration_preview(self) -> AccountMigrationPreview:
        """Return one read-only migration assessment and classification."""
        self._require_scheduler_quiescence()
        observation = self._inspect()
        assessment = assess_persistence(observation)
        classification = credential_migration_preflight(observation)
        return AccountMigrationPreview(assessment, classification)

    def permission_repair_preview(
        self,
    ) -> PersistenceAssessment | PersistenceCompositionFailure:
        """Return repair scope even when unsafe permissions block it."""
        try:
            return self.mutation_preview()
        except UnsafeManagedFileError as error:
            return PersistenceCompositionFailure(
                code=error.code,
                safe_path=self.path,
                artifact_basename=error.artifact_basename,
                message=str(error),
                next_command=recovery_next_command(error.code),
                guidance=recovery_guidance(error.code),
            )

    def repair_permissions(self) -> PermissionRepairOperationResult:
        """Repair a released layout and return its fresh assessment."""
        self._require_scheduler_quiescence()
        repair = self._private_credential_artifacts.repair_permissions(
            locked_precondition=self._require_scheduler_quiescence,
        )
        assessment = self.assess()
        if assessment.code is PersistenceCode.UNSAFE_PERMISSIONS:
            raise PersistenceMigrationStateError(assessment)
        return PermissionRepairOperationResult(repair, assessment)

    def read_accounts(self) -> tuple[Account, ...]:
        """Return a validated current snapshot without constructing a store."""
        observation = self._inspect()
        assessment = assess_persistence(observation)
        try:
            return accounts_from_current(observation, assessment)
        except ValueError:
            raise PersistenceMigrationStateError(assessment) from None

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        """Migrate generation zero or explicitly import a prototype."""
        if type(reimport_prototype) is not bool:
            raise TypeError("reimport_prototype must be Boolean.")
        self._require_scheduler_quiescence()
        intent = (
            PrototypeMigrationIntent.REIMPORT
            if reimport_prototype
            else PrototypeMigrationIntent.IMPORT
        )
        with self._lock_factory(self._filesystem).hold() as transaction:
            self._require_scheduler_quiescence()
            observation, assessment = self._locked_assessment(
                transaction,
                intent=intent,
            )
            self._require_explicit_prototype_valid(observation)
            match observation.authority.kind:
                case AuthorityKind.GENERATION_ZERO:
                    return self._migrate_legacy(
                        transaction,
                        observation,
                        assessment,
                    )
                case AuthorityKind.VERSION_ONE:
                    return self._migrate_legacy(
                        transaction,
                        observation,
                        assessment,
                    )
                case AuthorityKind.VERSION_TWO:
                    return self._migrate_or_resume_prototype(
                        transaction,
                        observation,
                        assessment,
                        reimport=reimport_prototype,
                    )
                case AuthorityKind.ABSENT:
                    return self._import_absent_prototype(
                        transaction,
                        observation,
                        assessment,
                        reimport=reimport_prototype,
                    )
                case _:
                    raise PersistenceMigrationStateError(assessment)

    def prepare_rollback(self) -> PersistenceOperationResult:
        """Prepare and verify exact compatibility with released v0.6.0."""
        self._require_scheduler_quiescence()
        self._verifier_preflight()
        with self._lock_factory(self._filesystem).hold() as transaction:
            self._require_scheduler_quiescence()
            self._verifier_preflight()
            preflight_observation = self._inspect()
            preflight_assessment = assess_persistence(preflight_observation)
            if (
                preflight_observation.authority.kind
                is AuthorityKind.VERSION_TWO
                and (
                    preflight_assessment.code in CURRENT_VERSION_TWO
                    or self._can_recover_temporaries(
                        preflight_observation,
                        preflight_assessment,
                    )
                )
            ):
                rollback_payload(preflight_observation)
            observation, assessment = self._locked_assessment(transaction)
            if (
                observation.authority.kind is AuthorityKind.GENERATION_ZERO
                and assessment.code is PersistenceCode.ROLLBACK_PREPARED
            ):
                source = self._authority_snapshot(observation)
                if source is None:
                    raise SourceChangedError
                self._verifier_verify(source)
                postcondition = self._require_postcondition(
                    {PersistenceCode.ROLLBACK_PREPARED}
                )
                snapshot = self._matching_rollback_snapshot(observation)
                return make_operation_result(
                    PersistenceCode.ROLLBACK_PREPARED,
                    postcondition,
                    artifact_basename=snapshot.basename,
                )
            if (
                observation.authority.kind is not AuthorityKind.VERSION_TWO
                or assessment.code not in CURRENT_VERSION_TWO
            ):
                raise PersistenceMigrationStateError(assessment)
            source = self._authority_snapshot(observation)
            if source is None:
                raise SourceChangedError
            payload = rollback_payload(observation)
            snapshot = transaction.publish_immutable(
                AuthorityGeneration.VERSION_TWO,
                source,
            )
            committed = transaction.commit_authority(
                AuthorityGeneration.GENERATION_ZERO,
                payload,
                source.fingerprint,
            )
            self._verifier_verify(committed)
            postcondition = self._require_postcondition(
                {PersistenceCode.ROLLBACK_PREPARED}
            )
            return make_operation_result(
                PersistenceCode.ROLLBACK_PREPARED,
                postcondition,
                artifact_basename=snapshot.basename,
            )

    def full_reset(self) -> PersistenceAssessment:
        """Delete every account credential artifact, even when empty."""
        self._require_scheduler_quiescence()
        with self._lock_factory(self._filesystem).hold() as transaction:
            self._require_scheduler_quiescence()
            observation, assessment = self._locked_assessment(transaction)
            self._require_resettable(observation, assessment)
            source = self._filesystem.read_authority()
            expected: ExpectedAuthority = (
                AuthorityExpectation.ABSENT
                if source is None
                else source.fingerprint
            )
            self._destroy_private_credentials()
            try:
                transaction.full_reset(expected)
            except PersistenceError:
                if observation.orphaned_credentials:
                    raise ResetIncompleteError(self.path.name) from None
                raise
            try:
                post_observation = self._inspect()
            except PersistenceError:
                raise ResetIncompleteError(self.path.name) from None
            if not self._reset_is_complete(post_observation):
                raise ResetIncompleteError(self.path.name)
            return assess_persistence(post_observation)

    def _migrate_legacy(
        self,
        transaction: MigrationTransaction,
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
    ) -> PersistenceAssessment:
        kind = observation.authority.kind
        if kind is AuthorityKind.GENERATION_ZERO:
            allowed = assessment.code in MIGRATABLE_GENERATION_ZERO
            generation = AuthorityGeneration.GENERATION_ZERO
            payload = generation_zero_payload(observation)
        elif kind is AuthorityKind.VERSION_ONE:
            allowed = assessment.code is PersistenceCode.MIGRATION_REQUIRED
            generation = AuthorityGeneration.VERSION_ONE
            payload = version_one_payload(observation)
        else:
            allowed = False
            generation = AuthorityGeneration.VERSION_ONE
            payload = b""
        if not allowed:
            raise PersistenceMigrationStateError(assessment)
        source = self._authority_snapshot(observation)
        if source is None:
            raise SourceChangedError
        transaction.publish_immutable(
            generation,
            source,
        )
        transaction.commit_authority(
            AuthorityGeneration.VERSION_TWO,
            payload,
            source.fingerprint,
        )
        return self._require_postcondition(CURRENT_VERSION_TWO)

    def _import_absent_prototype(
        self,
        transaction: MigrationTransaction,
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
        *,
        reimport: bool,
    ) -> PersistenceAssessment:
        if assessment.code not in {
            PersistenceCode.EMPTY,
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
        }:
            raise PersistenceMigrationStateError(assessment)
        prototype = self._prototype_source(observation)
        if prototype is None:
            if reimport:
                raise PersistenceMigrationStateError(assessment)
            return assessment
        if (
            self._has_historical_prototype_receipt(observation)
            and not reimport
        ):
            raise PrototypeReimportRequiredError(assessment)
        if assessment.code is PersistenceCode.EMPTY and not reimport:
            return assessment
        prototype_artifact, prototype_source = prototype
        payload = prototype_payload(prototype_artifact)
        self._revalidate_prototype(prototype_source)
        transaction.commit_authority(
            AuthorityGeneration.VERSION_TWO,
            payload,
            AuthorityExpectation.ABSENT,
        )
        self._publish_prototype_receipt(
            transaction,
            prototype_source,
        )
        return self._require_postcondition(
            {PersistenceCode.PROTOTYPE_IMPORTED}
        )

    def _migrate_or_resume_prototype(
        self,
        transaction: MigrationTransaction,
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
        *,
        reimport: bool,
    ) -> PersistenceAssessment:
        if assessment.code not in CURRENT_VERSION_TWO:
            raise PersistenceMigrationStateError(assessment)
        source = self._authority_snapshot(observation)
        if source is None:
            raise SourceChangedError
        prototype = self._prototype_source(observation)
        if prototype is None:
            if reimport:
                raise PersistenceMigrationStateError(assessment)
            return assessment
        prototype_artifact, prototype_source = prototype
        payload = prototype_payload(prototype_artifact)
        if payload != source.data:
            if not reimport:
                raise PrototypeReimportRequiredError(assessment)
            self._revalidate_prototype(prototype_source)
            transaction.publish_immutable(
                AuthorityGeneration.VERSION_TWO,
                source,
            )
            transaction.commit_authority(
                AuthorityGeneration.VERSION_TWO,
                payload,
                source.fingerprint,
            )
        self._publish_prototype_receipt(transaction, prototype_source)
        return self._require_postcondition(
            {PersistenceCode.PROTOTYPE_IMPORTED}
        )

    def _publish_prototype_receipt(
        self,
        transaction: MigrationTransaction,
        source: FileSnapshot,
    ) -> None:
        self._revalidate_prototype(source)
        digest = source.fingerprint.digest
        payload = encode_prototype_receipt(PrototypeReceipt(digest))
        transaction.publish_receipt(digest, payload)

    def _prototype_source(
        self,
        observation: PersistenceObservation,
    ) -> tuple[ArtifactObservation, FileSnapshot] | None:
        artifact = next(
            (
                candidate
                for candidate in observation.artifacts
                if candidate.kind is ArtifactKind.PROTOTYPE
            ),
            None,
        )
        if artifact is None:
            return None
        if (
            artifact.state is not ArtifactState.VALID
            or artifact.content is None
            or artifact.prototype is None
        ):
            raise PersistenceMigrationStateError(
                assess_persistence(observation)
            )
        source = self._prototype_filesystem.read_external_private_source()
        if (
            source is None
            or source.link_count != 1
            or source.data != artifact.content
        ):
            raise SourceChangedError
        decode_prototype(source.data)
        return artifact, source

    def _revalidate_prototype(self, expected: FileSnapshot) -> None:
        observed = self._prototype_filesystem.read_external_private_source()
        if (
            observed is None
            or observed.link_count != 1
            or observed != expected
        ):
            raise SourceChangedError

    def _locked_assessment(
        self,
        transaction: MigrationTransaction,
        *,
        intent: PrototypeMigrationIntent | None = None,
    ) -> tuple[PersistenceObservation, PersistenceAssessment]:
        observation = self._inspect(intent)
        assessment = assess_persistence(observation)
        if self._can_recover_temporaries(observation, assessment):
            temporaries = tuple(
                artifact
                for artifact in self._filesystem.discover_managed()
                if artifact.kind is ManagedArtifactKind.TEMPORARY
            )
            for temporary in sorted(
                temporaries,
                key=lambda artifact: artifact.basename,
            ):
                transaction.recover_or_discard_temporary(temporary)
            observation = self._inspect(intent)
            assessment = assess_persistence(observation)
        return observation, assessment

    @staticmethod
    def _can_recover_temporaries(
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
    ) -> bool:
        if assessment.code is not PersistenceCode.INTERRUPTED_ARTIFACTS:
            return False
        temporary_names = {
            artifact.basename
            for artifact in observation.artifacts
            if artifact.kind is ArtifactKind.TEMPORARY
            and artifact.state is ArtifactState.VALID
        }
        interruption_issues = tuple(
            issue
            for issue in assessment.issues
            if issue.code is PersistenceCode.INTERRUPTED_ARTIFACTS
        )
        return len(temporary_names) == 1 and all(
            issue.artifact_basename in temporary_names
            for issue in interruption_issues
        )

    @staticmethod
    def _matching_rollback_snapshot(
        observation: PersistenceObservation,
    ) -> ArtifactObservation:
        authority = observation.authority.content
        if authority is None:
            raise SourceChangedError
        for artifact in sorted(
            observation.artifacts,
            key=lambda candidate: candidate.basename,
        ):
            if not (
                artifact.kind is ArtifactKind.V2_SNAPSHOT
                and artifact.state is ArtifactState.VALID
                and artifact.version_two is not None
            ):
                continue
            try:
                reverse = encode_generation_zero(
                    version_two_to_v060(artifact.version_two)
                )
            except InvalidSchemaError, RollbackCompatibilityError:
                continue
            if reverse == authority:
                return artifact
        raise PersistenceMigrationStateError(assess_persistence(observation))

    def _authority_snapshot(
        self,
        observation: PersistenceObservation,
    ) -> FileSnapshot | None:
        source = self._filesystem.read_authority()
        if observation.authority.kind is AuthorityKind.ABSENT:
            if source is not None:
                raise SourceChangedError
            return None
        if (
            source is None
            or observation.authority.content is None
            or source.data != observation.authority.content
        ):
            raise SourceChangedError
        return source

    def _require_postcondition(
        self,
        expected: frozenset[PersistenceCode] | set[PersistenceCode],
    ) -> PersistenceAssessment:
        assessment = self.assess()
        if assessment.code not in expected:
            raise PersistenceMigrationStateError(assessment)
        return assessment

    @staticmethod
    def _has_historical_prototype_receipt(
        observation: PersistenceObservation,
    ) -> bool:
        return any(
            artifact.kind is ArtifactKind.PROTOTYPE_RECEIPT
            and artifact.state is ArtifactState.VALID
            for artifact in observation.artifacts
        )

    @staticmethod
    def _require_explicit_prototype_valid(
        observation: PersistenceObservation,
    ) -> None:
        prototype = next(
            (
                artifact
                for artifact in observation.artifacts
                if artifact.kind is ArtifactKind.PROTOTYPE
            ),
            None,
        )
        if prototype is None or prototype.state is ArtifactState.VALID:
            return
        if prototype.state is ArtifactState.UNSAFE:
            raise UnsafeManagedFileError(prototype.basename)
        if prototype.state in {
            ArtifactState.UNREADABLE,
            ArtifactState.BOUND_EXCEEDED,
        }:
            raise ManagedFileReadError(prototype.basename)
        raise InvalidSchemaError

    @staticmethod
    def _require_resettable(
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
    ) -> None:
        authority_is_safe = observation.authority.kind not in {
            AuthorityKind.UNREADABLE,
            AuthorityKind.UNSAFE,
            AuthorityKind.UNSUPPORTED_FILESYSTEM,
        }
        artifacts_are_safe = all(
            artifact.state is ArtifactState.VALID
            for artifact in observation.artifacts
            if artifact.kind is not ArtifactKind.PROTOTYPE
        )
        if not authority_is_safe or not artifacts_are_safe:
            raise PersistenceMigrationStateError(assessment)

    def _destroy_private_credentials(self) -> None:
        """Delete and immediately prove private artifacts absent under lock."""
        try:
            self._private_credential_artifacts.destroy_all()
            observed = self._private_credential_artifacts.observe()
        except PersistenceError:
            raise ResetIncompleteError(self.path.name) from None
        if observed is not OrphanedPrivateCredentials.ABSENT:
            raise ResetIncompleteError(self.path.name)

    @staticmethod
    def _reset_is_complete(observation: PersistenceObservation) -> bool:
        credential_kinds = {
            ArtifactKind.V0_BACKUP,
            ArtifactKind.V1_SNAPSHOT,
            ArtifactKind.V2_SNAPSHOT,
            ArtifactKind.TEMPORARY,
        }
        return (
            observation.authority.kind is AuthorityKind.ABSENT
            and not observation.orphaned_credentials
            and not any(
                artifact.kind in credential_kinds
                for artifact in observation.artifacts
            )
        )

    def _inspect(
        self,
        intent: PrototypeMigrationIntent | None = None,
    ) -> PersistenceObservation:
        orphaned = self._private_credential_artifacts.observe()
        if intent is None:
            return self._inventory.inspect(orphaned)
        return self._inventory.inspect_for_prototype_migration(
            orphaned,
            intent,
        )

    def _inventory_filesystem(self, path: Path) -> PersistenceFilesystem:
        if path == self.path:
            return self._filesystem
        if path == self._prototype_path:
            return self._prototype_filesystem
        return self._filesystem_factory(path)

    def _require_scheduler_quiescence(self) -> None:
        assessment = self._scheduler_assessor()
        if not assessment.quiescent:
            raise SchedulerMutationBlockedError(assessment)

    def _verifier_preflight(self) -> None:
        try:
            self._released_v060_verifier.preflight()
        except PersistenceError:
            raise
        except Exception:
            raise ReleasedVerifierBoundaryError(
                VerificationPhase.PREFLIGHT
            ) from None

    def _verifier_verify(self, expected: FileSnapshot) -> None:
        try:
            self._released_v060_verifier.verify(
                self.path,
                expected,
            )
        except PersistenceError:
            raise
        except Exception:
            raise ReleasedVerifierBoundaryError(
                VerificationPhase.POST_COMMIT
            ) from None


__all__ = [
    "AccountFilesystemFactory",
    "AccountLockFactory",
    "AccountMigrationCoordinator",
    "AccountMigrationLock",
    "MigrationTransaction",
    "PermissionRepairOperationResult",
    "PrivateCredentialArtifacts",
    "ReleasedV060Verifier",
]
